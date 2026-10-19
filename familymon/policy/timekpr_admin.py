"""timekpr-next transport: read live state over D-Bus, write via `timekpra`.

Why the split (see README "Enforcement direction"): timekpr 0.5.10's admin CLI
prints every enabled boolean as False (`str(dbus.Boolean(True))` is "1", which
its formatter compares against "true"), so reads MUST go over D-Bus. Writes go
through the `timekpra` CLI, which is stable, versioned, and validates input.

Runs on the target machine as root (or a `timekpr`-group member) — the daemon
listens on the *system* bus and checks caller privilege.

The pure planning logic lives in `enforcement.py`; this module only converts
D-Bus shapes to `LiveState` and executes `Change` lists.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

from dbus_next import BusType, Variant
from dbus_next.aio import MessageBus

from familymon.policy.enforcement import Change, HourWindow, LiveState, PlayTimeActivity

logger = logging.getLogger(__name__)

_BUS_NAME = "com.timekpr.server"
_OBJ_PATH = "/com/timekpr/server"
_USER_ADMIN_IFACE = "com.timekpr.server.user.admin"

_ALL_DAYS = ("1", "2", "3", "4", "5", "6", "7")


class TimekprError(RuntimeError):
    pass


def _unwrap(value):
    """Recursively strip dbus_next Variants to plain Python values."""
    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


async def _fetch_user_info(user: str) -> dict:
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        introspection = await bus.introspect(_BUS_NAME, _OBJ_PATH)
        obj = bus.get_proxy_object(_BUS_NAME, _OBJ_PATH, introspection)
        iface = obj.get_interface(_USER_ADMIN_IFACE)
        result, message, config = await iface.call_get_user_information(user, "F")
        if result != 0:
            raise TimekprError(f"getUserInformation({user!r}) failed: {message}")
        return {k: _unwrap(v) for k, v in config.items()}
    finally:
        bus.disconnect()


def _normalize_day_aligned(
    allowed_days: list, limits: list, what: str
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """timekpr aligns a limits array with its allowed-days array; re-spread
    both onto a fixed 7-day (Mon..Sun) axis, 0 for disallowed days."""
    days = tuple(int(d) for d in allowed_days)
    if len(limits) != len(days):
        raise TimekprError(
            f"{what}: {len(limits)} limits for {len(days)} allowed days"
        )
    by_day = dict(zip(days, (int(s) for s in limits)))
    return days, tuple(by_day.get(d, 0) for d in range(1, 8))


def _normalize_hours(config: dict) -> tuple[tuple[HourWindow, ...], ...]:
    out = []
    for day in _ALL_DAYS:
        raw = config.get(f"ALLOWED_HOURS_{day}") or {}
        # Value shape: {"22": {"STARTMIN": 0, "ENDMIN": 14, "UACC": 0}, ...}.
        # ENDMIN is 60 for a whole hour, else the last allowed minute.
        windows = []
        for h, spec in raw.items():
            spec = spec or {}
            smin = int(spec.get("STARTMIN", 0) or 0)
            emin = int(spec.get("ENDMIN", 60) if spec.get("ENDMIN") is not None else 60)
            windows.append(HourWindow(int(h), smin, 59 if emin >= 60 else emin))
        out.append(tuple(sorted(windows)))
    return tuple(out)


def _normalize_activities(raw: list) -> tuple[PlayTimeActivity, ...]:
    acts = []
    for pair in raw or []:
        mask = str(pair[0]) if len(pair) > 0 else ""
        desc = str(pair[1]) if len(pair) > 1 else ""
        if mask:
            acts.append(PlayTimeActivity(mask=mask, description=desc))
    return tuple(acts)


def _count_user_sessions(user: str) -> int:
    """Active logind sessions for `user`, via loginctl (no root needed)."""
    try:
        proc = subprocess.run(
            ["loginctl", "list-sessions", "--no-legend"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        # Fail SAFE: if we can't tell, assume a session exists so the
        # safety guard stays engaged rather than waved off.
        logger.warning("loginctl failed (%s); assuming an active session", e)
        return 1
    count = 0
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[2] == user:
            count += 1
    return count


class LiveTimekpr:
    """The real transport. Everything here talks to the local system."""

    def get_live_state(self, user: str) -> LiveState:
        config = asyncio.run(_fetch_user_info(user))

        allowed_days, daily_limits = _normalize_day_aligned(
            config.get("ALLOWED_WEEKDAYS") or [],
            config.get("LIMITS_PER_WEEKDAYS") or [],
            "daily limits",
        )
        pt_days, pt_limits = _normalize_day_aligned(
            config.get("PLAYTIME_ALLOWED_WEEKDAYS") or [],
            config.get("PLAYTIME_LIMITS_PER_WEEKDAYS") or [],
            "PlayTime limits",
        )
        return LiveState(
            allowed_weekdays=allowed_days,
            allowed_hours=_normalize_hours(config),
            daily_limits=daily_limits,
            lockout=str(config.get("LOCKOUT_TYPE", "")),
            playtime_enabled=bool(config.get("PLAYTIME_ENABLED", False)),
            playtime_allowed_weekdays=pt_days,
            playtime_daily_limits=pt_limits,
            playtime_activities=_normalize_activities(
                config.get("PLAYTIME_ACTIVITIES")
            ),
            active_session_count=_count_user_sessions(user),
        )

    def adjust_playtime_left(self, user: str, delta_seconds: int) -> None:
        """`timekpra --setplaytimeleft USER +/- N` — the realtime lever used by
        the browser-game charge-back. Negative delta deducts."""
        if delta_seconds == 0:
            return
        sign = "-" if delta_seconds < 0 else "+"
        argv = ["timekpra", "--setplaytimeleft", user, sign, str(abs(int(delta_seconds)))]
        logger.info("adjusting PlayTime left: %s", " ".join(argv))
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        output = (proc.stdout + proc.stderr).lower()
        if proc.returncode != 0 or "unexpected error" in output:
            raise TimekprError(
                f"timekpra failed adjusting PlayTime (rc={proc.returncode}): "
                f"{proc.stdout} {proc.stderr}"
            )

    def apply(self, changes: list[Change]) -> None:
        """Execute changes in order; abort on the first failure (the order is
        chosen so an interrupted apply leaves the least dangerous state)."""
        for change in changes:
            argv = ["timekpra", *change.cli_args]
            logger.info("applying %s: %s", change.setting, " ".join(argv))
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=30
            )
            # timekpra exits 0 even for some errors; it prints failures to
            # stdout/stderr. Trust the exit code first, then scan output.
            output = (proc.stdout + proc.stderr).lower()
            if proc.returncode != 0 or "unexpected error" in output:
                raise TimekprError(
                    f"timekpra failed on {change.setting} "
                    f"(rc={proc.returncode}): {proc.stdout} {proc.stderr}"
                )

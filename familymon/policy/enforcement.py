"""Enforcement policy → timekpr-next settings (the pure half of the driver).

familymon is the brain, timekpr-next is the muscle (see README "Enforcement
direction"). This module turns a parent-editable `policy.yaml` plus the
installed-game scan into a canonical desired state for one timekpr user, and
diffs it against the live daemon state into a minimal, ordered list of
`timekpra` invocations. No D-Bus or subprocess here — everything is unit-
testable; the side-effectful transport lives in `timekpr_admin.py`.

Safety rule (learned the hard way — a live spike terminated the kid's session
mid-login): applying an allowed-hours change that excludes the *current* hour
while the user has an active session kicks them off the machine on the spot.
`plan_is_safe_now` implements the guard; `apply` refuses without --force.

policy.yaml shape (see config/policy_example.yaml):

    user: kid
    lockout: lock                  # lock|suspend|suspendwake|terminate|kill|shutdown
    allowed_hours:                 # bedtime window, hour numbers 0-23 inclusive
      default: {start: 7, end: 21} # allowed 07:00 through 21:59
      saturday: {start: 8, end: 22}
    daily_limits:                  # total screen time per day ("2h30m", "45m", seconds)
      default: unlimited
      monday: 4h
    games:
      enabled: true
      daily_limits:
        default: 1h
        saturday: 2h
        sunday: 2h
      extra_masks:                 # curated non-Steam additions, "mask[Description]"
        - minecraft-launcher[Minecraft]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path

import yaml

# ISO 8601 day order — timekpr numbers Monday=1..Sunday=7.
_DAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_UNLIMITED_SECONDS = 86400  # timekpr's "no effective cap" per-day value

_DURATION_RE = re.compile(
    r"^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*$", re.IGNORECASE
)

# timekpr PlayTime masks may not contain []^$ (regex restrictions) nor ; (the
# activity separator); [] also delimit the description.
_MASK_FORBIDDEN = re.compile(r"[;\[\]^$]")
_EXTRA_MASK_RE = re.compile(r"^([^\[\];]+)(?:\[([^\[\];]*)\])?$")


class PolicyError(ValueError):
    """A policy.yaml value we refuse to guess about."""


def parse_duration(value: object) -> int:
    """'2h30m' / '45m' / '90s' / 7200 / 'unlimited' -> seconds."""
    if isinstance(value, bool):
        raise PolicyError(f"not a duration: {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise PolicyError(f"negative duration: {value!r}")
        return min(value, _UNLIMITED_SECONDS)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("unlimited", "none", ""):
            return _UNLIMITED_SECONDS
        if text.isdigit():
            return min(int(text), _UNLIMITED_SECONDS)
        m = _DURATION_RE.match(text)
        if m and any(m.groups()):
            h, mnt, s = (int(g) if g else 0 for g in m.groups())
            return min(h * 3600 + mnt * 60 + s, _UNLIMITED_SECONDS)
    raise PolicyError(f"can't parse duration: {value!r}")


def _per_day(node: object, parse, what: str) -> list:
    """Resolve a {default: X, <dayname>: Y} mapping to a 7-element list."""
    if node is None:
        raise PolicyError(f"missing {what}")
    if not isinstance(node, dict):
        # A single scalar applies to every day.
        return [parse(node)] * 7
    unknown = set(node) - {*_DAY_NAMES, "default"}
    if unknown:
        raise PolicyError(f"unknown day name(s) in {what}: {sorted(unknown)}")
    if "default" not in node and set(node) != set(_DAY_NAMES):
        raise PolicyError(f"{what} needs a 'default' or all 7 days")
    return [parse(node[day]) if day in node else parse(node["default"]) for day in _DAY_NAMES]


@dataclass(frozen=True, order=True)
class HourWindow:
    """One allowed hour, optionally restricted to a minute range (inclusive).

    timekpr's CLI spells a whole hour as "22" and a partial one as
    "22[00-29]"; over D-Bus it reports STARTMIN/ENDMIN with ENDMIN=60 meaning
    the whole hour. Minute ranges are what make one-off grants precise
    ("until 22:29", not "until 22:59")."""

    hour: int
    start_min: int = 0
    end_min: int = 59

    @property
    def whole(self) -> bool:
        return self.start_min == 0 and self.end_min >= 59

    def cli(self) -> str:
        if self.whole:
            return str(self.hour)
        return f"{self.hour}[{self.start_min:02d}-{self.end_min:02d}]"

    def covers(self, t: time) -> bool:
        return t.hour == self.hour and self.start_min <= t.minute <= self.end_min


def whole_hours(hours) -> tuple[HourWindow, ...]:
    return tuple(HourWindow(h) for h in hours)


def _parse_hour_window(node: object) -> list[HourWindow]:
    """{start: 7, end: 21} -> whole hours 7..21 inclusive."""
    if not isinstance(node, dict) or set(node) != {"start", "end"}:
        raise PolicyError(f"hour window must be {{start, end}}: {node!r}")
    start, end = node["start"], node["end"]
    if not (isinstance(start, int) and isinstance(end, int)):
        raise PolicyError(f"hour window bounds must be ints: {node!r}")
    if not (0 <= start <= 23 and 0 <= end <= 23 and start <= end):
        raise PolicyError(f"hour window out of range: {node!r}")
    return [HourWindow(h) for h in range(start, end + 1)]


def merge_hour_windows(*groups) -> tuple[HourWindow, ...]:
    """Union of hour windows: a whole hour absorbs any partial for that hour;
    two partials for the same hour merge to their covering span. Sorted."""
    by_hour: dict[int, HourWindow] = {}
    for group in groups:
        for w in group:
            cur = by_hour.get(w.hour)
            if cur is None:
                by_hour[w.hour] = w
            elif cur.whole or w.whole:
                by_hour[w.hour] = HourWindow(w.hour)
            else:
                by_hour[w.hour] = HourWindow(
                    w.hour, min(cur.start_min, w.start_min), max(cur.end_min, w.end_min)
                )
    return tuple(by_hour[h] for h in sorted(by_hour))


_LOCKOUT_TYPES = ("lock", "suspend", "suspendwake", "terminate", "kill", "shutdown")


@dataclass(frozen=True)
class PlayTimeActivity:
    mask: str
    description: str

    def as_cli(self) -> str:
        return f"{self.mask}[{self.description}]" if self.description else self.mask


@dataclass(frozen=True)
class TimekprUserSettings:
    """Canonical desired state for one timekpr user. Days are ISO-ordered
    (Monday first); hour lists are sorted ints; limits align with all 7 days
    (v1 always allows all 7 days — a day ban is expressible as a 0s limit)."""

    user: str
    allowed_hours: tuple[tuple[HourWindow, ...], ...]  # 7 tuples, Mon..Sun
    daily_limits: tuple[int, ...]  # 7 per-day budgets, seconds
    lockout: str = "lock"
    playtime_enabled: bool = False
    playtime_daily_limits: tuple[int, ...] = (0,) * 7
    playtime_activities: tuple[PlayTimeActivity, ...] = ()


def sanitize_mask_fragment(text: str) -> str:
    """Make a path/name fragment safe as a timekpr PlayTime regex mask part.

    Regex-escape, then replace the characters timekpr forbids in masks
    ([]^$ and the ; separator) with '.', which stays a valid regex.
    """
    escaped = re.escape(text)
    return _MASK_FORBIDDEN.sub(".", escaped)


def steam_masks(game_dirs: frozenset[str] | set[str]) -> list[PlayTimeActivity]:
    """PlayTime activities from installed-Steam game dirs (games.py scan).

    Masks match on `common/<installdir>` so they hold across library moves and
    catch both native processes (exe under the dir) and Proton (wine cmdline
    references the game .exe under the dir). Requires the enhanced activity
    monitor (cmdline matching) — enabled globally on the target.
    """
    activities: list[PlayTimeActivity] = []
    seen: set[str] = set()
    for game_dir in sorted(game_dirs):
        installdir = Path(game_dir).name
        if installdir in seen:
            continue
        seen.add(installdir)
        desc = _MASK_FORBIDDEN.sub(".", installdir)[:48]
        activities.append(
            PlayTimeActivity(
                # `common.` not `common/`: Proton games show the Windows path
                # (common\\MK10\\...) on their cmdline; `.` matches either separator.
                mask=f".*common.{sanitize_mask_fragment(installdir)}.*",
                description=desc,
            )
        )
    return activities


def _parse_extra_masks(node: object) -> list[PlayTimeActivity]:
    if node is None:
        return []
    if not isinstance(node, list):
        raise PolicyError(f"games.extra_masks must be a list: {node!r}")
    out = []
    for item in node:
        m = _EXTRA_MASK_RE.match(str(item).strip())
        if not m:
            raise PolicyError(f"bad extra mask (no ';' or nested '[]'): {item!r}")
        mask = m.group(1).strip()
        # timekpr full-matches a mask against the process's exe path (and, with
        # the enhanced monitor, its cmdline): a bare "sober" never matches
        # "/app/bin/sober". Verified live. Wrap anything not already anchored
        # with .* so a parent can write the natural thing.
        if not mask.startswith(".*"):
            mask = f".*{mask}.*"
        out.append(PlayTimeActivity(mask=mask, description=m.group(2) or ""))
    return out


def load_policy(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise PolicyError(f"policy file is not a mapping: {path}")
    return raw


def build_settings(
    policy: dict, game_dirs: frozenset[str] | set[str] = frozenset()
) -> TimekprUserSettings:
    """policy.yaml dict + installed-game dirs -> desired timekpr state."""
    user = policy.get("user")
    if not user or not isinstance(user, str):
        raise PolicyError("policy needs a 'user'")

    lockout = str(policy.get("lockout", "lock")).strip().lower()
    if lockout not in _LOCKOUT_TYPES:
        raise PolicyError(f"lockout must be one of {_LOCKOUT_TYPES}: {lockout!r}")

    hours = _per_day(policy.get("allowed_hours"), _parse_hour_window, "allowed_hours")
    limits = _per_day(policy.get("daily_limits"), parse_duration, "daily_limits")

    games = policy.get("games") or {}
    if not isinstance(games, dict):
        raise PolicyError(f"games must be a mapping: {games!r}")
    pt_enabled = bool(games.get("enabled", False))
    pt_limits = (
        _per_day(games.get("daily_limits"), parse_duration, "games.daily_limits")
        if pt_enabled
        else [0] * 7
    )
    activities: list[PlayTimeActivity] = []
    if pt_enabled:
        activities = steam_masks(game_dirs) + _parse_extra_masks(games.get("extra_masks"))

    return TimekprUserSettings(
        user=user,
        allowed_hours=tuple(tuple(h) for h in hours),  # HourWindows per day
        daily_limits=tuple(limits),
        lockout=lockout,
        playtime_enabled=pt_enabled,
        playtime_daily_limits=tuple(pt_limits),
        playtime_activities=tuple(activities),
    )


# ---------------------------------------------------------------------------
# Diffing desired state against live daemon state into timekpra invocations.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One timekpra invocation needed to reach the desired state."""

    setting: str  # human-readable key, e.g. "allowed_hours[monday]"
    current: str  # display of live value
    desired: str  # display of target value
    cli_args: tuple[str, ...]  # argv after `timekpra`


def _hours_display(hours: tuple[HourWindow, ...]) -> str:
    return ";".join(w.cli() for w in hours) if hours else "(none)"


def _limits_display(limits: tuple[int, ...]) -> str:
    return ";".join(str(s) for s in limits)


def plan_changes(desired: TimekprUserSettings, live: LiveState) -> list[Change]:
    """Minimal ordered changes to move `live` to `desired`.

    Ordering matters for safety: budgets and PlayTime config move first, the
    allowed-hours window LAST — so if an apply is interrupted, the state most
    likely to kick a user off (a narrowed window) hasn't been applied yet.
    """
    changes: list[Change] = []
    u = desired.user

    # All 7 days always allowed in v1 (day bans = 0-second limits).
    if live.allowed_weekdays != (1, 2, 3, 4, 5, 6, 7):
        changes.append(
            Change(
                "allowed_days",
                ";".join(map(str, live.allowed_weekdays)),
                "1;2;3;4;5;6;7",
                ("--setalloweddays", u, "1;2;3;4;5;6;7"),
            )
        )

    if live.daily_limits != desired.daily_limits:
        changes.append(
            Change(
                "daily_limits",
                _limits_display(live.daily_limits),
                _limits_display(desired.daily_limits),
                ("--settimelimits", u, _limits_display(desired.daily_limits)),
            )
        )

    if live.lockout != desired.lockout:
        changes.append(
            Change(
                "lockout",
                live.lockout,
                desired.lockout,
                ("--setlockouttype", u, desired.lockout),
            )
        )

    if live.playtime_enabled != desired.playtime_enabled:
        changes.append(
            Change(
                "playtime_enabled",
                str(live.playtime_enabled).lower(),
                str(desired.playtime_enabled).lower(),
                (
                    "--setplaytimeenabled",
                    u,
                    "true" if desired.playtime_enabled else "false",
                ),
            )
        )

    if desired.playtime_enabled or live.playtime_enabled:
        # Keep PlayTime days at all-7 too — its limits array aligns to its
        # allowed-days list, so a partial day list would skew the mapping.
        if live.playtime_allowed_weekdays != (1, 2, 3, 4, 5, 6, 7):
            changes.append(
                Change(
                    "playtime_allowed_days",
                    ";".join(map(str, live.playtime_allowed_weekdays)),
                    "1;2;3;4;5;6;7",
                    ("--setplaytimealloweddays", u, "1;2;3;4;5;6;7"),
                )
            )
        desired_acts = ";".join(a.as_cli() for a in desired.playtime_activities)
        live_acts = ";".join(a.as_cli() for a in live.playtime_activities)
        if live_acts != desired_acts:
            changes.append(
                Change(
                    "playtime_activities",
                    live_acts or "(none)",
                    desired_acts or "(none)",
                    ("--setplaytimeactivities", u, desired_acts),
                )
            )
        if live.playtime_daily_limits != desired.playtime_daily_limits:
            changes.append(
                Change(
                    "playtime_daily_limits",
                    _limits_display(live.playtime_daily_limits),
                    _limits_display(desired.playtime_daily_limits),
                    (
                        "--setplaytimelimits",
                        u,
                        _limits_display(desired.playtime_daily_limits),
                    ),
                )
            )

    # Allowed hours LAST (see docstring). One invocation per differing day.
    for i, day in enumerate(_DAY_NAMES):
        if live.allowed_hours[i] != desired.allowed_hours[i]:
            changes.append(
                Change(
                    f"allowed_hours[{day}]",
                    _hours_display(live.allowed_hours[i]),
                    _hours_display(desired.allowed_hours[i]),
                    (
                        "--setallowedhours",
                        u,
                        str(i + 1),
                        _hours_display(desired.allowed_hours[i]),
                    ),
                )
            )

    return changes


@dataclass(frozen=True)
class LiveState:
    """The daemon's current view of one user, normalized to our shapes.
    Built by the transport from D-Bus `getUserInformation(user, "F")`."""

    allowed_weekdays: tuple[int, ...]
    allowed_hours: tuple[tuple[HourWindow, ...], ...]
    daily_limits: tuple[int, ...]
    lockout: str
    playtime_enabled: bool
    playtime_allowed_weekdays: tuple[int, ...]
    playtime_daily_limits: tuple[int, ...]
    playtime_activities: tuple[PlayTimeActivity, ...]
    active_session_count: int = 0


@dataclass(frozen=True)
class SafetyVerdict:
    safe: bool
    reasons: tuple[str, ...] = field(default=())


def plan_is_safe_now(
    desired: TimekprUserSettings,
    live: LiveState,
    now: datetime | None = None,
) -> SafetyVerdict:
    """Would applying `desired` right now risk kicking an active user off?

    Two hazards, both requiring an active session to matter:
      1. The new allowed-hours window excludes the current hour.
      2. Today's new daily limit is at/below what's plausibly already spent —
         approximated conservatively: any *reduction* of today's limit while a
         session is active is flagged (we don't track spent time here).
    """
    when = now if now is not None else datetime.now()
    if live.active_session_count == 0:
        return SafetyVerdict(True)

    reasons: list[str] = []
    day_idx = when.weekday()  # Monday == 0, matches _DAY_NAMES order
    if not any(w.covers(when.time()) for w in desired.allowed_hours[day_idx]):
        reasons.append(
            f"new allowed-hours window for {_DAY_NAMES[day_idx]} excludes the "
            f"current time ({when:%H:%M}) — would cut off the active session"
        )
    if desired.daily_limits[day_idx] < live.daily_limits[day_idx]:
        reasons.append(
            f"today's limit shrinks ({live.daily_limits[day_idx]}s -> "
            f"{desired.daily_limits[day_idx]}s) while a session is active — "
            "may lock out immediately if already spent"
        )
    return SafetyVerdict(not reasons, tuple(reasons))


def apply_grants(
    settings: TimekprUserSettings,
    grant_windows_today: tuple[HourWindow, ...] | list[HourWindow],
    today: date,
) -> TimekprUserSettings:
    """Widen today's allowed hours by one-off grant windows. Grants only ever
    ADD hours, so the result is always at least the policy window — which is
    also why applying one passes the safety guard at any hour."""
    if not grant_windows_today:
        return settings
    idx = today.weekday()
    merged = merge_hour_windows(settings.allowed_hours[idx], grant_windows_today)
    hours = list(settings.allowed_hours)
    hours[idx] = merged
    return replace(settings, allowed_hours=tuple(hours))

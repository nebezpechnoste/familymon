"""The operator surface, as functions.

One implementation per thing a parent (or the agent acting for them) does to
familymon: read the policy, validate and replace it, plan and apply the
timekpr changes, grant an hour, import the timetable, read the day, read
health. Every function returns plain data (dicts, lists, strings) and never
prints; `familymonctl` renders that as text or, with `--json`, verbatim, and
`familymonctl mcp` exposes the same functions as tools. One implementation is
what keeps the two surfaces from drifting apart.

Failures the parent should read as a sentence are raised as `OpsError`; the
callers turn it into a click error or an MCP tool error. Nothing here applies
anything the caller did not ask for: writing the policy does not run `apply`,
and `apply` keeps the live-session guard (`UnsafeNow`) unless `force` is set.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

from familymon.config import (
    _DEFAULTS_DIR,
    capture_interval_seconds,
    config_file,
    db_path,
    policy_path,
    schedule_path,
    screenshots_dir,
    state_dir,
)
from familymon.policy.enforcement import (
    _DAY_NAMES,
    _UNLIMITED_SECONDS,
    PolicyError,
    TimekprUserSettings,
    apply_grants,
    build_settings,
    load_policy,
    parse_duration,
    plan_changes,
    plan_is_safe_now,
)
from familymon.storage import perms

DAY_NAMES = _DAY_NAMES
log = logging.getLogger(__name__)


class OpsError(Exception):
    """A failure the parent should read as a sentence, not a traceback."""


class UnsafeNow(OpsError):
    """Applying now would end the child's live session; needs an explicit force."""

    def __init__(self, reasons: tuple[str, ...], *, action: str = "apply") -> None:
        self.reasons = reasons
        self.action = action
        if action == "apply":
            hint = "Re-run with force only if the parent explicitly asked, or wait for logout."
        else:
            hint = (
                "Apply the pending policy first (enforce_plan, then enforce_apply; force "
                "only if the parent explicitly asked) or wait for logout."
            )
        super().__init__(
            f"refusing to {action} — it would cut off an active session: "
            + "; ".join(reasons)
            + ". "
            + hint
        )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _parse_mapping(text: str, what: str) -> dict:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise OpsError(f"{what} is not valid YAML: {e}") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise OpsError(f"{what} must be a YAML mapping, not {type(raw).__name__}")
    return raw


def _write_atomic(path: Path, text: str, *, mode: int) -> None:
    """Write via a temp file in the same directory, then rename, so a reader
    never sees a half-written file. Mode is set before the rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _keep_previous(path: Path, *, unless_equal: str | None = None) -> str | None:
    """Copy `path` to `<path>.prev` (one level of undo) and return that path.

    With `unless_equal` (the text about to be written), a write that changes
    nothing keeps the existing `.prev` instead of overwriting the undo copy
    with the current content (a retried `policy set` must not destroy the
    undo); the result is then the existing `.prev`, or None."""
    if not path.exists():
        return None
    prev = path.with_name(path.name + ".prev")
    if unless_equal is not None and path.read_text() == unless_equal:
        return str(prev) if prev.exists() else None
    shutil.copyfile(path, prev)
    shutil.copymode(path, prev)
    return str(prev)


def _changed_top_level(old: dict, new: dict) -> list[str]:
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def _audit(kind: str, data: dict) -> None:
    """Append an Event row when the DB exists. Never creates a DB just to
    log, and never fails the operation it records: by the time it runs the
    change has happened, so a locked or damaged DB is logged, not raised
    (an error here would read as "the change failed" and invite a retry)."""
    if not db_path().exists():
        return
    try:
        from familymon.storage.models import Event
        from familymon.storage.session import make_engine, make_session_factory

        with make_session_factory(make_engine(db_path()))() as s:
            s.add(Event(at=datetime.now(), kind=kind, data=data))
            s.commit()
    except Exception as e:  # broad on purpose, see docstring
        log.warning("audit event %s not written: %s", kind, e)


def _readonly_sessions():
    from familymon.storage.session import make_readonly_engine, make_session_factory

    if not db_path().exists():
        raise OpsError(f"no database at {db_path()}")
    return make_session_factory(make_readonly_engine(db_path()))


def _day(value: str | date | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise OpsError(f"day must be YYYY-MM-DD, got {value!r}") from None


def _hhmm(value: str, what: str) -> time:
    """HH:MM as a time, with the same rules as policy.yaml's personal_time."""
    from familymon.policy.personal_time import _hhmm as parse

    try:
        return parse(value, what)
    except PolicyError:
        raise OpsError(f"{what} must be HH:MM (00:00-23:59), got {value!r}") from None


def _hours_label(hours) -> str:
    """'07:00-21:59' for a contiguous run of whole hours, else timekpr's
    spelling ('7;8;22[00-29]'), '(none)' when empty."""
    hours = tuple(hours)
    if not hours:
        return "(none)"
    contiguous_whole = all(w.whole for w in hours) and [w.hour for w in hours] == list(
        range(hours[0].hour, hours[-1].hour + 1)
    )
    if contiguous_whole:
        return f"{hours[0].hour:02d}:00-{hours[-1].hour:02d}:59"
    return ";".join(w.cli() for w in hours)


def _limit_label(seconds: int) -> str:
    return "unlimited" if seconds >= _UNLIMITED_SECONDS else f"{seconds}s"


# ---------------------------------------------------------------------------
# Status and health
# ---------------------------------------------------------------------------


def status_summary() -> dict[str, Any]:
    """Where familymon's state is and what exists there."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        pkg_version = version("familymon")
    except PackageNotFoundError:
        pkg_version = None
    deb_version = None
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", "familymon"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            deb_version = proc.stdout.strip()
    user = None
    with contextlib.suppress(LookupError):
        from familymon.config import monitored_user

        user = monitored_user()
    return {
        "version": pkg_version,
        "package_version": deb_version,
        "uid": os.geteuid(),
        "state_dir": str(state_dir()),
        "config_dir": str(_config_override_dir()),
        "monitored_user": user,
        "db": {"path": str(db_path()), "exists": db_path().exists()},
        "screenshots_dir": {"path": str(screenshots_dir()), "exists": screenshots_dir().exists()},
        "policy": {"path": str(policy_path()), "exists": policy_path().exists()},
        "schedule": {"path": str(schedule_path()), "exists": schedule_path().exists()},
    }


def health_summary() -> dict[str, Any]:
    """The `health` report as data, with the warnings and hard failures the
    CLI prints and the exit code it would use (0 ok, 2 privacy violation,
    4 damaged DB)."""
    from familymon.health import compute_health
    from familymon.heartbeat import capture_problems, check_capture_freshness, latest_heartbeat
    from familymon.storage.session import make_engine, make_session_factory
    from familymon.timeline import day_bounds

    sessions = make_session_factory(make_engine(db_path()))
    r = compute_health(sessions, screenshots_dir())
    report = asdict(r)
    report["now"] = r.now.isoformat()

    warnings: list[str] = []
    if r.last_capture_age_seconds is not None and r.last_capture_age_seconds > 180:
        warnings.append("capture is stale (>3 min since last) — is capture-run alive?")
    if r.largest_gap_seconds_24h is not None and r.largest_gap_seconds_24h > 300:
        warnings.append(
            "a >5 min gap exists in the last 24h (crash, or laptop sleep — check the journal)"
        )
    if r.captures_last_24h and r.messaging_skips_24h / r.captures_last_24h > 0.10:
        warnings.append(
            "messaging full-skips are >10% of captures — either chats are often foregrounded, "
            "or a messaging window has unmappable geometry (screenshot gaps; capture "
            "rows/classification are unaffected)"
        )
    if r.last_db_recovery:
        warnings.append(f"DB was auto-recovered: {r.last_db_recovery}")

    failures: list[str] = []
    exit_code = 0
    if r.privacy_violations:
        failures.append(
            f"PRIVACY VIOLATION: {r.privacy_violations} capture(s) with a messaging window "
            "present have an UN-redacted screenshot on disk"
        )
        exit_code = 2
    elif r.db_integrity != "ok":
        failures.append(f"DB INTEGRITY: {r.db_integrity} (automatic recovery failed)")
        exit_code = 4

    heartbeat = latest_heartbeat(sessions) or check_capture_freshness(sessions)
    return {
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "report": report,
        "warnings": warnings,
        "failures": failures,
        "heartbeat": {
            "status": heartbeat.status,
            "session": heartbeat.session,
            "age_seconds": heartbeat.age_seconds,
            "last_capture_at": (
                heartbeat.last_capture_at.isoformat() if heartbeat.last_capture_at else None
            ),
            "checked_at": heartbeat.checked_at.isoformat(),
        },
        "capture_problems_today": capture_problems(sessions, *day_bounds(date.today())),
    }


def events_on(day: str | date | None = None, kinds: tuple[str, ...] = ()) -> dict[str, Any]:
    """The day's operational events (heartbeats left out unless asked for)."""
    from sqlalchemy import select

    from familymon.storage.models import Event

    target = _day(day)
    start = datetime.combine(target, time.min)
    q = select(Event).where(Event.at >= start, Event.at < start + timedelta(days=1))
    q = q.where(Event.kind.in_(kinds)) if kinds else q.where(Event.kind != "heartbeat")
    with _readonly_sessions()() as s:
        rows = s.scalars(q.order_by(Event.at)).all()
    return {
        "day": target.isoformat(),
        "count": len(rows),
        "events": [{"at": ev.at.isoformat(), "kind": ev.kind, "data": ev.data} for ev in rows],
    }


def timeline(day: str | date | None = None) -> dict[str, Any]:
    """One day as the dashboard shows it: blocks (app names and titles hidden
    during personal time), totals per category, lesson conflicts, the
    watcher's verdict. Never any image."""
    from familymon.timeline import day_summary

    return day_summary(_readonly_sessions(), _day(day), capture_interval_seconds())


# ---------------------------------------------------------------------------
# Policy (policy.yaml)
# ---------------------------------------------------------------------------


def _settings_summary(settings: TimekprUserSettings) -> dict[str, Any]:
    return {
        "user": settings.user,
        "lockout": settings.lockout,
        "allowed_hours": {
            day: _hours_label(settings.allowed_hours[i]) for i, day in enumerate(DAY_NAMES)
        },
        "daily_limits": {
            day: _limit_label(settings.daily_limits[i]) for i, day in enumerate(DAY_NAMES)
        },
        "games": {
            "enabled": settings.playtime_enabled,
            "daily_limits": {
                day: _limit_label(settings.playtime_daily_limits[i])
                for i, day in enumerate(DAY_NAMES)
            },
            "extra_masks": [a.as_cli() for a in settings.playtime_activities],
        },
    }


def validate_policy(text: str) -> dict[str, Any]:
    """Run a policy document through every parser that reads policy.yaml.

    Returns {"ok", "errors", "summary"}: `errors` lists one message per
    failing section (so a document with two mistakes reports both), `summary`
    is the resolved meaning of the sections that parsed, in the words the
    parent uses (per-day windows, "unlimited", ...)."""
    from familymon.policy.personal_time import parse_personal_time
    from familymon.policy.webblock import parse_web_block
    from familymon.storage.retention import screenshot_days

    try:
        policy = _parse_mapping(text, "policy")
    except OpsError as e:
        return {"ok": False, "errors": [str(e)], "summary": {}}

    errors: list[str] = []
    summary: dict[str, Any] = {}
    try:
        summary.update(_settings_summary(build_settings(policy)))
    except PolicyError as e:
        errors.append(f"enforcement: {e}")
    games = policy.get("games") or {}
    if isinstance(games, dict):
        when = str(games.get("charge_browser_games_when", "visible")).strip().lower()
        if when not in ("visible", "focused"):
            errors.append(
                f"games.charge_browser_games_when must be 'visible' or 'focused', got {when!r}"
            )
        summary.setdefault("games", {})
        summary["games"]["charge_browser_games"] = bool(games.get("charge_browser_games", False))
        summary["games"]["charge_browser_games_when"] = when
    try:
        rules = parse_personal_time(policy)
        summary["personal_time"] = {
            day: (
                None
                if rule is None
                else {
                    "from": rule.start.strftime("%H:%M"),
                    "until": rule.until.strftime("%H:%M") if rule.until else "bedtime lock",
                }
            )
            for day, rule in zip(DAY_NAMES, rules, strict=True)
        }
    except PolicyError as e:
        errors.append(f"personal_time: {e}")
    try:
        wb = parse_web_block(policy)
        summary["web_block"] = {
            "enabled": wb.enabled,
            "categories": list(wb.categories),
            "extra_domains": list(wb.extra_domains),
            "allow_domains": list(wb.allow_domains),
            "forbid_other_browsers": wb.forbid_other_browsers,
            "forbid_apps": list(wb.forbid_apps),
            "block_private": wb.block_private,
        }
    except PolicyError as e:
        errors.append(f"web_block: {e}")
    try:
        summary["retention_screenshot_days"] = screenshot_days(policy)
    except ValueError as e:
        errors.append(f"retention: {e}")
    return {"ok": not errors, "errors": errors, "summary": summary}


def policy_read() -> dict[str, Any]:
    """The policy file as it is on disk (comments included), plus its parsed
    meaning. `exists` is false when no policy has been written yet."""
    path = policy_path()
    if not path.exists():
        return {"path": str(path), "exists": False, "text": None, "summary": {}}
    text = path.read_text()
    return {"path": str(path), "exists": True, "text": text, **validate_policy(text)}


# What happens after each top-level key changes (docs/reference/config.md §2).
_POLICY_EFFECTS = {
    "user": "enforce plan, then enforce apply (or the hourly reconciler)",
    "lockout": "enforce plan, then enforce apply (or the hourly reconciler)",
    "allowed_hours": "enforce plan, then enforce apply (or the hourly reconciler)",
    "daily_limits": "enforce plan, then enforce apply (or the hourly reconciler)",
    "games": "enforce plan, then enforce apply (or the hourly reconciler); "
    "charge_browser_games* take effect at the next chargeback tick",
    "web_block": "live at the next web-block tick (every minute)",
    "retention": "next retention run (daily); preview with retention --dry-run",
    "personal_time": "live within a minute (capture and notify re-read it every tick)",
}


def policy_write(text: str) -> dict[str, Any]:
    """Validate a complete policy document and make it the policy.

    The previous file is kept as policy.yaml.prev (copy it back to undo). The
    file is written exactly as given, comments and all. Nothing is applied:
    the result says, per changed key, what makes it take effect."""
    result = validate_policy(text)
    if not result["ok"]:
        raise OpsError("policy rejected: " + "; ".join(result["errors"]))
    path = policy_path()
    old: dict = {}
    if path.exists():
        with contextlib.suppress(OSError, yaml.YAMLError):
            old = yaml.safe_load(path.read_text()) or {}
            if not isinstance(old, dict):
                old = {}
    previous = _keep_previous(path, unless_equal=text)
    if previous:
        perms.secure(Path(previous))
    new = yaml.safe_load(text) or {}
    changed = _changed_top_level(old, new)
    _write_atomic(path, text, mode=0o600)
    perms.secure(path)
    _audit("policy_changed", {"changed": changed, "previous": previous})
    return {
        "path": str(path),
        "changed_keys": changed,
        "previous_saved_to": previous,
        "summary": result["summary"],
        "takes_effect": {k: _POLICY_EFFECTS.get(k, "unknown key") for k in changed},
    }


# ---------------------------------------------------------------------------
# Parent-editable config files (/etc/familymon overrides)
# ---------------------------------------------------------------------------


def _validate_extras(path: Path) -> None:
    from familymon.policy.schedule import load_extras

    try:
        load_extras(path)
    except (ValueError, KeyError, TypeError) as e:
        raise OpsError(f"schedule_extras.yaml rejected: {e}") from None


def _validate_messaging(path: Path) -> None:
    """messaging_apps.yaml is the privacy contract: the loader tolerates a
    missing or misspelled key (it reads an empty list), so the write path
    must not. Unknown keys and an empty contract are refused."""
    import re

    from familymon.capture.messaging_skip import MessagingSkipList

    raw = _parse_mapping(path.read_text(), "messaging_apps.yaml")
    unknown = set(raw) - {"window_classes", "title_patterns"}
    if unknown:
        raise OpsError(
            f"messaging_apps.yaml: unknown key(s) {sorted(unknown)}; expected window_classes "
            "and title_patterns (a misspelled key would silently empty the privacy contract)"
        )
    try:
        skip = MessagingSkipList.from_file(path)
    except (re.error, AttributeError, TypeError, ValueError) as e:
        raise OpsError(f"messaging_apps.yaml rejected: {e}") from None
    if not skip.window_classes and not skip.title_patterns:
        raise OpsError(
            "messaging_apps.yaml rejected: no window classes and no title patterns, "
            "which would capture messaging windows to image"
        )


def _validate_string_lists(name: str, keys: tuple[str, ...]):
    def check(path: Path) -> None:
        raw = _parse_mapping(path.read_text(), name)
        unknown = set(raw) - set(keys)
        if unknown:
            raise OpsError(f"{name}: unknown key(s) {sorted(unknown)}; expected {list(keys)}")
        for key in keys:
            value = raw.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise OpsError(f"{name}: {key} must be a list of strings")

    return check


def _validate_category_table(name: str):
    """domains_starter.yaml / title_brands.yaml: category -> list of strings.

    Stricter than the loaders on purpose: they skip a category they do not
    know, so a typo would silently drop that whole section; here it is an
    error. The real loader runs as well, so anything else it rejects, this
    rejects."""

    def check(path: Path) -> None:
        from familymon.analyze.categories import Category

        raw = _parse_mapping(path.read_text(), name)
        known = {c.value for c in Category}
        for cat, items in raw.items():
            if cat not in known:
                raise OpsError(f"{name}: unknown category {cat!r}; known: {sorted(known)}")
            if items is not None and (
                not isinstance(items, list) or not all(isinstance(v, str) for v in items)
            ):
                raise OpsError(f"{name}: {cat} must be a list of strings")
        if name == "domains_starter.yaml":
            from familymon.categorize.domains import DomainCategoryLookup as loader
        else:
            from familymon.categorize.brands import BrandTitleLookup as loader
        try:
            loader.from_yaml(path)
        except (TypeError, ValueError) as e:
            raise OpsError(f"{name} rejected: {e}") from None

    return check


# name -> (what makes a change take effect, validator)
CONFIG_FILES: dict[str, tuple[str, Any]] = {
    "schedule_extras.yaml": (
        "not live: run schedule refresh (or schedule import)",
        _validate_extras,
    ),
    "messaging_apps.yaml": (
        "next capture tick (every probe loads it when it starts)",
        _validate_messaging,
    ),
    "game_apps.yaml": (
        "capture: next game-library scan (within 30 minutes), or restart familymon-capture.service",
        _validate_string_lists("game_apps.yaml", ("window_classes", "binaries")),
    ),
    "domains_starter.yaml": (
        "next web-block tick, notify tick and classify run",
        _validate_category_table("domains_starter.yaml"),
    ),
    "title_brands.yaml": (
        "next process start of each consumer (per tick for the timers)",
        _validate_category_table("title_brands.yaml"),
    ),
}


def _config_override_dir() -> Path:
    return Path(os.environ.get("FAMILYMON_CONFIG_DIR") or "/etc/familymon")


def config_read(name: str) -> dict[str, Any]:
    """A parent-editable config file: the override in /etc/familymon when
    there is one, else the bundled default (`overridden` says which)."""
    if name not in CONFIG_FILES:
        raise OpsError(f"unknown config file {name!r}; one of {sorted(CONFIG_FILES)}")
    path = config_file(name)
    override = _config_override_dir() / name
    return {
        "name": name,
        "path": str(path),
        "overridden": path.parent != _DEFAULTS_DIR,
        "override_path": str(override),
        "text": path.read_text(),
        "takes_effect": CONFIG_FILES[name][0],
    }


def config_write(name: str, text: str) -> dict[str, Any]:
    """Validate and write a config override to /etc/familymon (or
    $FAMILYMON_CONFIG_DIR). The previous override is kept as <name>.prev.
    Files stay world-readable: the capture probe runs as the monitored user
    and loads messaging_apps.yaml itself."""
    if name not in CONFIG_FILES:
        raise OpsError(f"unknown config file {name!r}; one of {sorted(CONFIG_FILES)}")
    effect, validator = CONFIG_FILES[name]
    target = _config_override_dir() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    probe = target.with_name(f".{name}.validate")
    probe.write_text(text)
    try:
        _parse_mapping(text, name)
        validator(probe)
    finally:
        probe.unlink(missing_ok=True)
    previous = _keep_previous(target, unless_equal=text)
    _write_atomic(target, text, mode=0o644)
    _audit("config_changed", {"name": name, "previous": previous})
    return {
        "name": name,
        "path": str(target),
        "previous_saved_to": previous,
        "takes_effect": effect,
    }


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def _schedule_dict(schedule, path: Path, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    by_category: dict[str, int] = {}
    for b in schedule.blocks:
        by_category[b.category] = by_category.get(b.category, 0) + 1
    return {
        "path": str(path),
        "exists": True,
        "timezone": schedule.timezone,
        "blocks": [b.to_dict() for b in schedule.blocks],
        "breaks": [
            {**br.to_dict(), "label": br.label, "in_progress": br.covers(today)}
            for br in schedule.breaks
        ],
        "count": len(schedule.blocks),
        "by_category": by_category,
    }


def schedule_summary() -> dict[str, Any]:
    """The imported weekly schedule: blocks (with their category and notify
    flag) and breaks, or `exists: false` before the first import."""
    from familymon.policy.schedule import Schedule

    path = schedule_path()
    if not path.exists():
        return {"path": str(path), "exists": False, "blocks": [], "breaks": [], "count": 0}
    return _schedule_dict(Schedule.load(path), path)


def schedule_import_data(data: Any, fmt: str = "kingsinterhigh") -> dict[str, Any]:
    """Replace the schedule from a timetable export (parsed JSON), applying
    the parent's extras (exclusions, custom blocks, breaks). The previous
    schedule is kept as schedule.yaml.prev."""
    from familymon.policy.importers import parse_kingsinterhigh_week
    from familymon.policy.schedule import apply_extras, load_extras

    if fmt != "kingsinterhigh":
        raise OpsError(f"unknown timetable format {fmt!r}; only 'kingsinterhigh' is supported")
    try:
        excludes, extras, breaks = load_extras(config_file("schedule_extras.yaml"))
    except (ValueError, KeyError, TypeError) as e:
        raise OpsError(f"schedule_extras.yaml is unusable: {e}") from None
    try:
        schedule = parse_kingsinterhigh_week(data, exclude_subjects=excludes)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise OpsError(f"timetable export not understood as {fmt}: {e}") from None
    if not schedule.blocks:
        raise OpsError(
            f"timetable export not understood as {fmt}: no lessons found in it "
            "(the current schedule is kept)"
        )
    schedule = apply_extras(schedule, excludes, extras, breaks)
    previous = _keep_previous(schedule_path())
    schedule.save(schedule_path())
    out = _schedule_dict(schedule, schedule_path())
    out["previous_saved_to"] = previous
    out["imported"] = {
        "extras": len(extras),
        "excluded_subjects": excludes,
        "breaks": len(breaks),
    }
    return out


def schedule_refresh() -> dict[str, Any]:
    """Re-apply the extras file to the current schedule: lessons are kept,
    exclusions, custom blocks and breaks are rebuilt. The previous schedule
    is kept as schedule.yaml.prev."""
    from familymon.policy.schedule import Schedule, apply_extras, load_extras

    path = schedule_path()
    if not path.exists():
        raise OpsError(f"no schedule at {path} — run 'familymonctl schedule import' first")
    current = Schedule.load(path)
    lessons = Schedule(
        blocks=[b for b in current.blocks if b.category == "school"], timezone=current.timezone
    )
    try:
        excludes, extras, breaks = load_extras(config_file("schedule_extras.yaml"))
    except (ValueError, KeyError, TypeError) as e:
        raise OpsError(f"schedule_extras.yaml is unusable: {e}") from None
    schedule = apply_extras(lessons, excludes, extras, breaks)
    previous = _keep_previous(path)
    schedule.save(path)
    out = _schedule_dict(schedule, path)
    out["previous_saved_to"] = previous
    out["imported"] = {
        "extras": len(extras),
        "excluded_subjects": excludes,
        "breaks": len(breaks),
    }
    return out


# ---------------------------------------------------------------------------
# Enforcement (timekpr-next)
# ---------------------------------------------------------------------------


def _transport():
    from familymon.policy import timekpr_admin

    return timekpr_admin.LiveTimekpr()


def game_library(user: str):
    """The monitored user's games: their Steam library as scanned now, plus
    every game seen installed before (games.py, KnownGames)."""
    import pwd

    from familymon.capture.games import GameLibrary

    try:
        home = Path(pwd.getpwnam(user).pw_dir)
    except KeyError:
        raise OpsError(f"user {user!r} does not exist") from None
    return GameLibrary.from_system(home=home, known_path=state_dir() / "known_games.json")


def desired_settings(now: datetime | None = None) -> TimekprUserSettings:
    """policy.yaml + the Steam scan + today's active grants -> desired timekpr state."""
    from familymon.capture.games import GameLibrary
    from familymon.policy.grants import GrantStore, active_windows

    path = policy_path()
    if not path.exists():
        raise OpsError(
            f"no policy at {path} — write one first (familymonctl policy set, or copy "
            "/usr/share/familymon/policy_example.yaml there)"
        )
    try:
        policy = load_policy(path)
        # Scan the *monitored* user's Steam library, not the invoker's — this
        # runs as root, whose home has no Steam, which would yield zero masks.
        user = policy.get("user")
        gamelib = game_library(user) if isinstance(user, str) else GameLibrary.from_system()
        settings = build_settings(policy, gamelib.game_dirs)
    except PolicyError as e:
        raise OpsError(f"policy.yaml is unusable: {e}") from None
    # One-off grants widen today's window; folding them in here is what stops
    # the hourly reconciler from reverting them.
    now = now or datetime.now()
    windows = active_windows(GrantStore(state_dir() / "grants.json"), settings.user, now)
    return apply_grants(settings, windows, now.date())


def _live_dict(user: str, live) -> dict[str, Any]:
    return {
        "user": user,
        "active_sessions": live.active_session_count,
        "lockout": live.lockout,
        "allowed_weekdays": list(live.allowed_weekdays),
        "allowed_hours": {
            day: _hours_label(live.allowed_hours[i]) for i, day in enumerate(DAY_NAMES)
        },
        "daily_limits": {
            day: _limit_label(live.daily_limits[i]) for i, day in enumerate(DAY_NAMES)
        },
        "playtime": {
            "enabled": live.playtime_enabled,
            "daily_limits": {
                day: _limit_label(live.playtime_daily_limits[i]) for i, day in enumerate(DAY_NAMES)
            },
            "activities": [
                {"mask": a.mask, "description": a.description} for a in live.playtime_activities
            ],
        },
    }


def _changes_list(changes) -> list[dict[str, str]]:
    return [{"setting": c.setting, "live": c.current, "desired": c.desired} for c in changes]


def _policy_user() -> str:
    path = policy_path()
    if not path.exists():
        raise OpsError(f"no policy at {path}; give a user name explicitly")
    try:
        user = load_policy(path).get("user")
    except PolicyError as e:
        raise OpsError(f"policy.yaml is unusable: {e}") from None
    if not isinstance(user, str) or not user:
        raise OpsError("policy.yaml has no 'user'")
    return user


def live_summary(user: str | None = None, *, transport=None) -> dict[str, Any]:
    """timekpr's current settings for the user (D-Bus), in the policy's words."""
    user = user or _policy_user()
    transport = transport or _transport()
    return _live_dict(user, transport.get_live_state(user))


def plan_summary(*, transport=None, now: datetime | None = None) -> dict[str, Any]:
    """Dry run of `apply`: the changes it would make and whether that is safe
    for a live session right now. Changes nothing."""
    now = now or datetime.now()
    desired = desired_settings(now)
    transport = transport or _transport()
    live = transport.get_live_state(desired.user)
    changes = plan_changes(desired, live)
    verdict = plan_is_safe_now(desired, live, now)
    return {
        "user": desired.user,
        "changes": _changes_list(changes),
        "count": len(changes),
        "up_to_date": not changes,
        "safe_now": verdict.safe,
        "reasons": list(verdict.reasons),
    }


def apply_policy(*, force: bool = False, transport=None, now: datetime | None = None) -> dict:
    """Apply the policy to the live timekpr daemon (minimal diff).

    Refuses with `UnsafeNow` when a change would end the child's live
    session, unless `force` is true — which the parent must have asked for."""
    now = now or datetime.now()
    desired = desired_settings(now)
    transport = transport or _transport()
    live = transport.get_live_state(desired.user)
    changes = plan_changes(desired, live)
    if not changes:
        return {"user": desired.user, "applied": [], "count": 0, "up_to_date": True}
    verdict = plan_is_safe_now(desired, live, now)
    if not verdict.safe and not force:
        raise UnsafeNow(verdict.reasons)
    transport.apply(changes)
    _audit(
        "policy_applied",
        {"user": desired.user, "changes": [c.setting for c in changes], "forced": bool(force)},
    )
    return {
        "user": desired.user,
        "applied": _changes_list(changes),
        "count": len(changes),
        "up_to_date": False,
        "forced": bool(force) and not verdict.safe,
    }


def grants_active(now: datetime | None = None) -> dict[str, Any]:
    """Today's active one-off grants."""
    from familymon.policy.grants import GrantStore

    now = now or datetime.now()
    rows = [g for g in GrantStore(state_dir() / "grants.json")._load() if g.is_active(now)]
    return {
        "grants": [
            {"user": g.user, "day": g.day, "start": g.start, "end": g.end, "note": g.note}
            for g in rows
        ],
        "count": len(rows),
    }


def grant(
    user: str,
    *,
    until: str | None = None,
    from_: str | None = None,
    playtime: str | None = None,
    note: str = "",
    transport=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A one-off grant for today: `until` widens the allowed hours up to
    HH:MM (from `from_`, default now), `playtime` adds game time ("30m").
    Applied immediately; honored by the reconciler; expires on its own.

    The grant is folded into the policy, so applying it would also apply
    whatever policy change is still pending. When that would cut the live
    session (the guard that `apply_policy` runs), only today's widened
    window is applied and the rest is reported as `deferred` for
    `apply_policy`; when even today's window would not cover the current
    time, the grant is not stored and `UnsafeNow` is raised."""
    from familymon.policy.grants import GrantStore, until_today

    if not until and not playtime:
        raise OpsError("give until=HH:MM and/or playtime=<duration>")
    policy_user = _policy_user()
    if user != policy_user:
        raise OpsError(f"policy user is {policy_user!r}, not {user!r}")
    now = now or datetime.now()
    transport = transport or _transport()
    out: dict[str, Any] = {"user": user}
    if until:
        try:
            start, end = until_today(
                _hhmm(until, "until"),
                now=now,
                start=_hhmm(from_, "from") if from_ else None,
            )
        except ValueError as e:
            raise OpsError(str(e)) from None
        store = GrantStore(state_dir() / "grants.json")
        g = store.add(user, start, end, now=now, note=note)
        deferred: list[str] = []
        try:
            desired = desired_settings(now)  # policy + grants, this one included
            live = transport.get_live_state(user)
            changes = plan_changes(desired, live)
            verdict = plan_is_safe_now(desired, live, now)
            if not verdict.safe:
                # A policy change is pending that apply refused (or nobody
                # applied yet). The grant must not push it onto the live
                # session: widen today's window only, leave the rest.
                idx = now.weekday()
                today = f"allowed_hours[{_DAY_NAMES[idx]}]"
                if not any(w.covers(now.time()) for w in desired.allowed_hours[idx]):
                    raise UnsafeNow(verdict.reasons, action="apply the grant")
                deferred = [c.setting for c in changes if c.setting != today]
                changes = [c for c in changes if c.setting == today]
        except OpsError:
            store.remove(g)  # only this grant; earlier ones stay
            raise
        transport.apply(changes)
        _audit(
            "grant_hours",
            {"user": user, "day": g.day, "start": g.start, "end": g.end, "note": note},
        )
        out["hours"] = {
            "day": g.day,
            "start": g.start,
            "end": g.end,
            "timekpr_changes": _changes_list(changes),
            "deferred": deferred,
        }
    if playtime:
        try:
            secs = parse_duration(playtime)
        except PolicyError as e:
            raise OpsError(str(e)) from None
        transport.adjust_playtime_left(user, secs)
        _audit("grant_playtime", {"user": user, "seconds": secs, "note": note})
        out["playtime_seconds"] = secs
    return out


def revoke_grants(user: str, *, transport=None, now: datetime | None = None) -> dict[str, Any]:
    """Drop today's hour grants for the user and converge back to policy,
    unless narrowing now would cut the active session (then the reconciler
    converges once it is safe)."""
    from familymon.policy.grants import GrantStore

    policy_user = _policy_user()
    if user != policy_user:
        raise OpsError(f"policy user is {policy_user!r}, not {user!r}")
    now = now or datetime.now()
    n = GrantStore(state_dir() / "grants.json").revoke(user, now)
    desired = desired_settings(now)
    transport = transport or _transport()
    live = transport.get_live_state(user)
    changes = plan_changes(desired, live)
    verdict = plan_is_safe_now(desired, live, now)
    if changes and not verdict.safe:
        return {
            "user": user,
            "revoked": n,
            "applied": [],
            "deferred": True,
            "reasons": list(verdict.reasons),
        }
    transport.apply(changes)
    return {"user": user, "revoked": n, "applied": _changes_list(changes), "deferred": False}


def games_known() -> dict[str, Any]:
    """The game dirs familymon budgets: remembered ones plus those installed now."""
    from familymon.capture.games import load_known_games

    dirs = sorted(load_known_games(state_dir() / "known_games.json"))
    return {
        "games": [{"dir": d, "installed": os.path.isdir(d)} for d in dirs],
        "count": len(dirs),
    }


def games_forget(dirs: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Stop budgeting these remembered game dirs (a still-installed one is
    re-added at the next scan). The previous list is kept as
    known_games.json.prev."""
    from familymon.capture.games import load_known_games, save_known_games

    path = state_dir() / "known_games.json"
    known = load_known_games(path)
    missing = [d for d in dirs if d not in known]
    if missing:
        raise OpsError(f"not remembered: {', '.join(missing)}")
    previous = _keep_previous(path)
    save_known_games(path, known - set(dirs))
    return {
        "forgotten": list(dirs),
        "remaining": len(known) - len(dirs),
        "previous_saved_to": previous,
    }


# ---------------------------------------------------------------------------
# Personal time
# ---------------------------------------------------------------------------


def personal_time_summary(
    day: str | date | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    """Today's personal-time window and what it covered on `day`: captures
    taken without a screenshot whose app and title are hidden, and the game
    minutes among them."""
    from sqlalchemy import func, select

    from familymon.policy.personal_time import current_window
    from familymon.storage.models import Capture, Classification

    now = now or datetime.now()
    window = None
    if policy_path().exists():
        try:
            window = current_window(
                now, policy_path=policy_path(), grants_path=state_dir() / "grants.json"
            )
        except Exception as e:
            raise OpsError(f"personal_time in policy.yaml is unusable: {e}") from None
    out: dict[str, Any] = {
        "now": now.isoformat(timespec="minutes"),
        "window": None
        if window is None
        else {
            "start": window.start.isoformat(timespec="minutes"),
            "end": window.end.isoformat(timespec="minutes"),
            "ends_at_bedtime": window.ends_at_bedtime,
            "label": window.label(),
        },
        "active_now": window is not None and window.active(now),
    }
    target = _day(day) if day else now.date()
    out["day"] = target.isoformat()
    if not db_path().exists():
        out.update({"hidden_captures": 0, "hidden_game_captures": 0, "hidden_game_minutes": 0})
        return out
    start = datetime.combine(target, time.min)
    in_window = (
        Capture.personal_time.is_(True),
        Capture.captured_at >= start,
        Capture.captured_at < start + timedelta(days=1),
    )
    with _readonly_sessions()() as s:
        total = s.scalar(select(func.count()).where(*in_window)) or 0
        games = (
            s.scalar(
                select(func.count())
                .select_from(Capture)
                .join(Classification, Classification.capture_id == Capture.id)
                .where(*in_window, Classification.category == "games")
            )
            or 0
        )
    out.update(
        {
            "hidden_captures": total,
            "hidden_game_captures": games,
            "hidden_game_minutes": round(games * capture_interval_seconds() / 60),
        }
    )
    return out


def to_json(data: Any) -> str:
    return json.dumps(data, indent=1, sort_keys=True, default=str)

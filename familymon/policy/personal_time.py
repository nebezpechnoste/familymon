"""Personal time: a daily window the parent does not look into.

The parent's words (2026-10-19): "I don't want to peek into what he watches,
but I don't want to lose control over gaming time either." So from
`personal_time.<day>.from` until timekpr's bedtime lock for that day (the end
of `allowed_hours`, widened by any active grant), or an explicit `until`:

  * the capture probe takes no screenshot; window class and title are still
    recorded, and the row is flagged `personal_time`;
  * the classifier and the browser-game chargeback run as usual on those
    rows, so game minutes keep counting against the PlayTime budget;
  * the dashboard and its API show the period as one grey block with its
    length and the game minutes in it, and nothing else: no titles, no
    categories, no apps.

The window ends at the bedtime lock rather than "when the session goes away"
so that, if the lock does not happen for any reason, normal monitoring
resumes at bedtime. A one-off grant (`enforce grant --until`) extends both the
lock and the window. A daily limit that runs out earlier just means there is
no session to capture.

`current_window` reads policy.yaml and grants.json, which are root-only, so it
runs in the root supervisor, the notify tick and `familymonctl personal-time`;
the dashboard never needs it, it reads the flag on the rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from familymon.policy.enforcement import (
    HourWindow,
    PolicyError,
    _parse_hour_window,
    _per_day,
    load_policy,
    merge_hour_windows,
)
from familymon.policy.grants import GrantStore, active_windows

SKIP_REASON = "personal_time"  # screenshot_skipped_reason on rows in the window

_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def _hhmm(value: object, what: str) -> time:
    m = _HHMM_RE.match(value.strip()) if isinstance(value, str) else None
    if m is None:
        raise PolicyError(f'{what} must be a quoted "HH:MM" time, got {value!r}')
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        raise PolicyError(f"{what} is not a time of day: {value!r}")
    return time(hour, minute)


@dataclass(frozen=True)
class PersonalTimeRule:
    """One day's rule: from `start`; until `until`, or the bedtime lock."""

    start: time
    until: time | None = None


def _is_off(node: object) -> bool:
    if node is None or node is False:
        return True
    return isinstance(node, str) and node.strip().lower() == "off"


def _parse_rule(node: object) -> PersonalTimeRule | None:
    if _is_off(node):
        return None
    shape_ok = isinstance(node, dict) and "from" in node and not set(node) - {"from", "until"}
    if not shape_ok:
        raise PolicyError(
            'personal_time entries are {from: "HH:MM"}, '
            f'{{from: "HH:MM", until: "HH:MM"}} or off, got {node!r}'
        )
    start = _hhmm(node["from"], "personal_time.from")
    until = None if node.get("until") is None else _hhmm(node["until"], "personal_time.until")
    if until is not None and until <= start:
        raise PolicyError(f"personal_time.until must be after from: {node!r}")
    return PersonalTimeRule(start, until)


def parse_personal_time(policy: dict) -> tuple[PersonalTimeRule | None, ...]:
    """The `personal_time` section as seven per-day rules (Monday first).
    Absent or `off` means no personal time on any day."""
    node = policy.get("personal_time")
    if _is_off(node):
        return (None,) * 7
    if not isinstance(node, dict):
        raise PolicyError(
            'personal_time must be a per-day mapping such as {default: {from: "20:00"}}, or off'
        )
    if "from" in node:
        node = {"default": node}  # one rule for every day
    return tuple(_per_day(node, _parse_rule, "personal_time"))


@dataclass(frozen=True)
class PersonalTimeWindow:
    start: datetime  # naive local time
    end: datetime  # exclusive
    ends_at_bedtime: bool

    def active(self, now: datetime) -> bool:
        return self.start <= _local(now) < self.end

    def label(self) -> str:
        tail = " (ends at the bedtime lock)" if self.ends_at_bedtime else ""
        return f"{self.start:%H:%M}–{self.end:%H:%M}{tail}"  # noqa: RUF001 (en dash)


def _local(now: datetime) -> datetime:
    """Policy times are the kid's wall clock; compare naive-local."""
    return now.astimezone().replace(tzinfo=None) if now.tzinfo is not None else now


def bedtime_end(day: date, allowed: Iterable[HourWindow]) -> datetime | None:
    """When timekpr locks the session on `day`: the minute after the last
    allowed one (allowed through 21:59 -> 22:00; through 23:59 -> midnight)."""
    last = max(allowed, key=lambda w: (w.hour, w.end_min), default=None)
    if last is None:
        return None
    return datetime.combine(day, time.min) + timedelta(hours=last.hour, minutes=last.end_min + 1)


def window_for(
    rules: tuple[PersonalTimeRule | None, ...],
    day: date,
    allowed_today: Iterable[HourWindow] = (),
) -> PersonalTimeWindow | None:
    """`day`'s window, or None (no rule, or it would start after it ends)."""
    rule = rules[day.weekday()]
    if rule is None:
        return None
    start = datetime.combine(day, rule.start)
    if rule.until is not None:
        end, bedtime = datetime.combine(day, rule.until), False
    else:
        end, bedtime = bedtime_end(day, allowed_today), True
        if end is None:
            return None
    if end <= start:
        return None
    return PersonalTimeWindow(start, end, bedtime)


def current_window(
    now: datetime, *, policy_path: Path, grants_path: Path | None = None
) -> PersonalTimeWindow | None:
    """Today's window from policy.yaml plus the active grants, or None when
    the policy has no personal time today. Raises PolicyError (or OSError,
    yaml.YAMLError) when the policy can't be used; callers decide what that
    means (the supervisor: monitor in full)."""
    local = _local(now)
    policy = load_policy(policy_path)
    rules = parse_personal_time(policy)
    rule = rules[local.weekday()]
    if rule is None:
        return None
    hours: tuple[HourWindow, ...] = ()
    if rule.until is None:
        hours = tuple(
            _per_day(policy.get("allowed_hours"), _parse_hour_window, "allowed_hours")[
                local.weekday()
            ]
        )
        user = policy.get("user")
        if grants_path is not None and isinstance(user, str):
            hours = merge_hour_windows(hours, active_windows(GrantStore(grants_path), user, local))
    return window_for(rules, local.date(), hours)

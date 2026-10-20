"""One-off time grants the reconciler honors.

`timekpra --setallowedhours` is persistent: a "let him finish until 22:29"
tweak would stay for every Monday — and our hourly reconciler, which
re-applies policy.yaml, would revert it at its next pass inside allowed
hours anyway. Neither is what a parent means by "20 more minutes tonight".

A grant is a date-bound exception stored in the root state dir
(`grants.json`). `familymonctl enforce plan/apply` folds the active grants
into the desired state, so the reconciler can never undo them; they expire
on their own and are pruned on the next run. Grants only ever WIDEN the
window, so applying one passes the safety guard at any hour.

PlayTime (game-minute) grants need no record: timekpr's `--setplaytimeleft`
adjusts a counter the reconciler never touches. They are offered through the
same CLI for one obvious place to go, and logged as an Event.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

from familymon.policy.enforcement import HourWindow


def _hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")[:2]
    return time(int(hh), int(mm))


@dataclass(frozen=True)
class Grant:
    user: str
    day: str  # ISO date the grant applies to
    start: str  # "HH:MM" inclusive
    end: str  # "HH:MM" inclusive (the last allowed minute)
    created_at: str
    note: str = ""

    def is_active(self, now: datetime) -> bool:
        return self.day == now.date().isoformat() and now.time() <= _hhmm(self.end)

    def windows(self) -> tuple[HourWindow, ...]:
        """HourWindows covering start..end on the grant's day.

        22:00-22:29 -> 22[00-29]; 21:40-23:10 -> 21[40-59], 22, 23[00-10]."""
        s, e = _hhmm(self.start), _hhmm(self.end)
        if (e.hour, e.minute) < (s.hour, s.minute):
            raise ValueError(f"grant end {self.end} before start {self.start}")
        out = []
        for hour in range(s.hour, e.hour + 1):
            start_min = s.minute if hour == s.hour else 0
            end_min = e.minute if hour == e.hour else 59
            out.append(HourWindow(hour, start_min, end_min))
        return tuple(out)


class GrantStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> list[Grant]:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        out = []
        for g in raw.get("grants") or []:
            try:
                out.append(Grant(**g))
            except TypeError:
                continue
        return out

    def _save(self, grants: list[Grant]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.write_text(json.dumps({"grants": [asdict(g) for g in grants]}, indent=1))

    def add(self, user: str, start: time, end: time, *, now: datetime, note: str = "") -> Grant:
        if end < start:
            raise ValueError("grant end must not be before start")
        g = Grant(
            user=user,
            day=now.date().isoformat(),
            start=start.strftime("%H:%M"),
            end=end.strftime("%H:%M"),
            created_at=now.replace(microsecond=0).isoformat(),
            note=note,
        )
        grants = [x for x in self._load() if x.is_active(now)]
        grants.append(g)
        self._save(grants)
        return g

    def active(self, user: str, now: datetime) -> list[Grant]:
        """Live grants for `user` today. Also prunes expired/old ones."""
        grants = self._load()
        live = [g for g in grants if g.is_active(now)]
        if len(live) != len(grants):
            self._save(live)
        return [g for g in live if g.user == user]

    def revoke(self, user: str, now: datetime) -> int:
        grants = self._load()
        keep = [g for g in grants if not (g.user == user and g.day == now.date().isoformat())]
        self._save(keep)
        return len(grants) - len(keep)


def active_windows(store: GrantStore, user: str, now: datetime) -> tuple[HourWindow, ...]:
    """All HourWindows from the user's active grants, for apply_grants()."""
    out: list[HourWindow] = []
    for g in store.active(user, now):
        out.extend(g.windows())
    return tuple(out)


def until_today(end: time, *, now: datetime, start: time | None = None) -> tuple[time, time]:
    """Resolve `--until HH:MM` into a (start, end) pair: from `start` if given,
    else from now (rounded down to the minute) — so a grant made at 22:06 for
    22:29 covers 22:06-22:29, and one made at noon covers noon-22:29, which
    merges harmlessly with the policy window in between."""
    s = start or now.time().replace(second=0, microsecond=0)
    if end < s:
        raise ValueError(f"--until {end:%H:%M} is already past (now {now:%H:%M})")
    return s, end


__all__ = ["Grant", "GrantStore", "active_windows", "date", "until_today"]

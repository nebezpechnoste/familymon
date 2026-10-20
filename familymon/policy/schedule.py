"""Recurring weekly schedule — the school timetable familymon checks activity
against.

Blocks are weekly-recurring (day-of-week + local start/end time), stored in a
parent-editable YAML. Times are in the *kid's local* timezone so they line up
directly with capture timestamps (which are local). Conflict detection (a later
piece) asks `block_at(local_datetime)` whether a capture falls inside a block.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path

import yaml

_DAY_TO_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _parse_hhmm(value: str) -> time:
    hh, mm = value.strip().split(":")[:2]
    return time(int(hh), int(mm))


def _parse_date(value) -> date:
    # yaml.safe_load already turns an unquoted 2026-10-19 into a date.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


@dataclass(frozen=True)
class ScheduleBlock:
    day: str  # "Monday" … "Sunday"
    start: time
    end: time
    subject: str
    category: str = "school"
    # The school's own course/group code, e.g. "10L CYCLE TEST AC-26/27". The
    # LMS (Canvas) titles its pages with exactly this string and nothing else
    # identifiable, so it doubles as a title keyword for classifying school
    # pages — see `Schedule.title_keywords`.
    group_name: str | None = None
    # Pop a desktop notification in the kid's session when this block starts
    # (parent-added self-study / routine items: "Maths", "Workout", ...).
    notify: bool = False

    @property
    def day_index(self) -> int:
        return _DAY_TO_INDEX[self.day.strip().lower()]

    def covers(self, dt: datetime) -> bool:
        return dt.weekday() == self.day_index and self.start <= dt.time() < self.end

    def to_dict(self) -> dict:
        d = {
            "day": self.day,
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
            "subject": self.subject,
            "category": self.category,
        }
        if self.group_name:
            d["group_name"] = self.group_name
        if self.notify:
            d["notify"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ScheduleBlock:
        return cls(
            day=d["day"],
            start=_parse_hhmm(d["start"]),
            end=_parse_hhmm(d["end"]),
            subject=d.get("subject", ""),
            category=d.get("category", "school"),
            group_name=(d.get("group_name") or None),
            notify=bool(d.get("notify", False)),
        )


@dataclass(frozen=True)
class SchoolBreak:
    """A date range (inclusive, local dates) during which blocks of the listed
    categories are not in effect: half term, Christmas, a week off sick. The
    imported lessons stay in the file; `Schedule.block_at` / `starting_at`
    skip them, so web blocking, the lesson sweep, live warnings, reminders
    and the Conflicts panel all pause and resume by themselves. Default
    categories: just `school` — parent extras (self-study, routine) carry on
    unless the break lists their category too."""

    start: date
    end: date
    name: str = ""
    categories: tuple[str, ...] = ("school",)

    def covers(self, d: date) -> bool:
        return self.start <= d <= self.end

    def suspends(self, block: ScheduleBlock, d: date) -> bool:
        return block.category in self.categories and self.covers(d)

    @property
    def label(self) -> str:
        return f"{self.name or 'school break'} {self.start.isoformat()} to {self.end.isoformat()}"

    def to_dict(self) -> dict:
        d: dict = {"start": self.start.isoformat(), "end": self.end.isoformat()}
        if self.name:
            d["name"] = self.name
        if self.categories != ("school",):
            d["categories"] = list(self.categories)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SchoolBreak:
        if not isinstance(d, dict) or "start" not in d or "end" not in d:
            raise ValueError(f"break needs start and end dates: {d!r}")
        start, end = _parse_date(d["start"]), _parse_date(d["end"])
        if end < start:
            raise ValueError(f"break ends before it starts: {end} < {start}")
        raw = d.get("categories")
        if raw is None:
            categories: tuple[str, ...] = ("school",)
        else:
            if isinstance(raw, str):
                raw = [raw]
            categories = tuple(str(c).strip() for c in raw if str(c).strip())
            if not categories:
                raise ValueError(f"break lists no categories: {d!r}")
        return cls(
            start=start, end=end, name=str(d.get("name") or "").strip(), categories=categories
        )


@dataclass
class Schedule:
    blocks: list[ScheduleBlock] = field(default_factory=list)
    timezone: str | None = None
    breaks: list[SchoolBreak] = field(default_factory=list)

    def suspended(self, block: ScheduleBlock, when: datetime | date) -> SchoolBreak | None:
        """The break that takes `block` out of effect on that date, if any."""
        d = when.date() if isinstance(when, datetime) else when
        for br in self.breaks:
            if br.suspends(block, d):
                return br
        return None

    def break_on(self, when: datetime | date) -> SchoolBreak | None:
        """The break covering a date (whatever its categories), for display."""
        d = when.date() if isinstance(when, datetime) else when
        for br in self.breaks:
            if br.covers(d):
                return br
        return None

    def block_at(self, dt: datetime, category: str | None = None) -> ScheduleBlock | None:
        """The block covering a local datetime, or None. First match wins.
        With `category`, only blocks of that category count — conflict
        detection asks for "school" so self-study / routine extras never
        turn a game into a lesson-time conflict. Blocks suspended by a
        break (see `SchoolBreak`) never match."""
        for block in self.blocks:
            if (
                block.covers(dt)
                and (category is None or block.category == category)
                and self.suspended(block, dt) is None
            ):
                return block
        return None

    def starting_at(self, dt: datetime, *, tolerance_minutes: int = 2) -> list[ScheduleBlock]:
        """Blocks flagged `notify` whose start falls in [dt - tolerance, dt].
        The tolerance absorbs timer jitter; callers dedupe per day."""
        out = []
        for b in self.blocks:
            if not b.notify or b.day_index != dt.weekday() or self.suspended(b, dt):
                continue
            start = datetime.combine(dt.date(), b.start)
            delta = (dt - start).total_seconds()
            if 0 <= delta <= tolerance_minutes * 60:
                out.append(b)
        return out

    def title_keywords(self) -> list[str]:
        """Distinct group names, for recognising school pages by window title.

        Only group codes — they're distinctive ("10A FMAT EXP 1 AC-26/27").
        Subject names ("Economics", "Psychology") are deliberately NOT used:
        they'd relabel any page mentioning the word. Refreshes with each
        term's import, so no hardcoded year suffix anywhere.
        """
        seen: list[str] = []
        for b in self.blocks:
            if b.group_name and b.group_name not in seen:
                seen.append(b.group_name)
        return seen

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload: dict = {
            "timezone": self.timezone,
            "blocks": [b.to_dict() for b in self.blocks],
        }
        if self.breaks:
            payload["breaks"] = [br.to_dict() for br in self.breaks]
        path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
        # Root-owned, readable by the dashboard's group (storage/perms.py).
        from familymon.storage.perms import secure

        secure(path, shared=True)

    @classmethod
    def load(cls, path: Path) -> Schedule:
        raw = yaml.safe_load(path.read_text()) or {}
        blocks = [ScheduleBlock.from_dict(b) for b in raw.get("blocks") or []]
        breaks = [SchoolBreak.from_dict(b) for b in raw.get("breaks") or []]
        return cls(blocks=blocks, timezone=raw.get("timezone"), breaks=breaks)


# --------------------------------------------------------------------------
# Parent-maintained extras merged on import (schedule_extras.yaml):
#
#   exclude_subjects:            # imported lessons to drop from monitored time
#     - Discovery UK             #   (self-paced; the kid works on it whenever)
#   blocks:                      # custom items, any category; notify: true
#     - {day: Monday, start: "10:15", end: "11:15", subject: Maths,
#        category: study, notify: true}
#   breaks:                      # date ranges (inclusive) with no lessons
#     - {start: 2026-10-19, end: 2026-11-01, name: Half term}
#     - {start: 2026-12-21, end: 2027-01-03, name: Christmas,
#        categories: [school, study]}  # also pause the self-study reminders
# --------------------------------------------------------------------------


def load_extras(path: Path) -> tuple[list[str], list[ScheduleBlock], list[SchoolBreak]]:
    """(exclude_subjects, extra_blocks, breaks) from an extras YAML; empty if absent."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError:
        return [], [], []
    if not isinstance(raw, dict):
        raise ValueError(f"extras file is not a mapping: {path}")
    excludes = [str(x).strip() for x in raw.get("exclude_subjects") or [] if str(x).strip()]
    blocks = [ScheduleBlock.from_dict(b) for b in raw.get("blocks") or []]
    for b in blocks:
        if b.end <= b.start:
            raise ValueError(
                f"extra block ends before it starts: {b.subject} {b.day} {b.start}-{b.end}"
            )
        _ = b.day_index  # validates the day name
    breaks = [SchoolBreak.from_dict(b) for b in raw.get("breaks") or []]
    return excludes, blocks, breaks


def apply_extras(
    schedule: Schedule,
    exclude_subjects: list[str],
    extra_blocks: list[ScheduleBlock],
    breaks: list[SchoolBreak] | tuple[SchoolBreak, ...] = (),
) -> Schedule:
    """Drop excluded subjects (case-insensitive), append extras not already
    present, replace the breaks, keep sorted. Re-applying to an already
    merged schedule is a no-op, which is what `schedule refresh` relies on."""
    drop = {x.lower() for x in exclude_subjects}
    merged = [b for b in schedule.blocks if b.subject.strip().lower() not in drop]
    for b in extra_blocks:
        if b not in merged:
            merged.append(b)
    merged.sort(key=lambda b: (b.day_index, b.start))
    return Schedule(blocks=merged, timezone=schedule.timezone, breaks=list(breaks))

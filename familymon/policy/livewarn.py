"""Live lesson-conflict warnings in the kid's session.

Runs every minute from `familymonctl schedule notify-tick`: looks at the last
few minutes of classified captures and, if any conflicts with the lesson in
progress (same rules as the dashboard's Conflicts panel), pops a desktop
warning. This is the "react" half of lesson-time enforcement: the Chrome
policy block (policy/webblock.py) stops *new* navigation to distraction
sites, while this covers what it can't — a chat tab that was already open,
a native game, streaming video (flagged but deliberately not blocked).

Warnings repeat at most once per cooldown per (lesson, category) so a long
episode nags rather than spams. State is a small JSON file in the state dir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from familymon.analyze.categories import Category
from familymon.analyze.heuristics import WindowView
from familymon.policy.conflicts import conflicting_block, streaming_brand_in_title
from familymon.policy.schedule import Schedule, ScheduleBlock

LOOKBACK_MINUTES = 3
DEFAULT_COOLDOWN_MINUTES = 5

_LABELS = {
    Category.MESSAGING.value: "Messaging",
    Category.SOCIAL.value: "Social media",
    Category.GAMES.value: "A game",
    Category.VIDEO.value: "Streaming video",
}
# Categories the warning reacts to when they are what the active window is.
WARN_CATEGORIES = frozenset(_LABELS)


def active_window_category(classifier, wm_class: str | None, title: str | None) -> str | None:
    """What the active window is *on its own*.

    A capture's stored category is the priority verdict over every visible
    window, so a chat app in front of a still-visible school page comes out
    as `school` — right for the timeline, wrong for a warning about what the
    kid is actually doing. Re-run the heuristics on the active window alone."""
    if not (wm_class or title):
        return None
    result = classifier.classify(0, [WindowView(wm_class=wm_class, title=title, active=True)])
    cat = result.category
    return cat.value if hasattr(cat, "value") else (str(cat) if cat else None)


def warning_category(blended: str | None, active: str | None) -> str | None:
    """The category a live warning judges a capture by: the active window's
    own category when that is a distraction, else the capture's verdict."""
    return active if active in WARN_CATEGORIES else blended


@dataclass(frozen=True)
class LiveWarning:
    block: ScheduleBlock
    category: str
    seen_at: datetime
    detail: str | None = None  # streaming brand for video

    @property
    def key(self) -> str:
        return f"{self.block.start:%H:%M}|{self.block.subject}|{self.category}"

    def message(self) -> tuple[str, str]:
        label = _LABELS.get(self.category, self.category.capitalize())
        if self.detail:
            label = f"{label} ({self.detail})"
        summary = f"{self.block.subject} — lesson in progress"
        body = (
            f"{label} seen at {self.seen_at:%H:%M} during {self.block.subject} "
            f"(until {self.block.end:%H:%M}). This is logged."
        )
        return summary, body


def pending_warnings(
    schedule: Schedule,
    rows: list[tuple[datetime, str | None, str | None]],
    now: datetime,
    last_warned: dict[str, datetime],
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
) -> list[LiveWarning]:
    """Warnings due now for `rows` = (captured_at, category, title), any order.

    Only the lesson in progress *now* counts: a conflict from a lesson that
    just ended isn't worth interrupting the break for. One warning per
    (lesson, category), newest sighting wins, suppressed inside the cooldown.
    """
    current = schedule.block_at(now, category="school")
    if current is None:
        return []
    cooldown = timedelta(minutes=cooldown_minutes)
    found: dict[str, LiveWarning] = {}
    for when, category, title in sorted(rows, key=lambda r: r[0]):
        block = conflicting_block(schedule, when, category, title=title)
        if block is None or block != current or not category:
            continue
        detail = streaming_brand_in_title(title) if category == Category.VIDEO else None
        w = LiveWarning(block=block, category=category, seen_at=when, detail=detail)
        found[w.key] = w  # later rows overwrite: newest sighting
    due = []
    for w in found.values():
        last = last_warned.get(w.key)
        if last is not None and now - last < cooldown:
            continue
        due.append(w)
    return due


class WarnStore:
    """{key: last-warned ISO timestamp}; entries from other days are pruned."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, datetime] = {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    self._data[str(k)] = datetime.fromisoformat(str(v))
                except ValueError:
                    continue

    def last_warned(self, today) -> dict[str, datetime]:
        return {k: v for k, v in self._data.items() if v.date() == today}

    def mark(self, key: str, when: datetime) -> None:
        self._data = {k: v for k, v in self._data.items() if v.date() == when.date()}
        self._data[key] = when

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.write_text(
            json.dumps({k: v.isoformat(timespec="seconds") for k, v in self._data.items()})
        )


def recent_rows(
    sessions, since: datetime, classifier=None
) -> list[tuple[datetime, str | None, str | None]]:
    """(captured_at, category, title) for classified captures since `since`.

    With a `classifier`, the category is what the active window is on its own
    whenever that is a distraction (see `warning_category`)."""
    from sqlalchemy import select

    from familymon.storage.models import Capture, Classification

    with sessions() as s:
        rows = s.execute(
            select(
                Capture.captured_at,
                Classification.category,
                Capture.active_title,
                Capture.active_wm_class,
            )
            .join(Classification, Classification.capture_id == Capture.id)
            .where(Capture.captured_at >= since)
        ).all()
    out = []
    for ts, cat, title, wm_class in rows:
        if classifier is not None:
            cat = warning_category(cat, active_window_category(classifier, wm_class, title))
        out.append((ts, cat, title))
    return out

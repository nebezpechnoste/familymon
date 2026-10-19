"""Conflict detection: leisure/distraction activity during a scheduled lesson.

A conflict is a capture whose local time falls inside a school block AND whose
activity is a "distraction" during class.

Always-distraction categories default to games/social/messaging. `video` and
`browsing_other` are deliberately NOT always-distractions, because this is an
online school: the legitimate class runs in the browser (Adobe Connect /
Canvas), so blanket-flagging "browser/video during class" would false-positive
on actually attending.

`video` is special-cased: it conflicts during a lesson ONLY when we have a
confident signal that it's a *well-known entertainment streaming platform*
(YouTube, Netflix, …) — matched by brand substring on the window title. A
generic class stream (which classifies as school/browsing_other, or as video
without a recognized streaming brand) is left alone.

  PLANNED INVERSION — school-platform whitelist (task #22): once we maintain a
  list of legitimate class platforms (Adobe Connect & co.), flip the video rule
  to flag *any* video during a lesson EXCEPT whitelisted school platforms. The
  streaming-brand allowlist below is the interim, conservative step until that
  whitelist exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from familymon.analyze.categories import Category
from familymon.policy.schedule import Schedule, ScheduleBlock

DEFAULT_CONFLICT_CATEGORIES = frozenset({"games", "social", "messaging"})

# Entertainment streaming markers that make a `video`-classified tab a
# lesson-time conflict, matched as lowercase substrings of the window title. A
# browser tab reaches `video` from two surfaces, so we cover both:
#   - human brand forms in the title ("Some Show - Netflix"), and
#   - the site's domain in the title ("... max.com ..."), which is how the
#     domain table (config/domains_starter.yaml `video:`) classifies it.
# Every domain in that `video:` list is entertainment (the online class runs on
# canvas/meet/zoom, classified elsewhere), so each should conflict during a
# lesson — we add the domain forms whose brand form wouldn't match as a
# substring (max.com, disneyplus.com, ...). TikTok is handled as `social` (an
# always-conflict), not here. Bare ambiguous tokens (e.g. "max" without
# ".com") are omitted to avoid false positives. Interim until the
# school-platform whitelist (task #22) + real URL extraction land.
DEFAULT_STREAMING_BRANDS = frozenset(
    {
        # human brand forms (as they appear in window titles)
        "youtube",
        "netflix",
        "twitch",
        "vimeo",
        "disney+",
        "hbo max",
        "hulu",
        "prime video",
        "crunchyroll",
        "peacock",
        "paramount+",
        "dailymotion",
        "hotstar",
        # entertainment domain forms (title carried the domain, not the brand)
        "youtu.be",
        "max.com",
        "hbomax.com",
        "disneyplus.com",
        "primevideo.com",
    }
)


def streaming_brand_in_title(
    title: str | None, streaming_brands: frozenset[str] = DEFAULT_STREAMING_BRANDS
) -> str | None:
    """The streaming-platform brand found in the title, or None."""
    if not title:
        return None
    t = title.lower()
    for brand in streaming_brands:
        if brand in t:
            return brand
    return None


def is_distraction(
    category: str | None,
    title: str | None,
    conflict_categories: frozenset[str],
    streaming_brands: frozenset[str],
) -> bool:
    """Whether this activity counts as a lesson-time distraction.

    Always-distraction categories qualify outright. `video` qualifies only when
    the title matches a recognized entertainment streaming platform.
    """
    if not category:
        return False
    if category in conflict_categories:
        return True
    if category == Category.VIDEO:
        return streaming_brand_in_title(title, streaming_brands) is not None
    return False


def conflicting_block(
    schedule: Schedule,
    when: datetime,
    category: str | None,
    conflict_categories: frozenset[str] = DEFAULT_CONFLICT_CATEGORIES,
    *,
    title: str | None = None,
    streaming_brands: frozenset[str] = DEFAULT_STREAMING_BRANDS,
) -> ScheduleBlock | None:
    """The school block this activity conflicts with, or None.

    A conflict requires both: the time falls in a school block, and the activity
    is a distraction (see `is_distraction`).
    """
    if not is_distraction(category, title, conflict_categories, streaming_brands):
        return None
    # Only real lessons count. Parent-added extras (self-study, workout) are
    # notification items, not monitored time.
    return schedule.block_at(when, category="school")


@dataclass
class Conflict:
    block_subject: str
    category: str
    start: datetime
    end: datetime
    count: int
    detail: str | None = None  # e.g. the matched streaming platform for video

    def seconds(self, default_interval: float = 60.0) -> float:
        # Same cadence-derived estimate the dashboard uses: N captures with mean
        # inter-capture gap -> N * mean = (end - start) + one tick. Single
        # capture falls back to the configured interval.
        if self.count <= 1:
            return float(default_interval)
        span = (self.end - self.start).total_seconds()
        return span * self.count / (self.count - 1)


def find_conflicts(
    schedule: Schedule,
    captures: Iterable[tuple[datetime, str | None] | tuple[datetime, str | None, str | None]],
    conflict_categories: frozenset[str] = DEFAULT_CONFLICT_CATEGORIES,
    streaming_brands: frozenset[str] = DEFAULT_STREAMING_BRANDS,
) -> list[Conflict]:
    """Group time-ordered captures into conflict episodes.

    Each capture is `(timestamp, category)` or `(timestamp, category, title)`.
    The title lets the video rule recognize a streaming platform; it's optional
    for callers that only deal in always-distraction categories.

    Contiguous conflicting captures with the same (subject, category) merge into
    one episode; any non-conflicting capture closes the current episode.
    """
    episodes: list[Conflict] = []
    cur: Conflict | None = None
    for when, category, *rest in captures:
        title = rest[0] if rest else None
        block = conflicting_block(
            schedule,
            when,
            category,
            conflict_categories,
            title=title,
            streaming_brands=streaming_brands,
        )
        if block is not None:
            if cur and cur.category == category and cur.block_subject == block.subject:
                cur.end = when
                cur.count += 1
            else:
                if cur is not None:
                    episodes.append(cur)
                detail = (
                    streaming_brand_in_title(title, streaming_brands)
                    if category == Category.VIDEO
                    else None
                )
                cur = Conflict(
                    block_subject=block.subject,
                    category=category or "",
                    start=when,
                    end=when,
                    count=1,
                    detail=detail,
                )
        elif cur is not None:
            episodes.append(cur)
            cur = None
    if cur is not None:
        episodes.append(cur)
    return episodes

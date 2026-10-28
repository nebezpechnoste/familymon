"""A day's activity as the parent sees it: masked captures, timeline blocks,
per-category totals and lesson-time conflicts.

Shared by the dashboard (HTML and JSON routes), `familymonctl` and the MCP
server, so every surface shows the same day the same way. In particular the
personal-time masking happens here, once: a surface that wants the day's
captures goes through `load_captures` and never reads the table directly.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from familymon.storage.models import Capture, Classification


def day_bounds(target: date) -> tuple[datetime, datetime]:
    # Local-naive, matching how the capture loop stores timestamps (SQLite
    # strips tz offsets on write — see health.py). Labeling these bounds UTC
    # made the day window drift from the local day and only worked by accident
    # of ISO-string comparison; tests seeded via now(UTC) then failed between
    # 00:00 and 02:00 CEST when the UTC date is still "yesterday".
    start = datetime.combine(target, time.min)
    return start, start + timedelta(days=1)


def load_captures(sessions: sessionmaker, target: date) -> list[dict[str, Any]]:
    start, end = day_bounds(target)
    with sessions() as session:
        rows = session.execute(
            select(Capture, Classification)
            .outerjoin(Classification, Classification.capture_id == Capture.id)
            .where(Capture.captured_at >= start, Capture.captured_at < end)
            .order_by(Capture.captured_at)
        ).all()

    out: list[dict[str, Any]] = []
    for cap, cls in rows:
        # Personal time (policy `personal_time`): the category stays, so game
        # time is still accounted for; the app, the title, the evidence (which
        # quotes the title) and the skip reason go. The parent chose not to
        # see what exactly was watched. Applies to every surface alike.
        hidden = bool(cap.personal_time)
        out.append(
            {
                "id": cap.id,
                "captured_at": cap.captured_at.isoformat(),
                "active_wm_class": None if hidden else cap.active_wm_class,
                "active_title": None if hidden else cap.active_title,
                "same_as_prev": cap.same_as_prev,
                "screenshot_skipped_reason": None if hidden else cap.screenshot_skipped_reason,
                "screenshot_on_disk": cap.screenshot_path is not None,
                "category": cls.category if cls else None,
                "confidence": cls.confidence if cls else None,
                "source": cls.source if cls else None,
                "evidence": None if hidden else (cls.evidence_text if cls else None),
                "present_categories": (cls.present_categories if cls else None) or [],
                "personal_time": hidden,
            }
        )
    return out


def block_duration_seconds(block: dict[str, Any], default_interval: float) -> float:
    """Compute a block's elapsed seconds from its own timestamps.

    For a block of N captures, the mean inter-capture interval is
    (end - start) / (N - 1). Total duration is N × that mean — which equals
    (end - start) + one tick at the end, treating each capture as the start of
    one tick of activity. Single-capture blocks fall back to the configured
    interval since we have no in-block evidence of the actual cadence.

    This avoids the bug where the dashboard assumes 60 s/capture regardless of
    what FAMILYMON_INTERVAL the capture run actually used.
    """
    count = block["count"]
    if count <= 1:
        return float(default_interval)
    start = datetime.fromisoformat(block["start"])
    end = datetime.fromisoformat(block["end"])
    span = (end - start).total_seconds()
    avg_interval = span / (count - 1)
    return count * avg_interval


def build_blocks(
    captures: list[dict[str, Any]],
    default_interval: float,
) -> list[dict[str, Any]]:
    """Collapse contiguous (category, wm_class) runs into single timeline blocks."""
    from familymon.policy.conflicts import streaming_brand_in_title

    blocks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for c in captures:
        cat = c["category"] or "unknown"
        # A streaming brand seen on *any* capture in the block — the first title
        # alone would miss a class-stream → YouTube switch within one browser
        # window (same category+wm_class, so the captures merge).
        brand = streaming_brand_in_title(c["active_title"])
        personal = bool(c.get("personal_time"))
        if (
            cur
            and cur["category"] == cat
            and cur["wm_class"] == c["active_wm_class"]
            and cur["personal_time"] == personal
        ):
            cur["count"] += 1
            cur["end"] = c["captured_at"]
            cur["present"].update(c["present_categories"])
            cur["stream_brand"] = cur["stream_brand"] or brand
        else:
            if cur is not None:
                blocks.append(cur)
            cur = {
                "category": cat,
                "wm_class": c["active_wm_class"],
                "title": c["active_title"],
                "skipped_reason": c["screenshot_skipped_reason"],
                "start": c["captured_at"],
                "end": c["captured_at"],
                "count": 1,
                "present": set(c["present_categories"]),
                "stream_brand": brand,
                "personal_time": personal,
            }
    if cur is not None:
        blocks.append(cur)
    for b in blocks:
        b["duration_seconds"] = block_duration_seconds(b, default_interval)
        # "Also present" = other categories seen on screen during the block,
        # besides the primary. Surfaces the multi-window reality at a glance.
        b["also"] = sorted(b.pop("present") - {b["category"]})
    return blocks


def personal_time_spans(blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Runs of consecutive personal-time blocks, for the note above the
    timeline: [{start, end}] as ISO timestamps."""
    spans: list[dict[str, str]] = []
    previous = False
    for b in blocks:
        if b.get("personal_time"):
            if previous:
                spans[-1]["end"] = b["end"]
            else:
                spans.append({"start": b["start"], "end": b["end"]})
        previous = bool(b.get("personal_time"))
    return spans


def compute_stats_from_blocks(
    captures: list[dict[str, Any]],
    blocks: list[dict[str, Any]],
) -> dict[str, Any]:
    by_cat: dict[str, float] = defaultdict(float)
    for b in blocks:
        by_cat[b["category"]] += b["duration_seconds"]
    return {
        "total_captures": len(captures),
        "seconds_per_category": dict(by_cat),
    }


def school_break_label(target: date) -> str | None:
    """'Half term 2026-10-19 to 2026-11-01' when the day falls in a schedule
    break (lessons out of effect, so no conflicts by design), else None.
    No-op without an imported schedule."""
    from familymon.config import schedule_path
    from familymon.policy.schedule import Schedule

    path = schedule_path()
    if not path.exists():
        return None
    br = Schedule.load(path).break_on(target)
    return br.label if br is not None else None


def annotate_conflicts(
    blocks: list[dict[str, Any]],
    captures: list[dict[str, Any]],
    default_interval: float,
) -> list[dict[str, Any]]:
    """Compute lesson-time conflicts and return them for the summary panel.

    Uses the SAME per-capture `find_conflicts` the CLI uses, so the dashboard
    and `familymonctl conflicts` never disagree — an earlier version tested only
    each block's start timestamp, which mis-handled blocks straddling a lesson
    boundary. Each timeline block that overlaps a conflict episode of its
    category is tagged with `conflict_subject` for the red row. No-op if no
    schedule has been imported."""
    from familymon.config import schedule_path
    from familymon.policy.conflicts import find_conflicts
    from familymon.policy.schedule import Schedule

    path = schedule_path()
    if not path.exists():
        return []
    schedule = Schedule.load(path)

    episodes = find_conflicts(
        schedule,
        [
            (datetime.fromisoformat(c["captured_at"]), c["category"], c["active_title"])
            for c in captures
        ],
    )
    if not episodes:
        return []

    for b in blocks:
        b_start = datetime.fromisoformat(b["start"])
        b_end = datetime.fromisoformat(b["end"])
        for ep in episodes:
            if ep.category == b["category"] and ep.start <= b_end and ep.end >= b_start:
                b["conflict_subject"] = ep.block_subject
                break

    return [
        {
            "start": ep.start.isoformat(),
            "end": ep.end.isoformat(),
            "category": ep.category,
            "conflict_subject": ep.block_subject,
            "duration_seconds": ep.seconds(default_interval),
            "detail": ep.detail,
        }
        for ep in episodes
    ]


def day_summary(sessions: sessionmaker, target: date, default_interval: float) -> dict[str, Any]:
    """Everything the timeline page shows for one day, as data: masked blocks,
    totals per category, conflicts, personal-time spans, the school break,
    the watcher's verdict and the day's capture problems."""
    from familymon.heartbeat import capture_problems, check_capture_freshness, latest_heartbeat

    captures = load_captures(sessions, target)
    blocks = build_blocks(captures, default_interval)
    stats = compute_stats_from_blocks(captures, blocks)
    conflicts = annotate_conflicts(blocks, captures, default_interval)
    # The root watcher's verdict knows whether anyone was logged in; without
    # one (dev runs, no timer) fall back to plain capture freshness.
    heartbeat = latest_heartbeat(sessions) or check_capture_freshness(sessions)
    return {
        "date": target.isoformat(),
        "blocks": blocks,
        "total_captures": stats["total_captures"],
        "seconds_per_category": stats["seconds_per_category"],
        "conflicts": conflicts,
        "personal_time_spans": personal_time_spans(blocks),
        "school_break": school_break_label(target),
        "heartbeat": {
            "status": heartbeat.status,
            "session": heartbeat.session,
            "age_seconds": heartbeat.age_seconds,
            "last_capture_at": (
                heartbeat.last_capture_at.isoformat() if heartbeat.last_capture_at else None
            ),
            "checked_at": heartbeat.checked_at.isoformat(),
        },
        "capture_problems": capture_problems(sessions, *day_bounds(target)),
    }

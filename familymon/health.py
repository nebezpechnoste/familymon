"""Data-health report for the running capture pipeline.

Run at the start of each dev iteration (`familymonctl health`) to catch
regressions before shipping new code: capture stalls, classification gaps,
dedup drift, runaway disk usage, and — most importantly — any privacy-skip
violation (a messaging-app capture that nonetheless persisted a screenshot).

Timestamps are stored local-naive (SQLite strips the tz offset on write), so
all windows here use `datetime.now()` (local-naive) to compare apples to
apples.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from familymon.storage.models import Capture, Classification, Event


@dataclass
class HealthReport:
    now: datetime
    total_captures: int
    captures_last_1h: int
    captures_last_24h: int
    last_capture_age_seconds: float | None
    largest_gap_seconds_24h: float | None
    classified_pct_24h: float
    category_counts_24h: dict[str, int] = field(default_factory=dict)
    dedup_pct_24h: float = 0.0
    # Captures where pHash dedup actually had a chance to run (the backend
    # produced bytes, i.e. screenshot_skipped_reason is NULL). Idle/locked and
    # messaging-skipped ticks are excluded — those never reach the pHash
    # compare, so they don't belong in the denominator.
    dedup_eligible_24h: int = 0
    # Captures in the last 24h where a messaging window forced a FULL screenshot
    # skip (reason "messaging_app"): redaction couldn't map a chat window safely,
    # so the whole frame was dropped for privacy. Surfaced so the redact-or-skip
    # rule can't silently open large screenshot gaps without us noticing. Note
    # this counts only full skips — the common case (chat window redacted, rest
    # of the frame kept) is NOT here and loses no monitoring.
    messaging_skips_24h: int = 0
    screenshots_on_disk: int = 0
    screenshot_disk_bytes: int = 0
    privacy_violations: int = 0
    # SQLite `PRAGMA quick_check` result: "ok", or the first error lines.
    # Unclean shutdowns (a flaky PSU on the deployed box) can leave the file
    # subtly damaged — captures may still insert while another table's pages
    # are broken, so the pipeline fails silently downstream.
    db_integrity: str = "ok"
    # Most recent automatic recovery (storage/recovery.py), if any, as a
    # one-line summary — the parent should know a crash cost data.
    last_db_recovery: str | None = None


def _screenshot_dir_usage(path: Path) -> tuple[int, int]:
    count = 0
    total = 0
    if path.is_dir():
        for p in path.rglob("*.png"):
            try:
                total += p.stat().st_size
                count += 1
            except OSError:
                continue
    return count, total


def _db_integrity(sessions: sessionmaker) -> str:
    """SQLite quick_check as a one-line string: "ok" or the error summary.
    A badly damaged file can make even the PRAGMA raise; report that too."""
    try:
        with sessions() as s:
            rows = s.execute(text("PRAGMA quick_check(5)")).scalars().all()
    except Exception as e:  # noqa: BLE001 — any DB-level failure IS the finding
        return f"{type(e).__name__}: {e}".splitlines()[0][:300]
    if rows == ["ok"]:
        return "ok"
    return "; ".join(str(r) for r in rows) or "unknown"


def compute_health(sessions: sessionmaker, screenshots_dir: Path) -> HealthReport:
    now = datetime.now()

    # Integrity FIRST: on a damaged file the counting queries below may raise
    # before we'd ever get to report the damage. Short-circuit with the
    # finding instead of crashing the health check.
    db_integrity = _db_integrity(sessions)
    if db_integrity != "ok":
        shot_count, shot_bytes = _screenshot_dir_usage(screenshots_dir)
        return HealthReport(
            now=now,
            total_captures=0,
            captures_last_1h=0,
            captures_last_24h=0,
            last_capture_age_seconds=None,
            largest_gap_seconds_24h=None,
            classified_pct_24h=0.0,
            screenshots_on_disk=shot_count,
            screenshot_disk_bytes=shot_bytes,
            db_integrity=db_integrity,
        )
    h1 = now - timedelta(hours=1)
    h24 = now - timedelta(hours=24)

    with sessions() as s:
        total = s.scalar(select(func.count()).select_from(Capture)) or 0
        last_1h = (
            s.scalar(
                select(func.count()).select_from(Capture).where(Capture.captured_at >= h1)
            )
            or 0
        )
        last_24h = (
            s.scalar(
                select(func.count())
                .select_from(Capture)
                .where(Capture.captured_at >= h24)
            )
            or 0
        )
        last_ts = s.scalar(select(func.max(Capture.captured_at)))

        timestamps = list(
            s.scalars(
                select(Capture.captured_at)
                .where(Capture.captured_at >= h24)
                .order_by(Capture.captured_at)
            )
        )
        largest_gap = None
        if len(timestamps) >= 2:
            largest_gap = max(
                (timestamps[i + 1] - timestamps[i]).total_seconds()
                for i in range(len(timestamps) - 1)
            )

        classified_24h = (
            s.scalar(
                select(func.count())
                .select_from(Capture)
                .join(Classification, Classification.capture_id == Capture.id)
                .where(Capture.captured_at >= h24)
            )
            or 0
        )
        classified_pct = (classified_24h / last_24h * 100.0) if last_24h else 0.0

        cat_rows = s.execute(
            select(Classification.category, func.count())
            .join(Capture, Capture.id == Classification.capture_id)
            .where(Capture.captured_at >= h24)
            .group_by(Classification.category)
        ).all()
        category_counts = {cat: n for cat, n in cat_rows}

        # pHash dedup is only meaningful for ticks that actually produced
        # screenshot bytes. Session idle/locked ticks skip the backend snapshot
        # entirely, and messaging ticks that couldn't be redacted safely are
        # full-skipped (reason "messaging_app"); both set
        # screenshot_skipped_reason and are excluded below. A *redacted*
        # messaging tick still produces (blacked-out) bytes, so it runs through
        # pHash and correctly counts as eligible. Excluding the skipped ticks
        # keeps an all-idle day from reporting a misleading 0% as if dedup were
        # broken, when really there was nothing to dedup.
        dedup_eligible_24h = (
            s.scalar(
                select(func.count())
                .select_from(Capture)
                .where(
                    Capture.captured_at >= h24,
                    Capture.screenshot_skipped_reason.is_(None),
                )
            )
            or 0
        )
        dup_24h = (
            s.scalar(
                select(func.count())
                .select_from(Capture)
                .where(
                    Capture.captured_at >= h24,
                    Capture.screenshot_skipped_reason.is_(None),
                    Capture.same_as_prev.is_(True),
                )
            )
            or 0
        )
        dedup_pct = (
            (dup_24h / dedup_eligible_24h * 100.0) if dedup_eligible_24h else 0.0
        )

        messaging_skips_24h = (
            s.scalar(
                select(func.count())
                .select_from(Capture)
                .where(
                    Capture.captured_at >= h24,
                    Capture.screenshot_skipped_reason == "messaging_app",
                )
            )
            or 0
        )

        # A messaging window was visible AND a screenshot was persisted AND it
        # was NOT redacted — i.e. unredacted chat pixels reached disk. By
        # construction this should be impossible (redact, or full-skip); the
        # check guards against a regression in that logic.
        privacy_violations = (
            s.scalar(
                select(func.count())
                .select_from(Capture)
                .where(
                    Capture.messaging_present.is_(True),
                    Capture.screenshot_path.is_not(None),
                    Capture.screenshot_redacted.is_(False),
                )
            )
            or 0
        )

    with sessions() as s:
        ev = s.execute(
            select(Event).where(Event.kind == "db_recovered").order_by(Event.at.desc()).limit(1)
        ).scalar_one_or_none()
    last_db_recovery = None
    if ev is not None:
        d = ev.data or {}
        last_db_recovery = (
            f"{ev.at:%Y-%m-%d %H:%M} via {d.get('method')} — damaged file kept at "
            f"{d.get('damaged_backup')}; salvaged {d.get('salvaged_tables')}"
        )

    shot_count, shot_bytes = _screenshot_dir_usage(screenshots_dir)
    last_age = (now - last_ts).total_seconds() if last_ts else None

    return HealthReport(
        now=now,
        total_captures=total,
        captures_last_1h=last_1h,
        captures_last_24h=last_24h,
        last_capture_age_seconds=last_age,
        largest_gap_seconds_24h=largest_gap,
        classified_pct_24h=classified_pct,
        category_counts_24h=category_counts,
        dedup_pct_24h=dedup_pct,
        dedup_eligible_24h=dedup_eligible_24h,
        messaging_skips_24h=messaging_skips_24h,
        screenshots_on_disk=shot_count,
        screenshot_disk_bytes=shot_bytes,
        privacy_violations=privacy_violations,
        db_integrity=db_integrity,
        last_db_recovery=last_db_recovery,
    )

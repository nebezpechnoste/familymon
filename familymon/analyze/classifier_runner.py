"""Drains the queue of unclassified Capture rows through HeuristicClassifier.

Phase 1: heuristic-only. Phase 3 will fork the queue — heuristic-confident rows
get written immediately; ambiguous ones (UNKNOWN, low confidence) go to a
secondary queue for the local VLM and/or Claude Code bootstrap labeling.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from familymon.analyze.categories import Category
from familymon.analyze.heuristics import HeuristicClassifier, WindowView
from familymon.storage.models import Capture as CaptureRow
from familymon.storage.models import Classification as ClassificationRow

logger = logging.getLogger(__name__)


def classify_unclassified(
    classifier: HeuristicClassifier,
    session_factory: sessionmaker,
    *,
    limit: int | None = None,
) -> int:
    """Write a Classification row for every Capture that doesn't have one.

    Returns the number of rows classified."""
    classified = 0
    with session_factory() as session:
        stmt = select(CaptureRow).where(~CaptureRow.classification.has())
        if limit is not None:
            stmt = stmt.limit(limit)
        for capture in list(session.scalars(stmt)):
            # One bad row must never block the queue: without this, a single
            # capture that trips the classifier aborts the run, the transaction
            # rolls back, and every subsequent timer run dies on the same row —
            # the dashboard then shows everything after it as "unknown". Record
            # the failure as an UNKNOWN classification so the row leaves the
            # queue and the error is visible in the evidence text.
            try:
                result = classifier.classify(
                    capture.id,
                    _window_views(capture),
                    suppressed_reason=capture.screenshot_skipped_reason,
                    game_running=bool(capture.game_running),
                )
                row = ClassificationRow(
                    capture_id=capture.id,
                    category=result.category.value,
                    confidence=result.confidence,
                    evidence_text=result.evidence_text,
                    source=result.source,
                    schema_version=result.schema_version,
                    present_categories=[c.value for c in result.present_categories],
                    raw_response=None,
                    # Local-naive, matching captured_at and the health/dashboard
                    # comparison windows (SQLite strips tz on write, so a UTC
                    # value would silently be off by the local offset).
                    created_at=datetime.now(),
                )
            except Exception as e:
                logger.exception("classifier failed on capture id=%s", capture.id)
                row = ClassificationRow(
                    capture_id=capture.id,
                    category=Category.UNKNOWN.value,
                    confidence=0.0,
                    evidence_text=f"classifier error: {type(e).__name__}: {e}"[:1024],
                    source="heuristic",
                    present_categories=[Category.UNKNOWN.value],
                    raw_response=None,
                    created_at=datetime.now(),
                )
            session.add(row)
            classified += 1
        session.commit()
    return classified


# Sources the heuristic layer writes. VLM / Claude Code bootstrap labels use
# other source values and are never discarded by a heuristic re-run.
_HEURISTIC_SOURCES = ("heuristic", "domain", "game_process")


def reclassify_heuristic(classifier: HeuristicClassifier, session_factory: sessionmaker) -> int:
    """Drop every heuristic-sourced Classification and re-run the heuristics.

    For when the heuristic *policy* changes (e.g. focused-window-first) and
    existing history should reflect it. Heuristics are deterministic and
    cheap, so this is safe to run over the whole table. Returns rows redone."""
    with session_factory() as session:
        session.execute(
            delete(ClassificationRow).where(ClassificationRow.source.in_(_HEURISTIC_SOURCES))
        )
        session.commit()
    return classify_unclassified(classifier, session_factory)


def _window_views(capture: CaptureRow) -> list[WindowView]:
    windows = [
        WindowView(
            wm_class=capture.active_wm_class,
            title=capture.active_title,
            active=True,
            minimized=False,
        )
    ]
    for w in capture.windows or []:
        if not isinstance(w, dict):
            raise TypeError(f"window entry is not a mapping: {w!r}")
        windows.append(
            WindowView(
                wm_class=w.get("wm_class"),
                title=w.get("title"),
                active=bool(w.get("active")),
                minimized=bool(w.get("minimized")),
                # Rows from before this field existed count as on screen.
                on_current_desktop=bool(w.get("on_current_desktop", True)),
            )
        )
    return windows

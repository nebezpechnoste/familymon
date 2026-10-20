"""The classify queue must survive a poison row.

Observed on the deployed box: one capture tripped the classifier, the whole
run's transaction rolled back, and every 5-minute timer run died on the same
row — 14 hours of captures showed as "unknown" on the dashboard.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from familymon.analyze.categories import Category
from familymon.analyze.classifier_runner import classify_unclassified
from familymon.analyze.schema import Classification
from familymon.storage.models import Base, Capture
from familymon.storage.models import Classification as ClassificationRow


@pytest.fixture
def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(sessions, wm_class, windows=None) -> int:
    with sessions() as s:
        c = Capture(
            captured_at=datetime.now(),
            active_wm_class=wm_class,
            active_title="t",
            windows=windows if windows is not None else [],
        )
        s.add(c)
        s.commit()
        return c.id


class _ExplodingOn:
    """Classifier stub: raises for one wm_class, classifies the rest."""

    def __init__(self, bad_wm_class: str) -> None:
        self.bad = bad_wm_class

    def classify(self, capture_id, windows, *, suppressed_reason=None, game_running=False):
        if windows[0].wm_class == self.bad:
            raise RuntimeError("boom")
        return Classification(
            capture_id=capture_id,
            category=Category.BROWSING_OTHER,
            confidence=0.5,
            evidence_text="ok",
            source="heuristic",
            present_categories=[Category.BROWSING_OTHER],
        )


def test_poison_row_is_recorded_and_queue_drains(sessions):
    good1 = _seed(sessions, "firefox")
    bad = _seed(sessions, "poison")
    good2 = _seed(sessions, "firefox")

    n = classify_unclassified(_ExplodingOn("poison"), sessions)
    assert n == 3

    with sessions() as s:
        rows = {r.capture_id: r for r in s.scalars(select(ClassificationRow))}
    assert rows[good1].category == "browsing_other"
    assert rows[good2].category == "browsing_other"
    # The bad row leaves the queue as UNKNOWN with the error visible.
    assert rows[bad].category == "unknown"
    assert "classifier error: RuntimeError: boom" in rows[bad].evidence_text

    # Second run: nothing left to do — the poison row doesn't come back.
    assert classify_unclassified(_ExplodingOn("poison"), sessions) == 0


def test_reclassify_heuristic_redoes_heuristic_rows_only(sessions):
    """--redo drops heuristic-sourced rows and re-runs; a bootstrap/VLM label
    (different source) must survive untouched."""
    from familymon.analyze.classifier_runner import reclassify_heuristic

    a = _seed(sessions, "firefox")
    b = _seed(sessions, "firefox")
    classify_unclassified(_ExplodingOn("never"), sessions)
    with sessions() as s:
        keep = s.scalar(select(ClassificationRow).where(ClassificationRow.capture_id == b))
        keep.source = "claude_code_bootstrap"
        keep.category = "school"
        s.commit()

    n = reclassify_heuristic(_ExplodingOn("never"), sessions)
    assert n == 1  # only the heuristic row was redone
    with sessions() as s:
        rows = {r.capture_id: r for r in s.scalars(select(ClassificationRow))}
    assert rows[a].source == "heuristic" and rows[a].category == "browsing_other"
    assert rows[b].source == "claude_code_bootstrap" and rows[b].category == "school"


def test_malformed_windows_json_is_isolated(sessions):
    """A capture whose windows list isn't a list of mappings (corrupt row)
    must not take the run down with it."""
    bad = _seed(sessions, "firefox", windows=["not-a-dict"])
    good = _seed(sessions, "firefox")

    n = classify_unclassified(_ExplodingOn("never"), sessions)
    assert n == 2
    with sessions() as s:
        rows = {r.capture_id: r for r in s.scalars(select(ClassificationRow))}
    assert rows[good].category == "browsing_other"
    assert rows[bad].category == "unknown"
    assert "TypeError" in rows[bad].evidence_text


def test_window_views_carry_desktop_membership_and_keep_old_rows_visible():
    from familymon.analyze.classifier_runner import _window_views

    row = Capture(
        captured_at=datetime.now(),
        active_wm_class="firefox",
        active_title="t",
        windows=[
            {"wm_class": "telegram-desktop", "title": "chat", "on_current_desktop": False},
            {"wm_class": "minetest", "title": "row from before the field existed"},
        ],
    )
    assert [v.hidden for v in _window_views(row)] == [False, True, False]

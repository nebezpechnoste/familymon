from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from familymon.health import compute_health
from familymon.storage.models import Base, Capture, Classification


@pytest.fixture
def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _add(
    sessions: sessionmaker,
    when: datetime,
    *,
    wm_class: str = "firefox_firefox",
    same_as_prev: bool = False,
    skipped_reason: str | None = None,
    screenshot_path: str | None = None,
    messaging_present: bool = False,
    screenshot_redacted: bool = False,
    category: str | None = None,
) -> None:
    with sessions() as s:
        cap = Capture(
            captured_at=when,
            active_wm_class=wm_class,
            active_title="t",
            windows=[],
            same_as_prev=same_as_prev,
            screenshot_skipped_reason=skipped_reason,
            screenshot_path=screenshot_path,
            messaging_present=messaging_present,
            screenshot_redacted=screenshot_redacted,
        )
        s.add(cap)
        s.flush()
        if category:
            s.add(
                Classification(
                    capture_id=cap.id,
                    category=category,
                    confidence=0.8,
                    source="heuristic",
                    evidence_text="x",
                    created_at=when,
                )
            )
        s.commit()


def test_counts_and_recency(sessions: sessionmaker, tmp_path: Path):
    now = datetime.now()
    _add(sessions, now - timedelta(minutes=5), category="browsing_other")
    _add(sessions, now - timedelta(minutes=2), category="browsing_other", same_as_prev=True)
    _add(sessions, now - timedelta(hours=10), category="games")

    r = compute_health(sessions, tmp_path)
    assert r.total_captures == 3
    assert r.captures_last_1h == 2
    assert r.captures_last_24h == 3
    assert r.last_capture_age_seconds is not None
    assert r.last_capture_age_seconds < 180
    assert r.dedup_eligible_24h == 3  # none were skipped, so all eligible
    assert r.dedup_pct_24h == pytest.approx(100 / 3, abs=0.5)
    assert r.classified_pct_24h == 100.0
    assert r.category_counts_24h == {"browsing_other": 2, "games": 1}


def test_dedup_excludes_session_idle_and_messaging_skipped(sessions: sessionmaker, tmp_path: Path):
    """Idle/locked and messaging-foreground ticks never run pHash, so they must
    not appear in the dedup denominator. Otherwise an all-idle day reports a
    misleading 0% as if dedup were broken."""
    now = datetime.now()
    # 1 eligible duplicate, 1 eligible novel, 2 ineligible (idle + msg-skip).
    _add(sessions, now - timedelta(minutes=4), same_as_prev=False)  # eligible, novel
    _add(sessions, now - timedelta(minutes=3), same_as_prev=True)  # eligible, dup
    _add(sessions, now - timedelta(minutes=2), skipped_reason="session_locked")
    _add(sessions, now - timedelta(minutes=1), skipped_reason="messaging_app")

    r = compute_health(sessions, tmp_path)
    assert r.dedup_eligible_24h == 2
    assert r.dedup_pct_24h == pytest.approx(50.0)


def test_dedup_reports_zero_eligible_when_all_idle(sessions: sessionmaker, tmp_path: Path):
    """All-idle 24h: dedup_eligible_24h == 0 so the CLI can say 'n/a' instead
    of a misleading 0% — that was the bug observed on the dogfood box after the
    laptop sat locked for a few days."""
    now = datetime.now()
    for i in range(5):
        _add(
            sessions,
            now - timedelta(minutes=i + 1),
            skipped_reason="session_locked",
        )
    r = compute_health(sessions, tmp_path)
    assert r.dedup_eligible_24h == 0
    assert r.dedup_pct_24h == 0.0  # the pct is meaningless here; CLI hides it


def test_detects_gap(sessions: sessionmaker, tmp_path: Path):
    now = datetime.now()
    _add(sessions, now - timedelta(minutes=30))
    _add(sessions, now - timedelta(minutes=2))  # 28-min gap
    r = compute_health(sessions, tmp_path)
    assert r.largest_gap_seconds_24h is not None
    assert r.largest_gap_seconds_24h > 1500


def test_privacy_violation_flagged(sessions: sessionmaker, tmp_path: Path):
    now = datetime.now()
    # Messaging window present, screenshot persisted, NOT redacted = violation.
    _add(
        sessions,
        now,
        wm_class="telegram-desktop_telegram-desktop",
        messaging_present=True,
        screenshot_path="/somewhere/leak.png",
        screenshot_redacted=False,
    )
    r = compute_health(sessions, tmp_path)
    assert r.privacy_violations == 1


def test_redacted_messaging_screenshot_is_not_a_violation(sessions: sessionmaker, tmp_path: Path):
    now = datetime.now()
    _add(
        sessions,
        now,
        wm_class="telegram-desktop_telegram-desktop",
        messaging_present=True,
        screenshot_path="/somewhere/redacted.png",
        screenshot_redacted=True,
    )
    r = compute_health(sessions, tmp_path)
    assert r.privacy_violations == 0


def test_full_skip_messaging_is_not_a_violation(sessions: sessionmaker, tmp_path: Path):
    now = datetime.now()
    _add(
        sessions,
        now,
        wm_class="telegram-desktop_telegram-desktop",
        messaging_present=True,
        skipped_reason="messaging_app",
        screenshot_path=None,
    )
    r = compute_health(sessions, tmp_path)
    assert r.privacy_violations == 0


def test_messaging_full_skips_are_counted(sessions: sessionmaker, tmp_path: Path):
    """Full screenshot skips forced by a messaging window (reason
    'messaging_app') are counted so screenshot gaps stay observable; session
    idle skips and normal captures are not."""
    now = datetime.now()
    _add(sessions, now - timedelta(minutes=1), skipped_reason="messaging_app")
    _add(sessions, now - timedelta(minutes=2), skipped_reason="messaging_app")
    _add(sessions, now - timedelta(minutes=3), skipped_reason="session_locked")
    _add(sessions, now - timedelta(minutes=4))  # normal capture
    r = compute_health(sessions, tmp_path)
    assert r.messaging_skips_24h == 2


def test_db_integrity_ok_on_healthy_db(sessions: sessionmaker, tmp_path: Path):
    _add(sessions, datetime.now(), category="browsing_other")
    r = compute_health(sessions, tmp_path)
    assert r.db_integrity == "ok"


def test_db_integrity_reports_corruption(tmp_path: Path):
    """Damage a real on-disk SQLite file and make sure health notices.
    (A flaky PSU on the deployed box makes unclean shutdowns a real risk.)"""
    from sqlalchemy import create_engine

    db = tmp_path / "captures.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)
    for i in range(50):
        _add(s, datetime.now() - timedelta(minutes=i), category="browsing_other")
    engine.dispose()

    # Overwrite bytes in the middle of the file — past the header, inside
    # table pages — so quick_check sees structural damage.
    data = bytearray(db.read_bytes())
    for off in range(4096 + 100, 4096 + 1100):
        data[off] = 0xFF
    db.write_bytes(bytes(data))

    engine2 = create_engine(f"sqlite:///{db}")
    r = compute_health(sessionmaker(bind=engine2, expire_on_commit=False), tmp_path)
    assert r.db_integrity != "ok"


def test_screenshot_disk_usage(sessions: sessionmaker, tmp_path: Path):
    day = tmp_path / "2026-05-27"
    day.mkdir()
    (day / "1.png").write_bytes(b"\x89PNG" + b"0" * 1000)
    (day / "2.png").write_bytes(b"\x89PNG" + b"0" * 2000)
    r = compute_health(sessions, tmp_path)
    assert r.screenshots_on_disk == 2
    assert r.screenshot_disk_bytes >= 3000

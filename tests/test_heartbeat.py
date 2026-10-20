from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from familymon.heartbeat import (
    DEAD_THRESHOLD_SECONDS,
    STALE_THRESHOLD_SECONDS,
    check_capture_freshness,
    record_heartbeat,
)
from familymon.storage.models import Base, Capture, Event


@pytest.fixture
def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _add_capture(sessions: sessionmaker, when: datetime) -> None:
    with sessions() as s:
        s.add(Capture(captured_at=when, active_wm_class="x", active_title="x", windows=[]))
        s.commit()


def test_never_when_db_is_empty(sessions: sessionmaker):
    r = check_capture_freshness(sessions, now=datetime.now())
    assert r.status == "never"
    assert r.last_capture_at is None and r.age_seconds is None


def test_ok_for_fresh_capture(sessions: sessionmaker):
    now = datetime.now()
    _add_capture(sessions, now - timedelta(seconds=30))
    r = check_capture_freshness(sessions, now=now)
    assert r.status == "ok"
    assert r.is_ok
    assert 25 <= r.age_seconds <= 35


def test_stale_when_past_stale_threshold(sessions: sessionmaker):
    now = datetime.now()
    _add_capture(sessions, now - timedelta(seconds=STALE_THRESHOLD_SECONDS + 30))
    r = check_capture_freshness(sessions, now=now)
    assert r.status == "stale"


def test_dead_when_past_dead_threshold(sessions: sessionmaker):
    now = datetime.now()
    _add_capture(sessions, now - timedelta(seconds=DEAD_THRESHOLD_SECONDS + 30))
    r = check_capture_freshness(sessions, now=now)
    assert r.status == "dead"


def test_thresholds_are_configurable(sessions: sessionmaker):
    now = datetime.now()
    _add_capture(sessions, now - timedelta(seconds=50))
    # Tighter thresholds — 50s is "stale" if stale_seconds=30.
    r = check_capture_freshness(sessions, now=now, stale_seconds=30, dead_seconds=120)
    assert r.status == "stale"


def test_record_heartbeat_writes_event_row(sessions: sessionmaker):
    now = datetime.now()
    _add_capture(sessions, now - timedelta(seconds=15))
    status = check_capture_freshness(sessions, now=now)
    event_id = record_heartbeat(sessions, status)
    assert event_id > 0

    with sessions() as s:
        events = list(s.scalars(select(Event)))
    assert len(events) == 1
    e = events[0]
    assert e.kind == "heartbeat"
    assert e.data["status"] == "ok"
    assert e.data["last_capture_at"] is not None
    assert 10 <= e.data["age_seconds"] <= 20


def test_record_heartbeat_never_status_has_null_last_capture(sessions: sessionmaker):
    status = check_capture_freshness(sessions, now=datetime.now())
    record_heartbeat(sessions, status)
    with sessions() as s:
        e = s.scalar(select(Event))
    assert e.data["status"] == "never"
    assert e.data["last_capture_at"] is None
    assert e.data["age_seconds"] is None


def test_idle_when_nobody_is_logged_in(sessions: sessionmaker):
    """No session: silence is expected, not an alarm (exit 0)."""
    from familymon.heartbeat import EXIT_CODES

    now = datetime(2026, 5, 26, 10, 0, 0)
    _add_capture(sessions, now - timedelta(hours=3))
    status = check_capture_freshness(sessions, now=now, session="none")
    assert status.status == "idle" and EXIT_CODES["idle"] == 0


def test_dead_while_the_session_is_open_is_the_alarm(sessions: sessionmaker):
    now = datetime(2026, 5, 26, 10, 0, 0)
    _add_capture(sessions, now - timedelta(hours=3))
    status = check_capture_freshness(sessions, now=now, session="active")
    assert status.status == "dead" and status.session == "active"


def test_fresh_capture_is_ok_even_if_logind_says_none(sessions: sessionmaker):
    now = datetime(2026, 5, 26, 10, 0, 0)
    _add_capture(sessions, now - timedelta(seconds=20))
    assert check_capture_freshness(sessions, now=now, session="none").status == "ok"


def test_latest_heartbeat_reads_the_watchers_verdict(sessions: sessionmaker):
    from familymon.heartbeat import latest_heartbeat

    now = datetime(2026, 5, 26, 10, 0, 0)
    assert latest_heartbeat(sessions, now=now) is None
    _add_capture(sessions, now - timedelta(hours=3))
    record_heartbeat(
        sessions, check_capture_freshness(sessions, now=now - timedelta(seconds=30), session="none")
    )
    hb = latest_heartbeat(sessions, now=now)
    assert hb.status == "idle" and hb.session == "none"


def test_latest_heartbeat_flags_a_dead_watcher(sessions: sessionmaker):
    from familymon.heartbeat import latest_heartbeat

    now = datetime(2026, 5, 26, 10, 0, 0)
    record_heartbeat(
        sessions,
        check_capture_freshness(sessions, now=now - timedelta(minutes=20), session="active"),
    )
    assert latest_heartbeat(sessions, now=now).status == "watcher_down"


def test_capture_problems_counts_the_days_events(sessions: sessionmaker):
    from familymon.heartbeat import capture_problems

    day = datetime(2026, 5, 26)
    with sessions() as s:
        for kind, hour in (
            ("capture_failed", 9),
            ("capture_failed", 10),
            ("capture_tamper", 11),
            ("heartbeat", 11),
            ("capture_failed", 25),
        ):
            s.add(Event(at=day + timedelta(hours=hour), kind=kind, data={}))
        s.commit()
    got = capture_problems(sessions, day, day + timedelta(days=1))
    assert got == {"capture_failed": 2, "capture_tamper": 1}

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

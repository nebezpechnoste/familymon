from __future__ import annotations

import pytest

from familymon.capture.session_state import SessionState, SessionStatus


def test_active_unlocked_does_not_suppress():
    assert SessionStatus(found=True, active=True, locked=False).suppress_reason() is None


def test_inactive_suppresses_as_inactive():
    assert (
        SessionStatus(found=True, active=False, locked=False).suppress_reason()
        == "session_inactive"
    )


def test_locked_suppresses_as_locked():
    assert (
        SessionStatus(found=True, active=True, locked=True).suppress_reason()
        == "session_locked"
    )


def test_inactive_takes_precedence_over_locked():
    assert (
        SessionStatus(found=True, active=False, locked=True).suppress_reason()
        == "session_inactive"
    )


def test_not_found_fails_open():
    # No graphical session located -> capture anyway, no suppression.
    assert SessionStatus(found=False, active=True, locked=False).suppress_reason() is None


@pytest.mark.asyncio
async def test_status_fails_open_when_connect_raises(monkeypatch):
    s = SessionState(uid=1000)

    async def boom():
        raise RuntimeError("no system bus")

    monkeypatch.setattr(s, "connect", boom)
    status = await s.status()
    assert status == SessionStatus(found=False, active=True, locked=False, queried=False)


@pytest.mark.asyncio
async def test_status_reads_discovered_session(monkeypatch):
    s = SessionState(uid=1000)
    s._bus = object()  # pretend already connected

    async def fake_discover():
        return "/org/freedesktop/login1/session/_34"

    async def fake_read(path):
        assert path == "/org/freedesktop/login1/session/_34"
        return SessionStatus(found=True, active=True, locked=True)

    monkeypatch.setattr(s, "_discover_graphical_session", fake_discover)
    monkeypatch.setattr(s, "_read", fake_read)

    status = await s.status()
    assert status.found and status.locked
    assert status.suppress_reason() == "session_locked"


@pytest.mark.asyncio
async def test_status_fails_open_when_no_graphical_session(monkeypatch):
    s = SessionState(uid=1000)
    s._bus = object()

    async def no_session():
        return None

    monkeypatch.setattr(s, "_discover_graphical_session", no_session)
    status = await s.status()
    # Asked logind and the user has no graphical session: distinguishable
    # from a failed query (the root supervisor skips the tick), but the
    # in-session loop still captures (no suppression).
    assert not status.found and status.queried
    assert status.suppress_reason() is None


@pytest.mark.asyncio
async def test_status_rediscovers_on_stale_path(monkeypatch):
    s = SessionState(uid=1000)
    s._bus = object()
    s._session_path = "/stale"
    calls = {"discover": 0}

    async def fake_read(path):
        if path == "/stale":
            raise RuntimeError("unknown object path")
        return SessionStatus(found=True, active=True, locked=False)

    async def fake_discover():
        calls["discover"] += 1
        return "/fresh"

    monkeypatch.setattr(s, "_read", fake_read)
    monkeypatch.setattr(s, "_discover_graphical_session", fake_discover)

    status = await s.status()
    assert status.found and status.active
    assert calls["discover"] == 1  # rediscovered exactly once after stale read failed
    assert s._session_path == "/fresh"

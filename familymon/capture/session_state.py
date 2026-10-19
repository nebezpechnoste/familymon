"""logind-backed session-activity detection.

Decides whether the monitored user is *actually* at the machine right now, so
the loop can skip pointless screenshots:

  - session backgrounded (fast-user-switched away)  -> stale/black Wayland frame
  - session locked (lock screen showing)            -> no useful content

Both cases: record window metadata, skip the screenshot, tag the reason.

Why we enumerate sessions instead of trusting XDG_SESSION_ID: the capture
service runs under the systemd --user manager (a seatless, type=unspecified
session), and an SSH shell has its own session entirely. The session we care
about is the user's *seated graphical* session (Type wayland/x11 on a real
seat). Seatless sessions report Active=yes trivially, so filtering by seat is
mandatory.

Fail-open: any logind error, or no graphical session found, returns
"capture anyway" — a monitoring tool must not silently stop recording because
of a D-Bus hiccup.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType

logger = logging.getLogger(__name__)

_LOGIND = "org.freedesktop.login1"
_LOGIND_PATH = "/org/freedesktop/login1"
_MANAGER_IFACE = "org.freedesktop.login1.Manager"
_SESSION_IFACE = "org.freedesktop.login1.Session"

_GRAPHICAL_TYPES = frozenset({"wayland", "x11", "mir"})


@dataclass(frozen=True)
class SessionStatus:
    found: bool
    active: bool
    locked: bool
    # False when logind couldn't be asked at all (the fail-open sentinel), as
    # opposed to "asked, and the user has no graphical session".
    queried: bool = True

    def suppress_reason(self) -> str | None:
        """Why the screenshot should be skipped this tick, or None to capture."""
        if not self.found:
            return None  # fail-open
        if not self.active:
            return "session_inactive"
        if self.locked:
            return "session_locked"
        return None


def session_label(status: SessionStatus) -> str:
    """One word for events and the heartbeat: active / locked / inactive,
    "none" (no graphical session) or "unknown" (logind not reachable)."""
    if not status.queried:
        return "unknown"
    if not status.found:
        return "none"
    if not status.active:
        return "inactive"
    return "locked" if status.locked else "active"


# Sentinel returned on any failure: capture anyway.
_FAIL_OPEN = SessionStatus(found=False, active=True, locked=False, queried=False)
_NO_SESSION = SessionStatus(found=False, active=False, locked=False)


class SessionState:
    def __init__(self, uid: int | None = None) -> None:
        self._uid = uid if uid is not None else os.getuid()
        self._bus: MessageBus | None = None
        self._session_path: str | None = None

    async def connect(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def close(self) -> None:
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None

    async def _iface(self, path: str, iface: str):
        assert self._bus is not None
        intro = await self._bus.introspect(_LOGIND, path)
        obj = self._bus.get_proxy_object(_LOGIND, path, intro)
        return obj.get_interface(iface)

    async def _discover_graphical_session(self) -> str | None:
        manager = await self._iface(_LOGIND_PATH, _MANAGER_IFACE)
        sessions = await manager.call_list_sessions()  # a(susso)
        for entry in sessions:
            _sid, uid, _user, seat, path = entry
            if uid != self._uid or not seat:
                continue
            session = await self._iface(path, _SESSION_IFACE)
            stype = await session.get_type()
            if stype in _GRAPHICAL_TYPES:
                return path
        return None

    async def _read(self, path: str) -> SessionStatus:
        session = await self._iface(path, _SESSION_IFACE)
        active = await session.get_active()
        locked = await session.get_locked_hint()
        return SessionStatus(found=True, active=bool(active), locked=bool(locked))

    async def status(self) -> SessionStatus:
        try:
            if self._bus is None:
                await self.connect()
            if self._session_path is None:
                self._session_path = await self._discover_graphical_session()
            if self._session_path is None:
                return _NO_SESSION
            try:
                return await self._read(self._session_path)
            except Exception:
                # Cached path likely went stale (session ended); rediscover once.
                self._session_path = await self._discover_graphical_session()
                if self._session_path is None:
                    return _NO_SESSION
                return await self._read(self._session_path)
        except Exception as e:  # noqa: BLE001 — fail-open is the whole point
            logger.warning("logind session query failed, capturing anyway: %s", e)
            return _FAIL_OPEN

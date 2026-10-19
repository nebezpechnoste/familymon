"""Heartbeat watcher: an independent liveness check on the capture pipeline.

The capture loop is a long-running process. If it dies — crashed, SIGKILL'd,
or the unit was stopped by the user we monitor — nothing in the dashboard
would shout about it; the timeline would just go quiet, which is easy to miss
or misread as "nobody used the laptop."

The watcher runs on its own timer (1/min) and answers two questions
independently of the capture process:

  1. How long ago did the last capture land? (read from the Capture table)
  2. Was the watcher itself running? (each tick records an Event row, so a gap
     in heartbeat events means the watcher itself was down)

Thresholds: <3 min = ok, 3-10 min = stale, >10 min = dead. They tolerate one
missed tick at the default 60s capture cadence (which can happen across a
suspend/resume), but turn red when the gap is clearly not just a hiccup.

In production the watcher is a root system timer (familymon-heartbeat.timer)
that the monitored user can't stop. It also asks logind whether the user has a
graphical session: no session means "idle" (nobody to capture, not a fault),
while stale/dead *with* a session is the signal that capture is being
interfered with or is broken. The dashboard reads the newest heartbeat event
(`latest_heartbeat`) and the day's `capture_failed` / `capture_tamper` events
(`capture_problems`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from familymon.storage.models import Capture, Event

Status = Literal["ok", "stale", "dead", "never", "idle", "watcher_down"]

STALE_THRESHOLD_SECONDS = 180.0
DEAD_THRESHOLD_SECONDS = 600.0

# Exit code per status — non-zero marks the systemd unit "failed", which is a
# secondary visible signal beyond the Event row.
EXIT_CODES: dict[Status, int] = {
    "ok": 0, "stale": 1, "dead": 2, "never": 3, "idle": 0, "watcher_down": 4,
}

# The heartbeat timer fires every minute; older than this, the watcher itself
# is down (or its timer was never enabled).
WATCHER_STALE_SECONDS = 300.0

# Session labels (supervisor._session_label): "none" means the user has no
# graphical session, "unknown" means logind couldn't be asked.
NO_SESSION = "none"


@dataclass(frozen=True)
class HeartbeatStatus:
    status: Status
    last_capture_at: datetime | None
    age_seconds: float | None
    checked_at: datetime
    session: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


def check_capture_freshness(
    sessions: sessionmaker,
    *,
    now: datetime | None = None,
    stale_seconds: float = STALE_THRESHOLD_SECONDS,
    dead_seconds: float = DEAD_THRESHOLD_SECONDS,
    session: str | None = None,
) -> HeartbeatStatus:
    """Compute the current capture-freshness status without writing anything.

    `session`: the monitored user's session state as the caller found it
    (supervisor._session_label), or None when unknown."""
    when = now if now is not None else datetime.now()
    with sessions() as s:
        last = s.scalar(select(func.max(Capture.captured_at)))
    if last is not None:
        last = last.replace(tzinfo=None)
    age = (when - last).total_seconds() if last is not None else None
    if session == NO_SESSION and (age is None or age >= stale_seconds):
        # Nobody logged in: silence is expected.
        return HeartbeatStatus("idle", last, age, when, session)
    if age is None:
        return HeartbeatStatus("never", None, None, when, session)
    if age < stale_seconds:
        status: Status = "ok"
    elif age < dead_seconds:
        status = "stale"
    else:
        status = "dead"
    return HeartbeatStatus(status, last, age, when, session)


def record_heartbeat(sessions: sessionmaker, status: HeartbeatStatus) -> int:
    """Append a `kind="heartbeat"` row to the event log."""
    with sessions() as s:
        event = Event(
            at=status.checked_at,
            kind="heartbeat",
            data={
                "status": status.status,
                "session": status.session,
                "age_seconds": status.age_seconds,
                "last_capture_at": (
                    status.last_capture_at.isoformat()
                    if status.last_capture_at is not None
                    else None
                ),
            },
        )
        s.add(event)
        s.commit()
        s.refresh(event)
        return event.id


def latest_heartbeat(
    sessions: sessionmaker,
    *,
    now: datetime | None = None,
    watcher_stale_seconds: float = WATCHER_STALE_SECONDS,
) -> HeartbeatStatus | None:
    """The watcher's newest verdict, as the dashboard shows it. None when no
    heartbeat was ever recorded (dev runs without the timer)."""
    when = now if now is not None else datetime.now()
    with sessions() as s:
        ev = s.scalars(
            select(Event).where(Event.kind == "heartbeat").order_by(Event.at.desc()).limit(1)
        ).first()
    if ev is None:
        return None
    data = ev.data or {}
    at = ev.at.replace(tzinfo=None)
    last_raw = data.get("last_capture_at")
    last = datetime.fromisoformat(last_raw).replace(tzinfo=None) if last_raw else None
    age = (when - last).total_seconds() if last is not None else None
    if (when - at).total_seconds() > watcher_stale_seconds:
        return HeartbeatStatus("watcher_down", last, age, at, data.get("session"))
    status = data.get("status")
    if status not in EXIT_CODES:
        status = "never"
    return HeartbeatStatus(status, last, age, at, data.get("session"))


def capture_problems(sessions: sessionmaker, start: datetime, end: datetime) -> dict[str, int]:
    """Counts of `capture_failed` and `capture_tamper` events in [start, end)."""
    kinds = ("capture_failed", "capture_tamper")
    with sessions() as s:
        rows = s.execute(
            select(Event.kind, func.count())
            .where(Event.kind.in_(kinds), Event.at >= start, Event.at < end)
            .group_by(Event.kind)
        ).all()
    counts = {k: 0 for k in kinds}
    counts.update({k: n for k, n in rows})
    return counts

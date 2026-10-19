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

Note: in dogfood we run this as a systemd --user service alongside the capture
loop. Per the plan, the production deployment will hoist it to a root service
so a teen who stops their own user units can't silence the watcher; the
in-process logic here is identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from familymon.storage.models import Capture, Event

Status = Literal["ok", "stale", "dead", "never"]

STALE_THRESHOLD_SECONDS = 180.0
DEAD_THRESHOLD_SECONDS = 600.0

# Exit code per status — non-zero marks the systemd unit "failed", which is a
# secondary visible signal beyond the Event row.
EXIT_CODES: dict[Status, int] = {"ok": 0, "stale": 1, "dead": 2, "never": 3}


@dataclass(frozen=True)
class HeartbeatStatus:
    status: Status
    last_capture_at: datetime | None
    age_seconds: float | None
    checked_at: datetime

    @property
    def is_ok(self) -> bool:
        return self.status == "ok"


def check_capture_freshness(
    sessions: sessionmaker,
    *,
    now: datetime | None = None,
    stale_seconds: float = STALE_THRESHOLD_SECONDS,
    dead_seconds: float = DEAD_THRESHOLD_SECONDS,
) -> HeartbeatStatus:
    """Compute the current capture-freshness status without writing anything."""
    when = now if now is not None else datetime.now()
    with sessions() as s:
        last = s.scalar(select(func.max(Capture.captured_at)))
    if last is None:
        return HeartbeatStatus("never", None, None, when)
    age = (when - last).total_seconds()
    if age < stale_seconds:
        status: Status = "ok"
    elif age < dead_seconds:
        status = "stale"
    else:
        status = "dead"
    return HeartbeatStatus(status, last, age, when)


def record_heartbeat(sessions: sessionmaker, status: HeartbeatStatus) -> int:
    """Append a `kind="heartbeat"` row to the event log."""
    with sessions() as s:
        event = Event(
            at=status.checked_at,
            kind="heartbeat",
            data={
                "status": status.status,
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

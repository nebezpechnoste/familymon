"""Charge browser-game minutes to the kid's timekpr PlayTime budget.

timekpr budgets *native* games by process mask; it cannot see a game played
inside a browser tab ("How Hungry? - Play on Armor Games"). familymon can:
its classifier labels those captures `games`. This reconciler bridges the two
— every few minutes it finds new game captures whose focused window is a
browser and deducts that much from PlayTime left via timekpr's own lever
(`timekpra --setplaytimeleft USER - SECONDS`), so the budget stays one number
and the tray shows it.

Rules that keep it honest:
  * Only captures where NO native game process was running (`game_running`
    False). If Roblox was alive in the background while he alt-tabbed to a
    browser game, timekpr already counted that minute — never double-charge.
  * Only today's captures, and only ids past a persisted per-user watermark,
    so a minute is charged at most once and a reconciler outage can't dump
    yesterday onto today's budget.
  * Per-run cap, and every charge is logged (journal + an Event row in the
    root DB) for audit.
  * The kid's database is opened READ-ONLY. A root process writing there
    would leave root-owned journal files the capture loop can't remove.

Runs as root from `familymon-chargeback.timer`; pure logic here, the timekpr
call is injected so it can be tested without a daemon.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from familymon.analyze.heuristics import looks_like_browser
from familymon.storage.models import Capture, Classification

logger = logging.getLogger(__name__)

DEFAULT_MAX_SECONDS_PER_RUN = 1800  # 30 min — a sane ceiling for one 5-minute tick


def make_readonly_engine(db_path: Path) -> Engine:
    """Open another user's SQLite file without ever writing to it (no
    create_all, no migration, no journal files in their directory)."""
    # pysqlite needs `uri=true` IN THE URL to honor the file: URI (a connect
    # arg is ignored with a warning and the path is taken literally).
    return create_engine(f"sqlite:///file:{db_path}?mode=ro&uri=true", future=True)


@dataclass
class ChargeState:
    """Per-user watermark, persisted as JSON in the root state dir."""

    day: str  # ISO date the watermark belongs to
    last_capture_id: int = 0
    charged_seconds_today: int = 0

    @classmethod
    def load(cls, path: Path, user: str, today: date) -> ChargeState:
        try:
            raw = json.loads(path.read_text()).get(user) or {}
        except (OSError, ValueError):
            raw = {}
        state = cls(
            day=str(raw.get("day") or today.isoformat()),
            last_capture_id=int(raw.get("last_capture_id") or 0),
            charged_seconds_today=int(raw.get("charged_seconds_today") or 0),
        )
        if state.day != today.isoformat():
            # New day: PlayTime reset in timekpr, so our daily tally resets
            # too. Keep the id watermark — ids only grow.
            state = cls(day=today.isoformat(), last_capture_id=state.last_capture_id)
        return state

    def save(self, path: Path, user: str) -> None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        data[user] = {
            "day": self.day,
            "last_capture_id": self.last_capture_id,
            "charged_seconds_today": self.charged_seconds_today,
        }
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(json.dumps(data, indent=1))


def find_chargeable_captures(
    sessions: sessionmaker,
    *,
    since_id: int,
    today: date,
    focused_title_is_game: Callable[[str | None], bool] | None = None,
) -> list[tuple[int, datetime]]:
    """Today's `games` captures focused on a browser with no native game
    process running, newer than the watermark. Oldest first.

    Charge rule (policy `games.charge_browser_games_when`):
      * "visible" (default, parent's choice 2026-10-05): the capture's primary
        category is `games` — a browser game visible anywhere on screen bills
        the minute, focused or not. Same semantics timekpr applies to native
        games (Roblox running in the background still counts).
      * "focused": additionally require the FOCUSED window's own title to be a
        game (`focused_title_is_game`), so a game tab merely open beside the
        school portal is flagged as a conflict but not billed.
    """
    start = datetime.combine(today, time.min)
    end = start + timedelta(days=1)
    with sessions() as s:
        rows = s.execute(
            select(Capture.id, Capture.captured_at, Capture.active_wm_class, Capture.active_title)
            .join(Classification, Classification.capture_id == Capture.id)
            .where(
                Capture.id > since_id,
                Capture.captured_at >= start,
                Capture.captured_at < end,
                Classification.category == "games",
                Capture.game_running.is_(False),
            )
            .order_by(Capture.id)
        ).all()
    out = []
    for cid, ts, wm, title in rows:
        if not looks_like_browser(wm):
            continue
        if focused_title_is_game is not None and not focused_title_is_game(title):
            continue
        out.append((cid, ts))
    return out


@dataclass(frozen=True)
class ChargeResult:
    captures: int
    seconds: int
    last_capture_id: int
    capped: bool


def run_chargeback(
    user: str,
    user_sessions: sessionmaker,
    state_path: Path,
    apply_delta: Callable[[str, int], None],
    *,
    interval_seconds: float = 60.0,
    now: datetime | None = None,
    max_seconds_per_run: int = DEFAULT_MAX_SECONDS_PER_RUN,
    focused_title_is_game: Callable[[str | None], bool] | None = None,
) -> ChargeResult:
    """One reconcile tick: charge new browser-game minutes, advance watermark.

    `apply_delta(user, seconds)` performs the timekpr deduction; the watermark
    is only advanced after it returns, so a failed call is retried next tick
    rather than silently dropped.
    """
    when = now if now is not None else datetime.now()
    today = when.date()
    state = ChargeState.load(state_path, user, today)

    rows = find_chargeable_captures(
        user_sessions,
        since_id=state.last_capture_id,
        today=today,
        focused_title_is_game=focused_title_is_game,
    )
    if not rows:
        return ChargeResult(0, 0, state.last_capture_id, False)

    seconds = int(round(len(rows) * interval_seconds))
    capped = seconds > max_seconds_per_run
    if capped:
        # Charge the cap now and leave the rest for the next tick: advance the
        # watermark only as far as what was charged.
        n = max(1, int(max_seconds_per_run // interval_seconds))
        rows = rows[:n]
        seconds = int(round(len(rows) * interval_seconds))

    apply_delta(user, seconds)

    state.last_capture_id = rows[-1][0]
    state.charged_seconds_today += seconds
    state.save(state_path, user)
    logger.info(
        "chargeback: %s browser-game capture(s) -> -%ss PlayTime for %s (today total %ss)",
        len(rows), seconds, user, state.charged_seconds_today,
    )
    return ChargeResult(len(rows), seconds, state.last_capture_id, capped)


def record_charge_event(root_sessions: sessionmaker, user: str, result: ChargeResult) -> None:
    """Audit row in the ROOT state DB (never the kid's)."""
    from familymon.storage.models import Event

    with root_sessions() as s:
        s.add(
            Event(
                at=datetime.now(),
                kind="playtime_chargeback",
                data={
                    "user": user,
                    "captures": result.captures,
                    "seconds": result.seconds,
                    "last_capture_id": result.last_capture_id,
                    "capped": result.capped,
                },
            )
        )
        s.commit()


def sessions_for(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)

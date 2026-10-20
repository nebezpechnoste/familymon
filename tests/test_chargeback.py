from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from familymon.policy.chargeback import (
    ChargeState,
    find_chargeable_captures,
    run_chargeback,
)
from familymon.storage.models import Base, Capture, Classification

TODAY = datetime(2026, 10, 4, 15, 0)


@pytest.fixture
def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _cap(sessions, when, wm_class, category, game_running=False) -> int:
    with sessions() as s:
        c = Capture(
            captured_at=when,
            active_wm_class=wm_class,
            active_title="t",
            windows=[],
            game_running=game_running,
        )
        s.add(c)
        s.flush()
        s.add(
            Classification(
                capture_id=c.id,
                category=category,
                confidence=0.7,
                evidence_text="x",
                source="heuristic",
                created_at=when,
            )
        )
        s.commit()
        return c.id


def test_selects_only_browser_games_without_native_game_running(sessions):
    t = TODAY
    a = _cap(sessions, t, "google-chrome", "games")  # charge
    _cap(
        sessions, t + timedelta(minutes=1), "org.vinegarhq.Sober", "games"
    )  # native: timekpr counts
    _cap(
        sessions, t + timedelta(minutes=2), "google-chrome", "games", game_running=True
    )  # alt-tab mid-game
    _cap(sessions, t + timedelta(minutes=3), "google-chrome", "browsing_other")  # not a game
    _cap(sessions, t - timedelta(days=1), "google-chrome", "games")  # yesterday
    b = _cap(sessions, t + timedelta(minutes=4), "firefox_firefox", "games")  # charge

    rows = find_chargeable_captures(sessions, since_id=0, today=t.date())
    assert [cid for cid, _ in rows] == [a, b]


def test_run_charges_and_advances_watermark(sessions, tmp_path):
    t = TODAY
    ids = [_cap(sessions, t + timedelta(minutes=i), "google-chrome", "games") for i in range(3)]
    calls: list[tuple[str, int]] = []
    state = tmp_path / "charges.json"

    r = run_chargeback("kid", sessions, state, lambda u, s: calls.append((u, s)), now=t)
    assert calls == [("kid", 180)]
    assert (r.captures, r.seconds, r.last_capture_id, r.capped) == (3, 180, ids[-1], False)

    # Second tick: nothing new -> no timekpr call, watermark unchanged.
    r2 = run_chargeback("kid", sessions, state, lambda u, s: calls.append((u, s)), now=t)
    assert calls == [("kid", 180)] and r2.captures == 0

    # A new capture after the watermark is charged alone.
    new = _cap(sessions, t + timedelta(minutes=10), "google-chrome", "games")
    r3 = run_chargeback("kid", sessions, state, lambda u, s: calls.append((u, s)), now=t)
    assert calls[-1] == ("kid", 60) and r3.last_capture_id == new
    assert ChargeState.load(state, "kid", t.date()).charged_seconds_today == 240


def test_failed_timekpr_call_does_not_advance_watermark(sessions, tmp_path):
    _cap(sessions, TODAY, "google-chrome", "games")
    state = tmp_path / "charges.json"

    def boom(u, s):
        raise RuntimeError("daemon down")

    with pytest.raises(RuntimeError):
        run_chargeback("kid", sessions, state, boom, now=TODAY)
    # Retried next tick: the capture is still uncharged.
    calls = []
    run_chargeback("kid", sessions, state, lambda u, s: calls.append(s), now=TODAY)
    assert calls == [60]


def test_per_run_cap_charges_partially_and_resumes(sessions, tmp_path):
    ids = [
        _cap(sessions, TODAY + timedelta(minutes=i), "google-chrome", "games") for i in range(10)
    ]
    state = tmp_path / "charges.json"
    calls = []
    r = run_chargeback(
        "kid", sessions, state, lambda u, s: calls.append(s), now=TODAY, max_seconds_per_run=240
    )
    assert r.capped and calls == [240] and r.last_capture_id == ids[3]
    r2 = run_chargeback(
        "kid", sessions, state, lambda u, s: calls.append(s), now=TODAY, max_seconds_per_run=240
    )
    assert calls == [240, 240] and r2.last_capture_id == ids[7]


def test_daily_tally_resets_but_watermark_persists(tmp_path):
    state = tmp_path / "charges.json"
    ChargeState(day="2026-10-03", last_capture_id=500, charged_seconds_today=900).save(state, "kid")
    st = ChargeState.load(state, "kid", datetime(2026, 10, 4).date())
    assert st.day == "2026-10-04"
    assert st.last_capture_id == 500  # ids only grow; never recharge old rows
    assert st.charged_seconds_today == 0  # PlayTime reset in timekpr, so do we


def test_readonly_engine_reads_real_file_and_refuses_writes(tmp_path):
    """The kid's DB must be opened read-only for real: readable, and any write
    attempt fails instead of leaving root-owned journal files behind."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from familymon.policy.chargeback import make_readonly_engine, sessions_for

    db = tmp_path / "captures.db"
    rw = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(rw)
    rw_sessions = sessionmaker(bind=rw, expire_on_commit=False)
    cid = _cap(rw_sessions, TODAY, "google-chrome", "games")
    rw.dispose()

    ro = sessions_for(make_readonly_engine(db))
    rows = find_chargeable_captures(ro, since_id=0, today=TODAY.date())
    assert [c for c, _ in rows] == [cid]
    with pytest.raises(OperationalError), ro() as s:
        s.execute(text("INSERT INTO event (at, kind) VALUES ('2026-10-05', 'x')"))
        s.commit()
    assert not list(tmp_path.glob("captures.db-*")), "no journal files must appear"


def test_focused_rule_bills_only_when_the_game_tab_is_focused(sessions, tmp_path):
    """Parent's 2026-10-05 data: 60 charged minutes, 47 of them the school
    portal / WhatsApp focused with a game window visible. Under 'visible'
    (default) they bill; under 'focused' only the game-tab minutes do."""
    t = TODAY

    def cap(minute, title):
        with sessions() as s:
            c = Capture(
                captured_at=t + timedelta(minutes=minute),
                active_wm_class="google-chrome",
                active_title=title,
                windows=[],
                game_running=False,
            )
            s.add(c)
            s.flush()
            s.add(
                Classification(
                    capture_id=c.id,
                    category="games",
                    confidence=0.6,
                    evidence_text="x",
                    source="heuristic",
                    created_at=t,
                )
            )
            s.commit()
            return c.id

    game = cap(0, "Territory War | Addicting Games - Google Chrome")
    portal = cap(1, "King's InterHigh - Student portal - Google Chrome")
    chat = cap(2, "WhatsApp - Google Chrome")

    visible = find_chargeable_captures(sessions, since_id=0, today=t.date())
    assert [c for c, _ in visible] == [game, portal, chat]

    def is_game(title):
        return "addicting games" in (title or "").lower()

    focused = find_chargeable_captures(
        sessions, since_id=0, today=t.date(), focused_title_is_game=is_game
    )
    assert [c for c, _ in focused] == [game]

    calls = []
    r = run_chargeback(
        "kid",
        sessions,
        tmp_path / "s.json",
        lambda u, s: calls.append(s),
        now=t,
        focused_title_is_game=is_game,
    )
    assert calls == [60] and r.captures == 1


def test_replaced_database_resets_the_watermark(sessions, tmp_path):
    """A recreated DB restarts ids at 1; a stale higher watermark must not
    hide every new capture from billing (the old bug)."""
    import json

    state = tmp_path / "charges.json"
    today = TODAY.date().isoformat()
    state.write_text(
        json.dumps({"kid": {"day": today, "last_capture_id": 10_000, "charged_seconds_today": 0}})
    )
    _cap(sessions, TODAY - timedelta(minutes=2), "firefox", "games")
    deltas = []
    r = run_chargeback("kid", sessions, state, lambda u, s: deltas.append(s), now=TODAY)
    assert r.captures == 1 and deltas == [60]

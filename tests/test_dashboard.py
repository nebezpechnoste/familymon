from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from familymon.dashboard.app import create_app
from familymon.storage.models import Base, Capture, Classification


_USER = "u"
_PASS = "p"


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    # Isolate schedule_path() per test so conflict annotation doesn't read a
    # real ~/.local/state/familymon/schedule.yaml that may exist on the dev box.
    monkeypatch.setenv("FAMILYMON_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def sessions() -> sessionmaker:
    # StaticPool + check_same_thread=False so every session shares the one
    # in-memory DB connection. Without this, each session sees its own
    # empty `:memory:` instance and the tables are "missing".
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed(sessions: sessionmaker, captured_at: datetime, **kwargs) -> int:
    present = kwargs.pop("present_categories", None)
    cap_kwargs = {
        "active_wm_class": "firefox_firefox",
        "active_title": "page",
        "windows": [],
        "same_as_prev": False,
        **{k: v for k, v in kwargs.items() if k != "category"},
    }
    with sessions() as s:
        c = Capture(captured_at=captured_at, **cap_kwargs)
        s.add(c)
        s.flush()
        if kwargs.get("category"):
            s.add(
                Classification(
                    capture_id=c.id,
                    category=kwargs["category"],
                    confidence=0.85,
                    evidence_text="seeded",
                    source="heuristic",
                    present_categories=present or [kwargs["category"]],
                    created_at=datetime.now(),
                )
            )
        s.commit()
        return c.id


@pytest.fixture
def today_iso() -> str:
    return datetime.now().date().isoformat()


@pytest.fixture
def seeded_today(sessions: sessionmaker) -> sessionmaker:
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    _seed(sessions, now + timedelta(minutes=0), category="browsing_other")
    _seed(sessions, now + timedelta(minutes=1), category="browsing_other", same_as_prev=True)
    _seed(
        sessions,
        now + timedelta(minutes=2),
        active_wm_class="telegram-desktop_telegram-desktop",
        active_title="chat",
        category="messaging",
        screenshot_skipped_reason="messaging_app",
    )
    return sessions


@pytest.fixture
def client(seeded_today: sessionmaker) -> TestClient:
    return TestClient(create_app(session_factory=seeded_today, user=_USER, pwd=_PASS))


def test_index_requires_auth(client: TestClient):
    r = client.get("/")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_wrong_password_is_rejected(client: TestClient):
    r = client.get("/", auth=(_USER, "nope"))
    assert r.status_code == 401


def test_heartbeat_banner_renders_ok_for_fresh_capture(sessions: sessionmaker):
    """The dashboard surfaces capture liveness on every page."""
    # Local-naive matches the capture loop's convention; heartbeat compares
    # against datetime.now() (also local-naive).
    fresh = datetime.now() - timedelta(seconds=15)
    _seed(sessions, fresh, category="browsing_other")
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get(f"/d/{fresh.date().isoformat()}", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert "heartbeat ok" in r.text
    assert "Capture: OK" in r.text


def test_heartbeat_banner_shows_dead_when_no_recent_captures(
    sessions: sessionmaker,
):
    old = datetime.now() - timedelta(hours=2)
    _seed(sessions, old, category="browsing_other")
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get(f"/d/{old.date().isoformat()}", auth=(_USER, _PASS))
    assert "heartbeat dead" in r.text
    assert "Capture DOWN" in r.text


def test_heartbeat_banner_shows_never_when_db_empty(sessions: sessionmaker):
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert "heartbeat never" in r.text
    assert "No captures yet" in r.text


def test_index_renders_html_with_auth(client: TestClient):
    r = client.get("/", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "familymon" in r.text
    # Both seeded categories should appear in the stats strip
    assert "browsing_other" in r.text
    assert "messaging" in r.text


def test_messaging_block_renders_skip_tag(client: TestClient):
    r = client.get("/", auth=(_USER, _PASS))
    assert "messaging_app" in r.text  # the skip-tag rendered next to the block


def test_conflict_renders_with_imported_schedule(sessions: sessionmaker):
    """games during a scheduled lesson shows in the Conflicts section."""
    from datetime import time as dtime

    from familymon.config import schedule_path
    from familymon.policy.schedule import Schedule, ScheduleBlock

    now = datetime.now()
    base = now.replace(hour=10, minute=1, second=0, microsecond=0)
    Schedule(
        blocks=[
            ScheduleBlock(
                day=now.strftime("%A"), start=dtime(10, 0), end=dtime(10, 30), subject="Maths"
            )
        ]
    ).save(schedule_path())
    _seed(sessions, base, active_wm_class="minetest", category="games")

    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert "Conflicts" in r.text
    assert "during Maths" in r.text


def test_streaming_video_during_lesson_renders_conflict(sessions: sessionmaker):
    """YouTube during a lesson is flagged; a class stream in the same slot is not."""
    from datetime import time as dtime

    from familymon.config import schedule_path
    from familymon.policy.schedule import Schedule, ScheduleBlock

    now = datetime.now()
    Schedule(
        blocks=[
            ScheduleBlock(
                day=now.strftime("%A"), start=dtime(10, 0), end=dtime(10, 30), subject="Maths"
            )
        ]
    ).save(schedule_path())
    # A class stream first (no streaming brand) then YouTube — only the latter
    # should surface as a conflict.
    _seed(
        sessions,
        now.replace(hour=10, minute=1, second=0, microsecond=0),
        active_wm_class="firefox_firefox",
        active_title="Maths Lesson - Adobe Connect",
        category="video",
    )
    _seed(
        sessions,
        now.replace(hour=10, minute=5, second=0, microsecond=0),
        active_wm_class="firefox_firefox",
        active_title="Distraction - YouTube",
        category="video",
    )

    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert "Conflicts" in r.text
    assert "during Maths" in r.text
    assert "Adobe Connect" not in r.text.split("Timeline")[0]  # not in the conflicts panel


def test_conflict_flagged_when_block_straddles_lesson_start(sessions: sessionmaker):
    """A distraction block that begins just before a lesson and continues into
    it must be flagged — the CLI (per-capture) always did; the dashboard used
    to test only the block's start time and miss it. This pins the parity fix."""
    from datetime import time as dtime

    from familymon.config import schedule_path
    from familymon.policy.schedule import Schedule, ScheduleBlock

    now = datetime.now()
    Schedule(
        blocks=[
            ScheduleBlock(
                day=now.strftime("%A"), start=dtime(10, 0), end=dtime(10, 30), subject="Maths"
            )
        ]
    ).save(schedule_path())
    # Same wm_class + category -> one merged block spanning 09:59 to 10:01.
    _seed(sessions, now.replace(hour=9, minute=59, second=0, microsecond=0),
          active_wm_class="minetest", category="games")
    _seed(sessions, now.replace(hour=10, minute=1, second=0, microsecond=0),
          active_wm_class="minetest", category="games")

    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert "during Maths" in r.text  # the in-lesson portion is flagged


def test_no_conflict_section_without_schedule(sessions: sessionmaker):
    now = datetime.now().replace(hour=10, minute=1, second=0, microsecond=0)
    _seed(sessions, now, active_wm_class="minetest", category="games")
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert "Conflicts —" not in r.text  # no schedule imported -> no conflict section


def test_also_present_categories_render(sessions: sessionmaker):
    """A capture with a primary 'games' and messaging also present should show
    an 'also: messaging' annotation on the timeline."""
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    _seed(
        sessions,
        now,
        active_wm_class="minetest",
        category="games",
        present_categories=["games", "messaging"],
    )
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert "also: messaging" in r.text


def test_api_captures_returns_today_data(client: TestClient):
    r = client.get("/api/captures", auth=(_USER, _PASS))
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 3
    cats = {row["category"] for row in data}
    assert cats == {"browsing_other", "messaging"}


def test_api_stats_aggregates_time(client: TestClient):
    r = client.get("/api/stats", auth=(_USER, _PASS))
    assert r.status_code == 200
    data = r.json()
    assert data["total_captures"] == 3
    spc = data["seconds_per_category"]
    # 2 captures of browsing_other × 60s, 1 of messaging × 60s
    assert spc["browsing_other"] == 120
    assert spc["messaging"] == 60


def test_specific_day_route(client: TestClient, today_iso: str):
    r = client.get(f"/d/{today_iso}", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert today_iso in r.text


def test_bad_date_returns_400(client: TestClient):
    r = client.get("/d/not-a-date", auth=(_USER, _PASS))
    assert r.status_code == 400


def test_empty_day_renders_without_error(
    sessions: sessionmaker, today_iso: str
):
    # No seed data
    app = create_app(session_factory=sessions, user=_USER, pwd=_PASS)
    c = TestClient(app)
    r = c.get(f"/d/{today_iso}", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert "No captures recorded" in r.text


def test_create_app_requires_credentials(
    sessions: sessionmaker, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("FAMILYMON_DASH_USER", raising=False)
    monkeypatch.delenv("FAMILYMON_DASH_PASS", raising=False)
    with pytest.raises(RuntimeError, match="FAMILYMON_DASH_USER"):
        create_app(session_factory=sessions)


def test_stats_use_actual_cadence_not_assumed_60s(sessions: sessionmaker):
    """A 5-capture block fired at 5-second intervals (one short rapid burst,
    20s wallclock total) must report ~25s of activity, NOT 5 minutes. This
    pins the dashboard against the bug where it assumed 60s/capture regardless
    of FAMILYMON_INTERVAL."""
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    for i in range(5):
        _seed(
            sessions,
            now + timedelta(seconds=5 * i),
            active_wm_class="firefox_firefox",
            active_title="x",
            category="browsing_other",
            same_as_prev=(i > 0),
        )

    app = create_app(session_factory=sessions, user=_USER, pwd=_PASS)
    client = TestClient(app)
    stats = client.get("/api/stats", auth=(_USER, _PASS)).json()

    # 5 captures, mean interval 5s, total duration ≈ 25s.
    secs = stats["seconds_per_category"]["browsing_other"]
    assert 24.9 <= secs <= 25.1, f"expected ~25s, got {secs}"


def test_single_capture_block_falls_back_to_configured_interval(
    sessions: sessionmaker,
):
    now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    _seed(sessions, now, active_wm_class="firefox_firefox", category="browsing_other")

    app = create_app(session_factory=sessions, user=_USER, pwd=_PASS)
    client = TestClient(app)
    stats = client.get("/api/stats", auth=(_USER, _PASS)).json()

    # Default FAMILYMON_INTERVAL is 60s when env var unset.
    assert stats["seconds_per_category"]["browsing_other"] == 60.0


def _write_schedule(tmp_path, *, with_break: bool) -> None:
    """A school block covering today 10:00–11:00 (the seeded messaging capture
    sits at 10:02), optionally with a break covering today."""
    from datetime import date, time, timedelta

    from familymon.policy.schedule import Schedule, ScheduleBlock, SchoolBreak

    today = date.today()
    breaks = [SchoolBreak(today - timedelta(days=1), today + timedelta(days=1), name="Half term")]
    Schedule(
        blocks=[ScheduleBlock(today.strftime("%A"), time(10, 0), time(11, 0), "Economics")],
        breaks=breaks if with_break else [],
    ).save(tmp_path / "state" / "schedule.yaml")


def test_conflicts_panel_shows_without_a_break(client: TestClient, tmp_path):
    _write_schedule(tmp_path, with_break=False)
    r = client.get("/", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert "Conflicts" in r.text and "during Economics" in r.text
    assert "School break" not in r.text


def test_school_break_banner_and_no_conflicts(client: TestClient, tmp_path):
    _write_schedule(tmp_path, with_break=True)
    r = client.get("/", auth=(_USER, _PASS))
    assert r.status_code == 200
    assert "School break: Half term" in r.text
    assert "during Economics" not in r.text


def _event(sessions: sessionmaker, kind: str, data: dict, at: datetime | None = None) -> None:
    from familymon.storage.models import Event

    with sessions() as s:
        s.add(Event(at=at or datetime.now(), kind=kind, data=data))
        s.commit()


def test_banner_shows_not_logged_in_from_the_watcher(sessions: sessionmaker):
    """The root watcher knows nobody is logged in; a quiet timeline then is
    not 'Capture DOWN'."""
    _seed(sessions, datetime.now() - timedelta(hours=2), category="browsing_other")
    _event(sessions, "heartbeat", {"status": "idle", "session": "none",
                                   "age_seconds": 7200, "last_capture_at": None})
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert "heartbeat idle" in r.text and "Not logged in" in r.text


def test_banner_says_when_capture_is_down_during_a_session(sessions: sessionmaker):
    _event(sessions, "heartbeat", {"status": "dead", "session": "active",
                                   "age_seconds": 900, "last_capture_at": None})
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert "Capture DOWN" in r.text and "while the session is active" in r.text


def test_banner_flags_a_silent_watcher(sessions: sessionmaker):
    _event(sessions, "heartbeat", {"status": "ok", "session": "active"},
           at=datetime.now() - timedelta(hours=1))
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert "Heartbeat watcher not running" in r.text


def test_failures_and_tamper_attempts_are_shown_for_the_day(sessions: sessionmaker):
    _event(sessions, "capture_failed", {"error": "probe exited -9", "session": "active"})
    _event(sessions, "capture_failed", {"error": "probe exited -9", "session": "active"})
    _event(sessions, "capture_tamper", {"sink_rejected": 2, "session": "active"})
    c = TestClient(create_app(session_factory=sessions, user=_USER, pwd=_PASS))
    r = c.get("/", auth=(_USER, _PASS))
    assert "2 capture(s) failed while the session was open" in r.text
    assert "1 forged window-list attempt(s) rejected" in r.text


def test_dashboard_opens_the_database_read_only(tmp_path, monkeypatch):
    """It faces the network: it must not be able to write the history."""
    import familymon.dashboard.app as dash
    from familymon.storage.session import make_engine

    monkeypatch.setenv("FAMILYMON_STATE_DIR", str(tmp_path))
    make_engine(tmp_path / "captures.db").dispose()
    opened = []
    real = dash.make_readonly_engine
    monkeypatch.setattr(dash, "make_readonly_engine", lambda p: opened.append(p) or real(p))
    app = create_app(user=_USER, pwd=_PASS)
    assert opened == [tmp_path / "captures.db"]
    r = TestClient(app).get("/", auth=(_USER, _PASS))
    assert r.status_code == 200

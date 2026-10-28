"""The operations layer shared by the CLI and the MCP server (familymon/ops.py).

Everything runs against a temp state dir and a fake timekpr transport; no
real process, D-Bus or /etc is touched."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from familymon import ops
from familymon.cli import main
from familymon.policy.enforcement import LiveState, build_settings, load_policy, whole_hours
from familymon.storage.models import Capture, Classification, Event
from familymon.storage.session import make_engine, make_session_factory

EXAMPLE = (Path(__file__).parent.parent / "familymon/defaults/policy_example.yaml").read_text()
SAMPLE_WEEK = Path(__file__).parent / "data" / "kingsinterhigh_week_sample.json"


@pytest.fixture
def state(tmp_path, monkeypatch) -> Path:
    state = tmp_path / "state"
    cfg = tmp_path / "cfg"
    state.mkdir()
    cfg.mkdir()
    monkeypatch.setenv("FAMILYMON_STATE_DIR", str(state))
    monkeypatch.setenv("FAMILYMON_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("FAMILYMON_USER", raising=False)
    return state


def _env(state: Path) -> dict[str, str]:
    return {
        "FAMILYMON_STATE_DIR": str(state),
        "FAMILYMON_CONFIG_DIR": str(state.parent / "cfg"),
    }


class FakeTimekpr:
    """Records what would be sent to timekpra; never runs anything."""

    def __init__(self, live: LiveState) -> None:
        self.live = live
        self.applied: list = []
        self.playtime_adjustments: list[tuple[str, int]] = []

    def get_live_state(self, user: str) -> LiveState:
        return self.live

    def apply(self, changes) -> None:
        self.applied.extend(changes)

    def adjust_playtime_left(self, user: str, delta: int) -> None:
        self.playtime_adjustments.append((user, delta))


def _open_live(sessions: int = 0) -> LiveState:
    return LiveState(
        allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        allowed_hours=(whole_hours(range(24)),) * 7,
        daily_limits=(86400,) * 7,
        lockout="terminate",
        playtime_enabled=False,
        playtime_allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        playtime_daily_limits=(0,) * 7,
        playtime_activities=(),
        active_session_count=sessions,
    )


def _no_steam(monkeypatch) -> None:
    """The monitored user 'kid' does not exist here; skip the Steam scan."""
    from familymon.capture.games import GameLibrary

    monkeypatch.setattr(
        ops, "game_library", lambda user: GameLibrary(frozenset(), frozenset(), frozenset())
    )


# ---- policy ------------------------------------------------------------------


def test_validate_policy_reports_every_failing_section():
    bad = EXAMPLE.replace("lockout: lock", "lockout: nuke").replace(
        'default: {from: "20:00"}', 'default: {from: "25:00"}'
    )
    r = ops.validate_policy(bad)
    assert r["ok"] is False
    assert [e.split(":")[0] for e in r["errors"]] == ["enforcement", "personal_time"]
    assert ops.validate_policy("- not a mapping")["errors"] == [
        "policy must be a YAML mapping, not list"
    ]
    assert ops.validate_policy("a: [")["ok"] is False


def test_validate_policy_summary_speaks_the_parents_language():
    s = ops.validate_policy(EXAMPLE)["summary"]
    assert s["user"] == "kid"
    assert s["allowed_hours"]["monday"] == "07:00-21:59"
    assert s["allowed_hours"]["saturday"] == "08:00-22:59"
    assert s["daily_limits"]["monday"] == "unlimited"
    assert s["games"]["daily_limits"]["saturday"] == "7200s"
    assert s["games"]["charge_browser_games_when"] == "visible"
    assert s["personal_time"]["monday"] == {"from": "20:00", "until": "bedtime lock"}
    assert s["web_block"]["categories"] == ["messaging", "social", "games"]
    assert s["retention_screenshot_days"] == 90


def test_policy_write_keeps_previous_logs_event_and_names_effects(state: Path):
    first = ops.policy_write(EXAMPLE)
    assert first["previous_saved_to"] is None
    assert "user" in first["changed_keys"]
    path = state / "policy.yaml"
    assert path.read_text() == EXAMPLE  # verbatim, comments included
    assert oct(path.stat().st_mode & 0o777) == "0o600"

    # Audit rows are only written when a DB exists; make one.
    make_engine(state / "captures.db")
    second = ops.policy_write(EXAMPLE.replace("default: unlimited", "default: 4h"))
    assert second["changed_keys"] == ["daily_limits"]
    assert second["previous_saved_to"] == str(state / "policy.yaml.prev")
    assert (state / "policy.yaml.prev").read_text() == EXAMPLE
    assert second["takes_effect"] == {
        "daily_limits": "enforce plan, then enforce apply (or the hourly reconciler)"
    }
    with make_session_factory(make_engine(state / "captures.db"))() as s:
        ev = s.query(Event).filter_by(kind="policy_changed").one()
    assert ev.data["changed"] == ["daily_limits"]


def test_policy_write_rejects_without_touching_the_file(state: Path):
    ops.policy_write(EXAMPLE)
    with pytest.raises(ops.OpsError, match="lockout must be one of"):
        ops.policy_write(EXAMPLE.replace("lockout: lock", "lockout: nuke"))
    assert (state / "policy.yaml").read_text() == EXAMPLE
    assert not (state / "policy.yaml.prev").exists()


def test_policy_read_before_and_after(state: Path):
    assert ops.policy_read()["exists"] is False
    ops.policy_write(EXAMPLE)
    r = ops.policy_read()
    assert r["exists"] and r["text"] == EXAMPLE and r["summary"]["user"] == "kid"


def test_cli_policy_set_get_validate(state: Path, tmp_path: Path):
    env = _env(state)
    doc = tmp_path / "p.yaml"
    doc.write_text(EXAMPLE)
    r = CliRunner().invoke(main, ["policy", "set", str(doc)], env=env)
    assert r.exit_code == 0, r.output
    assert "wrote" in r.output and "allowed_hours: enforce plan" in r.output
    r = CliRunner().invoke(main, ["policy", "get"], env=env)
    assert r.exit_code == 0 and r.output == EXAMPLE
    r = CliRunner().invoke(main, ["policy", "get", "--json"], env=env)
    assert json.loads(r.output)["summary"]["lockout"] == "lock"
    r = CliRunner().invoke(main, ["policy", "validate"], env=env)
    assert r.exit_code == 0 and "policy OK: user kid" in r.output
    # Validation from stdin, invalid: exit 1, the file untouched.
    r = CliRunner().invoke(
        main, ["policy", "set", "-"], input=EXAMPLE.replace("user: kid", "user: ''"), env=env
    )
    assert r.exit_code == 1 and "policy rejected" in r.output
    assert (state / "policy.yaml").read_text() == EXAMPLE


# ---- config files --------------------------------------------------------------


def test_config_write_validates_with_the_real_loader(state: Path):
    cfg = state.parent / "cfg"
    with pytest.raises(ops.OpsError, match=r"schedule_extras\.yaml rejected"):
        ops.config_write("schedule_extras.yaml", "blocks:\n  - {day: Monday, start: '10:00'}\n")
    assert not (cfg / "schedule_extras.yaml").exists()
    r = ops.config_write("schedule_extras.yaml", "exclude_subjects: [PE]\n")
    assert r["path"] == str(cfg / "schedule_extras.yaml")
    assert "schedule refresh" in r["takes_effect"]
    assert oct((cfg / "schedule_extras.yaml").stat().st_mode & 0o777) == "0o644"
    got = ops.config_read("schedule_extras.yaml")
    assert got["overridden"] is True and got["text"] == "exclude_subjects: [PE]\n"
    with pytest.raises(ops.OpsError, match="unknown key"):
        ops.config_write("game_apps.yaml", "foo: [x]\n")
    with pytest.raises(ops.OpsError, match="unknown category"):
        ops.config_write("domains_starter.yaml", "nonsense: [a.com]\n")
    with pytest.raises(ops.OpsError, match=r"messaging_apps\.yaml rejected"):
        ops.config_write("messaging_apps.yaml", "title_patterns: ['(']\n")
    with pytest.raises(ops.OpsError, match="unknown config file"):
        ops.config_read("policy.yaml")


def test_config_read_falls_back_to_bundled(state: Path):
    r = ops.config_read("messaging_apps.yaml")
    assert r["overridden"] is False and "window_classes" in r["text"]


# ---- schedule --------------------------------------------------------------------


def test_schedule_import_refresh_and_summary(state: Path):
    assert ops.schedule_summary()["exists"] is False
    data = json.loads(SAMPLE_WEEK.read_text())
    r = ops.schedule_import_data(data)
    assert r["exists"] and r["count"] > 0 and r["by_category"].get("school")
    ops.config_write(
        "schedule_extras.yaml",
        "blocks:\n"
        "  - {day: Monday, start: '18:00', end: '18:30', subject: Maths, category: study}\n"
        "breaks:\n  - {start: 2026-10-19, end: 2026-11-01, name: Half term}\n",
    )
    r = ops.schedule_refresh()
    assert r["by_category"]["study"] == 1 and r["imported"]["breaks"] == 1
    assert r["previous_saved_to"] == str(state / "schedule.yaml.prev")
    assert (state / "schedule.yaml.prev").exists()
    assert r["breaks"][0]["label"] == "Half term 2026-10-19 to 2026-11-01"
    s = ops.schedule_summary()
    assert s["count"] == r["count"] and s["breaks"][0]["name"] == "Half term"
    with pytest.raises(ops.OpsError, match="only 'kingsinterhigh'"):
        ops.schedule_import_data(data, "other")
    with pytest.raises(ops.OpsError, match="not understood"):
        ops.schedule_import_data({"nope": 1})


def test_cli_schedule_show_json(state: Path):
    ops.schedule_import_data(json.loads(SAMPLE_WEEK.read_text()))
    r = CliRunner().invoke(main, ["schedule", "show", "--json"], env=_env(state))
    assert r.exit_code == 0 and json.loads(r.output)["exists"] is True


# ---- enforcement -----------------------------------------------------------------


def test_plan_apply_guard_and_force(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    fake = FakeTimekpr(_open_live(sessions=1))
    # 03:00 on a Monday is outside 07:00-21:59 -> applying would cut the session.
    now = datetime(2026, 10, 26, 3, 0)
    plan = ops.plan_summary(transport=fake, now=now)
    assert plan["user"] == "kid" and plan["count"] > 0 and plan["safe_now"] is False
    assert any("excludes the current time" in r for r in plan["reasons"])
    with pytest.raises(ops.UnsafeNow):
        ops.apply_policy(transport=fake, now=now)
    assert fake.applied == []
    forced = ops.apply_policy(force=True, transport=fake, now=now)
    assert forced["count"] == len(fake.applied) == plan["count"] and forced["forced"] is True
    # Nothing left to do once live matches.
    desired = build_settings(load_policy(state / "policy.yaml"))
    fake.live = LiveState(
        allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        allowed_hours=desired.allowed_hours,
        daily_limits=desired.daily_limits,
        lockout=desired.lockout,
        playtime_enabled=desired.playtime_enabled,
        playtime_allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        playtime_daily_limits=desired.playtime_daily_limits,
        playtime_activities=desired.playtime_activities,
        active_session_count=1,
    )
    assert ops.plan_summary(transport=fake, now=now)["up_to_date"] is True
    assert ops.apply_policy(transport=fake, now=now)["up_to_date"] is True


def test_apply_without_policy_is_an_ops_error(state: Path):
    with pytest.raises(ops.OpsError, match="no policy"):
        ops.plan_summary(transport=FakeTimekpr(_open_live()))


def test_live_summary_uses_policy_user_by_default(state: Path):
    ops.policy_write(EXAMPLE)
    r = ops.live_summary(transport=FakeTimekpr(_open_live()))
    assert r["user"] == "kid" and r["allowed_hours"]["monday"] == "00:00-23:59"
    assert r["daily_limits"]["monday"] == "unlimited"


def test_grant_until_and_playtime(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    fake = FakeTimekpr(_open_live(sessions=1))
    now = datetime(2026, 10, 26, 21, 30)
    r = ops.grant("kid", until="22:29", playtime="30m", note="homework", transport=fake, now=now)
    assert r["hours"]["start"] == "21:30" and r["hours"]["end"] == "22:29"
    assert r["playtime_seconds"] == 1800 and fake.playtime_adjustments == [("kid", 1800)]
    assert ops.grants_active(now)["grants"][0]["note"] == "homework"
    # The grant is folded into the desired state: 22[00-29] on Monday.
    desired = ops.desired_settings(now)
    assert "22[00-29]" in ops._hours_label(desired.allowed_hours[0])
    with pytest.raises(ops.OpsError, match="already past"):
        ops.grant("kid", until="20:00", transport=fake, now=now)
    with pytest.raises(ops.OpsError, match="policy user is 'kid'"):
        ops.grant("other", until="23:00", transport=fake, now=now)
    assert ops.grants_active(now)["count"] == 1  # the failed grant was rolled back
    with pytest.raises(ops.OpsError, match="give until"):
        ops.grant("kid", transport=fake, now=now)
    revoked = ops.revoke_grants("kid", transport=fake, now=now)
    assert revoked["revoked"] == 1 and ops.grants_active(now)["count"] == 0


def test_cli_enforce_plan_json_with_fake_transport(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    monkeypatch.setattr(ops, "_transport", lambda: FakeTimekpr(_open_live()))
    r = CliRunner().invoke(main, ["enforce", "plan", "--json"], env=_env(state))
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["user"] == "kid" and out["safe_now"] is True and out["count"] > 0
    r = CliRunner().invoke(main, ["enforce", "show"], env=_env(state))
    assert r.exit_code == 0 and "mon: 00:00-23:59  limit=unlimited" in r.output


def test_games_known_and_forget(state: Path):
    from familymon.capture.games import save_known_games

    save_known_games(state / "known_games.json", {"/games/common/A", "/games/common/B"})
    assert [g["dir"] for g in ops.games_known()["games"]] == ["/games/common/A", "/games/common/B"]
    forgotten = ops.games_forget(["/games/common/A"])
    assert forgotten["remaining"] == 1
    assert forgotten["previous_saved_to"] == str(state / "known_games.json.prev")
    with pytest.raises(ops.OpsError, match="not remembered"):
        ops.games_forget(["/games/common/Z"])


# ---- health, events, timeline, personal time -------------------------------------


def _seed_day(state: Path, when: datetime) -> None:
    sessions = make_session_factory(make_engine(state / "captures.db"))
    with sessions() as s:
        for i, (cat, hidden) in enumerate(
            [("browsing_other", False), ("games", False), ("games", True), ("video", True)]
        ):
            cap = Capture(
                captured_at=when + timedelta(minutes=i),
                active_wm_class="firefox_firefox",
                active_title=f"title {i}",
                windows=[],
                same_as_prev=False,
                personal_time=hidden,
                screenshot_skipped_reason="personal_time" if hidden else None,
            )
            s.add(cap)
            s.flush()
            s.add(
                Classification(
                    capture_id=cap.id,
                    category=cat,
                    confidence=0.9,
                    source="heuristic",
                    evidence_text="seeded",
                    present_categories=[cat],
                    created_at=when,
                )
            )
        s.add(Event(at=when, kind="grant_hours", data={"user": "kid"}))
        s.add(Event(at=when, kind="heartbeat", data={"status": "ok"}))
        s.commit()


def test_health_events_timeline_and_personal_time(state: Path):
    ops.policy_write(EXAMPLE)
    today = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    _seed_day(state, today)

    h = ops.health_summary()
    assert h["ok"] is True and h["exit_code"] == 0 and h["report"]["total_captures"] == 4
    assert h["heartbeat"]["status"] in ("ok", "stale", "dead", "watcher_down")

    ev = ops.events_on(today.date())
    assert [e["kind"] for e in ev["events"]] == ["grant_hours"]  # heartbeat left out
    assert ops.events_on(today.date(), ("heartbeat",))["count"] == 1
    with pytest.raises(ops.OpsError, match="YYYY-MM-DD"):
        ops.events_on("yesterday")

    t = ops.timeline(today.date())
    cats = [b["category"] for b in t["blocks"]]
    assert cats == ["browsing_other", "games", "games", "video"]
    hidden = [b for b in t["blocks"] if b["personal_time"]]
    assert all(b["wm_class"] is None and b["title"] is None for b in hidden)
    assert t["blocks"][0]["title"] == "title 0"
    assert len(t["personal_time_spans"]) == 1
    assert set(t["seconds_per_category"]) == {"browsing_other", "games", "video"}

    pt = ops.personal_time_summary(today.date(), now=today)
    assert pt["hidden_captures"] == 2 and pt["hidden_game_captures"] == 1
    assert pt["window"]["label"].startswith(("20:00", "21:00"))


def test_cli_health_and_events_json(state: Path):
    today = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
    _seed_day(state, today)
    env = _env(state)
    r = CliRunner().invoke(main, ["health", "--json"], env=env)
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["report"]["total_captures"] == 4
    r = CliRunner().invoke(main, ["health"], env=env)
    assert r.exit_code == 0 and "privacy check:    OK" in r.output and "heartbeat:" in r.output
    r = CliRunner().invoke(main, ["events", "--json", "--day", today.date().isoformat()], env=env)
    assert json.loads(r.output)["count"] == 1
    r = CliRunner().invoke(main, ["personal-time", "--json"], env=env)
    assert r.exit_code == 0 and json.loads(r.output)["window"] is None  # no policy here


def test_events_without_db(state: Path):
    with pytest.raises(ops.OpsError, match="no database"):
        ops.events_on()


# ---- review fixes (2026-10-28) -----------------------------------------------


class _Frozen0300(datetime):
    """Monday 03:00: outside the example policy's 07:00-21:59 window."""

    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 26, 3, 0)


def test_cli_apply_and_grant_refusals_exit_3(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    fake = FakeTimekpr(_open_live(sessions=1))
    monkeypatch.setattr(ops, "_transport", lambda: fake)
    monkeypatch.setattr(ops, "datetime", _Frozen0300)
    env = _env(state)
    r = CliRunner().invoke(main, ["enforce", "apply"], env=env)
    assert r.exit_code == 3, r.output
    assert "refusing" in r.output and "excludes the current time" in r.output
    assert fake.applied == []
    # A grant whose window would still exclude now is refused the same way.
    r = CliRunner().invoke(
        main, ["enforce", "grant", "kid", "--from", "20:00", "--until", "22:00"], env=env
    )
    assert r.exit_code == 3, r.output
    assert "refusing" in r.output and "not stored" in r.output
    assert fake.applied == [] and ops.grants_active(_Frozen0300.now())["count"] == 0
    r = CliRunner().invoke(main, ["enforce", "apply", "--force"], env=env)
    assert r.exit_code == 0, r.output
    assert fake.applied


def test_grant_does_not_push_a_pending_unsafe_policy(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(
        EXAMPLE.replace("daily_limits:\n  default: unlimited", "daily_limits:\n  default: 2h")
    )
    fake = FakeTimekpr(_open_live(sessions=1))  # live: unlimited, all day
    now = datetime(2026, 10, 26, 15, 0)  # Monday, inside the window
    plan = ops.plan_summary(transport=fake, now=now)
    assert plan["safe_now"] is False and any("limit shrinks" in r for r in plan["reasons"])
    r = ops.grant("kid", until="23:00", transport=fake, now=now)
    # Only today's widened window went to timekpr; the shrink waits for apply.
    assert [c.setting for c in fake.applied] == ["allowed_hours[monday]"]
    assert "daily_limits" in r["hours"]["deferred"]
    assert r["hours"]["timekpr_changes"][0]["setting"] == "allowed_hours[monday]"
    assert r["hours"]["end"] == "23:00"
    assert ops.grants_active(now)["count"] == 1
    # With no session the whole plan is safe and the grant applies all of it.
    fake2 = FakeTimekpr(_open_live(sessions=0))
    r2 = ops.grant("kid", until="23:30", transport=fake2, now=now)
    assert r2["hours"]["deferred"] == [] and len(fake2.applied) > 1


def test_grant_refuses_when_todays_window_would_cut_the_session(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    fake = FakeTimekpr(_open_live(sessions=1))
    now = datetime(2026, 10, 26, 3, 0)
    with pytest.raises(ops.UnsafeNow, match="refusing to apply the grant"):
        ops.grant("kid", from_="20:00", until="22:00", transport=fake, now=now)
    assert fake.applied == [] and ops.grants_active(now)["count"] == 0
    assert not (state / "grants.json").exists() or ops.grants_active(now)["count"] == 0


def test_failed_grant_keeps_earlier_grants(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    fake = FakeTimekpr(_open_live(sessions=1))
    now = datetime(2026, 10, 26, 21, 30)
    ops.grant("kid", until="22:29", transport=fake, now=now)
    assert ops.grants_active(now)["count"] == 1

    def boom(now=None):
        raise ops.OpsError("policy.yaml is unusable: boom")

    monkeypatch.setattr(ops, "desired_settings", boom)
    with pytest.raises(ops.OpsError, match="boom"):
        ops.grant("kid", until="23:00", transport=fake, now=now)
    active = ops.grants_active(now)
    assert active["count"] == 1 and active["grants"][0]["end"] == "22:29"


def test_grant_and_revoke_need_the_policy_user(state: Path, monkeypatch):
    _no_steam(monkeypatch)
    ops.policy_write(EXAMPLE)
    fake = FakeTimekpr(_open_live())
    with pytest.raises(ops.OpsError, match="policy user is 'kid', not 'other'"):
        ops.grant("other", playtime="10m", transport=fake)
    assert fake.playtime_adjustments == []
    with pytest.raises(ops.OpsError, match="policy user is 'kid', not 'other'"):
        ops.revoke_grants("other", transport=fake)
    with pytest.raises(ops.OpsError, match="HH:MM"):
        ops.grant("kid", until="24:00", transport=fake)
    with pytest.raises(ops.OpsError, match="HH:MM"):
        ops.grant("kid", until="7pm", transport=fake)


def test_identical_rewrite_keeps_the_undo_copy(state: Path):
    v2 = EXAMPLE.replace("default: {start: 7, end: 21}", "default: {start: 8, end: 21}")
    ops.policy_write(EXAMPLE)
    r = ops.policy_write(v2)
    prev = state / "policy.yaml.prev"
    assert r["previous_saved_to"] == str(prev) and prev.read_text() == EXAMPLE
    r = ops.policy_write(v2)  # a retry of the same write
    assert r["changed_keys"] == [] and r["previous_saved_to"] == str(prev)
    assert prev.read_text() == EXAMPLE  # the undo still undoes
    # Same for config overrides.
    text = "exclude_subjects: [Assembly]\n"
    assert ops.config_write("schedule_extras.yaml", text)["previous_saved_to"] is None
    assert ops.config_write("schedule_extras.yaml", text)["previous_saved_to"] is None
    r = ops.config_write("schedule_extras.yaml", "exclude_subjects: [Art]\n")
    assert r["previous_saved_to"].endswith("schedule_extras.yaml.prev")
    r = ops.config_write("schedule_extras.yaml", "exclude_subjects: [Art]\n")
    assert Path(r["previous_saved_to"]).read_text() == text


def test_messaging_contract_cannot_be_emptied_or_misspelled(state: Path):
    with pytest.raises(ops.OpsError, match="no window classes"):
        ops.config_write("messaging_apps.yaml", "{}\n")
    with pytest.raises(ops.OpsError, match="no window classes"):
        ops.config_write("messaging_apps.yaml", "window_classes: []\ntitle_patterns: []\n")
    with pytest.raises(ops.OpsError, match="unknown key"):
        ops.config_write("messaging_apps.yaml", "windows_classes: [signal]\n")
    r = ops.config_write("messaging_apps.yaml", "window_classes: [signal]\n")
    assert r["path"].endswith("messaging_apps.yaml")
    with pytest.raises(ops.OpsError, match="unknown category"):
        ops.config_write("title_brands.yaml", "vidoe: [youtube]\n")
    assert ops.config_write("title_brands.yaml", "video: [youtube]\n")["path"]

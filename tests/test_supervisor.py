"""Root capture supervisor: tick logic, probe plumbing, script drop.

No real session, logind, setpriv or KWin is touched: the probe is injected,
and run_probe only ever runs a Python one-liner the test itself spawns.
"""

from __future__ import annotations

import io
import stat
import sys
from datetime import datetime
from unittest.mock import Mock

import pytest
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from familymon.analyze.dedup import PHashRingBuffer
from familymon.capture.backend import CaptureResult, WindowSnapshot
from familymon.capture.session_state import SessionStatus
from familymon.capture.supervisor import (
    PersonalTimeGate,
    ProbeOutcome,
    SessionTarget,
    kwin_script_drop,
    probe_argv,
    run_probe,
    session_target,
    supervised_tick,
)
from familymon.policy.personal_time import PersonalTimeWindow
from familymon.storage.models import Base, Event

AT = datetime(2026, 10, 19, 10, 0).astimezone()


@pytest.fixture
def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _result(**kw) -> CaptureResult:
    return CaptureResult(
        captured_at=datetime(2001, 1, 1).astimezone(),  # a probe clock root ignores
        active_window=WindowSnapshot(wm_class="firefox", title="t", pid=4242, active=True),
        **kw,
    )


class _State:
    def __init__(self, status: SessionStatus) -> None:
        self._status = status

    async def status(self) -> SessionStatus:
        return self._status


ACTIVE = SessionStatus(found=True, active=True, locked=False)
LOCKED = SessionStatus(found=True, active=True, locked=True)
NONE = SessionStatus(found=False, active=False, locked=False)


async def _tick(sessions, status, outcome, *, gamelib=None, personal_time=False):
    asked = []

    async def probe(suppress):
        asked.append(suppress)
        return outcome

    persistence = Mock()
    persistence.write = Mock(return_value=7)
    tick = await supervised_tick(
        session_state=_State(status),
        probe=probe,
        persistence=persistence,
        sessions=sessions,
        phash_buf=PHashRingBuffer(),
        gamelib=gamelib,
        now=lambda: AT,
        personal_time=personal_time,
    )
    return tick, asked, persistence


def _events(sessions) -> list[Event]:
    with sessions() as s:
        return list(s.scalars(select(Event).order_by(Event.id)))


@pytest.mark.asyncio
async def test_no_session_skips_without_probing_or_recording(sessions):
    tick, asked, persistence = await _tick(sessions, NONE, ProbeOutcome(_result()))
    assert tick.skipped == "no_session"
    assert asked == []
    persistence.write.assert_not_called()
    assert _events(sessions) == []


@pytest.mark.asyncio
async def test_good_probe_is_persisted_with_roots_clock(sessions):
    tick, asked, persistence = await _tick(
        sessions, ACTIVE, ProbeOutcome(_result(screenshot_bytes=_png()))
    )
    assert tick.capture_id == 7 and asked == [None]
    written = persistence.write.call_args.args[0]
    assert written.captured_at == AT
    assert persistence.write.call_args.kwargs["phash"] is not None
    assert _events(sessions) == []


@pytest.mark.asyncio
async def test_locked_session_asks_the_probe_to_skip_the_screenshot(sessions):
    _, asked, _ = await _tick(sessions, LOCKED, ProbeOutcome(_result()))
    assert asked == ["session_locked"]


@pytest.mark.asyncio
async def test_personal_time_skips_the_screenshot_and_flags_the_row(sessions):
    tick, asked, persistence = await _tick(
        sessions, ACTIVE, ProbeOutcome(_result()), personal_time=True
    )
    assert tick.capture_id == 7 and asked == ["personal_time"]
    assert persistence.write.call_args.kwargs["personal_time"] is True
    assert _events(sessions) == []


@pytest.mark.asyncio
async def test_a_locked_session_in_personal_time_still_classifies_as_idle(sessions):
    """The session reason wins: a locked screen must not be billed as the
    game that was left open behind it."""
    _, asked, persistence = await _tick(
        sessions, LOCKED, ProbeOutcome(_result()), personal_time=True
    )
    assert asked == ["session_locked"]
    assert persistence.write.call_args.kwargs["personal_time"] is True


@pytest.mark.asyncio
async def test_outside_personal_time_rows_are_not_flagged(sessions):
    _, asked, persistence = await _tick(sessions, ACTIVE, ProbeOutcome(_result()))
    assert asked == [None] and persistence.write.call_args.kwargs["personal_time"] is False


def test_gate_reports_the_window_only_while_it_is_active():
    window = PersonalTimeWindow(datetime(2026, 10, 19, 20, 0), datetime(2026, 10, 19, 22, 0), True)
    gate = PersonalTimeGate(lambda now: window)
    assert gate.check(datetime(2026, 10, 19, 19, 59)) is None
    assert gate.check(datetime(2026, 10, 19, 20, 0)) is window
    assert gate.check(datetime(2026, 10, 19, 22, 0)) is None
    assert PersonalTimeGate(lambda now: None).check(datetime(2026, 10, 19, 21, 0)) is None


def test_gate_treats_an_unusable_policy_as_no_personal_time_and_warns_once(caplog):
    def broken(now):
        raise ValueError("personal_time.from must be a quoted time")

    gate = PersonalTimeGate(broken)
    with caplog.at_level("WARNING", logger="familymon.capture.supervisor"):
        assert gate.check(datetime(2026, 10, 19, 21, 0)) is None
        assert gate.check(datetime(2026, 10, 19, 21, 1)) is None
    warnings = [r for r in caplog.records if "policy unusable" in r.getMessage()]
    assert len(warnings) == 1 and "monitoring in full" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_failed_probe_with_a_session_is_recorded_not_persisted(sessions):
    tick, _, persistence = await _tick(sessions, ACTIVE, ProbeOutcome(None, "probe exited -9"))
    assert tick.error == "probe exited -9"
    persistence.write.assert_not_called()
    [ev] = _events(sessions)
    assert ev.kind == "capture_failed"
    assert ev.data == {"error": "probe exited -9", "session": "active"}


@pytest.mark.asyncio
async def test_missing_window_list_counts_as_a_failure(sessions):
    """A tick that saw nothing must not refresh the heartbeat."""
    tick, _, persistence = await _tick(
        sessions, ACTIVE, ProbeOutcome(_result(window_list_ok=False))
    )
    assert tick.capture_id is None
    persistence.write.assert_not_called()
    assert [e.kind for e in _events(sessions)] == ["capture_failed"]


@pytest.mark.asyncio
async def test_rejected_pushes_are_recorded_as_tamper_evidence(sessions):
    tick, _, _ = await _tick(sessions, ACTIVE, ProbeOutcome(_result(sink_rejected=3)))
    assert tick.capture_id == 7
    [ev] = _events(sessions)
    assert ev.kind == "capture_tamper" and ev.data["sink_rejected"] == 3


@pytest.mark.asyncio
async def test_game_detection_runs_root_side(sessions):
    gamelib = Mock()
    gamelib.is_game = Mock(return_value=True)
    _, _, persistence = await _tick(sessions, ACTIVE, ProbeOutcome(_result()), gamelib=gamelib)
    gamelib.is_game.assert_called_with("firefox", 4242)
    assert persistence.write.call_args.kwargs["game_running"] is True


def test_probe_result_round_trips_screenshot_bytes_as_json():
    png = _png()
    back = CaptureResult.model_validate_json(_result(screenshot_bytes=png).model_dump_json())
    assert back.screenshot_bytes == png


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


@pytest.mark.asyncio
async def test_run_probe_parses_output_and_passes_the_request():
    payload = _result(screenshot_bytes=b"\x89PNG").model_dump_json()
    code = (
        "import sys, json; req = json.load(sys.stdin); "
        "assert req == {'suppress': None, 'script': '/s.js', 'nonce': 'n1'}, req; "
        f"sys.stdout.write({payload!r})"
    )
    out = await run_probe(_py(code), {"suppress": None, "script": "/s.js", "nonce": "n1"})
    assert out.error is None and out.result.screenshot_bytes == b"\x89PNG"


@pytest.mark.asyncio
async def test_run_probe_reports_exit_code_and_stderr():
    out = await run_probe(_py("import sys; sys.stderr.write('kwin gone'); sys.exit(3)"), {})
    assert out.result is None and "exited 3" in out.error and "kwin gone" in out.error


@pytest.mark.asyncio
async def test_run_probe_rejects_garbage():
    out = await run_probe(_py("print('{\"not\": \"a capture\"}')"), {})
    assert out.result is None and "unreadable" in out.error


@pytest.mark.asyncio
async def test_run_probe_kills_a_hung_probe():
    out = await run_probe(_py("import time; time.sleep(30)"), {}, timeout=0.3)
    assert out.result is None and "timed out" in out.error


def test_probe_argv_scrubs_the_environment_and_isolates_python():
    t = SessionTarget("kid", 1001, 1001, "/home/kid", "/run/user/1001", "wayland-1")
    argv = probe_argv(t, python="/opt/familymon/venv/bin/python3")
    assert argv[:6] == ["setpriv", "--reuid=1001", "--regid=1001", "--init-groups",
                        "--no-new-privs", "--"]
    assert argv[6:8] == ["env", "-i"]
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus" in argv
    assert "WAYLAND_DISPLAY=wayland-1" in argv
    assert argv[-5:] == ["/opt/familymon/venv/bin/python3", "-I", "-m", "familymon.cli",
                         "capture-probe"]
    assert not any(a.startswith(("PYTHONPATH", "LD_PRELOAD")) for a in argv)


def test_session_target_needs_a_session_bus(tmp_path):
    import getpass
    import os

    user = getpass.getuser()
    if os.getuid() == 0:
        pytest.skip("session_target refuses uid 0")
    run = tmp_path / "run"
    assert session_target(user, run_user_root=run) is None
    rt = run / str(os.getuid())
    rt.mkdir(parents=True)
    (rt / "bus").touch()
    (rt / "wayland-0.lock").touch()
    (rt / "wayland-0").touch()
    t = session_target(user, run_user_root=run)
    assert t is not None and t.wayland_display == "wayland-0" and t.runtime_dir == str(rt)


def test_script_drop_is_private_readonly_and_cleaned_up(tmp_path):
    d = tmp_path / "kwin"
    with kwin_script_drop(d) as (path, nonce):
        assert stat.S_IMODE(d.stat().st_mode) == 0o711
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert f'nonce: "{nonce}"' in path.read_text()
        assert len(path.name) > 30  # unguessable
    assert not path.exists()


def test_two_drops_use_different_nonces_and_names(tmp_path):
    with kwin_script_drop(tmp_path) as (p1, n1), kwin_script_drop(tmp_path) as (p2, n2):
        assert n1 != n2 and p1 != p2

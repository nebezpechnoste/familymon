"""Root capture supervisor: capture without trusting the monitored session.

Window enumeration (KWin scripting) and screenshots (spectacle) only work as a
client of the monitored user's own Plasma session: KWin authorizes screenshots
by the calling executable, and a root process is not accepted as a session
client at all (tested on Plasma 6: spectacle as root reports that KWin "does
not seem to be available"). Everything else belongs to root.

So the long-running loop runs as root (familymon-capture.service, a system
unit the user can neither stop nor mask), follows the user's graphical session
through logind, and per tick launches a short-lived helper *as the user*:

    root supervisor ── setpriv (uid kid) ── familymonctl capture-probe
         │  per-tick KWin script (nonce) in /run/familymon/kwin, read-only
         │  request on stdin, CaptureResult JSON (redacted) on stdout
         └─ game detection, dedup, persistence, events: all root-side

What that buys:
  * The loop, its state (DB, screenshots) and its history are root-owned.
  * Nothing of familymon keeps running in the session between ticks; each
    probe starts clean from root-owned code with a scrubbed environment
    (`env -i`, `python -I`), and makes itself non-dumpable so other processes
    of the user can't reach into it through /proc.
  * Under the unit's PrivateTmp, the helper's temporary screenshot file is in
    a /tmp the user can't see.
  * The window list is accepted only from KWin's own bus connection with the
    tick's nonce (kwin_wayland._WindowSink).
  * A tick that fails while the session exists is recorded as a
    `capture_failed` event, and rejected window-list pushes as
    `capture_tamper`, so interference is visible instead of silent.

What it can't stop: the user can still make a tick fail (kill the helper,
restart KWin, log out). That is detected, not prevented; see heartbeat.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import secrets
import sys
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from familymon.analyze.dedup import PHashRingBuffer
from familymon.capture.backend import CaptureResult
from familymon.capture.games import GameLibrary
from familymon.capture.kwin_wayland import new_nonce, render_window_list_script
from familymon.capture.persist import CapturePersistence
from familymon.capture.session_state import SessionState, session_label
from familymon.policy.personal_time import SKIP_REASON, PersonalTimeWindow
from familymon.storage.models import Event

logger = logging.getLogger(__name__)

KWIN_SCRIPT_DIR = Path("/run/familymon/kwin")
PROBE_TIMEOUT_SECONDS = 45.0
MAX_PROBE_OUTPUT_BYTES = 64 * 1024 * 1024
GAMELIB_REFRESH_SECONDS = 1800.0


@dataclass(frozen=True)
class SessionTarget:
    """Where the monitored user's session lives, for the probe's env."""

    user: str
    uid: int
    gid: int
    home: str
    runtime_dir: str
    wayland_display: str


def session_target(user: str, *, run_user_root: Path = Path("/run/user")) -> SessionTarget | None:
    """The user's session endpoints, or None when they have no session bus
    (not logged in)."""
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return None
    if pw.pw_uid == 0:
        raise ValueError("refusing to monitor uid 0")
    runtime = run_user_root / str(pw.pw_uid)
    if not (runtime / "bus").exists():
        return None
    sockets = sorted(p.name for p in runtime.glob("wayland-*") if not p.name.endswith(".lock"))
    return SessionTarget(
        user=user,
        uid=pw.pw_uid,
        gid=pw.pw_gid,
        home=pw.pw_dir,
        runtime_dir=str(runtime),
        wayland_display=sockets[0] if sockets else "wayland-0",
    )


def probe_argv(target: SessionTarget, python: str = sys.executable) -> list[str]:
    """Drop to the user with `setpriv` and a scrubbed environment: nothing
    from the user's shell or profile reaches the probe (no PYTHONPATH, no
    LD_PRELOAD), and `-I` keeps Python from importing anything user-writable.

    setpriv rather than runuser: no PAM stack, so no "session opened" line in
    the auth log every minute, and `--no-new-privs` for free."""
    return [
        "setpriv",
        f"--reuid={target.uid}",
        f"--regid={target.gid}",
        "--init-groups",
        "--no-new-privs",
        "--",
        "env",
        "-i",
        f"HOME={target.home}",
        f"USER={target.user}",
        f"LOGNAME={target.user}",
        "PATH=/usr/bin:/bin",
        "LANG=C.UTF-8",
        f"XDG_RUNTIME_DIR={target.runtime_dir}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={target.runtime_dir}/bus",
        f"WAYLAND_DISPLAY={target.wayland_display}",
        "QT_QPA_PLATFORM=wayland",
        python,
        "-I",
        "-m",
        "familymon.cli",
        "capture-probe",
    ]


@contextmanager
def kwin_script_drop(directory: Path = KWIN_SCRIPT_DIR) -> Iterator[tuple[Path, str]]:
    """This tick's window-list script, where KWin (running as the user) can
    read it but the user can't change it: a root-owned 0711 directory (no
    listing) holding a root-owned 0644 file with an unguessable name."""
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o711)
    nonce = new_nonce()
    path = directory / f"winlist-{secrets.token_hex(16)}.js"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(render_window_list_script(nonce))
        os.chmod(path, 0o644)
        yield path, nonce
    finally:
        path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ProbeOutcome:
    result: CaptureResult | None
    error: str | None = None


async def run_probe(
    argv: list[str],
    request: dict,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeOutcome:
    """Run one probe process and parse its answer. Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/",
        )
    except OSError as e:
        return ProbeOutcome(None, f"could not start the probe: {e}")
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(json.dumps(request).encode()), timeout=timeout
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return ProbeOutcome(None, f"probe timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-3:]
        return ProbeOutcome(None, f"probe exited {proc.returncode}: {' | '.join(tail)}")
    if len(out) > MAX_PROBE_OUTPUT_BYTES:
        return ProbeOutcome(None, f"probe output too large ({len(out)} bytes)")
    try:
        return ProbeOutcome(CaptureResult.model_validate_json(out))
    except ValueError as e:
        return ProbeOutcome(None, f"probe output unreadable: {str(e)[:200]}")


Probe = Callable[[str | None], Awaitable[ProbeOutcome]]
WindowLookup = Callable[[datetime], PersonalTimeWindow | None]


class PersonalTimeGate:
    """Per tick: is this personal time (policy/personal_time.py)? The lookup
    reads policy.yaml and grants.json, root-only files, so the kid cannot
    widen the window. A policy that can't be read is logged once and counts
    as no personal time: monitoring in full is the safe failure."""

    def __init__(self, lookup: WindowLookup) -> None:
        self._lookup = lookup
        self._last_error: str | None = None
        self._active = False

    def check(self, now: datetime) -> PersonalTimeWindow | None:
        try:
            window = self._lookup(now)
        except Exception as e:  # PolicyError, OSError, yaml errors
            message = f"{type(e).__name__}: {e}"
            if message != self._last_error:
                logger.warning("personal time: policy unusable (%s); monitoring in full", message)
                self._last_error = message
            window = None
        else:
            self._last_error = None
        active = window if window is not None and window.active(now) else None
        if (active is not None) != self._active:
            self._active = active is not None
            if active is not None:
                logger.info("personal time until %s: no screenshots", f"{active.end:%H:%M}")
            else:
                logger.info("personal time over: screenshots resume")
        return active


@dataclass(frozen=True)
class SupervisedTick:
    capture_id: int | None
    skipped: str | None = None  # "no_session": nothing to capture
    error: str | None = None


def _event(sessions: sessionmaker, kind: str, data: dict, at: datetime) -> None:
    with sessions() as s:
        s.add(Event(at=at, kind=kind, data=data))
        s.commit()


async def supervised_tick(
    *,
    session_state: SessionState,
    probe: Probe,
    persistence: CapturePersistence,
    sessions: sessionmaker,
    phash_buf: PHashRingBuffer,
    gamelib: GameLibrary | None,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    personal_time: bool = False,
) -> SupervisedTick:
    """One root-side tick: session check, probe, verify, persist.

    `personal_time`: the tick falls in the policy's personal-time window, so
    no screenshot is taken (a locked or inactive session skips it anyway, and
    that reason wins so the row still classifies as idle) and the row is
    flagged for the dashboard to hide its details."""
    status = await session_state.status()
    if status.queried and not status.found:
        return SupervisedTick(None, skipped="no_session")

    suppress = status.suppress_reason() or (SKIP_REASON if personal_time else None)
    outcome = await probe(suppress)
    result = outcome.result
    session = session_label(status)
    at = now()

    if result is not None and result.sink_rejected:
        _event(
            sessions,
            "capture_tamper",
            {
                "sink_rejected": result.sink_rejected,
                "session": session,
            },
            at,
        )
    if result is None or not result.window_list_ok:
        error = outcome.error or "KWin window list unavailable"
        _event(sessions, "capture_failed", {"error": error, "session": session}, at)
        return SupervisedTick(None, error=error)

    # Root's clock, not the probe's.
    result = result.model_copy(update={"captured_at": at})

    phash_hex: str | None = None
    phash_dup = False
    if result.screenshot_bytes:
        phash_hex, phash_dup = phash_buf.check_and_add(result.screenshot_bytes)

    game_running = False
    if gamelib is not None:
        windows = [w for w in (result.active_window, *result.other_windows) if w is not None]
        game_running = any(gamelib.is_game(w.wm_class, w.pid) for w in windows)

    cap_id = persistence.write(
        result,
        same_as_prev=phash_dup,
        phash=phash_hex,
        game_running=game_running,
        personal_time=personal_time,
    )
    return SupervisedTick(cap_id)


async def run_supervisor(
    user: str,
    *,
    persistence: CapturePersistence,
    sessions: sessionmaker,
    interval: float = 60.0,
    load_gamelib: Callable[[], GameLibrary] | None = None,
    script_dir: Path = KWIN_SCRIPT_DIR,
    personal_time: WindowLookup | None = None,
) -> None:
    """The root loop. Runs until cancelled. `personal_time` is consulted every
    tick (policy changes and grants take effect within a minute)."""
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        raise RuntimeError(f"monitored user {user!r} does not exist") from None
    if uid == 0:
        raise RuntimeError("refusing to monitor uid 0")

    session_state = SessionState(uid=uid)
    phash_buf = PHashRingBuffer(maxlen=8, threshold=4)
    loop = asyncio.get_running_loop()
    gamelib: GameLibrary | None = None
    gamelib_at = float("-inf")
    gate = PersonalTimeGate(personal_time) if personal_time is not None else None

    async def probe(suppress: str | None) -> ProbeOutcome:
        target = session_target(user)
        if target is None:
            return ProbeOutcome(None, "no session bus")
        with kwin_script_drop(script_dir) as (script, nonce):
            return await run_probe(
                probe_argv(target),
                {"suppress": suppress, "script": str(script), "nonce": nonce},
            )

    try:
        while True:
            tick_start = loop.time()
            if load_gamelib is not None and tick_start - gamelib_at > GAMELIB_REFRESH_SECONDS:
                # Re-scanned periodically, so a game installed mid-session
                # counts without a restart.
                try:
                    gamelib = load_gamelib()
                except Exception:
                    logger.exception("game library scan failed; keeping the previous one")
                gamelib_at = tick_start
            try:
                paused = gate is not None and gate.check(datetime.now()) is not None
                tick = await supervised_tick(
                    session_state=session_state,
                    probe=probe,
                    persistence=persistence,
                    sessions=sessions,
                    phash_buf=phash_buf,
                    gamelib=gamelib,
                    personal_time=paused,
                )
                if tick.error:
                    logger.warning("capture tick failed: %s", tick.error)
                elif tick.capture_id is not None:
                    logger.info("tick id=%d", tick.capture_id)
            except Exception:
                logger.exception("capture tick failed")
            elapsed = loop.time() - tick_start
            await asyncio.sleep(max(0.0, interval - elapsed))
    finally:
        await session_state.close()

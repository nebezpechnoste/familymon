"""KDE Plasma 6 Wayland capture backend.

Two D-Bus surfaces are involved:

- Window enumeration: `org.kde.KWin` service, path `/Scripting`, interface
  `org.kde.kwin.Scripting`. A small JS script enumerates `workspace.windowList()`
  and calls back into our `org.familymon.WindowSink` service with the JSON.

- Screenshot: spawned as a `spectacle --background --nonotify --fullscreen`
  subprocess. KWin's `org.kde.KWin.ScreenShot2` D-Bus interface enforces caller
  authorization in Plasma 6, so arbitrary processes get
  `DBusError: The process is not authorized to take a screenshot`. Spectacle is
  internally authorized; the subprocess sidesteps the permission system without
  requiring an xdg-desktop-portal consent dialog.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, RequestNameReply
from dbus_next.service import ServiceInterface, method

from .backend import CaptureResult, WindowSnapshot
from .messaging_skip import MessagingSkipList
from .redact import redact_messaging_regions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WindowList:
    windows: list[WindowSnapshot]
    logical_size: tuple[float, float]
    screen_count: int


_KWIN_WINDOW_LIST_SCRIPT = r"""
var out = {windows: [], vw: 0, vh: 0, screens: 0, err: null};
try {
    var wins = workspace.windowList();
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        var g = w.frameGeometry;
        out.windows.push({
            caption: w.caption || null,
            resourceClass: w.resourceClass || null,
            resourceName: w.resourceName || null,
            pid: w.pid || null,
            active: w === workspace.activeWindow,
            internalId: w.internalId ? w.internalId.toString() : null,
            minimized: w.minimized,
            skipPager: w.skipPager,
            skipTaskbar: w.skipTaskbar,
            windowType: w.windowType,
            x: g.x, y: g.y, width: g.width, height: g.height
        });
    }
    out.screens = workspace.screens.length;
    var vs = workspace.virtualScreenSize;
    out.vw = vs.width; out.vh = vs.height;
} catch (e) {
    out.err = "" + e;
}
callDBus(
    "org.familymon.WindowSink", "/sink",
    "org.familymon.WindowSink", "Push",
    JSON.stringify(out)
);
"""


class _WindowSink(ServiceInterface):
    """D-Bus service the KWin script calls back into with the window-list JSON."""

    def __init__(self) -> None:
        super().__init__("org.familymon.WindowSink")
        self._payload: str | None = None
        self._event = asyncio.Event()

    @method()
    def Push(self, json_str: "s"):  # noqa: N802 — D-Bus method naming
        # NOTE (trust model): this sink lives on the *session* bus, so any
        # same-UID process could also call Push and forge/suppress the window
        # list. We can't fully close that here — an in-session nonce is readable
        # by the same UID, and dbus-next doesn't expose the caller's unique name
        # to a high-level @method handler for sender-pinning. The same actor
        # could also just kill the capture process. Robust defence is the
        # phase-4 root deployment + heartbeat watcher (task #17); __aenter__ at
        # least refuses to start if it can't take sole ownership of the name,
        # so a pre-squatted sink can't silently divert KWin's callback.
        self._payload = json_str
        self._event.set()

    def reset(self) -> None:
        """Clear pending payload + event. Call before triggering KWin script.

        Kept separate from `wait()` so a Push that arrives between reset() and
        loadScript() is preserved by the subsequent wait().
        """
        self._event.clear()
        self._payload = None

    async def wait(self, timeout: float = 3.0) -> str | None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except TimeoutError:
            return None
        return self._payload


def _user_session_env() -> dict[str, str]:
    """Env vars Spectacle needs when invoked from a non-graphical context."""
    uid = os.getuid()
    return {
        "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", "wayland-0"),
        "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"),
        "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
            "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus"
        ),
        "QT_QPA_PLATFORM": "wayland",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/"),
    }


class KWinWaylandBackend:
    name = "kwin_wayland"

    def __init__(self, skip_list: MessagingSkipList | None = None) -> None:
        self._skip_list = skip_list or MessagingSkipList.from_file()
        self._bus: MessageBus | None = None
        self._sink: _WindowSink | None = None
        self._scripting: Any = None
        self._script_path: Path | None = None
        self._spectacle_env = _user_session_env()

    async def __aenter__(self) -> "KWinWaylandBackend":
        self._bus = await MessageBus(bus_type=BusType.SESSION).connect()

        self._sink = _WindowSink()
        self._bus.export("/sink", self._sink)
        reply = await self._bus.request_name("org.familymon.WindowSink")
        if reply not in (
            RequestNameReply.PRIMARY_OWNER,
            RequestNameReply.ALREADY_OWNER,
        ):
            # Someone is squatting the sink name; KWin's callback would go to
            # them and our window list would come from an unknown sender.
            # Refuse to start rather than trust forged data.
            raise RuntimeError(
                "familymon could not take sole ownership of the "
                f"org.familymon.WindowSink D-Bus name (reply={reply.name}); "
                "another process may be squatting it"
            )

        intro = await self._bus.introspect("org.kde.KWin", "/Scripting")
        obj = self._bus.get_proxy_object("org.kde.KWin", "/Scripting", intro)
        self._scripting = obj.get_interface("org.kde.kwin.Scripting")

        fd, path = tempfile.mkstemp(suffix=".js", prefix="familymon_winlist_")
        os.write(fd, _KWIN_WINDOW_LIST_SCRIPT.encode())
        os.close(fd)
        self._script_path = Path(path)

        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._script_path and self._script_path.exists():
            try:
                self._script_path.unlink()
            except OSError:
                pass
        if self._bus:
            try:
                self._bus.disconnect()
            except Exception:
                pass
            self._bus = None

    async def _enumerate_windows(self) -> _WindowList:
        assert self._scripting is not None
        assert self._sink is not None
        assert self._script_path is not None

        empty = _WindowList(windows=[], logical_size=(0.0, 0.0), screen_count=0)

        self._sink.reset()
        try:
            await self._scripting.call_load_script(str(self._script_path))
            await self._scripting.call_start()
        except Exception as e:
            logger.warning("KWin scripting load/start failed: %s", e)
            return empty

        payload = await self._sink.wait(timeout=3.0)
        if payload is None:
            logger.warning("KWin scripting timed out without payload")
            return empty

        try:
            raw = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("KWin scripting returned non-JSON payload")
            return empty

        if raw.get("err"):
            logger.warning("KWin script error: %s", raw["err"])

        windows: list[WindowSnapshot] = []
        for w in raw.get("windows", []):
            # Filter out Plasma's own internal/decoration windows. These have
            # both skipPager and skipTaskbar set and add only noise.
            if w.get("skipPager") and w.get("skipTaskbar"):
                continue
            windows.append(
                WindowSnapshot(
                    wm_class=w.get("resourceClass"),
                    title=w.get("caption"),
                    pid=w.get("pid"),
                    active=bool(w.get("active")),
                    minimized=bool(w.get("minimized")),
                    uuid=w.get("internalId"),
                    x=float(w.get("x") or 0.0),
                    y=float(w.get("y") or 0.0),
                    width=float(w.get("width") or 0.0),
                    height=float(w.get("height") or 0.0),
                )
            )
        return _WindowList(
            windows=windows,
            logical_size=(float(raw.get("vw") or 0.0), float(raw.get("vh") or 0.0)),
            screen_count=int(raw.get("screens") or 0),
        )

    async def _spectacle(self) -> bytes:
        with tempfile.NamedTemporaryFile(
            prefix="familymon_shot_", suffix=".png", delete=False
        ) as tmp:
            out_path = Path(tmp.name)

        try:
            proc = await asyncio.create_subprocess_exec(
                "spectacle",
                "--background",
                "--nonotify",
                "--fullscreen",
                "--output",
                str(out_path),
                env=self._spectacle_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    "spectacle failed (rc=%s): %s",
                    proc.returncode,
                    stderr.decode(errors="replace").strip(),
                )
                return b""
            return out_path.read_bytes()
        finally:
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def snapshot(self, *, suppress_screenshot_reason: str | None = None) -> CaptureResult:
        wl = await self._enumerate_windows()
        windows = wl.windows
        active = next((w for w in windows if w.active), None)
        others = tuple(w for w in windows if not w.active)
        captured_at = datetime.now().astimezone()

        messaging_present = any(
            not w.minimized and self._skip_list.should_skip(w.wm_class, w.title)
            for w in windows
        )

        common = {
            "captured_at": captured_at,
            "active_window": active,
            "other_windows": others,
            "messaging_present": messaging_present,
        }

        # Caller-driven suppression (session backgrounded / locked) takes
        # precedence — no point grabbing a frame at all.
        if suppress_screenshot_reason is not None:
            return CaptureResult(
                **common,
                screenshot_bytes=None,
                screenshot_skipped_reason=suppress_screenshot_reason,
            )

        screenshot = await self._spectacle()
        if not screenshot:
            return CaptureResult(**common, screenshot_bytes=None)

        if messaging_present:
            redacted = redact_messaging_regions(
                screenshot,
                windows,
                wl.logical_size,
                self._skip_list,
            )
            if redacted is None:
                # Non-uniform image scale (can't map safely). Never leak:
                # fall back to a full skip.
                return CaptureResult(
                    **common,
                    screenshot_bytes=None,
                    screenshot_skipped_reason="messaging_app",
                )
            png, any_redacted = redacted
            return CaptureResult(
                **common, screenshot_bytes=png, screenshot_redacted=any_redacted
            )

        return CaptureResult(**common, screenshot_bytes=screenshot)

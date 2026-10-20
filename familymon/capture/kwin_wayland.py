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
import secrets
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dbus_next import Message, MessageType
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, RequestNameReply

from .backend import CaptureResult, WindowSnapshot
from .messaging_skip import MessagingSkipList
from .redact import redact_messaging_regions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _WindowList:
    windows: list[WindowSnapshot]
    logical_size: tuple[float, float]
    screen_count: int
    # False when KWin's answer never arrived (as opposed to an empty desktop).
    ok: bool = True


_KWIN_WINDOW_LIST_SCRIPT = r"""
function desktopIds(w) {
    var ids = [];
    try { for (var j = 0; j < w.desktops.length; j++) ids.push(w.desktops[j].id); } catch (e) {}
    return ids;
}
var out = {nonce: "@NONCE@", windows: [], vw: 0, vh: 0, screens: 0, err: null,
           currentDesktop: null, currentActivity: null};
try {
    var cur = workspace.currentDesktop;
    out.currentDesktop = cur ? cur.id : null;
    out.currentActivity = workspace.currentActivity || null;
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
            onAllDesktops: w.onAllDesktops,
            desktops: desktopIds(w),
            activities: w.activities || [],
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

SINK_NAME = "org.familymon.WindowSink"
_SINK_PATH = "/sink"


def new_nonce() -> str:
    return secrets.token_hex(16)


def render_window_list_script(nonce: str) -> str:
    """The KWin script for one tick. The nonce is echoed back in the payload,
    so the sink can tell this tick's answer from a replayed or forged one."""
    if not nonce.isalnum():
        raise ValueError("nonce must be alphanumeric")
    return _KWIN_WINDOW_LIST_SCRIPT.replace("@NONCE@", nonce)


class _WindowSink:
    """Receives the KWin script's callback on the session bus.

    Trust model: the session bus is shared with everything the monitored user
    runs, so anyone can call Push. A push is accepted only when it comes from
    KWin's own bus connection (its unique name, which the bus assigns and
    nobody can claim) AND carries this tick's nonce. Everything else is
    counted in `rejected`, which the capture supervisor records as tamper
    evidence. A script the user loads into KWin themselves would pass the
    sender check, which is what the nonce is for: it lives in a file only the
    script KWin was told to load contains.
    """

    def __init__(self) -> None:
        self._payload: dict | None = None
        self._event = asyncio.Event()
        self._nonce: str | None = None
        self._sender: str | None = None
        self.rejected = 0

    def arm(self, nonce: str, kwin_sender: str | None) -> None:
        """Clear any pending payload and accept only this nonce from this
        sender. Call before loading the KWin script, so a Push that arrives
        between arm() and wait() is kept."""
        self._event.clear()
        self._payload = None
        self._nonce = nonce
        self._sender = kwin_sender

    def offer(self, sender: str | None, json_str: str) -> bool:
        """One Push call. Returns whether it was accepted."""
        try:
            raw = json.loads(json_str)
        except (TypeError, ValueError):
            raw = None
        ok = (
            self._nonce is not None
            and self._sender is not None
            and sender == self._sender
            and isinstance(raw, dict)
            and raw.get("nonce") == self._nonce
            and self._payload is None
        )
        if not ok:
            self.rejected += 1
            return False
        self._payload = raw
        self._event.set()
        return True

    def handle(self, msg: Message) -> Message | None:
        """dbus-next message handler: answers Push calls on /sink."""
        if (
            msg.message_type != MessageType.METHOD_CALL
            or msg.path != _SINK_PATH
            or msg.interface != SINK_NAME
            or msg.member != "Push"
        ):
            return None
        body = msg.body[0] if msg.body and isinstance(msg.body[0], str) else None
        self.offer(msg.sender, body)
        # Same empty reply either way: a forger learns nothing.
        return Message.new_method_return(msg)

    async def wait(self, timeout: float = 3.0) -> dict | None:
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


def _on_current_desktop(w: dict, desktop: str | None, activity: str | None) -> bool:
    """Is this window on the virtual desktop and activity shown right now?

    Missing information (an older KWin, a window without a desktop list)
    counts as "yes": over-redacting is the safe failure."""
    if desktop and not w.get("onAllDesktops"):
        desktops = w.get("desktops")
        if isinstance(desktops, list) and desktops and desktop not in desktops:
            return False
    if activity:
        activities = w.get("activities")
        if isinstance(activities, list) and activities and activity not in activities:
            return False
    return True


def _parse_window_list(raw: dict) -> _WindowList:
    desktop = raw.get("currentDesktop") or None
    activity = raw.get("currentActivity") or None
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
                on_current_desktop=_on_current_desktop(w, desktop, activity),
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


class KWinWaylandBackend:
    name = "kwin_wayland"

    def __init__(
        self,
        skip_list: MessagingSkipList | None = None,
        *,
        script_path: Path | None = None,
        nonce: str | None = None,
    ) -> None:
        """`script_path` + `nonce`: a window-list script someone else wrote
        (the root supervisor puts it where the session user can't modify
        it). Without them the backend writes its own temp script per run."""
        if (script_path is None) != (nonce is None):
            raise ValueError("script_path and nonce go together")
        self._skip_list = skip_list or MessagingSkipList.from_file()
        self._bus: MessageBus | None = None
        self._sink: _WindowSink | None = None
        self._scripting: Any = None
        self._script_path: Path | None = script_path
        self._nonce: str | None = nonce
        self._owns_script = script_path is None
        self._spectacle_env = _user_session_env()

    async def __aenter__(self) -> KWinWaylandBackend:
        self._bus = await MessageBus(bus_type=BusType.SESSION).connect()

        self._sink = _WindowSink()
        self._bus.add_message_handler(self._sink.handle)
        reply = await self._bus.request_name(SINK_NAME)
        if reply not in (
            RequestNameReply.PRIMARY_OWNER,
            RequestNameReply.ALREADY_OWNER,
        ):
            # Someone is squatting the sink name; KWin's callback would go to
            # them. Refuse to start rather than run blind.
            raise RuntimeError(
                "familymon could not take sole ownership of the "
                f"{SINK_NAME} D-Bus name (reply={reply.name}); "
                "another process may be squatting it"
            )

        intro = await self._bus.introspect("org.kde.KWin", "/Scripting")
        obj = self._bus.get_proxy_object("org.kde.KWin", "/Scripting", intro)
        self._scripting = obj.get_interface("org.kde.kwin.Scripting")

        if self._owns_script:
            self._nonce = new_nonce()
            fd, path = tempfile.mkstemp(suffix=".js", prefix="familymon_winlist_")
            os.write(fd, render_window_list_script(self._nonce).encode())
            os.close(fd)
            self._script_path = Path(path)

        return self

    async def _kwin_sender(self) -> str | None:
        """KWin's unique bus name: the only sender the sink trusts."""
        assert self._bus is not None
        reply = await self._bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="GetNameOwner",
                signature="s",
                body=["org.kde.KWin"],
            )
        )
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            return None
        return reply.body[0]

    async def _unload_script(self) -> None:
        try:
            await self._scripting.call_unload_script(str(self._script_path))
        except Exception as e:  # best effort
            logger.debug("KWin unloadScript failed: %s", e)

    def take_sink_rejected(self) -> int:
        """Rejected pushes since the last call (then reset)."""
        if self._sink is None:
            return 0
        n, self._sink.rejected = self._sink.rejected, 0
        return n

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_script and self._script_path and self._script_path.exists():
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

        assert self._nonce is not None

        empty = _WindowList(windows=[], logical_size=(0.0, 0.0), screen_count=0, ok=False)

        try:
            kwin = await self._kwin_sender()
        except Exception as e:
            logger.warning("could not resolve KWin's bus name: %s", e)
            return empty
        self._sink.arm(self._nonce, kwin)
        try:
            await self._scripting.call_load_script(str(self._script_path))
            await self._scripting.call_start()
        except Exception as e:
            logger.warning("KWin scripting load/start failed: %s", e)
            await self._unload_script()
            return empty

        raw = await self._sink.wait(timeout=3.0)
        # KWin keeps every loaded instance (re-loading the same path adds
        # another), so unload each tick's or they pile up in the compositor.
        await self._unload_script()
        if raw is None:
            logger.warning(
                "KWin scripting timed out without an accepted payload (rejected pushes so far: %d)",
                self._sink.rejected,
            )
            return empty

        if raw.get("err"):
            logger.warning("KWin script error: %s", raw["err"])

        return _parse_window_list(raw)

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
        result = await self._snapshot(suppress_screenshot_reason)
        # Counted last, so pushes that arrive while spectacle runs are included.
        rejected = self.take_sink_rejected()
        return result.model_copy(update={"sink_rejected": rejected}) if rejected else result

    async def _snapshot(self, suppress_screenshot_reason: str | None) -> CaptureResult:
        wl = await self._enumerate_windows()
        windows = wl.windows
        active = next((w for w in windows if w.active), None)
        others = tuple(w for w in windows if not w.active)
        captured_at = datetime.now().astimezone()

        messaging_present = any(
            not w.hidden and self._skip_list.should_skip(w.wm_class, w.title) for w in windows
        )

        common = {
            "captured_at": captured_at,
            "active_window": active,
            "other_windows": others,
            "messaging_present": messaging_present,
        }

        # Without a window list there is no way to know whether a messaging
        # window is on screen, so no screenshot: the redaction contract
        # outranks a frame.
        if not wl.ok:
            return CaptureResult(
                **common,
                window_list_ok=False,
                screenshot_bytes=None,
                screenshot_skipped_reason=suppress_screenshot_reason or "window_list_unavailable",
            )

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
            return CaptureResult(**common, screenshot_bytes=png, screenshot_redacted=any_redacted)

        return CaptureResult(**common, screenshot_bytes=screenshot)

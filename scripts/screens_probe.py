"""Multi-monitor geometry probe (task #18 A, multi-output).

Gathers everything needed to map KWin's logical window geometry onto Spectacle's
fullscreen image when more than one output is present:
  - virtualScreenSize / virtualScreenGeometry (logical bounding box)
  - per output: name, logical geometry, scale / devicePixelRatio (best-effort)
  - per window: class, frameGeometry, which output, active/minimized
  - the actual Spectacle fullscreen image pixel dimensions

Run with the external monitor connected:
    uv run python scripts/screens_probe.py

Uses a unique D-Bus sink name so it doesn't clash with a running
familymon-capture service.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.service import ServiceInterface, method
from PIL import Image

_SCRIPT = r"""
var out = {vw:0, vh:0, vg:null, screens:[], windows:[], err:null};
try {
    var vs = workspace.virtualScreenSize; out.vw = vs.width; out.vh = vs.height;
    try { var vg = workspace.virtualScreenGeometry;
          out.vg = {x:vg.x, y:vg.y, width:vg.width, height:vg.height}; } catch(e){}
    var screens = workspace.screens;
    for (var i = 0; i < screens.length; i++) {
        var s = screens[i]; var g = s.geometry;
        var rec = {name:(s.name||null), x:g.x, y:g.y, width:g.width, height:g.height,
                   scale:null, dpr:null};
        try { rec.scale = s.scale; } catch(e){}
        try { rec.dpr = s.devicePixelRatio; } catch(e){}
        out.screens.push(rec);
    }
    var wins = workspace.windowList();
    for (var j = 0; j < wins.length; j++) {
        var w = wins[j]; var fg = w.frameGeometry; var oname = null;
        try { oname = w.output ? w.output.name : null; } catch(e){}
        out.windows.push({cls:w.resourceClass||null, active:w===workspace.activeWindow,
                          minimized:w.minimized, x:fg.x, y:fg.y, width:fg.width,
                          height:fg.height, output:oname});
    }
} catch (e) { out.err = "" + e; }
callDBus("org.familymon.ScreensProbe", "/sink",
         "org.familymon.ScreensProbe", "Push", JSON.stringify(out));
"""


class _Sink(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.familymon.ScreensProbe")
        self.payload: str | None = None
        self.event = asyncio.Event()

    @method()
    def Push(self, json_str: "s"):  # noqa: F821, UP037 (dbus-next signature)
        self.payload = json_str
        self.event.set()


async def _query() -> dict:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    sink = _Sink()
    bus.export("/sink", sink)
    await bus.request_name("org.familymon.ScreensProbe")
    fd, path = tempfile.mkstemp(suffix=".js", prefix="familymon_screens_")
    os.write(fd, _SCRIPT.encode())
    os.close(fd)
    try:
        intro = await bus.introspect("org.kde.KWin", "/Scripting")
        obj = bus.get_proxy_object("org.kde.KWin", "/Scripting", intro)
        scripting = obj.get_interface("org.kde.kwin.Scripting")
        await scripting.call_load_script(path)
        await scripting.call_start()
        await asyncio.wait_for(sink.event.wait(), timeout=3.0)
        return json.loads(sink.payload or "{}")
    finally:
        Path(path).unlink(missing_ok=True)
        bus.disconnect()


def _spectacle_dims() -> tuple[int, int]:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        out = Path(t.name)
    try:
        env = {
            "WAYLAND_DISPLAY": os.environ.get("WAYLAND_DISPLAY", "wayland-0"),
            "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
            "DBUS_SESSION_BUS_ADDRESS": os.environ.get(
                "DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{os.getuid()}/bus"
            ),
            "QT_QPA_PLATFORM": "wayland",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/"),
        }
        subprocess.run(
            ["spectacle", "--background", "--nonotify", "--fullscreen", "--output", str(out)],
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        with Image.open(io.BytesIO(out.read_bytes())) as im:
            return im.size
    finally:
        out.unlink(missing_ok=True)


async def main() -> None:
    info = await _query()
    if info.get("err"):
        print(f"KWin script error: {info['err']}")
    iw, ih = _spectacle_dims()
    print(f"spectacle image:   {iw} x {ih} px")
    print(f"virtualScreenSize: {info.get('vw')} x {info.get('vh')} (logical)")
    print(f"virtualScreenGeom: {info.get('vg')}")
    print(f"outputs ({len(info.get('screens', []))}):")
    for s in info.get("screens", []):
        print(
            f"  {s.get('name')!r}: geom=({s.get('x')},{s.get('y')},"
            f"{s.get('width')},{s.get('height')}) "
            f"scale={s.get('scale')} dpr={s.get('dpr')}"
        )
    print("windows:")
    for w in info.get("windows", []):
        print(
            f"  out={w['output']!r:12} class={w['cls']!r:45} "
            f"rect=({w['x']},{w['y']},{w['width']},{w['height']}) "
            f"active={w['active']} min={w['minimized']}"
        )


if __name__ == "__main__":
    asyncio.run(main())

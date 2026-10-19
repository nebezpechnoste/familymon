"""Prototype + probe for per-window messaging redaction (task #18 A).

Does, in one shot:
  1. Asks KWin (scripting D-Bus) for every window's frameGeometry + the virtual
     screen size (logical coords).
  2. Takes a real fullscreen Spectacle screenshot.
  3. Compares image pixels to logical size to derive the scale factor.
  4. Blacks out the rectangle of every messaging-class window (per the skip list)
     and writes the redacted PNG to /tmp for eyeballing.

Run on Plasma 6 Wayland:
    uv run python scripts/redact_demo.py /tmp/familymon-redact-demo.png

SAFE TO RUN with a messaging window open as long as it shows nothing private —
the point is to confirm the messaging rectangle is blacked and neighbours are
preserved.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.service import ServiceInterface, method
from PIL import Image, ImageDraw

from familymon.capture.messaging_skip import MessagingSkipList

_GEOM_SCRIPT = r"""
var out = {windows: [], vw: 0, vh: 0, err: null};
try {
    var wins = workspace.windowList();
    for (var i = 0; i < wins.length; i++) {
        var w = wins[i];
        var g = w.frameGeometry;
        out.windows.push({
            caption: w.caption || null,
            resourceClass: w.resourceClass || null,
            active: w === workspace.activeWindow,
            minimized: w.minimized,
            x: g.x, y: g.y, width: g.width, height: g.height
        });
    }
} catch (e) {
    out.err = "windows: " + e;
}
try {
    var vs = workspace.virtualScreenSize;
    out.vw = vs.width; out.vh = vs.height;
} catch (e2) {
    try {
        var vg = workspace.virtualScreenGeometry;
        out.vw = vg.width; out.vh = vg.height;
    } catch (e3) {
        out.err = (out.err || "") + " screen: " + e3;
    }
}
callDBus("org.familymon.RedactDemo", "/sink",
         "org.familymon.RedactDemo", "Push", JSON.stringify(out));
"""


class _Sink(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.familymon.RedactDemo")
        self.payload: str | None = None
        self.event = asyncio.Event()

    @method()
    def Push(self, json_str: "s"):  # noqa: N802
        self.payload = json_str
        self.event.set()


async def _windows_and_screen() -> dict:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    sink = _Sink()
    bus.export("/sink", sink)
    await bus.request_name("org.familymon.RedactDemo")
    fd, path = tempfile.mkstemp(suffix=".js", prefix="familymon_geom_")
    os.write(fd, _GEOM_SCRIPT.encode())
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


def _spectacle_png() -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as t:
        out = Path(t.name)
    try:
        import subprocess

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
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


async def main(out_path: str) -> None:
    info = await _windows_and_screen()
    if info.get("err"):
        print(f"KWin script error: {info['err']}")
    png = _spectacle_png()
    img = Image.open(io.BytesIO(png)).convert("RGB")
    iw, ih = img.size
    vw, vh = info.get("vw", 0), info.get("vh", 0)

    print(f"image pixels:      {iw} x {ih}")
    print(f"virtual screen:    {vw} x {vh} (logical)")
    sx = iw / vw if vw else 1.0
    sy = ih / vh if vh else 1.0
    print(f"derived scale:     x={sx:.3f} y={sy:.3f}")

    skip = MessagingSkipList.from_file()
    draw = ImageDraw.Draw(img)
    redacted = 0
    print("windows:")
    for w in info.get("windows", []):
        cls = w.get("resourceClass")
        is_msg = skip.should_skip(cls, w.get("caption"))
        tag = "REDACT" if is_msg else "      "
        print(
            f"  [{tag}] class={cls!r:45} "
            f"rect=({w['x']},{w['y']},{w['width']},{w['height']}) "
            f"min={w['minimized']} active={w['active']}"
        )
        if is_msg and not w["minimized"]:
            x0 = int(w["x"] * sx)
            y0 = int(w["y"] * sy)
            x1 = int((w["x"] + w["width"]) * sx)
            y1 = int((w["y"] + w["height"]) * sy)
            draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))
            redacted += 1

    img.save(out_path)
    print(f"\nredacted {redacted} window(s); wrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/familymon-redact-demo.png"))

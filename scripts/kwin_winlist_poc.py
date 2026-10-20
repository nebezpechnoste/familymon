"""Standalone proof-of-concept: enumerate Plasma 6 Wayland windows.

We register a tiny `org.familymon.WindowSink` D-Bus service. Then we load a KWin
JS script via `org.kde.kwin.Scripting` that iterates `workspace.windowList()` and
calls back into our sink with the JSON payload. Python collects the payload and
prints the windows.

This sidesteps the lack of a direct "list windows" D-Bus method on KWin: window
enumeration goes through KWin's scripting subsystem.

Run on a Plasma 6 Wayland session:

    uv run python scripts/kwin_winlist_poc.py
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile

from dbus_next.aio import MessageBus
from dbus_next.constants import BusType
from dbus_next.service import ServiceInterface, method

KWIN_SCRIPT = r"""
var output = [];
var wins = workspace.windowList();
for (var i = 0; i < wins.length; i++) {
    var w = wins[i];
    output.push({
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
        desktop: w.desktop,
    });
}
callDBus(
    "org.familymon.WindowSink", "/sink",
    "org.familymon.WindowSink", "Push",
    JSON.stringify(output)
);
"""


class WindowSink(ServiceInterface):
    def __init__(self) -> None:
        super().__init__("org.familymon.WindowSink")
        self.payload: str | None = None
        self.event = asyncio.Event()

    @method()
    def Push(self, json_str: s):
        self.payload = json_str
        self.event.set()


async def main() -> None:
    bus = await MessageBus(bus_type=BusType.SESSION).connect()
    sink = WindowSink()
    bus.export("/sink", sink)
    await bus.request_name("org.familymon.WindowSink")

    fd, script_path = tempfile.mkstemp(suffix=".js", prefix="familymon_winlist_")
    os.write(fd, KWIN_SCRIPT.encode())
    os.close(fd)

    try:
        intro = await bus.introspect("org.kde.KWin", "/Scripting")
        obj = bus.get_proxy_object("org.kde.KWin", "/Scripting", intro)
        scripting = obj.get_interface("org.kde.kwin.Scripting")

        script_id = await scripting.call_load_script(script_path)
        print(f"loaded KWin script id={script_id}")
        await scripting.call_start()

        try:
            await asyncio.wait_for(sink.event.wait(), timeout=3.0)
        except TimeoutError:
            print("TIMEOUT — sink never received the payload")
            return

        windows = json.loads(sink.payload or "[]")
        print(f"got {len(windows)} windows")
        for w in windows:
            tag = "*" if w.get("active") else " "
            print(
                f"  {tag} class={w.get('resourceClass')!r}"
                f" title={w.get('caption')!r}"
                f" pid={w.get('pid')}"
            )
    finally:
        try:
            os.unlink(script_path)
        except FileNotFoundError:
            pass
        bus.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

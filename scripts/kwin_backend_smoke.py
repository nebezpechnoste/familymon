"""End-to-end smoke test of the KWinWaylandBackend.

Run on a Plasma 6 Wayland session:

    uv run python scripts/kwin_backend_smoke.py
"""

from __future__ import annotations

import asyncio
import sys

from familymon.capture.kwin_wayland import KWinWaylandBackend


async def main() -> int:
    async with KWinWaylandBackend() as backend:
        result = await backend.snapshot()

    active = result.active_window
    if active:
        print(f"active: class={active.wm_class!r} title={active.title!r} pid={active.pid}")
    else:
        print("active: (none)")
    print(f"other windows: {len(result.other_windows)}")
    for w in result.other_windows[:8]:
        print(f"  - class={w.wm_class!r} title={w.title!r}")

    if result.screenshot_bytes:
        print(f"screenshot: {len(result.screenshot_bytes)} bytes PNG")
    else:
        print(f"screenshot skipped: reason={result.screenshot_skipped_reason}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

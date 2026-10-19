"""Standalone proof-of-concept: take one screenshot via Spectacle.

Why Spectacle subprocess instead of `org.kde.KWin.ScreenShot2` D-Bus directly:
KWin's ScreenShot2 interface enforces caller authorization in Plasma 6 — arbitrary
D-Bus clients get `DBusError: The process is not authorized to take a screenshot`.
Spectacle is internally authorized; calling it as a subprocess sidesteps the
permission system without involving the xdg-desktop-portal consent dialog.

Run on a Plasma 6 Wayland session:

    uv run python scripts/kwin_screenshot_poc.py /tmp/familymon-poc.png
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


async def capture_active_screen(*, active_window_only: bool = False) -> bytes:
    """Spawn spectacle to capture and return PNG bytes."""
    with tempfile.NamedTemporaryFile(
        prefix="familymon_capture_", suffix=".png", delete=False
    ) as tmp:
        out_path = Path(tmp.name)

    try:
        args = [
            "spectacle",
            "--background",
            "--nonotify",
            "--activewindow" if active_window_only else "--fullscreen",
            "--output",
            str(out_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"spectacle exited {proc.returncode}: {stderr.decode(errors='replace')}"
            )
        return out_path.read_bytes()
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass


async def main(out_path: str) -> None:
    data = await capture_active_screen()
    Path(out_path).write_bytes(data)
    print(f"wrote {len(data)} bytes to {out_path}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/familymon-poc.png"
    asyncio.run(main(out))

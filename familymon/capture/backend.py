from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class WindowSnapshot(BaseModel):
    """A single window observed at capture time.

    Distinct from `familymon.analyze.schema.WindowInfo`: that one is the labeling
    contract (frozen, minimal, schema-versioned). This one is the raw capture
    surface, with backend-specific bits (KWin's internal-id UUID, etc.) that
    don't belong in the analysis contract.
    """

    model_config = ConfigDict(frozen=True)

    wm_class: str | None = None
    title: str | None = None
    pid: int | None = None
    exe: str | None = None
    active: bool = False
    minimized: bool = False
    uuid: str | None = None
    # frameGeometry in logical (unscaled) coordinates; 0 when unknown.
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


class CaptureResult(BaseModel):
    """One tick of the capture loop, before any persistence or dedup."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    captured_at: datetime
    active_window: WindowSnapshot | None
    other_windows: tuple[WindowSnapshot, ...] = ()
    screenshot_bytes: bytes | None = None
    screenshot_skipped_reason: str | None = None
    # True when a messaging window was visible this tick (whether redacted or
    # full-skipped). Lets the health check verify the privacy invariant.
    messaging_present: bool = False
    # True when screenshot_bytes is a redacted image (messaging regions blacked).
    screenshot_redacted: bool = False


@runtime_checkable
class CaptureBackend(Protocol):
    """Display-server-specific capture surface.

    Implementations: `KWinWaylandBackend` (Plasma 6 Wayland, primary),
    `X11Backend` (legacy and dev-machine fallback).
    """

    name: str

    async def snapshot(self, *, suppress_screenshot_reason: str | None = None) -> CaptureResult:
        """One full tick: active window, all windows, foreground screenshot.

        Returning a CaptureResult with `screenshot_bytes=None` is valid
        — e.g. when the active window matches the messaging skip list.

        `suppress_screenshot_reason`: when set, the backend skips the
        screenshot and records this reason (e.g. "session_inactive",
        "session_locked"). Window enumeration still happens. The caller (the
        loop, via logind) decides when capturing a screenshot is pointless.
        """
        ...

    async def close(self) -> None:
        """Tear down D-Bus connections / file handles."""
        ...

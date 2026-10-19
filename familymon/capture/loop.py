"""The 60 s capture loop.

Per-tick pipeline:
    backend.snapshot()  →  pHash dedup  →  persist

Screenshot persistence keys on **visual novelty (perceptual hash) alone**, not
on "same active window". A playing video keeps one constant window title while
its content changes every second — a same-window check would wrongly suppress
every frame after the first, and we'd capture almost no video content. The
pHash already collapses a genuinely static screen (identical frames), so the
same-window signal added nothing and broke videos. `same_as_prev` therefore now
means "this screenshot is a perceptual-hash duplicate of a recent one, so it
wasn't persisted".
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from familymon.analyze.dedup import PHashRingBuffer
from familymon.capture.backend import CaptureBackend
from familymon.capture.games import GameLibrary
from familymon.capture.persist import CapturePersistence
from familymon.capture.session_state import SessionState

logger = logging.getLogger(__name__)


@dataclass
class TickOutcome:
    capture_id: int
    same_as_prev: bool
    screenshot_bytes_len: int
    active_wm_class: str | None
    skipped_reason: str | None


async def run_one_tick(
    backend: CaptureBackend,
    persistence: CapturePersistence,
    *,
    phash_buf: PHashRingBuffer,
    session_state: SessionState | None = None,
    gamelib: GameLibrary | None = None,
) -> TickOutcome:
    suppress_reason: str | None = None
    if session_state is not None:
        suppress_reason = (await session_state.status()).suppress_reason()

    result = await backend.snapshot(suppress_screenshot_reason=suppress_reason)

    active = result.active_window
    phash_hex: str | None = None
    phash_dup = False
    if result.screenshot_bytes:
        phash_hex, phash_dup = phash_buf.check_and_add(result.screenshot_bytes)

    # Trusted game detection: is any visible window an installed game? Resolved
    # from window PIDs against the installed-library set — not a forgeable flag.
    game_running = False
    if gamelib is not None:
        windows = [w for w in (active, *result.other_windows) if w is not None]
        game_running = any(gamelib.is_game(w.wm_class, w.pid) for w in windows)

    # Persist the screenshot only when it's visually novel. pHash, not
    # same-window: a video keeps one title but changes content every tick.
    cap_id = persistence.write(
        result, same_as_prev=phash_dup, phash=phash_hex, game_running=game_running
    )

    return TickOutcome(
        capture_id=cap_id,
        same_as_prev=phash_dup,
        screenshot_bytes_len=len(result.screenshot_bytes or b""),
        active_wm_class=active.wm_class if active else None,
        skipped_reason=result.screenshot_skipped_reason,
    )


async def run_capture_loop(
    backend: CaptureBackend,
    persistence: CapturePersistence,
    *,
    interval: float = 60.0,
    phash_buffer_size: int = 8,
    phash_threshold: int = 4,
    session_state: SessionState | None = None,
    gamelib: GameLibrary | None = None,
) -> None:
    phash_buf = PHashRingBuffer(maxlen=phash_buffer_size, threshold=phash_threshold)

    loop = asyncio.get_running_loop()
    while True:
        tick_start = loop.time()
        try:
            outcome = await run_one_tick(
                backend,
                persistence,
                phash_buf=phash_buf,
                session_state=session_state,
                gamelib=gamelib,
            )
            logger.info(
                "tick id=%d active=%r dup=%s shot=%d skipped=%s",
                outcome.capture_id,
                outcome.active_wm_class,
                outcome.same_as_prev,
                outcome.screenshot_bytes_len,
                outcome.skipped_reason,
            )
        except Exception:
            logger.exception("capture tick failed")

        elapsed = loop.time() - tick_start
        await asyncio.sleep(max(0.0, interval - elapsed))

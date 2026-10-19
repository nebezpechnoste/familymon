"""Regression tests for the capture loop's dedup decision.

The bug these guard: a playing video keeps one constant window title, so a
"same active window" dedup check suppressed every frame after the first. Dedup
must key on visual novelty (pHash) only.
"""

from __future__ import annotations

import io
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image, ImageDraw

from familymon.analyze.dedup import PHashRingBuffer
from familymon.capture.backend import CaptureResult, WindowSnapshot
from familymon.capture.loop import run_one_tick


def _png(kind: str) -> bytes:
    img = Image.new("L", (256, 256), 0)
    d = ImageDraw.Draw(img)
    if kind == "vstripes":
        for x in range(0, 256, 16):
            d.rectangle([x, 0, x + 8, 256], fill=255)
    elif kind == "hstripes":
        for y in range(0, 256, 16):
            d.rectangle([0, y, 256, y + 8], fill=255)
    elif kind == "blob":
        d.ellipse([0, 0, 140, 140], fill=255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _result(png: bytes) -> CaptureResult:
    # Same window/title every time — the video case.
    return CaptureResult(
        captured_at=datetime.now(),
        active_window=WindowSnapshot(
            wm_class="firefox_firefox", title="Some Video - YouTube", active=True
        ),
        other_windows=(),
        screenshot_bytes=png,
    )


def _backend_persistence():
    backend = Mock()
    backend.snapshot = AsyncMock()
    persistence = Mock()
    persistence.write = Mock(return_value=1)
    return backend, persistence


@pytest.mark.asyncio
async def test_same_window_distinct_frames_are_not_deduped():
    backend, persistence = _backend_persistence()
    buf = PHashRingBuffer(threshold=4)
    outcomes = []
    for kind in ("vstripes", "hstripes", "blob"):
        backend.snapshot.return_value = _result(_png(kind))
        outcomes.append(await run_one_tick(backend, persistence, phash_buf=buf))
    # Despite the identical window/title, distinct frames must each be saved.
    assert [o.same_as_prev for o in outcomes] == [False, False, False]


@pytest.mark.asyncio
async def test_identical_frames_are_deduped():
    backend, persistence = _backend_persistence()
    buf = PHashRingBuffer(threshold=4)
    backend.snapshot.return_value = _result(_png("vstripes"))
    first = await run_one_tick(backend, persistence, phash_buf=buf)
    second = await run_one_tick(backend, persistence, phash_buf=buf)
    assert first.same_as_prev is False
    assert second.same_as_prev is True


@pytest.mark.asyncio
async def test_game_running_computed_and_persisted():
    backend, persistence = _backend_persistence()
    backend.snapshot.return_value = _result(_png("blob"))
    buf = PHashRingBuffer(threshold=4)
    gamelib = Mock()
    gamelib.is_game = Mock(return_value=True)

    await run_one_tick(backend, persistence, phash_buf=buf, gamelib=gamelib)

    _, kwargs = persistence.write.call_args
    assert kwargs["game_running"] is True


@pytest.mark.asyncio
async def test_no_gamelib_means_game_running_false():
    backend, persistence = _backend_persistence()
    backend.snapshot.return_value = _result(_png("blob"))
    buf = PHashRingBuffer(threshold=4)

    await run_one_tick(backend, persistence, phash_buf=buf)

    _, kwargs = persistence.write.call_args
    assert kwargs["game_running"] is False


@pytest.mark.asyncio
async def test_persist_called_with_phash_based_dedup_flag():
    backend, persistence = _backend_persistence()
    buf = PHashRingBuffer(threshold=4)
    backend.snapshot.return_value = _result(_png("blob"))
    await run_one_tick(backend, persistence, phash_buf=buf)
    # same_as_prev passed to persistence is the pHash-novelty result (False here).
    _, kwargs = persistence.write.call_args
    assert kwargs["same_as_prev"] is False

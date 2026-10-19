"""Unit tests for KWinWaylandBackend's pure-Python logic.

We mock the two I/O seams — `_enumerate_windows()` (KWin Scripting D-Bus) and
`_spectacle()` (subprocess) — and exercise `snapshot()`'s redaction/skip
branching. Redaction math itself is covered by test_redact.py; the full D-Bus
integration by the smoke script.
"""

from __future__ import annotations

import io
import json
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from familymon.capture.backend import WindowSnapshot
from familymon.capture.kwin_wayland import KWinWaylandBackend, _WindowList, _WindowSink
from familymon.capture.messaging_skip import MessagingSkipList


def _skip(*classes: str) -> MessagingSkipList:
    return MessagingSkipList(window_classes=frozenset(classes), title_patterns=())


def _png(w: int = 100, h: int = 100, color=(40, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_backend(skip: MessagingSkipList) -> KWinWaylandBackend:
    """Construct a backend bypassing __aenter__ (no real D-Bus connection)."""
    backend = KWinWaylandBackend(skip_list=skip)
    backend._enumerate_windows = AsyncMock()  # type: ignore[assignment]
    backend._spectacle = AsyncMock()  # type: ignore[assignment]
    return backend


def _winlist(windows, *, logical=(100.0, 100.0), screens=1) -> _WindowList:
    return _WindowList(windows=windows, logical_size=logical, screen_count=screens)


@pytest.mark.asyncio
async def test_messaging_window_is_redacted_not_dropped_single_screen():
    """Single screen: a visible messaging window -> the screenshot is captured
    and the messaging rectangle redacted (not the whole frame dropped)."""
    backend = _make_backend(_skip("telegram-desktop"))
    backend._enumerate_windows.return_value = _winlist(
        [
            WindowSnapshot(
                wm_class="telegram-desktop_telegram-desktop",
                title="news",
                active=False,
                x=0, y=0, width=50, height=100,
            ),
            WindowSnapshot(wm_class="firefox_firefox", title="x", active=True,
                           x=50, y=0, width=50, height=100),
        ],
        logical=(100.0, 100.0),
        screens=1,
    )
    backend._spectacle.return_value = _png(100, 100)

    result = await backend.snapshot()

    backend._spectacle.assert_awaited_once()
    assert result.screenshot_bytes is not None
    assert result.screenshot_redacted is True
    assert result.messaging_present is True
    assert result.screenshot_skipped_reason is None
    # The left half (Telegram) should be black; the right half preserved.
    img = Image.open(io.BytesIO(result.screenshot_bytes)).convert("RGB")
    assert img.getpixel((10, 50)) == (0, 0, 0)
    assert img.getpixel((75, 50)) != (0, 0, 0)


@pytest.mark.asyncio
async def test_messaging_redacts_on_uniform_multi_monitor():
    """Multi-monitor with a uniform image scale (spectacle composites the whole
    logical desktop at one factor) still redacts — screen count alone no longer
    forces a skip."""
    backend = _make_backend(_skip("telegram-desktop"))
    backend._enumerate_windows.return_value = _winlist(
        [
            WindowSnapshot(wm_class="telegram-desktop_telegram-desktop", title="news",
                           active=False, x=0, y=0, width=50, height=100),
            WindowSnapshot(wm_class="firefox_firefox", title="x", active=True,
                           x=50, y=0, width=50, height=100),
        ],
        logical=(100.0, 100.0),
        screens=2,
    )
    backend._spectacle.return_value = _png(100, 100)  # uniform 1x

    result = await backend.snapshot()

    assert result.screenshot_bytes is not None
    assert result.screenshot_redacted is True
    assert result.messaging_present is True


@pytest.mark.asyncio
async def test_messaging_non_uniform_scale_falls_back_to_full_skip():
    """If the image isn't a uniform scaling of the logical layout, we can't map
    rectangles — never leak, drop the whole screenshot."""
    backend = _make_backend(_skip("telegram-desktop"))
    backend._enumerate_windows.return_value = _winlist(
        [
            WindowSnapshot(wm_class="telegram-desktop_telegram-desktop", title="news",
                           active=True, x=0, y=0, width=50, height=50),
        ],
        logical=(100.0, 50.0),  # vs a 100x100 image -> sx=1, sy=2, non-uniform
    )
    backend._spectacle.return_value = _png(100, 100)

    result = await backend.snapshot()

    assert result.screenshot_bytes is None
    assert result.screenshot_skipped_reason == "messaging_app"
    assert result.messaging_present is True
    assert result.screenshot_redacted is False


@pytest.mark.asyncio
async def test_no_messaging_keeps_full_screenshot():
    backend = _make_backend(_skip("telegram-desktop"))
    backend._enumerate_windows.return_value = _winlist(
        [WindowSnapshot(wm_class="firefox_firefox", title="x", active=True,
                        x=0, y=0, width=100, height=100)]
    )
    shot = _png()
    backend._spectacle.return_value = shot

    result = await backend.snapshot()

    assert result.screenshot_bytes == shot
    assert result.screenshot_redacted is False
    assert result.messaging_present is False
    assert result.screenshot_skipped_reason is None


@pytest.mark.asyncio
async def test_minimized_messaging_window_is_not_present():
    """A minimized chat isn't on screen, so it's neither redacted nor counted
    as 'present' — the screenshot is kept whole."""
    backend = _make_backend(_skip("telegram-desktop"))
    backend._enumerate_windows.return_value = _winlist(
        [
            WindowSnapshot(wm_class="firefox_firefox", title="x", active=True,
                           x=0, y=0, width=100, height=100),
            WindowSnapshot(wm_class="telegram-desktop_telegram-desktop", title="news",
                           active=False, minimized=True, x=0, y=0, width=50, height=100),
        ]
    )
    shot = _png()
    backend._spectacle.return_value = shot

    result = await backend.snapshot()
    assert result.screenshot_bytes == shot
    assert result.messaging_present is False


@pytest.mark.asyncio
async def test_suppress_reason_skips_screenshot_entirely():
    backend = _make_backend(_skip("telegram-desktop"))
    backend._enumerate_windows.return_value = _winlist(
        [WindowSnapshot(wm_class="firefox_firefox", title="x", active=True)]
    )
    backend._spectacle.return_value = _png()

    result = await backend.snapshot(suppress_screenshot_reason="session_locked")

    backend._spectacle.assert_not_called()
    assert result.screenshot_bytes is None
    assert result.screenshot_skipped_reason == "session_locked"


@pytest.mark.asyncio
async def test_empty_spectacle_output_means_no_screenshot():
    backend = _make_backend(_skip())
    backend._enumerate_windows.return_value = _winlist(
        [WindowSnapshot(wm_class="firefox_firefox", title="x", active=True)]
    )
    backend._spectacle.return_value = b""

    result = await backend.snapshot()
    assert result.screenshot_bytes is None
    assert result.screenshot_skipped_reason is None


@pytest.mark.asyncio
async def test_window_sink_returns_none_on_timeout():
    sink = _WindowSink()
    result = await sink.wait(timeout=0.05)
    assert result is None


KWIN = ":1.42"


def _payload(nonce: str, **extra) -> str:
    return json.dumps({"nonce": nonce, "windows": [], **extra})


@pytest.mark.asyncio
async def test_window_sink_accepts_kwin_push_with_the_nonce():
    sink = _WindowSink()
    sink.arm("abc123", KWIN)
    assert sink.offer(KWIN, _payload("abc123", vw=10))
    result = await sink.wait(timeout=0.5)
    assert result["vw"] == 10
    assert sink.rejected == 0


@pytest.mark.asyncio
async def test_window_sink_rejects_a_push_from_another_connection():
    """Any session process can call Push; only KWin's connection counts."""
    sink = _WindowSink()
    sink.arm("abc123", KWIN)
    assert not sink.offer(":1.99", _payload("abc123"))
    assert await sink.wait(timeout=0.05) is None
    assert sink.rejected == 1


@pytest.mark.asyncio
async def test_window_sink_rejects_kwin_push_with_a_wrong_nonce():
    """A script the user loaded into KWin themselves passes the sender check
    but doesn't know this tick's nonce."""
    sink = _WindowSink()
    sink.arm("abc123", KWIN)
    assert not sink.offer(KWIN, _payload("guess"))
    assert not sink.offer(KWIN, "not json")
    assert await sink.wait(timeout=0.05) is None
    assert sink.rejected == 2


@pytest.mark.asyncio
async def test_window_sink_counts_a_second_push_as_rejected():
    """The first accepted payload wins; a racing duplicate is tamper evidence."""
    sink = _WindowSink()
    sink.arm("abc123", KWIN)
    assert sink.offer(KWIN, _payload("abc123", vw=1))
    assert not sink.offer(KWIN, _payload("abc123", vw=2))
    assert (await sink.wait(timeout=0.5))["vw"] == 1
    assert sink.rejected == 1


@pytest.mark.asyncio
async def test_window_sink_rejects_everything_before_arm():
    sink = _WindowSink()
    assert not sink.offer(KWIN, _payload("abc123"))
    assert sink.rejected == 1


@pytest.mark.asyncio
async def test_window_sink_arm_drops_a_stale_payload():
    sink = _WindowSink()
    sink.arm("one", KWIN)
    sink.offer(KWIN, _payload("one"))
    sink.arm("two", KWIN)
    assert await sink.wait(timeout=0.05) is None


def test_window_sink_handler_replies_identically_and_ignores_other_calls():
    from dbus_next import Message, MessageType

    sink = _WindowSink()
    sink.arm("abc123", KWIN)
    forged = Message(
        destination="org.familymon.WindowSink", path="/sink",
        interface="org.familymon.WindowSink", member="Push",
        signature="s", body=[_payload("abc123")], serial=7,
    )
    forged.sender = ":1.99"
    reply = sink.handle(forged)
    assert reply is not None and reply.message_type == MessageType.METHOD_RETURN
    assert sink.rejected == 1
    other = Message(
        destination="org.familymon.WindowSink", path="/elsewhere",
        interface="org.familymon.WindowSink", member="Push",
        signature="s", body=["x"], serial=8,
    )
    assert sink.handle(other) is None


def test_rendered_script_carries_the_nonce_and_refuses_odd_ones():
    from familymon.capture.kwin_wayland import render_window_list_script

    assert 'nonce: "abc123"' in render_window_list_script("abc123")
    with pytest.raises(ValueError):
        render_window_list_script('x"; callDBus(')


@pytest.mark.asyncio
async def test_no_screenshot_when_the_window_list_failed():
    """Without KWin's window list a messaging window can't be found, so a
    frame could leak a chat: skip it (this used to save it unredacted)."""
    backend = _make_backend(_skip("telegram-desktop"))
    backend._enumerate_windows.return_value = _WindowList(
        windows=[], logical_size=(0.0, 0.0), screen_count=0, ok=False
    )
    backend._spectacle.return_value = _png()

    result = await backend.snapshot()
    assert result.screenshot_bytes is None
    assert result.window_list_ok is False
    assert result.screenshot_skipped_reason == "window_list_unavailable"
    backend._spectacle.assert_not_called()


def test_parse_marks_windows_on_other_desktops_and_activities_hidden():
    from familymon.capture.kwin_wayland import _parse_window_list

    raw = {
        "currentDesktop": "d2", "currentActivity": "a1", "vw": 1, "vh": 1, "screens": 1,
        "windows": [
            {"caption": "chat", "resourceClass": "telegram-desktop", "desktops": ["d1"],
             "onAllDesktops": False, "activities": ["a1"]},
            {"caption": "here", "resourceClass": "firefox", "desktops": ["d2"],
             "onAllDesktops": False, "activities": ["a1"]},
            {"caption": "everywhere", "resourceClass": "konsole", "desktops": [],
             "onAllDesktops": True, "activities": []},
            {"caption": "other activity", "resourceClass": "slack", "desktops": ["d2"],
             "onAllDesktops": False, "activities": ["a2"]},
            {"caption": "old kwin", "resourceClass": "x"},
        ],
    }
    by = {w.title: w.on_current_desktop for w in _parse_window_list(raw).windows}
    assert by == {"chat": False, "here": True, "everywhere": True,
                  "other activity": False, "old kwin": True}


@pytest.mark.asyncio
async def test_messaging_window_on_another_virtual_desktop_is_not_redacted():
    """A chat parked on another desktop is not on screen: its rectangle shows
    something else there, so blacking it out would hide unrelated content and
    tag the capture messaging_present for nothing."""
    backend = _make_backend(_skip("telegram-desktop"))
    backend._enumerate_windows.return_value = _winlist(
        [
            WindowSnapshot(wm_class="telegram-desktop_telegram-desktop", title="news",
                           on_current_desktop=False, x=0, y=0, width=50, height=100),
            WindowSnapshot(wm_class="firefox_firefox", title="x", active=True,
                           x=50, y=0, width=50, height=100),
        ]
    )
    png = _png()
    backend._spectacle.return_value = png

    result = await backend.snapshot()
    assert result.screenshot_bytes == png
    assert result.messaging_present is False
    assert result.screenshot_redacted is False

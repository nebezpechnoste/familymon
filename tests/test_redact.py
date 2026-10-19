import io

from PIL import Image

from familymon.capture.backend import WindowSnapshot
from familymon.capture.messaging_skip import MessagingSkipList
from familymon.capture.redact import redact_messaging_regions


def _skip(*classes: str) -> MessagingSkipList:
    return MessagingSkipList(window_classes=frozenset(classes), title_patterns=())


def _png(w: int, h: int, color=(50, 50, 50)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _msg_win(x, y, w, h, *, minimized=False):
    return WindowSnapshot(
        wm_class="telegram-desktop_telegram-desktop",
        title="news",
        x=x, y=y, width=w, height=h,
        minimized=minimized,
    )


def test_redacts_messaging_rect_at_unit_scale():
    png = _png(100, 100)
    out = redact_messaging_regions(png, [_msg_win(0, 0, 40, 100)], (100.0, 100.0), _skip("telegram-desktop"))
    assert out is not None
    data, any_redacted = out
    assert any_redacted is True
    img = Image.open(io.BytesIO(data)).convert("RGB")
    assert img.getpixel((10, 50)) == (0, 0, 0)       # inside the chat rect
    assert img.getpixel((70, 50)) == (50, 50, 50)    # outside it, preserved


def test_handles_uniform_hidpi_scale():
    # logical 50x50, image 100x100 -> uniform scale 2x. A logical (0,0,25,50)
    # rect must black the left half of the 100px image. This is also the
    # multi-monitor case: spectacle gives a uniform image=virtual*K scaling.
    png = _png(100, 100)
    out = redact_messaging_regions(png, [_msg_win(0, 0, 25, 50)], (50.0, 50.0), _skip("telegram-desktop"))
    assert out is not None
    img = Image.open(io.BytesIO(out[0])).convert("RGB")
    assert img.getpixel((10, 50)) == (0, 0, 0)
    assert img.getpixel((75, 50)) == (50, 50, 50)


def test_non_uniform_scale_returns_none():
    # image 100x100 but logical 100x50 -> sx=1.0, sy=2.0 (non-uniform). We can't
    # map with a single factor, so bail to a full skip rather than misplace.
    png = _png(100, 100)
    assert (
        redact_messaging_regions(png, [_msg_win(0, 0, 40, 50)], (100.0, 50.0), _skip("telegram-desktop"))
        is None
    )


def test_invalid_logical_size_returns_none():
    png = _png(100, 100)
    assert redact_messaging_regions(png, [_msg_win(0, 0, 40, 100)], (0.0, 0.0), _skip("telegram-desktop")) is None


def test_no_messaging_returns_original_unchanged():
    png = _png(100, 100)
    out = redact_messaging_regions(png, [_msg_win(0, 0, 40, 100)], (100.0, 100.0), _skip("discord"))
    assert out == (png, False)


def test_minimized_messaging_not_redacted():
    png = _png(100, 100)
    out = redact_messaging_regions(
        png, [_msg_win(0, 0, 40, 100, minimized=True)], (100.0, 100.0), _skip("telegram-desktop")
    )
    assert out == (png, False)


def test_zero_geometry_messaging_forces_full_skip():
    # A visible messaging window whose geometry KWin couldn't report (width or
    # height 0) must NOT yield an unredacted image — the privacy invariant
    # demands a full skip (None) so the caller drops the screenshot entirely.
    png = _png(100, 100)
    assert (
        redact_messaging_regions(
            png, [_msg_win(0, 0, 0, 100)], (100.0, 100.0), _skip("telegram-desktop")
        )
        is None
    )
    assert (
        redact_messaging_regions(
            png, [_msg_win(0, 0, 40, 0)], (100.0, 100.0), _skip("telegram-desktop")
        )
        is None
    )


def test_mixed_valid_and_zero_geometry_messaging_forces_full_skip():
    # One mappable messaging window + one with unknown geometry must still force
    # a full skip, not a partial redaction that leaves the second chat visible.
    png = _png(100, 100)
    windows = [_msg_win(0, 0, 40, 100), _msg_win(50, 0, 0, 100)]
    assert (
        redact_messaging_regions(png, windows, (100.0, 100.0), _skip("telegram-desktop"))
        is None
    )


def test_messaging_window_on_another_desktop_is_left_alone():
    png = _png(100, 100)
    parked = _msg_win(0, 0, 40, 100).model_copy(update={"on_current_desktop": False})
    out = redact_messaging_regions(png, [parked], (100.0, 100.0), _skip("telegram-desktop"))
    assert out == (png, False)

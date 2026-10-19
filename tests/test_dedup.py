import io

from PIL import Image, ImageDraw

from familymon.analyze.dedup import PHashRingBuffer


def _png_vertical_stripes(size: int = 256) -> bytes:
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    for x in range(0, size, 16):
        draw.rectangle([x, 0, x + 8, size], fill=255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_horizontal_stripes(size: int = 256) -> bytes:
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    for y in range(0, size, 16):
        draw.rectangle([0, y, size, y + 8], fill=255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_corner_blob(size: int = 256) -> bytes:
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size // 2, size // 2], fill=255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_identical_image_is_duplicate():
    buf = PHashRingBuffer(threshold=4)
    a = _png_vertical_stripes()
    _, dup1 = buf.check_and_add(a)
    _, dup2 = buf.check_and_add(a)
    assert dup1 is False
    assert dup2 is True


def test_distinct_image_is_not_duplicate():
    buf = PHashRingBuffer(threshold=4)
    _, dup1 = buf.check_and_add(_png_vertical_stripes())
    _, dup2 = buf.check_and_add(_png_corner_blob())
    assert dup1 is False
    assert dup2 is False


def test_ring_buffer_evicts_oldest():
    buf = PHashRingBuffer(maxlen=2, threshold=4)
    a = _png_vertical_stripes()
    b = _png_horizontal_stripes()
    c = _png_corner_blob()

    buf.check_and_add(a)
    buf.check_and_add(b)
    buf.check_and_add(c)  # evicts a
    assert len(buf) == 2
    # `a` is no longer in the buffer; presenting it again should NOT match
    _, dup = buf.check_and_add(a)
    assert dup is False

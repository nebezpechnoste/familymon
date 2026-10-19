"""Black out messaging-window regions in a fullscreen screenshot.

Privacy-by-construction: we locate the regions to hide from the window
manager's geometry — never by analysing the pixels — so a chat's content is
never inspected, only its rectangle.

Returns None when coordinates can't be mapped safely (the caller then falls
back to a full screenshot skip — we never persist an unredacted messaging
window).

Coordinate model: Spectacle renders the whole logical desktop into one image at
a single uniform scale, even across mixed-DPI outputs — verified on a stacked
1.3x laptop + 1.0x external, which produced an image exactly 2x the logical
virtual-screen size in both axes. So a single `scale = image_px / logical_px`
maps every window correctly regardless of monitor count. We guard this with a
uniform-scale check (sx == sy within tolerance); if Spectacle ever produces a
non-uniform composite, we fall back to a full skip rather than misplace a
redaction rectangle.

Scope (task #18 A):
  - Bounding-box redaction. If another window overlaps a messaging window, the
    overlap is blacked too (safe over-redaction); z-order-aware partial
    redaction is possible but deferred.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageDraw

from .backend import WindowSnapshot
from .messaging_skip import MessagingSkipList

logger = logging.getLogger(__name__)


# Max allowed relative difference between the horizontal and vertical scale
# factors before we treat the image as a non-uniform composite and bail.
_SCALE_TOLERANCE = 0.01


def redact_messaging_regions(
    png_bytes: bytes,
    windows: list[WindowSnapshot],
    logical_size: tuple[float, float],
    skip_list: MessagingSkipList,
) -> tuple[bytes, bool] | None:
    """Return (png, any_redacted), or None if redaction can't be done safely.

    None means "fall back to a full skip" — the caller must not persist the
    original image, since it may contain unredacted messaging content.
    """
    vw, vh = logical_size
    if vw <= 0 or vh <= 0:
        return None

    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        logger.warning("redact: could not open screenshot: %s", e)
        return None

    iw, ih = img.size
    sx = iw / vw
    sy = ih / vh

    # Spectacle composites the logical desktop at one uniform scale (even with
    # mixed-DPI outputs). If sx and sy disagree, the image is NOT a uniform
    # scaling of the logical layout and our single-factor mapping would misplace
    # rectangles — never leak, fall back to a full skip.
    if max(sx, sy) > 0 and abs(sx - sy) / max(sx, sy) > _SCALE_TOLERANCE:
        logger.warning(
            "redact: non-uniform scale (sx=%.4f sy=%.4f); falling back to skip", sx, sy
        )
        return None

    # Separate "a messaging window is visible" from "we have geometry to hide
    # it". If any visible messaging window lacks usable geometry (KWin reported
    # 0/unknown), we cannot guarantee its pixels are covered, so fall back to a
    # full skip (None) rather than risk persisting an unredacted chat. Silently
    # dropping such a window from the redaction set — as an earlier version did
    # via a `width > 0 and height > 0` filter — leaked it whenever it was the
    # only messaging window (empty set -> original image returned) or sat
    # alongside a second, mappable one (partial redaction returned as success).
    messaging = [
        w
        for w in windows
        if not w.hidden and skip_list.should_skip(w.wm_class, w.title)
    ]
    if not messaging:
        # The caller only invokes us when a messaging window is present, but
        # stay defensive: nothing to hide, return the image unchanged.
        return png_bytes, False
    if any(w.width <= 0 or w.height <= 0 for w in messaging):
        logger.warning(
            "redact: a visible messaging window has no usable geometry; "
            "falling back to a full screenshot skip"
        )
        return None

    draw = ImageDraw.Draw(img)
    for w in messaging:
        x0 = max(0, int(w.x * sx))
        y0 = max(0, int(w.y * sy))
        x1 = min(iw, int((w.x + w.width) * sx))
        y1 = min(ih, int((w.y + w.height) * sy))
        if x1 > x0 and y1 > y0:
            draw.rectangle([x0, y0, x1, y1], fill=(0, 0, 0))

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), True

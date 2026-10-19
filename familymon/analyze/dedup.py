"""Perceptual-hash ring buffer for screenshot deduplication.

A `pHash` distance under a small threshold (default 4) means the two screenshots
are visually indistinguishable for our purposes — the kid hasn't scrolled, the
animation hasn't changed, the same article is on screen, etc. We tag the new
capture as `same_as_prev` and skip persisting the screenshot to disk.
"""

from __future__ import annotations

import collections
import io

import imagehash
from PIL import Image


class PHashRingBuffer:
    def __init__(self, *, maxlen: int = 8, threshold: int = 4) -> None:
        self._buf: collections.deque[imagehash.ImageHash] = collections.deque(maxlen=maxlen)
        self._threshold = threshold

    def check_and_add(self, png_bytes: bytes) -> tuple[str, bool]:
        """Compute pHash for the PNG; return (hex_hash, near_duplicate_of_recent)."""
        with Image.open(io.BytesIO(png_bytes)) as img:
            ph = imagehash.phash(img)
        is_dup = any((ph - prev) < self._threshold for prev in self._buf)
        self._buf.append(ph)
        return str(ph), is_dup

    def __len__(self) -> int:
        return len(self._buf)

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from familymon.analyze.categories import Category

# Sources include not-yet-built phases (ocr_kw, local_vlm, claude_code_bootstrap).
# "same_window" was dropped: same-window dedup was abandoned in favour of pHash
# (see capture/loop.py), so no code ever emits it.
ClassificationSource = Literal[
    "phash",
    "heuristic",
    "domain",
    "game_process",
    "ocr_kw",
    "local_vlm",
    "claude_code_bootstrap",
]


class WindowInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    wm_class: str | None = None
    title: str | None = None
    pid: int | None = None
    exe: str | None = None
    active: bool = False


class Capture(BaseModel):
    capture_id: int
    captured_at: datetime
    active_window: WindowInfo
    other_windows: list[WindowInfo] = Field(default_factory=list)
    screenshot_path: Path | None = None
    screenshot_phash: str | None = None
    screenshot_skipped_reason: str | None = None


class Classification(BaseModel):
    capture_id: int
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_text: str = ""
    source: ClassificationSource
    schema_version: int = 1
    # All distinct categories present among visible windows this tick, ordered
    # by priority (so present_categories[0] == category). The "primary +
    # also-present" half of the hybrid model; overlap-aware time stats are still
    # deferred. Always contains the primary category at minimum.
    present_categories: list[Category] = Field(default_factory=list)

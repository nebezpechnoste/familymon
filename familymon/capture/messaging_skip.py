"""Messaging-app skip/redact list.

Identifies messaging-app windows (by window class or title pattern) whose
content must never be persisted as image bytes. ANY visible messaging window
counts — not just the focused one. The capture backend blacks out each such
window's region in the screenshot (redaction), falling back to skipping the
whole screenshot when a region can't be located safely. Window metadata is
always recorded; the teen's private chats never hit disk as image bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MessagingSkipList:
    window_classes: frozenset[str]
    title_patterns: tuple[re.Pattern[str], ...]

    @classmethod
    def from_file(cls, path: Path | None = None) -> MessagingSkipList:
        # Resolved at call time so /etc/familymon and FAMILYMON_CONFIG_DIR
        # overrides are honored — this file is the privacy contract the parent
        # (and teen) edit on the deployed machine.
        from familymon.config import config_file

        path = path or config_file("messaging_apps.yaml")
        raw = yaml.safe_load(path.read_text())
        classes = frozenset((c or "").strip().lower() for c in raw.get("window_classes") or [])
        patterns = tuple(re.compile(p, re.IGNORECASE) for p in raw.get("title_patterns") or [])
        return cls(window_classes=classes, title_patterns=patterns)

    def should_skip(self, wm_class: str | None, title: str | None) -> bool:
        if wm_class:
            # Snap-packaged apps report classes like "telegram-desktop_telegram-desktop".
            # Flatpak/native typically use a clean class or reverse-DNS form.
            # Match the whole class OR any underscore-separated segment, all
            # case-insensitive — covers all three packaging conventions.
            normalized = wm_class.strip().lower()
            candidates = {normalized, *normalized.split("_")}
            if candidates & self.window_classes:
                return True
        if title:
            return any(p.search(title) for p in self.title_patterns)
        return False

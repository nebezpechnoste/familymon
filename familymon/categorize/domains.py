"""Domain → Category lookup.

Loaded from a YAML file (one bundled starter list ships in config/, more to be
imported in phase 3 from Toulouse blacklists + v2fly/domain-list-community).

Matching is apex-aware: observing `m.youtube.com` matches the entry
`youtube.com` because each entry is treated as an apex against which we check
every suffix of the observed hostname.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from familymon.analyze.categories import Category


def _suffix_apexes(host: str) -> list[str]:
    """All apex-form suffixes of a hostname.

    `m.youtube.com` → `['m.youtube.com', 'youtube.com', 'com']`. Used for
    matching against entries: an entry `youtube.com` matches any of those.
    """
    host = host.strip().lower().rstrip(".")
    parts = host.split(".") if host else []
    return [".".join(parts[i:]) for i in range(len(parts))]


class DomainCategoryLookup:
    """Domain → Category, with apex-suffix matching."""

    def __init__(self) -> None:
        # domain (lowercased) → Category
        self._table: dict[str, Category] = {}

    def __len__(self) -> int:
        return len(self._table)

    def add(self, domain: str, category: Category) -> None:
        normalized = domain.strip().lower().rstrip(".")
        if normalized:
            self._table.setdefault(normalized, category)

    def lookup(self, host: str | None) -> Category | None:
        if not host:
            return None
        for apex in _suffix_apexes(host):
            cat = self._table.get(apex)
            if cat is not None:
                return cat
        return None

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> DomainCategoryLookup:
        from familymon.config import config_file

        path = path or config_file("domains_starter.yaml")
        raw = yaml.safe_load(path.read_text()) or {}
        lookup = cls()
        for cat_name, domains in raw.items():
            try:
                cat = Category(cat_name)
            except ValueError:
                # Unknown category in YAML — skip silently to allow user-added
                # entries in YAMLs we haven't taught the enum about yet.
                continue
            for d in domains or []:
                lookup.add(d, cat)
        return lookup

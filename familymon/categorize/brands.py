"""Brand-substring → Category lookup for browser window titles.

A stopgap until real URL extraction (DNS log + AT-SPI URL bar) lands in a later
phase. Browser titles usually carry the site brand even when they don't carry
the domain.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from familymon.analyze.categories import Category



class BrandTitleLookup:
    def __init__(self) -> None:
        # Ordered list of (lowercase substring, category); first match wins.
        self._brands: list[tuple[str, Category]] = []

    def __len__(self) -> int:
        return len(self._brands)

    def add(self, brand: str, category: Category) -> None:
        b = brand.strip().lower()
        if b:
            self._brands.append((b, category))

    def lookup(self, title: str | None) -> tuple[Category, str] | None:
        if not title:
            return None
        t = title.lower()
        for brand, category in self._brands:
            if brand in t:
                return category, brand
        return None

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> "BrandTitleLookup":
        # Resolved at call time so /etc/familymon and FAMILYMON_CONFIG_DIR
        # overrides are honored (see familymon.config.config_file).
        from familymon.config import config_file

        path = path or config_file("title_brands.yaml")
        raw = yaml.safe_load(path.read_text()) or {}
        lookup = cls()
        for cat_name, brands in raw.items():
            try:
                cat = Category(cat_name)
            except ValueError:
                continue
            for b in brands or []:
                lookup.add(b, cat)
        return lookup

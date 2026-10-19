"""Scan FreeDesktop `.desktop` files and build a wm_class → Category lookup.

Why multiple keys per entry:
  StartupWMClass is the canonical match key but only ~8% of .desktop files set
  it (227 system entries on a stock Plasma 6; 19 had StartupWMClass). Worse,
  snap-packaged Telegram declares StartupWMClass=TelegramDesktop but the
  runtime resourceClass is `telegram-desktop_telegram-desktop` — declared is
  stale. So for each .desktop we register all of {StartupWMClass, lowercase
  Name, basename(Exec)} as lookup keys mapping to the same Category.

Lookup at runtime:
  Normalize the observed wm_class to lowercase, split on `_` to handle
  snap-style `name_binary` packaging, and try every candidate against the
  table. First hit wins.
"""

from __future__ import annotations

import logging
import os
import re
from configparser import ConfigParser, Error as ConfigParserError
from dataclasses import dataclass
from pathlib import Path

from familymon.analyze.categories import Category

from .freedesktop import freedesktop_to_familymon

logger = logging.getLogger(__name__)


# Only system/root-owned application dirs are scanned. The per-user dir
# (~/.local/share/applications) is deliberately EXCLUDED: its .desktop files are
# writable by the monitored user, and `Categories=` from there is a monitoring
# signal the teen could forge (e.g. `Categories=Education;` on a game) to dodge
# distraction/conflict flagging — see the trust-resistant-signals rule. Apps
# that exist only in user space fall through to UNKNOWN (still shown on the
# timeline, just not auto-categorised); games are detected separately from the
# installed Steam library, not from these Categories. A parent-curated override
# can be added later via the explicit `dirs=` argument if needed.
DEFAULT_APPLICATION_DIRS: tuple[Path, ...] = (
    Path("/usr/share/applications"),
    Path("/usr/local/share/applications"),
    Path("/var/lib/flatpak/exports/share/applications"),
    Path("/var/lib/snapd/desktop/applications"),
)


_EXEC_ARG_RE = re.compile(r"\s+%[a-zA-Z]")


@dataclass(frozen=True)
class DesktopEntry:
    name: str | None
    startup_wm_class: str | None
    exec_command: str | None
    categories: tuple[str, ...]
    # The .desktop file's own id (filename stem, e.g. "org.vinegarhq.Sober").
    # On Wayland this IS the app_id KWin reports for flatpak and most modern
    # apps — a Roblox (Sober) window came through as "org.vinegarhq.Sober" and
    # matched nothing because only Name/Exec/StartupWMClass were indexed.
    desktop_id: str | None = None


def _parse_desktop_file(path: Path) -> DesktopEntry | None:
    parser = ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
    except (UnicodeDecodeError, ConfigParserError) as e:
        logger.debug("skipping unparseable %s: %s", path, e)
        return None

    if "Desktop Entry" not in parser:
        return None
    entry = parser["Desktop Entry"]

    if entry.get("NoDisplay", "false").strip().lower() == "true":
        return None
    if entry.get("Type", "Application").strip() != "Application":
        return None

    cats_raw = entry.get("Categories", "")
    cats = tuple(c for c in (s.strip() for s in cats_raw.split(";")) if c)

    return DesktopEntry(
        name=entry.get("Name") or None,
        startup_wm_class=entry.get("StartupWMClass") or None,
        exec_command=entry.get("Exec") or None,
        categories=cats,
        desktop_id=path.stem or None,
    )


def _exec_basename(exec_command: str | None) -> str | None:
    """Pull the binary name out of an Exec= line, e.g. `/snap/bin/telegram-desktop %U`
    -> `telegram-desktop`. Strips both path and FreeDesktop `%U`/`%f`/etc."""
    if not exec_command:
        return None
    cleaned = _EXEC_ARG_RE.sub("", exec_command).strip()
    if not cleaned:
        return None
    first_token = cleaned.split()[0]
    base = os.path.basename(first_token)
    return base or None


def _keys_for_entry(entry: DesktopEntry) -> set[str]:
    """All lookup keys to register for this .desktop entry, lowercased."""
    keys: set[str] = set()
    if entry.desktop_id:
        keys.add(entry.desktop_id.strip().lower())
    if entry.startup_wm_class:
        keys.add(entry.startup_wm_class.strip().lower())
    if entry.name:
        keys.add(entry.name.strip().lower())
    exec_base = _exec_basename(entry.exec_command)
    if exec_base:
        keys.add(exec_base.lower())
    return {k for k in keys if k}


class AppCategoryLookup:
    """Map an observed wm_class to a familymon Category.

    Built by scanning system (root-owned) .desktop directories only; see the
    DEFAULT_APPLICATION_DIRS note for why user-writable dirs are excluded.
    """

    def __init__(self) -> None:
        self._table: dict[str, Category] = {}

    def __len__(self) -> int:
        return len(self._table)

    def add(self, key: str, category: Category) -> None:
        normalized = key.strip().lower()
        if not normalized:
            return
        # First registration wins — later .desktop files don't override. All
        # scanned dirs are system/root-owned, so collisions are rare and
        # consistent (and never a user-forged override of a system entry).
        self._table.setdefault(normalized, category)

    def add_from_desktop(self, path: Path) -> None:
        entry = _parse_desktop_file(path)
        if entry is None or not entry.categories:
            return
        category = freedesktop_to_familymon(list(entry.categories))
        if category is None:
            return
        for key in _keys_for_entry(entry):
            self.add(key, category)

    def lookup(self, wm_class: str | None) -> Category | None:
        if not wm_class:
            return None
        normalized = wm_class.strip().lower()
        candidates: list[str] = [normalized, *normalized.split("_")]
        for c in candidates:
            cat = self._table.get(c)
            if cat is not None:
                return cat
        return None

    @classmethod
    def from_dirs(cls, dirs: list[Path] | None = None) -> "AppCategoryLookup":
        lookup = cls()
        for directory in dirs if dirs is not None else DEFAULT_APPLICATION_DIRS:
            if not directory.is_dir():
                continue
            for entry_path in directory.glob("*.desktop"):
                lookup.add_from_desktop(entry_path)
        # XDG_DATA_DIRS may include additional system app roots. Skip any entry
        # under the user's home: XDG_DATA_DIRS is user-settable, so a home-rooted
        # path is another forgeable category source (see DEFAULT_APPLICATION_DIRS).
        home = Path.home()
        for extra in (os.environ.get("XDG_DATA_DIRS", "") or "").split(":"):
            if not extra:
                continue
            d = Path(extra) / "applications"
            if home == d or home in d.parents:
                continue
            if d.is_dir():
                for entry_path in d.glob("*.desktop"):
                    lookup.add_from_desktop(entry_path)
        return lookup

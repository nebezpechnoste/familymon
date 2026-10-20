"""Trusted game detection from installed game libraries.

Replaces GameMode (a user-forgeable opt-in) with a signal the monitored user
can't trivially fake: what games are actually *installed*. We scan the Steam
library for installed titles and match running window processes against their
install directories, plus a parent-curated list for non-Steam games.

Trust model: only directories that have a real `appmanifest_*.acf` count, so the
teen can't `mkdir steamapps/common/fakegame` and run anything from it — they'd
have to actually install a game (or forge a manifest, a far higher bar than
`gamemoderun firefox`).

Detection ties a *window* to a game via its process: a window's `/proc/<pid>/exe`
or `cmdline` referencing an installed-game dir flags it — this catches native
games (exe under common/<dir>/) and Proton games (the wine process's cmdline
points at the game .exe under common/<dir>/).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


_STEAM_ROOT_CANDIDATES = (
    "~/.steam/steam",
    "~/.steam/root",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
)

_PATH_RE = re.compile(r'"path"\s*"([^"]+)"')
_INSTALLDIR_RE = re.compile(r'"installdir"\s*"([^"]+)"', re.IGNORECASE)
_NAME_RE = re.compile(r'"name"\s*"([^"]+)"', re.IGNORECASE)

# Steam "games" that are really tools/runtimes — exclude so we don't flag the
# Proton runtime itself as a game (it co-runs with real games anyway).
_TOOL_HINTS = (
    "proton",
    "steam linux runtime",
    "steamworks shared",
    "steam controller",
)


def _steam_roots(home: Path | None = None) -> list[Path]:
    """Steam install roots under `home` (default: the current user's).

    The enforcement driver runs as root but must scan the *monitored* user's
    library — root has no Steam, so without `home` it would find nothing and
    the generated PlayTime masks would silently cover zero games.
    """
    base = home if home is not None else Path.home()
    roots = []
    for cand in _STEAM_ROOT_CANDIDATES:
        p = base / cand.removeprefix("~/")
        if (p / "steamapps").is_dir():
            roots.append(p)
    return roots


def _library_dirs(steam_root: Path) -> list[Path]:
    """Library steamapps dirs from libraryfolders.vdf (plus the root's own)."""
    libs = [steam_root / "steamapps"]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return libs
    for path in _PATH_RE.findall(text):
        sapps = Path(path) / "steamapps"
        if sapps.is_dir() and sapps not in libs:
            libs.append(sapps)
    return libs


def _is_tool(name: str, installdir: str) -> bool:
    blob = f"{name} {installdir}".lower()
    return any(h in blob for h in _TOOL_HINTS)


def _scan_steam_game_dirs(home: Path | None = None) -> set[str]:
    dirs: set[str] = set()
    for root in _steam_roots(home):
        for sapps in _library_dirs(root):
            for acf in sapps.glob("appmanifest_*.acf"):
                try:
                    text = acf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                m = _INSTALLDIR_RE.search(text)
                if not m:
                    continue
                installdir = m.group(1)
                name_m = _NAME_RE.search(text)
                name = name_m.group(1) if name_m else ""
                if _is_tool(name, installdir):
                    continue
                game_dir = sapps / "common" / installdir
                if game_dir.is_dir():
                    dirs.add(str(game_dir.resolve()))
    return dirs


def _process_path_tokens(pid: int) -> list[str]:
    """Executable path + cmdline argv for a pid, for substring/prefix matching."""
    tokens: list[str] = []
    try:
        tokens.append(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            tokens.extend(
                a for a in f.read().decode("utf-8", "replace").split("\0") if a
            )
    except OSError:
        pass
    return tokens


def load_known_games(path: Path) -> set[str]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    return {str(d) for d in raw if isinstance(d, str)} if isinstance(raw, list) else set()


def save_known_games(path: Path, dirs: set[str]) -> None:
    from familymon.storage.perms import secure

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(sorted(dirs), indent=1))
    secure(tmp)
    tmp.replace(path)


def remember_games(path: Path, scanned: set[str]) -> set[str]:
    """Union of every game dir ever seen installed and the current scan.

    The Steam library lives in the monitored user's home, so they could hide
    a game from the scan (delete its appmanifest, move the library) and with
    it the game's PlayTime mask. Remembered dirs keep the mask until the
    parent removes it (`familymonctl enforce games --forget`)."""
    known = load_known_games(path)
    merged = known | scanned
    if merged != known:
        save_known_games(path, merged)
        for d in sorted(merged - known):
            logger.info("game library: remembering %s", d)
    return merged


@dataclass(frozen=True)
class GameLibrary:
    game_dirs: frozenset[str]
    curated_classes: frozenset[str]
    curated_binaries: frozenset[str]

    @classmethod
    def from_system(
        cls,
        curated_path: Path | None = None,
        home: Path | None = None,
        known_path: Path | None = None,
    ) -> GameLibrary:
        """Scan the Steam library under `home` (default: current user's) plus
        the curated list. Pass the monitored user's home when running as root,
        and `known_path` to keep every game ever seen (remember_games)."""
        from familymon.config import config_file

        curated_path = curated_path or config_file("game_apps.yaml")
        try:
            raw = yaml.safe_load(curated_path.read_text()) or {}
        except OSError:
            raw = {}
        classes = frozenset(
            (c or "").strip().lower() for c in raw.get("window_classes") or []
        )
        binaries = frozenset(
            (b or "").strip().lower() for b in raw.get("binaries") or []
        )
        dirs = _scan_steam_game_dirs(home)
        if known_path is not None:
            dirs = remember_games(known_path, dirs)
        logger.info(
            "game library: %d installed Steam game dir(s), %d curated class(es)",
            len(dirs),
            len(classes),
        )
        return cls(frozenset(dirs), classes, binaries)

    def is_game(self, wm_class: str | None, pid: int | None) -> bool:
        if wm_class:
            normalized = wm_class.strip().lower()
            candidates = {normalized, *normalized.split("_")}
            if candidates & self.curated_classes:
                return True
        if pid:
            tokens = _process_path_tokens(pid)
            for tok in tokens:
                base = os.path.basename(tok).lower()
                if base in self.curated_binaries:
                    return True
                for game_dir in self.game_dirs:
                    if tok.startswith(game_dir):
                        return True
        return False

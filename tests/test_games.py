from __future__ import annotations

from pathlib import Path

import pytest

from familymon.capture import games
from familymon.capture.games import GameLibrary


def _make_steam(tmp_path: Path) -> Path:
    root = tmp_path / "steam"
    sapps = root / "steamapps"
    (sapps / "common" / "CoolGame").mkdir(parents=True)
    (sapps / "common" / "Proton 9.0").mkdir(parents=True)
    (sapps / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n  "0"\n  {\n    "path"\t\t"' + str(root) + '"\n  }\n}\n'
    )
    (sapps / "appmanifest_111.acf").write_text(
        '"AppState"\n{\n  "appid"\t"111"\n  "name"\t"Cool Game"\n'
        '  "installdir"\t"CoolGame"\n}\n'
    )
    # A tool (Proton) — must be excluded from the trusted game set.
    (sapps / "appmanifest_222.acf").write_text(
        '"AppState"\n{\n  "name"\t"Proton 9.0"\n  "installdir"\t"Proton 9.0"\n}\n'
    )
    return root


def test_scan_finds_installed_games_and_excludes_tools(tmp_path, monkeypatch):
    root = _make_steam(tmp_path)
    monkeypatch.setattr(games, "_steam_roots", lambda home=None: [root])
    dirs = games._scan_steam_game_dirs()
    cool = str((root / "steamapps" / "common" / "CoolGame").resolve())
    assert cool in dirs
    assert not any("Proton" in d for d in dirs)


def test_from_system_scans_the_given_users_home(tmp_path):
    """The enforcement driver runs as root but must scan the monitored user's
    library. A real Steam layout under an explicit `home` is found; a
    different home (root's, in production) finds nothing."""
    kid_home = tmp_path / "kid"
    steam = kid_home / ".local" / "share" / "Steam"
    sapps = steam / "steamapps"
    (sapps / "common" / "Among Us").mkdir(parents=True)
    (sapps / "appmanifest_945360.acf").write_text(
        '"AppState"\n{\n  "name"\t"Among Us"\n  "installdir"\t"Among Us"\n}\n'
    )
    other_home = tmp_path / "root"
    other_home.mkdir()

    found = GameLibrary.from_system(home=kid_home)
    assert any(d.endswith("/common/Among Us") for d in found.game_dirs)

    empty = GameLibrary.from_system(home=other_home)
    assert empty.game_dirs == frozenset()


def test_is_game_by_curated_class():
    lib = GameLibrary(frozenset(), frozenset({"minecraft"}), frozenset())
    assert lib.is_game("minecraft", None)
    assert lib.is_game("Minecraft_minecraft", None)  # snap-style segment match
    assert not lib.is_game("firefox_firefox", None)


def test_is_game_by_curated_binary(monkeypatch):
    lib = GameLibrary(frozenset(), frozenset(), frozenset({"retroarch"}))
    monkeypatch.setattr(games, "_process_path_tokens", lambda pid: ["/usr/bin/retroarch", "--x"])
    assert lib.is_game(None, 1234)


def test_is_game_by_native_exe_under_game_dir(monkeypatch):
    gd = "/home/u/.steam/steam/steamapps/common/CoolGame"
    lib = GameLibrary(frozenset({gd}), frozenset(), frozenset())
    monkeypatch.setattr(games, "_process_path_tokens", lambda pid: [gd + "/bin/coolgame"])
    assert lib.is_game(None, 999)


def test_is_game_proton_via_cmdline(monkeypatch):
    gd = "/games/SteamLibrary/steamapps/common/CoolGame"
    lib = GameLibrary(frozenset({gd}), frozenset(), frozenset())
    # wine/proton process exe, but cmdline points at the game .exe under common/.
    monkeypatch.setattr(
        games, "_process_path_tokens", lambda pid: ["/x/proton/wine", gd + "/game.exe"]
    )
    assert lib.is_game(None, 5)


def test_not_a_game(monkeypatch):
    lib = GameLibrary(frozenset({"/games/common/CoolGame"}), frozenset({"minecraft"}), frozenset())
    monkeypatch.setattr(games, "_process_path_tokens", lambda pid: ["/usr/bin/firefox"])
    assert not lib.is_game("firefox_firefox", 7)


def test_from_system_loads_packaged_curated_list():
    lib = GameLibrary.from_system()  # no Steam install needed; curated list always loads
    assert "minecraft" in lib.curated_classes

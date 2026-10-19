from pathlib import Path

from familymon.analyze.categories import Category
from familymon.categorize.apps import AppCategoryLookup, _exec_basename


def _write_desktop(tmp_path: Path, name: str, **kwargs: str) -> Path:
    body = ["[Desktop Entry]", "Type=Application"]
    for k, v in kwargs.items():
        body.append(f"{k}={v}")
    path = tmp_path / f"{name}.desktop"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def test_exec_basename_strips_path_and_field_codes():
    assert _exec_basename("/snap/bin/telegram-desktop -- %U") == "telegram-desktop"
    assert _exec_basename("firefox %u") == "firefox"
    assert _exec_basename(None) is None
    assert _exec_basename("") is None


def test_lookup_via_startup_wm_class(tmp_path: Path):
    _write_desktop(
        tmp_path,
        "telegram",
        Name="Telegram",
        StartupWMClass="TelegramDesktop",
        Exec="/snap/bin/telegram-desktop %U",
        Categories="Chat;Network;InstantMessaging;Qt;",
    )
    lookup = AppCategoryLookup.from_dirs([tmp_path])
    assert lookup.lookup("TelegramDesktop") is Category.MESSAGING


def test_lookup_falls_through_to_exec_basename(tmp_path: Path):
    """Real-world case: Telegram's StartupWMClass is `TelegramDesktop` but
    runtime resourceClass is `telegram-desktop_telegram-desktop` (snap form).
    Lookup must succeed via the Exec basename `telegram-desktop`."""
    _write_desktop(
        tmp_path,
        "telegram",
        Name="Telegram",
        StartupWMClass="TelegramDesktop",
        Exec="/snap/bin/telegram-desktop -- %U",
        Categories="Chat;Network;InstantMessaging;",
    )
    lookup = AppCategoryLookup.from_dirs([tmp_path])
    assert (
        lookup.lookup("telegram-desktop_telegram-desktop") is Category.MESSAGING
    )


def test_lookup_via_lowercased_name(tmp_path: Path):
    _write_desktop(
        tmp_path,
        "minetest",
        Name="Minetest",
        Exec="minetest",
        Categories="Game;",
    )
    lookup = AppCategoryLookup.from_dirs([tmp_path])
    assert lookup.lookup("minetest") is Category.GAMES
    assert lookup.lookup("Minetest") is Category.GAMES


def test_nodisplay_entries_are_skipped(tmp_path: Path):
    _write_desktop(
        tmp_path,
        "hidden",
        Name="Hidden",
        NoDisplay="true",
        Exec="hidden",
        Categories="Game;",
    )
    lookup = AppCategoryLookup.from_dirs([tmp_path])
    assert lookup.lookup("hidden") is None


def test_entries_without_categories_are_skipped(tmp_path: Path):
    _write_desktop(tmp_path, "nocats", Name="NoCats", Exec="nocats")
    lookup = AppCategoryLookup.from_dirs([tmp_path])
    assert lookup.lookup("nocats") is None


def test_unknown_wm_class_returns_none(tmp_path: Path):
    _write_desktop(
        tmp_path, "x", Name="X", Exec="x", Categories="Game;"
    )
    lookup = AppCategoryLookup.from_dirs([tmp_path])
    assert lookup.lookup("definitely-not-installed") is None
    assert lookup.lookup(None) is None
    assert lookup.lookup("") is None


def test_lookup_via_desktop_file_id_as_wayland_app_id(tmp_path: Path):
    """Flatpak/Wayland apps report the .desktop id as their app_id — Roblox via
    Sober shows up as 'org.vinegarhq.Sober'. Nothing in Name/Exec/StartupWMClass
    carries that string; the file's own name must be a lookup key."""
    _write_desktop(
        tmp_path,
        "org.vinegarhq.Sober",
        Name="Sober",
        Exec="/usr/bin/flatpak run --command=sober org.vinegarhq.Sober -- @@u %u @@",
        Categories="GNOME;GTK;Game;",
    )
    lookup = AppCategoryLookup.from_dirs([tmp_path])
    assert lookup.lookup("org.vinegarhq.Sober") is Category.GAMES
    assert lookup.lookup("ORG.VINEGARHQ.SOBER") is Category.GAMES  # case-insensitive
    assert lookup.lookup("sober") is Category.GAMES  # exec basename still works

import re

from familymon.capture.messaging_skip import MessagingSkipList


def _make(classes=(), patterns=()):
    return MessagingSkipList(
        window_classes=frozenset(classes),
        title_patterns=tuple(re.compile(p, re.IGNORECASE) for p in patterns),
    )


def test_skip_by_class_case_insensitive():
    skip = _make(classes=["discord", "signal"])
    assert skip.should_skip("Discord", "anything")
    assert skip.should_skip("DISCORD", None)
    assert not skip.should_skip("firefox", "Discord")


def test_skip_matches_snap_namespaced_class():
    """Snap packages report `name_binary`-style classes (e.g. Telegram-Desktop
    snap reports `telegram-desktop_telegram-desktop`). A single skip-list entry
    must match all three packaging conventions: native, flatpak, snap."""
    skip = _make(classes=["telegram-desktop"])
    assert skip.should_skip("telegram-desktop_telegram-desktop", None)
    assert skip.should_skip("Telegram-Desktop_Telegram-Desktop", None)
    assert skip.should_skip("telegram-desktop", None)  # native


def test_skip_does_not_partial_match_substrings():
    """Splitting on `_` is segment-level, not substring — `firefox` in skip
    list must not accidentally catch `firefox-screenshot-tool`."""
    skip = _make(classes=["firefox"])
    assert skip.should_skip("firefox", None)
    assert skip.should_skip("firefox_firefox", None)
    assert not skip.should_skip("firefox-screenshot-tool", None)


def test_skip_by_title_pattern():
    skip = _make(patterns=[r"\bDM\b"])
    assert skip.should_skip("firefox", "DM with Alice")
    assert not skip.should_skip("firefox", "Documents folder")


def test_no_skip_when_lists_empty():
    skip = _make()
    assert not skip.should_skip("discord", "hello")
    assert not skip.should_skip(None, None)


def test_loads_packaged_default():
    """The shipped default YAML should parse and include at least Discord."""
    skip = MessagingSkipList.from_file()
    assert "discord" in skip.window_classes


def test_packaged_whatsapp_web_title_pattern():
    """The shipped WhatsApp-Web title pattern catches the browser tab but not a
    page merely mentioning WhatsApp."""
    skip = MessagingSkipList.from_file()
    # WhatsApp Web tab (any browser suffix), with/without unread count.
    assert skip.should_skip("firefox_firefox", "WhatsApp — Mozilla Firefox")
    assert skip.should_skip("firefox_firefox", "(3) WhatsApp — Mozilla Firefox")
    assert skip.should_skip("chromium", "WhatsApp - Google Chrome")
    # A page about WhatsApp must NOT match.
    assert not skip.should_skip(
        "firefox_firefox", "WhatsApp adds a feature - TechCrunch — Mozilla Firefox"
    )
    assert not skip.should_skip("firefox_firefox", "How to use WhatsApp — Mozilla Firefox")

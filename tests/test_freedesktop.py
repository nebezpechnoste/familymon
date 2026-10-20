from familymon.analyze.categories import Category
from familymon.categorize.freedesktop import freedesktop_to_familymon


def test_game_wins_over_anything_else():
    assert freedesktop_to_familymon(["Game", "Education", "Network"]) is Category.GAMES


def test_messaging_wins_over_network():
    """Telegram lists ['Chat','Network','InstantMessaging','Qt']. The matcher
    must surface MESSAGING, not the generic Network fallback."""
    assert (
        freedesktop_to_familymon(["Chat", "Network", "InstantMessaging", "Qt"])
        is Category.MESSAGING
    )


def test_email_classified_as_messaging():
    """Email is private content; lump under MESSAGING for retention rules."""
    assert freedesktop_to_familymon(["Network", "Email", "Office"]) is Category.MESSAGING


def test_browser_classified_as_browsing_other():
    assert (
        freedesktop_to_familymon(["GNOME", "GTK", "Network", "WebBrowser"])
        is Category.BROWSING_OTHER
    )


def test_ide_classified_as_code():
    assert freedesktop_to_familymon(["IDE", "Development"]) is Category.CODE


def test_unknown_categories_return_none():
    assert freedesktop_to_familymon(["KDE", "Qt", "FooBar"]) is None
    assert freedesktop_to_familymon([]) is None

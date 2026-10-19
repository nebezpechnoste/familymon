"""Map FreeDesktop menu-spec categories to familymon's Category enum.

See https://specifications.freedesktop.org/menu-spec/latest/category-registry.html

Priority matters — many .desktop files list several categories (e.g. Telegram is
"Chat;Network;InstantMessaging;Qt;"). We walk the priority list and return on
the first hit, so MESSAGING wins over the generic Network fallback.
"""

from __future__ import annotations

from familymon.analyze.categories import Category


# Order matters. First matching rule wins.
_PRIORITY: list[tuple[Category, frozenset[str]]] = [
    (Category.GAMES, frozenset({"Game"})),
    (
        Category.MESSAGING,
        frozenset(
            {
                "InstantMessaging",
                "Chat",
                "IRCClient",
                "Telephony",
                "VideoConference",
                "Email",
                "ContactManagement",
            }
        ),
    ),
    (Category.CODE, frozenset({"IDE", "Development", "Debugger", "RevisionControl"})),
    (Category.SCHOOL, frozenset({"Education", "Languages", "Literature", "Science"})),
    (
        Category.VIDEO,
        frozenset(
            {"Player", "Video", "TV", "VideoConference", "AudioVideo", "Music"}
        ),
    ),
    (
        Category.PRODUCTIVITY,
        frozenset(
            {
                "Office",
                "WordProcessor",
                "Spreadsheet",
                "Presentation",
                "Database",
                "Calculator",
                "Graphics",
                "Photography",
                "2DGraphics",
                "3DGraphics",
                "VectorGraphics",
                "RasterGraphics",
                "ProjectManagement",
            }
        ),
    ),
    (Category.BROWSING_OTHER, frozenset({"WebBrowser"})),
]


def freedesktop_to_familymon(fd_categories: list[str] | set[str]) -> Category | None:
    """Return our Category for a list of FreeDesktop categories, or None.

    None means "no opinion" — caller should consider other signals (domain
    lookup, OCR, LLM) before falling back to UNKNOWN.
    """
    fd_set = frozenset(fd_categories)
    for cat, markers in _PRIORITY:
        if fd_set & markers:
            return cat
    return None

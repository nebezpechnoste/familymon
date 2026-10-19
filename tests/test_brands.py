from familymon.analyze.categories import Category
from familymon.categorize.brands import BrandTitleLookup


def _lookup(*pairs: tuple[str, Category]) -> BrandTitleLookup:
    b = BrandTitleLookup()
    for kw, cat in pairs:
        b.add(kw, cat)
    return b


def test_matches_brand_substring_case_insensitive():
    b = _lookup(("youtube", Category.VIDEO))
    hit = b.lookup("24 hours of black screen - YouTube — Mozilla Firefox")
    assert hit == (Category.VIDEO, "youtube")


def test_first_match_wins_in_insertion_order():
    b = _lookup(("youtube", Category.VIDEO), ("reddit", Category.SOCIAL))
    assert b.lookup("Reddit - the front page")[0] is Category.SOCIAL


def test_no_match_returns_none():
    b = _lookup(("youtube", Category.VIDEO))
    assert b.lookup("Some random blog post") is None
    assert b.lookup(None) is None
    assert b.lookup("") is None


def test_packaged_yaml_loads_known_brands():
    b = BrandTitleLookup.from_yaml()
    assert b.lookup("X - YouTube")[0] is Category.VIDEO
    assert b.lookup("r/python - Reddit")[0] is Category.SOCIAL
    assert b.lookup("familymon - GitHub")[0] is Category.CODE


def test_bundled_browser_game_brands_without_domain_in_title():
    """Poki-style titles carry no domain; the bundled brand list must still
    label them games — and school/video brands must keep precedence."""
    from familymon.analyze.categories import Category
    from familymon.categorize.brands import BrandTitleLookup

    b = BrandTitleLookup.from_yaml()
    assert b.lookup("Subway Surfers - Play on Poki")[0] is Category.GAMES
    assert b.lookup("Shell Shockers | Play Now on CrazyGames")[0] is Category.GAMES
    assert b.lookup("Melon Sandbox 🕹️ Online Game | Gameflare.com")[0] is Category.GAMES
    # precedence: earlier sections win on a shared title
    assert b.lookup("Online game - Wikipedia")[0] is Category.SCHOOL
    assert b.lookup("Top 10 online games of 2026 - YouTube")[0] is Category.VIDEO

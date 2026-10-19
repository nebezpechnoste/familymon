from familymon.analyze.categories import Category
from familymon.analyze.heuristics import HeuristicClassifier, WindowView
from familymon.categorize.apps import AppCategoryLookup
from familymon.categorize.brands import BrandTitleLookup
from familymon.categorize.domains import DomainCategoryLookup


def _classifier(
    apps_map: dict[str, Category] | None = None,
    domains_map: dict[str, Category] | None = None,
    brands: list[tuple[str, Category]] | None = None,
) -> HeuristicClassifier:
    a = AppCategoryLookup()
    for k, v in (apps_map or {}).items():
        a.add(k, v)
    d = DomainCategoryLookup()
    for k, v in (domains_map or {}).items():
        d.add(k, v)
    b = BrandTitleLookup()
    for kw, cat in brands or []:
        b.add(kw, cat)
    return HeuristicClassifier(a, d, b)


def _win(wm_class=None, title=None, active=False, minimized=False):
    return WindowView(wm_class=wm_class, title=title, active=active, minimized=minimized)


def test_single_native_app():
    c = _classifier(apps_map={"telegram-desktop": Category.MESSAGING})
    cls = c.classify(1, [_win("telegram-desktop_telegram-desktop", "chat", active=True)])
    assert cls.category is Category.MESSAGING
    assert cls.confidence >= 0.8


def test_browser_domain_in_title_beats_brand():
    c = _classifier(
        domains_map={"youtube.com": Category.VIDEO},
        brands=[("youtube", Category.VIDEO)],
    )
    cls = c.classify(
        1, [_win("firefox_firefox", "vid - youtube.com - Mozilla Firefox", active=True)]
    )
    assert cls.category is Category.VIDEO
    assert cls.source == "domain"


def test_browser_brand_in_title_when_no_domain():
    c = _classifier(brands=[("youtube", Category.VIDEO)])
    cls = c.classify(1, [_win("firefox_firefox", "Cat videos - YouTube", active=True)])
    assert cls.category is Category.VIDEO
    assert cls.source == "heuristic"
    assert "brand=" in cls.evidence_text


def test_browser_plain_is_browsing_other():
    c = _classifier()
    cls = c.classify(1, [_win("firefox_firefox", "Some Blog", active=True)])
    assert cls.category is Category.BROWSING_OTHER


def test_loophole_messaging_focus_does_not_mask_visible_game():
    """The whole point of B: chat focused, game visible but unfocused -> games."""
    c = _classifier(
        apps_map={"telegram-desktop": Category.MESSAGING, "minetest": Category.GAMES}
    )
    windows = [
        _win("telegram-desktop_telegram-desktop", "chat", active=True),
        _win("minetest", "Minetest", active=False),
    ]
    cls = c.classify(1, windows)
    assert cls.category is Category.GAMES
    # present_categories lists all present, priority-ordered, primary first.
    assert cls.present_categories == [Category.GAMES, Category.MESSAGING]


def test_present_categories_single_window():
    c = _classifier(apps_map={"minetest": Category.GAMES})
    cls = c.classify(1, [_win("minetest", "Minetest", active=True)])
    assert cls.present_categories == [Category.GAMES]


def test_present_categories_idle_when_suppressed():
    c = _classifier()
    cls = c.classify(1, [_win(None, None)], suppressed_reason="session_locked")
    assert cls.category is Category.IDLE
    assert cls.present_categories == [Category.IDLE]


def test_game_running_classifies_as_games_even_when_window_unmapped():
    """Trusted game detection is authoritative: classifies GAMES even if the
    game's window isn't in our .desktop scan."""
    c = _classifier()
    cls = c.classify(
        1, [_win("some-unmapped-game", "Game", active=True)], game_running=True
    )
    assert cls.category is Category.GAMES
    assert cls.source == "game_process"
    assert Category.GAMES in cls.present_categories


def test_game_running_with_foreground_video_still_games_but_video_present():
    c = _classifier(brands=[("youtube", Category.VIDEO)])
    cls = c.classify(
        1, [_win("firefox_firefox", "Movie - YouTube", active=True)], game_running=True
    )
    assert cls.category is Category.GAMES
    assert Category.VIDEO in cls.present_categories


def test_video_outranks_messaging():
    c = _classifier(
        apps_map={"telegram-desktop": Category.MESSAGING},
        brands=[("youtube", Category.VIDEO)],
    )
    windows = [
        _win("telegram-desktop_telegram-desktop", "chat", active=True),
        _win("firefox_firefox", "Movie - YouTube", active=False),
    ]
    assert c.classify(1, windows).category is Category.VIDEO


def test_school_outranks_messaging_but_not_games():
    c = _classifier(
        apps_map={
            "telegram-desktop": Category.MESSAGING,
            "minetest": Category.GAMES,
        },
        brands=[("wikipedia", Category.SCHOOL)],
    )
    # school + messaging -> school
    win_school = [
        _win("telegram-desktop_telegram-desktop", "chat", active=True),
        _win("firefox_firefox", "Photosynthesis - Wikipedia", active=False),
    ]
    assert c.classify(1, win_school).category is Category.SCHOOL
    # school + game -> games (parent wants the game surfaced during schoolwork)
    win_game = [
        _win("firefox_firefox", "Photosynthesis - Wikipedia", active=True),
        _win("minetest", "Minetest", active=False),
    ]
    assert c.classify(1, win_game).category is Category.GAMES


def test_minimized_window_does_not_contribute():
    c = _classifier(
        apps_map={"telegram-desktop": Category.MESSAGING, "minetest": Category.GAMES}
    )
    windows = [
        _win("telegram-desktop_telegram-desktop", "chat", active=True),
        _win("minetest", "Minetest", active=False, minimized=True),  # minimized
    ]
    # The minimized game must not surface; only messaging is visible.
    assert c.classify(1, windows).category is Category.MESSAGING


def test_suppressed_session_classifies_as_idle():
    c = _classifier()
    cls = c.classify(1, [_win(None, None)], suppressed_reason="session_locked")
    assert cls.category is Category.IDLE
    cls2 = c.classify(1, [_win("firefox_firefox", "x", active=True)], suppressed_reason="session_inactive")
    assert cls2.category is Category.IDLE


def test_messaging_skip_reason_is_not_idle():
    """A messaging_app screenshot-skip is NOT a session-idle reason — the tick
    still classifies normally (and a visible game would surface)."""
    c = _classifier(apps_map={"telegram-desktop": Category.MESSAGING})
    cls = c.classify(
        1,
        [_win("telegram-desktop_telegram-desktop", "chat", active=True)],
        suppressed_reason="messaging_app",
    )
    assert cls.category is Category.MESSAGING


def test_no_signal_is_unknown():
    c = _classifier()
    cls = c.classify(1, [_win("some-unknown-app", "x", active=True)])
    assert cls.category is Category.UNKNOWN
    assert cls.confidence < 0.5


def test_empty_window_list_is_unknown():
    c = _classifier()
    assert c.classify(1, []).category is Category.UNKNOWN


def test_schedule_group_code_in_title_classifies_as_school():
    """The LMS titles its course pages with the school's group code and nothing
    else identifiable ("10L CYCLE TEST AC-26/27 - Google Chrome"). With the
    imported schedule's codes added as school brands, it's school, not
    browsing_other."""
    c = _classifier(brands=[("10L CYCLE TEST AC-26/27", Category.SCHOOL)])
    cls = c.classify(
        1, [_win("google-chrome", "10L CYCLE TEST AC-26/27 - Google Chrome", active=True)]
    )
    assert cls.category is Category.SCHOOL


def test_window_on_another_virtual_desktop_does_not_contribute():
    """Parked on another desktop or activity = not on screen, like minimized."""
    c = _classifier(
        apps_map={"telegram-desktop": Category.MESSAGING, "minetest": Category.GAMES}
    )
    windows = [
        _win("telegram-desktop_telegram-desktop", "chat", active=True),
        WindowView(wm_class="minetest", title="Minetest", on_current_desktop=False),
    ]
    cls = c.classify(1, windows)
    assert cls.category is Category.MESSAGING
    assert cls.present_categories == [Category.MESSAGING]
    assert "of 1 visible window)" in cls.evidence_text

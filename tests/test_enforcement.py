from __future__ import annotations

from datetime import datetime

import pytest

from familymon.policy.enforcement import (
    Change,
    HourWindow,
    LiveState,
    PlayTimeActivity,
    PolicyError,
    build_settings,
    parse_duration,
    plan_changes,
    plan_is_safe_now,
    sanitize_mask_fragment,
    steam_masks,
    whole_hours,
)

# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2h30m", 9000),
        ("45m", 2700),
        ("90s", 90),
        ("1h", 3600),
        ("2h30m15s", 9015),
        (7200, 7200),
        ("7200", 7200),
        ("unlimited", 86400),
        ("none", 86400),
        (999999, 86400),  # clamped to timekpr's per-day ceiling
    ],
)
def test_parse_duration(raw, expected):
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("bad", ["2x", "h", -5, True, None, "1h2x"])
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(PolicyError):
        parse_duration(bad)


# ---------------------------------------------------------------------------
# Policy -> settings
# ---------------------------------------------------------------------------


def _policy(**overrides):
    base = {
        "user": "kid",
        "lockout": "lock",
        "allowed_hours": {
            "default": {"start": 7, "end": 21},
            "saturday": {"start": 8, "end": 22},
        },
        "daily_limits": {"default": "unlimited"},
        "games": {
            "enabled": True,
            "daily_limits": {"default": "1h", "saturday": "2h", "sunday": "2h"},
            "extra_masks": ["minecraft-launcher[Minecraft]"],
        },
    }
    base.update(overrides)
    return base


def test_build_settings_shapes():
    s = build_settings(_policy(), game_dirs={"/lib/steamapps/common/Factorio"})
    assert s.user == "kid"
    assert s.lockout == "lock"
    # Mon-Fri + Sun from default (7..21), Saturday overridden (8..22).
    assert s.allowed_hours[0] == whole_hours(range(7, 22))
    assert s.allowed_hours[5] == whole_hours(range(8, 23))
    assert s.allowed_hours[6] == whole_hours(range(7, 22))
    assert s.daily_limits == (86400,) * 7
    assert s.playtime_enabled
    assert s.playtime_daily_limits == (3600,) * 5 + (7200, 7200)
    # Steam mask generated + curated extra kept, in that order.
    masks = [a.mask for a in s.playtime_activities]
    assert masks == [".*common.Factorio.*", ".*minecraft-launcher.*"]  # bare -> wrapped
    assert s.playtime_activities[1].description == "Minecraft"


def test_build_settings_games_disabled_zeroes_playtime():
    s = build_settings(
        _policy(games={"enabled": False}), game_dirs={"/x/common/Doom"}
    )
    assert not s.playtime_enabled
    assert s.playtime_daily_limits == (0,) * 7
    assert s.playtime_activities == ()


@pytest.mark.parametrize(
    "mutation",
    [
        {"user": None},
        {"lockout": "explode"},
        {"allowed_hours": {"default": {"start": 22, "end": 7}}},  # inverted
        {"allowed_hours": {"default": {"start": 0, "end": 24}}},  # 24 invalid
        {"allowed_hours": {"funday": {"start": 1, "end": 2}}},  # bad day name
        {"daily_limits": None},
        {"games": {"enabled": True, "daily_limits": None}},
        {"games": {"enabled": True, "daily_limits": "1h", "extra_masks": ["a[b];c"]}},
    ],
)
def test_build_settings_rejects_bad_policy(mutation):
    with pytest.raises(PolicyError):
        build_settings(_policy(**mutation))


def test_scalar_and_full_week_day_maps():
    s = build_settings(
        _policy(
            daily_limits="2h",
            allowed_hours={
                d: {"start": 9, "end": 17}
                for d in (
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                )
            },
        )
    )
    assert s.daily_limits == (7200,) * 7
    assert all(h == whole_hours(range(9, 18)) for h in s.allowed_hours)


# ---------------------------------------------------------------------------
# Steam mask generation
# ---------------------------------------------------------------------------


def test_steam_masks_sanitize_forbidden_chars():
    # []^$; are forbidden in timekpr masks; regex specials must be escaped.
    acts = steam_masks({"/lib/steamapps/common/Game [Deluxe]; v2^"})
    assert len(acts) == 1
    mask = acts[0].mask
    assert not set(mask) & set("[];^$")
    assert mask.startswith(".*common.") and mask.endswith(".*")
    assert "[" not in acts[0].description and ";" not in acts[0].description


def test_steam_masks_dedupe_and_sort():
    acts = steam_masks(
        {
            "/a/steamapps/common/Factorio",
            "/b/steamapps/common/Factorio",  # same game, second library
            "/a/steamapps/common/Celeste",
        }
    )
    assert [a.description for a in acts] == ["Celeste", "Factorio"]


def test_sanitize_mask_fragment_escapes_regex_specials():
    frag = sanitize_mask_fragment("Age of Empires II (2013)")
    assert "(" in frag and "\\(" in frag  # escaped, not stripped


# ---------------------------------------------------------------------------
# Diff planning
# ---------------------------------------------------------------------------


def _live_matching(settings) -> LiveState:
    return LiveState(
        allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        allowed_hours=settings.allowed_hours,
        daily_limits=settings.daily_limits,
        lockout=settings.lockout,
        playtime_enabled=settings.playtime_enabled,
        playtime_allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        playtime_daily_limits=settings.playtime_daily_limits,
        playtime_activities=settings.playtime_activities,
        active_session_count=0,
    )


_OPEN_LIVE = LiveState(
    allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
    allowed_hours=(whole_hours(range(24)),) * 7,
    daily_limits=(86400,) * 7,
    lockout="terminate",
    playtime_enabled=False,
    playtime_allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
    playtime_daily_limits=(0,) * 7,
    playtime_activities=(),
    active_session_count=0,
)


def test_plan_noop_when_live_matches():
    s = build_settings(_policy(), game_dirs=set())
    assert plan_changes(s, _live_matching(s)) == []


def test_plan_from_unrestricted_orders_hours_last():
    s = build_settings(_policy(), game_dirs={"/x/steamapps/common/Factorio"})
    changes = plan_changes(s, _OPEN_LIVE)
    settings_touched = [c.setting for c in changes]
    # lockout + playtime first, all 7 hour windows last
    assert settings_touched[-7:] == [
        f"allowed_hours[{d}]"
        for d in (
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        )
    ]
    assert "lockout" in settings_touched
    assert "playtime_enabled" in settings_touched
    assert "playtime_activities" in settings_touched
    # No daily_limits change (both unlimited), no allowed_days change.
    assert "daily_limits" not in settings_touched
    assert "allowed_days" not in settings_touched


def test_plan_emits_valid_cli_args():
    s = build_settings(_policy(), game_dirs=set())
    changes = plan_changes(s, _OPEN_LIVE)
    by_setting = {c.setting: c for c in changes}
    assert by_setting["allowed_hours[saturday]"].cli_args == (
        "--setallowedhours",
        "kid",
        "6",
        ";".join(str(h) for h in range(8, 23)),
    )
    assert by_setting["playtime_daily_limits"].cli_args == (
        "--setplaytimelimits",
        "kid",
        "3600;3600;3600;3600;3600;7200;7200",
    )


def test_plan_clears_playtime_when_disabling():
    s = build_settings(_policy(games={"enabled": False}))
    live = LiveState(
        allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        allowed_hours=s.allowed_hours,
        daily_limits=s.daily_limits,
        lockout="lock",
        playtime_enabled=True,
        playtime_allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        playtime_daily_limits=(3600,) * 7,
        playtime_activities=(PlayTimeActivity("steam", "Steam"),),
        active_session_count=0,
    )
    changes = plan_changes(s, live)
    settings_touched = {c.setting for c in changes}
    assert {
        "playtime_enabled",
        "playtime_activities",
        "playtime_daily_limits",
    } <= settings_touched


def test_plan_realigns_partial_playtime_days():
    """Live PlayTime allowed on weekdays only -> re-spread to all 7 so the
    limits array stays aligned."""
    s = build_settings(_policy(), game_dirs=set())
    live = LiveState(
        allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        allowed_hours=s.allowed_hours,
        daily_limits=s.daily_limits,
        lockout="lock",
        playtime_enabled=True,
        playtime_allowed_weekdays=(1, 2, 3, 4, 5),
        playtime_daily_limits=(3600,) * 5 + (0, 0),
        playtime_activities=s.playtime_activities,
        active_session_count=0,
    )
    changes = plan_changes(s, live)
    by_setting = {c.setting: c for c in changes}
    assert by_setting["playtime_allowed_days"].cli_args == (
        "--setplaytimealloweddays",
        "kid",
        "1;2;3;4;5;6;7",
    )


# ---------------------------------------------------------------------------
# Safety guard — the session-kill regression
# ---------------------------------------------------------------------------


def _at(hour: int) -> datetime:
    return datetime(2026, 8, 22, hour, 30)  # a Saturday


def test_unsafe_when_new_window_excludes_current_hour_with_session():
    """Pins the incident: setting hours 7-21 at 22:35 with the kid logged in
    terminated their session. The guard must catch exactly this."""
    s = build_settings(_policy())  # Saturday window 8-22
    live = LiveState(
        **{**_OPEN_LIVE.__dict__, "active_session_count": 1}
    )
    verdict = plan_is_safe_now(s, live, now=_at(23))  # Sat 23:30, outside 8-22
    assert not verdict.safe
    assert any("excludes the current time" in r for r in verdict.reasons)


def test_safe_when_no_active_session():
    s = build_settings(_policy())
    verdict = plan_is_safe_now(s, _OPEN_LIVE, now=_at(23))
    assert verdict.safe


def test_safe_when_current_hour_stays_allowed():
    s = build_settings(_policy())  # Saturday window 8-22
    live = LiveState(**{**_OPEN_LIVE.__dict__, "active_session_count": 1})
    verdict = plan_is_safe_now(s, live, now=_at(14))
    assert verdict.safe


def test_unsafe_when_todays_limit_shrinks_mid_session():
    s = build_settings(_policy(daily_limits="2h"))
    live = LiveState(
        **{
            **_OPEN_LIVE.__dict__,
            "allowed_hours": s.allowed_hours,
            "active_session_count": 1,
        }
    )
    verdict = plan_is_safe_now(s, live, now=_at(14))
    assert not verdict.safe
    assert any("limit shrinks" in r for r in verdict.reasons)


def test_change_is_frozen_dataclass():
    c = Change("x", "a", "b", ("--x",))
    with pytest.raises(AttributeError):
        c.setting = "y"  # type: ignore[misc]

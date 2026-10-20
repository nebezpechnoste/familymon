from __future__ import annotations

from datetime import datetime, time

import pytest

from familymon.policy.enforcement import (
    HourWindow,
    LiveState,
    apply_grants,
    build_settings,
    merge_hour_windows,
    plan_changes,
    plan_is_safe_now,
    whole_hours,
)
from familymon.policy.grants import Grant, GrantStore, active_windows, until_today

MON_2206 = datetime(2026, 10, 5, 22, 6)  # the real case: grant made at 22:06 for 22:29


def _policy():
    return {
        "user": "kid",
        "lockout": "suspend",
        "allowed_hours": {
            "default": {"start": 7, "end": 21},
            "saturday": {"start": 8, "end": 22},
            "sunday": {"start": 8, "end": 22},
        },
        "daily_limits": {"default": "unlimited"},
        "games": {"enabled": False},
    }


def test_hour_window_cli_and_covers():
    assert HourWindow(22).cli() == "22"
    assert HourWindow(22, 0, 29).cli() == "22[00-29]"
    assert HourWindow(22, 0, 29).covers(time(22, 6)) and not HourWindow(22, 0, 29).covers(
        time(22, 30)
    )
    assert HourWindow(22).covers(time(22, 59))


def test_grant_windows_span_hours():
    assert Grant("kid", "2026-10-05", "22:00", "22:29", "x").windows() == (HourWindow(22, 0, 29),)
    assert Grant("kid", "2026-10-05", "21:40", "23:10", "x").windows() == (
        HourWindow(21, 40, 59),
        HourWindow(22),
        HourWindow(23, 0, 10),
    )
    with pytest.raises(ValueError):
        Grant("kid", "2026-10-05", "23:00", "22:00", "x").windows()


def test_merge_whole_hour_absorbs_partial_and_partials_union():
    merged = merge_hour_windows(
        whole_hours(range(20, 22)), (HourWindow(21, 40, 59), HourWindow(22, 0, 29))
    )
    assert merged == (HourWindow(20), HourWindow(21), HourWindow(22, 0, 29))
    assert merge_hour_windows((HourWindow(22, 0, 10),), (HourWindow(22, 20, 29),)) == (
        HourWindow(22, 0, 29),
    )


def test_apply_grants_widens_only_today_and_is_safe_at_any_hour():
    s = build_settings(_policy())
    widened = apply_grants(s, (HourWindow(22, 0, 29),), MON_2206.date())
    assert widened.allowed_hours[0] == whole_hours(range(7, 22)) + (HourWindow(22, 0, 29),)
    assert widened.allowed_hours[1] == s.allowed_hours[1]  # Tuesday untouched
    live = LiveState(
        allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        allowed_hours=s.allowed_hours,
        daily_limits=s.daily_limits,
        lockout="suspend",
        playtime_enabled=False,
        playtime_allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        playtime_daily_limits=(0,) * 7,
        playtime_activities=(),
        active_session_count=1,
    )
    # At 22:06 with a session active: applying the widened window is safe
    # (22:06 is inside 22[00-29]); the plan emits exactly the Monday hours.
    assert plan_is_safe_now(widened, live, now=MON_2206).safe
    changes = plan_changes(widened, live)
    assert [c.setting for c in changes] == ["allowed_hours[monday]"]
    assert changes[0].cli_args[-1].endswith(";21;22[00-29]")
    # ...while converging back to bare policy at 22:06 would be refused.
    assert not plan_is_safe_now(s, live, now=MON_2206).safe


def test_reconciler_sees_no_change_while_grant_is_live(tmp_path):
    """The whole point: after a grant, plan(desired-with-grants) == live."""
    s = build_settings(_policy())
    store = GrantStore(tmp_path / "grants.json")
    store.add("kid", time(22, 6), time(22, 29), now=MON_2206)
    desired = apply_grants(s, active_windows(store, "kid", MON_2206), MON_2206.date())
    live_hours = list(s.allowed_hours)
    live_hours[0] = whole_hours(range(7, 22)) + (HourWindow(22, 6, 29),)
    live = LiveState(
        allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        allowed_hours=tuple(live_hours),
        daily_limits=s.daily_limits,
        lockout="suspend",
        playtime_enabled=False,
        playtime_allowed_weekdays=(1, 2, 3, 4, 5, 6, 7),
        playtime_daily_limits=(0,) * 7,
        playtime_activities=(),
        active_session_count=1,
    )
    assert plan_changes(desired, live) == []


def test_store_expiry_prune_and_revoke(tmp_path):
    store = GrantStore(tmp_path / "grants.json")
    store.add("kid", time(22, 6), time(22, 29), now=MON_2206, note="finish match")
    assert [g.end for g in store.active("kid", MON_2206)] == ["22:29"]
    # expired same day
    assert store.active("kid", datetime(2026, 10, 5, 22, 30)) == []
    # next day: pruned from the file
    store.add("kid", time(22, 0), time(22, 29), now=MON_2206)
    assert store.active("kid", datetime(2026, 10, 6, 9, 0)) == []
    assert store._load() == []
    store.add("kid", time(22, 0), time(22, 29), now=MON_2206)
    assert store.revoke("kid", MON_2206) == 1 and store.active("kid", MON_2206) == []


def test_until_today_defaults_start_to_now_and_rejects_past():
    s, e = until_today(time(22, 29), now=MON_2206)
    assert (s, e) == (time(22, 6), time(22, 29))
    s2, _ = until_today(time(22, 29), now=MON_2206, start=time(22, 0))
    assert s2 == time(22, 0)
    with pytest.raises(ValueError):
        until_today(time(21, 0), now=MON_2206)

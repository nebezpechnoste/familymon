from __future__ import annotations

import pytest

from familymon.policy.enforcement import HourWindow
from familymon.policy.timekpr_admin import (
    TimekprError,
    _normalize_activities,
    _normalize_day_aligned,
    _normalize_hours,
)


def test_normalize_day_aligned_full_week():
    days, limits = _normalize_day_aligned(
        ["1", "2", "3", "4", "5", "6", "7"],
        [7200, 7200, 7200, 7200, 7200, 14400, 14400],
        "t",
    )
    assert days == (1, 2, 3, 4, 5, 6, 7)
    assert limits == (7200,) * 5 + (14400, 14400)


def test_normalize_day_aligned_partial_week_spreads_zeroes():
    # timekpr's limits array aligns to the allowed-days list; disallowed days
    # must come back as 0 on the fixed Mon..Sun axis.
    days, limits = _normalize_day_aligned(["1", "3", "7"], [100, 300, 700], "t")
    assert days == (1, 3, 7)
    assert limits == (100, 0, 300, 0, 0, 0, 700)


def test_normalize_day_aligned_rejects_misaligned():
    with pytest.raises(TimekprError):
        _normalize_day_aligned(["1", "2"], [100], "t")


def test_normalize_hours_from_dbus_shape():
    # Real shape from getUserInformation(user, "F"): per-day dicts keyed by
    # hour string, valued {SMIN, EMIN, UACC}.
    config = {
        "ALLOWED_HOURS_1": {
            "7": {"STARTMIN": 0, "ENDMIN": 60, "UACC": False},
            "8": {"STARTMIN": 0, "ENDMIN": 60, "UACC": False},
            "21": {"STARTMIN": 0, "ENDMIN": 45, "UACC": False},
        },
        "ALLOWED_HOURS_2": {},
        # days 3-7 absent entirely
    }
    hours = _normalize_hours(config)
    assert hours[0] == (HourWindow(7), HourWindow(8), HourWindow(21, 0, 45))
    assert hours[1] == ()
    assert hours[6] == ()


def test_normalize_hours_real_dbus_shape_with_partial_hour():
    # Exactly what getUserInformation returned after a 22[00-14] grant.
    config = {"ALLOWED_HOURS_5": {"7": {"STARTMIN": 0, "ENDMIN": 60, "UACC": 0},
                                  "22": {"STARTMIN": 0, "ENDMIN": 14, "UACC": 0}}}
    hours = _normalize_hours(config)
    assert hours[4] == (HourWindow(7), HourWindow(22, 0, 14))
    assert [w.cli() for w in hours[4]] == ["7", "22[00-14]"]


def test_normalize_activities_pairs():
    acts = _normalize_activities(
        [["steam", "Steam"], [".*common/Factorio.*", "Factorio"], ["", "junk"]]
    )
    assert [(a.mask, a.description) for a in acts] == [
        ("steam", "Steam"),
        (".*common/Factorio.*", "Factorio"),
    ]

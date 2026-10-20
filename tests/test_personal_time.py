"""Personal time (policy/personal_time.py) and the kid's once-a-day notice.
Everything lives under tmp_path; no session, no notify-send."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from familymon.policy.enforcement import HourWindow, PolicyError
from familymon.policy.personal_time import (
    PersonalTimeRule,
    bedtime_end,
    current_window,
    parse_personal_time,
    window_for,
)

MON = date(2026, 10, 19)  # a Monday
SAT = date(2026, 10, 24)
HOURS_7_21 = [HourWindow(h) for h in range(7, 22)]  # allowed through 21:59


def test_absent_or_off_means_no_personal_time():
    assert parse_personal_time({}) == (None,) * 7
    assert parse_personal_time({"personal_time": None}) == (None,) * 7
    assert parse_personal_time({"personal_time": False}) == (None,) * 7  # YAML `off`
    assert parse_personal_time({"personal_time": "off"}) == (None,) * 7


def test_a_bare_rule_applies_to_every_day():
    rules = parse_personal_time({"personal_time": {"from": "20:00"}})
    assert rules == (PersonalTimeRule(time(20, 0)),) * 7


def test_per_day_rules_with_a_day_off_and_an_explicit_end():
    rules = parse_personal_time(
        {
            "personal_time": {
                "default": {"from": "20:00"},
                "saturday": False,
                "sunday": {"from": "19:30", "until": "21:00"},
            }
        }
    )
    assert rules[0] == PersonalTimeRule(time(20, 0))
    assert rules[5] is None
    assert rules[6] == PersonalTimeRule(time(19, 30), time(21, 0))


@pytest.mark.parametrize(
    "bad",
    [
        {"personal_time": "20:00"},  # not a mapping
        {"personal_time": {"default": {"from": 20}}},  # unquoted time
        {"personal_time": {"default": {"from": "25:00"}}},
        {"personal_time": {"default": {"from": "20:00", "until": "19:00"}}},
        {"personal_time": {"default": {"from": "20:00", "til": "21:00"}}},  # typo
        {"personal_time": {"default": {"until": "21:00"}}},  # no from
        {"personal_time": {"monday": {"from": "20:00"}}},  # no default
        {"personal_time": {"default": True}},
    ],
)
def test_rejected_shapes(bad):
    with pytest.raises(PolicyError):
        parse_personal_time(bad)


def test_bedtime_end_is_the_minute_after_the_last_allowed_one():
    assert bedtime_end(MON, HOURS_7_21) == datetime(2026, 10, 19, 22, 0)
    assert bedtime_end(MON, [HourWindow(23)]) == datetime(2026, 10, 20, 0, 0)
    partial = [HourWindow(21), HourWindow(22, 0, 29)]
    assert bedtime_end(MON, partial) == datetime(2026, 10, 19, 22, 30)
    assert bedtime_end(MON, []) is None


def test_window_ends_at_the_bedtime_lock_by_default():
    rules = parse_personal_time({"personal_time": {"from": "20:00"}})
    w = window_for(rules, MON, HOURS_7_21)
    assert (w.start, w.end, w.ends_at_bedtime) == (
        datetime(2026, 10, 19, 20, 0),
        datetime(2026, 10, 19, 22, 0),
        True,
    )
    assert w.label() == "20:00–22:00 (ends at the bedtime lock)"
    assert not w.active(datetime(2026, 10, 19, 19, 59))
    assert w.active(datetime(2026, 10, 19, 20, 0))
    assert w.active(datetime(2026, 10, 19, 21, 59, 59))
    assert not w.active(datetime(2026, 10, 19, 22, 0))


def test_window_accepts_an_aware_now():
    rules = parse_personal_time({"personal_time": {"from": "20:00"}})
    w = window_for(rules, MON, HOURS_7_21)
    local_2030 = datetime(2026, 10, 19, 20, 30).astimezone()
    assert w.active(local_2030)
    assert w.active(local_2030.astimezone(UTC))


def test_explicit_until_does_not_need_allowed_hours():
    rules = parse_personal_time({"personal_time": {"from": "20:00", "until": "21:00"}})
    w = window_for(rules, MON)
    assert (w.end, w.ends_at_bedtime) == (datetime(2026, 10, 19, 21, 0), False)


def test_no_window_when_it_would_start_after_bedtime_or_on_a_day_off():
    rules = parse_personal_time(
        {"personal_time": {"default": {"from": "22:00"}, "saturday": "off"}}
    )
    assert window_for(rules, MON, HOURS_7_21) is None
    assert window_for(rules, SAT, HOURS_7_21) is None
    assert window_for(rules, MON, []) is None


def _policy(tmp_path, personal_time: str = 'personal_time: {from: "20:00"}') -> None:
    (tmp_path / "policy.yaml").write_text(
        "user: kid\nallowed_hours:\n  default: {start: 7, end: 21}\n"
        "  saturday: {start: 8, end: 22}\n" + personal_time + "\n"
    )


def test_current_window_reads_policy_and_grants(tmp_path):
    _policy(tmp_path)
    policy = tmp_path / "policy.yaml"
    grants = tmp_path / "grants.json"
    now = datetime(2026, 10, 19, 20, 30)

    def end(at=now):
        return current_window(at, policy_path=policy, grants_path=grants).end

    w = current_window(now, policy_path=policy, grants_path=grants)
    assert (w.start, w.end) == (datetime(2026, 10, 19, 20, 0), datetime(2026, 10, 19, 22, 0))

    # Saturday's bedtime is later; and a grant tonight extends the lock and the window.
    assert end(datetime(2026, 10, 24, 21, 0)) == datetime(2026, 10, 24, 23, 0)
    grants.write_text(
        json.dumps(
            {
                "grants": [
                    {
                        "user": "kid",
                        "day": "2026-10-19",
                        "start": "22:00",
                        "end": "22:29",
                        "created_at": "2026-10-19T19:00:00",
                        "note": "",
                    }
                ]
            }
        )
    )
    assert end() == datetime(2026, 10, 19, 22, 30)
    # Another user's grant changes nothing.
    grants.write_text(
        json.dumps(
            {
                "grants": [
                    {
                        "user": "other",
                        "day": "2026-10-19",
                        "start": "22:00",
                        "end": "22:29",
                        "created_at": "2026-10-19T19:00:00",
                        "note": "",
                    }
                ]
            }
        )
    )
    assert end() == datetime(2026, 10, 19, 22, 0)


def test_current_window_without_a_rule_or_with_a_bad_one(tmp_path):
    policy = tmp_path / "policy.yaml"
    _policy(tmp_path, "")
    assert current_window(datetime(2026, 10, 19, 20, 30), policy_path=policy) is None
    _policy(tmp_path, "personal_time: {from: 20}")
    with pytest.raises(PolicyError):
        current_window(datetime(2026, 10, 19, 20, 30), policy_path=policy)


# ---- the kid's notice (cli._notify_personal_time) ---------------------------


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILYMON_STATE_DIR", str(tmp_path))
    _policy(tmp_path)
    return tmp_path


def _sender(result: bool):
    calls: list[tuple] = []

    def send(summary, body, **kw):
        calls.append((summary, body, kw))
        return result

    return send, calls


def test_notice_once_per_day_only_when_delivered(state):
    from familymon.cli import _notify_personal_time

    send, calls = _sender(False)
    at = datetime(2026, 10, 19, 20, 5)
    assert _notify_personal_time(at, send) == 0  # nobody logged in: retry later
    assert calls and not (state / "personal_time_notified.json").exists()

    send, calls = _sender(True)
    assert _notify_personal_time(at + timedelta(minutes=30), send) == 1
    assert "Until 22:00" in calls[0][1] and "game time" in calls[0][1].lower()
    assert json.loads((state / "personal_time_notified.json").read_text())["day"] == "2026-10-19"
    assert _notify_personal_time(at + timedelta(minutes=31), send) == 0  # already told today
    assert _notify_personal_time(datetime(2026, 10, 20, 20, 5), send) == 1  # a new day


def test_no_notice_outside_the_window_or_without_a_rule(state):
    from familymon.cli import _notify_personal_time

    send, calls = _sender(True)
    assert _notify_personal_time(datetime(2026, 10, 19, 19, 59), send) == 0
    assert _notify_personal_time(datetime(2026, 10, 19, 22, 0), send) == 0
    _policy(state, "")
    assert _notify_personal_time(datetime(2026, 10, 19, 20, 30), send) == 0
    _policy(state, "personal_time: {from: 20}")  # unusable: stays quiet, logged elsewhere
    assert _notify_personal_time(datetime(2026, 10, 19, 20, 30), send) == 0
    assert calls == []

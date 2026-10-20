from __future__ import annotations

from datetime import datetime, time

from familymon.policy.conflicts import (
    DEFAULT_CONFLICT_CATEGORIES,
    Conflict,
    conflicting_block,
    find_conflicts,
)
from familymon.policy.schedule import Schedule, ScheduleBlock

# A Monday (2026-05-25) Maths block 09:20-10:00.
_SCHED = Schedule(
    blocks=[
        ScheduleBlock(day="Monday", start=time(9, 20), end=time(10, 0), subject="Maths"),
        ScheduleBlock(day="Monday", start=time(10, 50), end=time(11, 30), subject="English"),
    ]
)


def _dt(h, m):
    return datetime(2026, 5, 25, h, m)  # a Monday


def test_games_during_lesson_is_a_conflict():
    block = conflicting_block(_SCHED, _dt(9, 30), "games")
    assert block is not None and block.subject == "Maths"


def test_browsing_during_lesson_is_not_a_conflict():
    # Online class runs in the browser — browsing_other is not a distraction.
    assert conflicting_block(_SCHED, _dt(9, 30), "browsing_other") is None


def test_games_outside_any_lesson_is_not_a_conflict():
    assert conflicting_block(_SCHED, _dt(10, 15), "games") is None  # between blocks


def test_social_and_messaging_are_default_conflicts():
    assert conflicting_block(_SCHED, _dt(9, 30), "social") is not None
    assert conflicting_block(_SCHED, _dt(9, 30), "messaging") is not None


def test_conflict_categories_are_configurable():
    only_games = frozenset({"games"})
    assert conflicting_block(_SCHED, _dt(9, 30), "social", only_games) is None
    assert conflicting_block(_SCHED, _dt(9, 30), "games", only_games) is not None


def test_find_conflicts_groups_contiguous_episode():
    captures = [
        (_dt(9, 21), "games"),
        (_dt(9, 22), "games"),
        (_dt(9, 23), "games"),
    ]
    episodes = find_conflicts(_SCHED, captures)
    assert len(episodes) == 1
    e = episodes[0]
    assert e.block_subject == "Maths" and e.category == "games" and e.count == 3


def test_find_conflicts_splits_on_non_conflict_and_subject_change():
    captures = [
        (_dt(9, 30), "games"),  # Maths conflict
        (_dt(9, 40), "browsing_other"),  # attending class — breaks the episode
        (_dt(9, 50), "games"),  # Maths conflict again (new episode)
        (_dt(10, 55), "games"),  # English conflict (different subject)
    ]
    episodes = find_conflicts(_SCHED, captures)
    assert [(e.block_subject, e.count) for e in episodes] == [
        ("Maths", 1),
        ("Maths", 1),
        ("English", 1),
    ]


def test_conflict_seconds_estimate():
    e = Conflict("Maths", "games", _dt(9, 20), _dt(9, 24), count=5)
    # 4-minute span, 5 captures -> 5 * (240/4) = 300s
    assert e.seconds() == 300.0
    single = Conflict("Maths", "games", _dt(9, 20), _dt(9, 20), count=1)
    assert single.seconds(default_interval=60) == 60.0


def test_default_categories_exclude_video_and_browsing():
    assert "video" not in DEFAULT_CONFLICT_CATEGORIES
    assert "browsing_other" not in DEFAULT_CONFLICT_CATEGORIES
    assert {"games", "social", "messaging"} <= DEFAULT_CONFLICT_CATEGORIES


def test_streaming_video_during_lesson_is_a_conflict():
    block = conflicting_block(_SCHED, _dt(9, 30), "video", title="Funny Cats Compilation - YouTube")
    assert block is not None and block.subject == "Maths"


def test_generic_video_during_lesson_is_not_a_conflict():
    # A class stream (no recognized streaming brand) must not false-positive —
    # this is what protects "actually attending an online lesson".
    assert (
        conflicting_block(_SCHED, _dt(9, 30), "video", title="Maths Lesson - Adobe Connect") is None
    )
    # And video with no title at all is not enough signal to flag.
    assert conflicting_block(_SCHED, _dt(9, 30), "video", title=None) is None


def test_streaming_video_outside_lesson_is_not_a_conflict():
    assert conflicting_block(_SCHED, _dt(10, 15), "video", title="Show - Netflix") is None


def test_find_conflicts_flags_streaming_video_with_detail():
    captures = [
        (_dt(9, 25), "video", "Lecture - Adobe Connect"),  # attending — no conflict
        (_dt(9, 30), "video", "Trailer - YouTube"),  # streaming — conflict
        (_dt(9, 31), "video", "Another clip - YouTube"),
    ]
    episodes = find_conflicts(_SCHED, captures)
    assert len(episodes) == 1
    e = episodes[0]
    assert e.block_subject == "Maths" and e.category == "video" and e.count == 2
    assert e.detail == "youtube"


def test_entertainment_domain_video_during_lesson_is_a_conflict():
    # A video tab classified from its entertainment domain (title carries the
    # domain, e.g. "max.com", not a human brand) must still be flagged.
    assert conflicting_block(_SCHED, _dt(9, 30), "video", title="Watch now — max.com") is not None
    assert (
        conflicting_block(_SCHED, _dt(9, 30), "video", title="Movie — disneyplus.com") is not None
    )


def test_streaming_brands_are_configurable():
    only_vimeo = frozenset({"vimeo"})
    assert (
        conflicting_block(
            _SCHED, _dt(9, 30), "video", title="x - YouTube", streaming_brands=only_vimeo
        )
        is None
    )
    assert (
        conflicting_block(
            _SCHED, _dt(9, 30), "video", title="x - Vimeo", streaming_brands=only_vimeo
        )
        is not None
    )


def test_extras_blocks_never_create_conflicts():
    """Self-study / routine extras are notification items, not monitored
    school time: games during 'Maths (study)' is NOT a lesson conflict."""
    from datetime import time as dtime

    sched = Schedule(
        blocks=[
            ScheduleBlock(
                "Monday", dtime(10, 15), dtime(11, 15), "Maths", category="study", notify=True
            ),
            ScheduleBlock("Monday", dtime(11, 45), dtime(12, 25), "English", category="school"),
        ]
    )
    assert conflicting_block(sched, _dt(10, 30), "games") is None
    assert conflicting_block(sched, _dt(12, 0), "games") is not None

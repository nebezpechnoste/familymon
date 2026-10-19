from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path

from familymon.policy.importers import parse_kingsinterhigh_week
from familymon.policy.schedule import Schedule, ScheduleBlock

_SAMPLE = Path(__file__).parent / "data" / "kingsinterhigh_week_sample.json"


def _sample() -> dict:
    return json.loads(_SAMPLE.read_text())


def test_import_builds_blocks_from_observed_local_times():
    sched = parse_kingsinterhigh_week(_sample())
    # 3 lessons, but Monday Maths and Tuesday Maths are distinct (different day);
    # no exact dups in the sample -> 3 blocks.
    assert len(sched.blocks) == 3
    mon_first = sched.blocks[0]
    assert mon_first.day == "Monday"
    assert mon_first.subject == "Maths"
    assert mon_first.start == time(9, 20)  # observed (local), not origin 07:20
    assert mon_first.end == time(10, 0)
    assert mon_first.category == "school"
    assert "Berlin" in (sched.timezone or "")


def test_import_dedups_identical_recurring_slots():
    data = _sample()
    # Duplicate the Monday Maths lesson — should collapse to one block.
    data["timetableLessons"].append(dict(data["timetableLessons"][0]))
    sched = parse_kingsinterhigh_week(data)
    assert len(sched.blocks) == 3


def test_import_skips_lessons_missing_times():
    data = _sample()
    data["timetableLessons"].append(
        {"lessonDay": "Wednesday", "subjectName": "Broken"}  # no times
    )
    sched = parse_kingsinterhigh_week(data)
    assert all(b.subject != "Broken" for b in sched.blocks)


def test_blocks_sorted_by_day_then_start():
    sched = parse_kingsinterhigh_week(_sample())
    keys = [(b.day_index, b.start) for b in sched.blocks]
    assert keys == sorted(keys)


def test_block_at_finds_covering_block():
    sched = parse_kingsinterhigh_week(_sample())
    # Monday 2026-05-25 is a Monday; 09:30 is inside the 09:20-10:00 Maths block.
    monday_0930 = datetime(2026, 5, 25, 9, 30)
    hit = sched.block_at(monday_0930)
    assert hit is not None and hit.subject == "Maths"


def test_block_at_returns_none_outside_blocks():
    sched = parse_kingsinterhigh_week(_sample())
    # Monday 12:00 — after the last Monday block.
    assert sched.block_at(datetime(2026, 5, 25, 12, 0)) is None
    # End time is exclusive: exactly 10:00 is not covered by a 09:20-10:00 block.
    assert sched.block_at(datetime(2026, 5, 25, 10, 0)) is None


def test_block_at_respects_weekday():
    sched = parse_kingsinterhigh_week(_sample())
    # A Wednesday at 09:30 — sample has no Wednesday blocks.
    assert sched.block_at(datetime(2026, 5, 27, 9, 30)) is None


def test_yaml_round_trip(tmp_path: Path):
    sched = parse_kingsinterhigh_week(_sample())
    p = tmp_path / "schedule.yaml"
    sched.save(p)
    loaded = Schedule.load(p)
    assert [b.to_dict() for b in loaded.blocks] == [b.to_dict() for b in sched.blocks]
    assert loaded.timezone == sched.timezone


def test_schedule_block_covers_boundaries():
    b = ScheduleBlock(day="Monday", start=time(9, 20), end=time(10, 0), subject="x")
    assert b.covers(datetime(2026, 5, 25, 9, 20))  # start inclusive
    assert not b.covers(datetime(2026, 5, 25, 10, 0))  # end exclusive
    assert not b.covers(datetime(2026, 5, 26, 9, 30))  # wrong weekday


def test_group_name_round_trips_and_is_optional(tmp_path):
    from datetime import time as dtime

    from familymon.policy.schedule import Schedule, ScheduleBlock

    s = Schedule(
        blocks=[
            ScheduleBlock("Tuesday", dtime(9, 20), dtime(10, 0), "Cycle Testing",
                          group_name="10L CYCLE TEST AC-26/27"),
            ScheduleBlock("Monday", dtime(9, 20), dtime(10, 0), "Assembly"),  # no group
        ]
    )
    p = tmp_path / "s.yaml"
    s.save(p)
    loaded = Schedule.load(p)
    assert loaded.blocks[0].group_name == "10L CYCLE TEST AC-26/27"
    assert loaded.blocks[1].group_name is None
    # Old files without the key still load (field absent -> None).
    assert "group_name" not in s.blocks[1].to_dict()


def test_title_keywords_are_distinct_group_codes_only():
    from datetime import time as dtime

    from familymon.policy.schedule import Schedule, ScheduleBlock

    s = Schedule(
        blocks=[
            ScheduleBlock("Tuesday", dtime(9, 20), dtime(10, 0), "Cycle Testing",
                          group_name="10L CYCLE TEST AC-26/27"),
            ScheduleBlock("Friday", dtime(9, 20), dtime(10, 0), "Cycle Testing",
                          group_name="10L CYCLE TEST AC-26/27"),  # same code, other day
            ScheduleBlock("Monday", dtime(16, 15), dtime(16, 55), "Economics",
                          group_name="10RA ECO EXP 11 AC-26/27"),
            ScheduleBlock("Monday", dtime(9, 20), dtime(10, 0), "Assembly"),
        ]
    )
    # Deduped, order preserved, subjects NOT included ("Economics" would
    # relabel any page mentioning the word).
    assert s.title_keywords() == ["10L CYCLE TEST AC-26/27", "10RA ECO EXP 11 AC-26/27"]


def _blk(day, s, e, subject, category="school", notify=False):
    from datetime import time as dtime

    from familymon.policy.schedule import ScheduleBlock

    return ScheduleBlock(day, dtime(*s), dtime(*e), subject, category=category, notify=notify)


def test_block_at_category_filter_and_notify_round_trip(tmp_path):
    from datetime import datetime

    from familymon.policy.schedule import Schedule

    s = Schedule(blocks=[
        _blk("Monday", (10, 15), (11, 15), "Maths", category="study", notify=True),
        _blk("Monday", (11, 45), (12, 25), "English Language"),
    ])
    at_study = datetime(2026, 10, 5, 10, 30)  # Monday
    assert s.block_at(at_study).subject == "Maths"
    assert s.block_at(at_study, category="school") is None   # self-study isn't school
    assert s.block_at(datetime(2026, 10, 5, 12, 0), category="school").subject == "English Language"
    p = tmp_path / "s.yaml"
    s.save(p)
    loaded = Schedule.load(p)
    assert loaded.blocks[0].notify is True and loaded.blocks[0].category == "study"
    assert loaded.blocks[1].notify is False
    assert "notify" not in s.blocks[1].to_dict()


def test_starting_at_tolerance_and_notify_flag():
    from datetime import datetime

    from familymon.policy.schedule import Schedule

    s = Schedule(blocks=[
        _blk("Tuesday", (12, 0), (13, 0), "Workout", category="routine", notify=True),
        _blk("Tuesday", (12, 0), (12, 40), "Lesson"),  # not flagged
    ])
    tue = datetime(2026, 10, 6, 12, 1)
    assert [b.subject for b in s.starting_at(tue)] == ["Workout"]
    assert s.starting_at(datetime(2026, 10, 6, 11, 59)) == []           # not yet
    assert s.starting_at(datetime(2026, 10, 6, 12, 3)) == []            # past tolerance
    assert s.starting_at(datetime(2026, 10, 7, 12, 0)) == []            # wrong day


def test_extras_exclude_and_merge(tmp_path):
    from familymon.policy.importers import parse_kingsinterhigh_week
    from familymon.policy.schedule import apply_extras, load_extras

    extras = tmp_path / "extras.yaml"
    extras.write_text("""
exclude_subjects: [Discovery UK]
blocks:
  - {day: Monday, start: "10:15", end: "11:15", subject: Maths, category: study, notify: true}
  - {day: Saturday, start: "12:15", end: "13:15", subject: Workout, category: routine, notify: true}
""")
    excludes, blocks, breaks = load_extras(extras)
    assert breaks == []
    data = {"timetableLessons": [
        {"lessonDay": "Monday", "observedStartTime": "09:20", "observedEndTime": "10:00", "subjectName": "Assembly"},
        {"lessonDay": "Monday", "observedStartTime": "10:05", "observedEndTime": "10:45", "subjectName": "Discovery UK"},
    ]}
    s = apply_extras(parse_kingsinterhigh_week(data, exclude_subjects=excludes), excludes, blocks)
    assert [(b.day, b.subject, b.category, b.notify) for b in s.blocks] == [
        ("Monday", "Assembly", "school", False),
        ("Monday", "Maths", "study", True),
        ("Saturday", "Workout", "routine", True),
    ]
    # absent file -> nothing
    assert load_extras(tmp_path / "missing.yaml") == ([], [], [])


# --- school breaks -----------------------------------------------------------


def _half_term():
    from datetime import date

    from familymon.policy.schedule import SchoolBreak

    return SchoolBreak(date(2026, 10, 19), date(2026, 11, 1), name="Half term")


def test_break_suspends_school_blocks_only():
    from datetime import date, datetime

    from familymon.policy.schedule import Schedule

    s = Schedule(
        blocks=[
            _blk("Monday", (9, 20), (10, 0), "Assembly"),
            _blk("Monday", (10, 15), (11, 15), "Maths", category="study", notify=True),
        ],
        breaks=[_half_term()],
    )
    in_break = datetime(2026, 10, 19, 9, 30)  # Monday inside the break
    assert s.block_at(in_break, category="school") is None
    assert s.block_at(in_break) is None  # no category filter: still suspended
    assert s.block_at(datetime(2026, 10, 19, 10, 30)).subject == "Maths"  # extras carry on
    assert s.block_at(datetime(2026, 10, 19, 10, 16)) is not None
    # Boundaries are inclusive dates: last day suspended, the Monday after is back.
    assert s.block_at(datetime(2026, 10, 26, 9, 30), category="school") is None
    assert s.block_at(datetime(2026, 11, 2, 9, 30), category="school").subject == "Assembly"
    assert s.block_at(datetime(2026, 10, 12, 9, 30), category="school").subject == "Assembly"
    assert s.break_on(date(2026, 10, 25)) is not None
    assert s.break_on(date(2026, 11, 2)) is None
    assert s.suspended(s.blocks[0], date(2026, 10, 20)).name == "Half term"
    assert s.suspended(s.blocks[1], date(2026, 10, 20)) is None


def test_break_with_more_categories_silences_reminders_too():
    from datetime import date, datetime

    from familymon.policy.schedule import Schedule, SchoolBreak

    xmas = SchoolBreak(date(2026, 12, 21), date(2027, 1, 3), categories=("school", "study"))
    s = Schedule(
        blocks=[
            _blk("Monday", (10, 15), (11, 15), "Maths", category="study", notify=True),
            _blk("Monday", (12, 0), (13, 0), "Workout", category="routine", notify=True),
        ],
        breaks=[xmas],
    )
    mon = datetime(2026, 12, 21, 10, 16)
    assert s.starting_at(mon) == []  # study reminder paused
    assert [b.subject for b in s.starting_at(datetime(2026, 12, 21, 12, 1))] == ["Workout"]
    assert [b.subject for b in s.starting_at(datetime(2027, 1, 4, 10, 16))] == ["Maths"]


def test_breaks_round_trip_and_yaml_dates(tmp_path):
    from datetime import date

    from familymon.policy.schedule import Schedule, SchoolBreak

    s = Schedule(
        blocks=[_blk("Monday", (9, 20), (10, 0), "Assembly")],
        timezone="Europe/Berlin",
        breaks=[
            _half_term(),
            SchoolBreak(date(2026, 12, 21), date(2027, 1, 3), categories=("school", "study")),
        ],
    )
    path = tmp_path / "schedule.yaml"
    s.save(path)
    loaded = Schedule.load(path)
    assert loaded.breaks == s.breaks
    assert loaded.blocks == s.blocks
    # No breaks -> key absent, older readers unaffected.
    Schedule(blocks=s.blocks).save(path)
    assert "breaks" not in path.read_text()
    assert Schedule.load(path).breaks == []
    # Unquoted dates (what yaml hands back as date objects) and quoted strings both parse.
    path.write_text(
        "blocks: []\nbreaks:\n  - {start: 2026-10-19, end: 2026-11-01, name: Half term}\n"
        '  - {start: "2026-12-21", end: "2027-01-03", categories: study}\n'
    )
    br = Schedule.load(path).breaks
    assert br[0] == _half_term()
    assert br[1].categories == ("study",) and br[1].name == ""


def test_break_validation():
    import pytest

    from familymon.policy.schedule import SchoolBreak

    with pytest.raises(ValueError):
        SchoolBreak.from_dict({"start": "2026-11-01", "end": "2026-10-19"})
    with pytest.raises(ValueError):
        SchoolBreak.from_dict({"start": "2026-10-19"})
    with pytest.raises(ValueError):
        SchoolBreak.from_dict({"start": "2026-10-19", "end": "2026-10-20", "categories": []})
    with pytest.raises(ValueError):
        SchoolBreak.from_dict({"start": "not a date", "end": "2026-10-20"})
    assert SchoolBreak.from_dict({"start": "2026-10-19", "end": "2026-10-19"}).label == (
        "school break 2026-10-19 to 2026-10-19"
    )


def test_extras_with_breaks_and_idempotent_apply(tmp_path):
    from familymon.policy.schedule import Schedule, apply_extras, load_extras

    extras = tmp_path / "extras.yaml"
    extras.write_text(
        """
exclude_subjects: [Discovery UK]
blocks:
  - {day: Monday, start: "10:15", end: "11:15", subject: Maths, category: study, notify: true}
breaks:
  - {start: 2026-10-19, end: 2026-11-01, name: Half term}
"""
    )
    excludes, blocks, breaks = load_extras(extras)
    assert [b.name for b in breaks] == ["Half term"]
    base = Schedule(
        blocks=[
            _blk("Monday", (9, 20), (10, 0), "Assembly"),
            _blk("Monday", (10, 5), (10, 45), "Discovery UK"),
        ]
    )
    once = apply_extras(base, excludes, blocks, breaks)
    twice = apply_extras(once, excludes, blocks, breaks)
    assert [(b.subject, b.category) for b in once.blocks] == [
        ("Assembly", "school"),
        ("Maths", "study"),
    ]
    assert twice.blocks == once.blocks  # extras not duplicated on re-application
    assert twice.breaks == breaks


def test_cli_schedule_refresh_rebuilds_extras_without_export(tmp_path):
    """`schedule refresh` keeps imported lessons, rebuilds extras + breaks from the file."""
    from click.testing import CliRunner

    from familymon.cli import main
    from familymon.policy.schedule import Schedule

    state = tmp_path / "state"
    cfg = tmp_path / "cfg"
    state.mkdir()
    cfg.mkdir()
    Schedule(
        blocks=[
            _blk("Monday", (9, 20), (10, 0), "Assembly"),
            _blk("Monday", (10, 5), (10, 45), "Extra Curricular"),
            _blk("Tuesday", (12, 0), (13, 0), "Old routine", category="routine", notify=True),
        ],
        timezone="Europe/Berlin",
    ).save(state / "schedule.yaml")
    (cfg / "schedule_extras.yaml").write_text(
        """
exclude_subjects: [Extra Curricular]
blocks:
  - {day: Monday, start: "10:15", end: "11:15", subject: Maths, category: study, notify: true}
breaks:
  - {start: 2026-10-19, end: 2026-11-01, name: Half term}
"""
    )
    env = {"FAMILYMON_STATE_DIR": str(state), "FAMILYMON_CONFIG_DIR": str(cfg)}
    r = CliRunner().invoke(main, ["schedule", "refresh"], env=env)
    assert r.exit_code == 0, r.output
    assert "1 break(s)" in r.output and "Half term 2026-10-19 to 2026-11-01" in r.output
    s = Schedule.load(state / "schedule.yaml")
    assert [(b.subject, b.category) for b in s.blocks] == [
        ("Assembly", "school"),
        ("Maths", "study"),
    ]
    assert s.timezone == "Europe/Berlin"
    assert [b.name for b in s.breaks] == ["Half term"]
    # Running it again changes nothing.
    r2 = CliRunner().invoke(main, ["schedule", "refresh"], env=env)
    assert r2.exit_code == 0, r2.output
    assert Schedule.load(state / "schedule.yaml") == s
    show = CliRunner().invoke(main, ["schedule", "show"], env=env)
    assert show.exit_code == 0, show.output
    assert "break: Half term 2026-10-19 to 2026-11-01 (school blocks suspended)" in show.output
    # Without a schedule the command refuses instead of inventing one.
    missing = CliRunner().invoke(
        main, ["schedule", "refresh"], env={**env, "FAMILYMON_STATE_DIR": str(tmp_path / "none")}
    )
    assert missing.exit_code != 0 and "no schedule" in missing.output

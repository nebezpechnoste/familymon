"""Import a school timetable into a familymon Schedule.

Currently supports the Kings InterHigh student API
(`GET /student/api/timetable/week`). The parent fetches that JSON once a term
(it's termly-stable) and runs `familymonctl schedule import`.

We use `observedStartTime`/`observedEndTime` (the kid's *local* time) + `lessonDay`
to build weekly-recurring blocks. The `origin*` fields are UTC; we ignore them
because captures are timestamped local and the observed local times are stable
across DST for this kid (UK and the local zone shift together). All lessons map
to category "school"; the subject becomes the block label. Breaks (in
`timetablePeriods` but not `timetableLessons`) are intentionally not imported —
only actual lessons are "school" blocks; the parent can add others by hand.
"""

from __future__ import annotations

from familymon.policy.schedule import Schedule, ScheduleBlock, _parse_hhmm


def parse_kingsinterhigh_week(data: dict, exclude_subjects: list[str] | None = None) -> Schedule:
    """`exclude_subjects`: lesson subjects to leave out (case-insensitive) —
    e.g. self-paced "Discovery UK" that shouldn't count as monitored time."""
    drop = {x.strip().lower() for x in (exclude_subjects or [])}
    seen: dict[tuple, ScheduleBlock] = {}
    for lesson in data.get("timetableLessons") or []:
        day = lesson.get("lessonDay")
        start_s = lesson.get("observedStartTime")
        end_s = lesson.get("observedEndTime")
        if not (day and start_s and end_s):
            continue
        start = _parse_hhmm(start_s)
        end = _parse_hhmm(end_s)
        subject = (lesson.get("subjectName") or "").strip()
        if subject.lower() in drop:
            continue
        group_name = (lesson.get("groupName") or "").strip() or None
        # Dedup identical recurring slots (the week JSON lists dated lessons;
        # the day+time+subject recurs weekly).
        key = (day, start, end, subject)
        seen[key] = ScheduleBlock(
            day=day,
            start=start,
            end=end,
            subject=subject,
            category="school",
            group_name=group_name,
        )
    blocks = sorted(seen.values(), key=lambda b: (b.day_index, b.start))
    return Schedule(blocks=blocks, timezone=data.get("timezone"))

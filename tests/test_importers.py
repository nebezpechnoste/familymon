def test_import_captures_group_names():
    from familymon.policy.importers import parse_kingsinterhigh_week

    data = {
        "timetableLessons": [
            {
                "lessonDay": "Tuesday",
                "observedStartTime": "09:20",
                "observedEndTime": "10:00",
                "subjectName": "Cycle Testing",
                "groupName": "10L CYCLE TEST AC-26/27",
            },
            {
                "lessonDay": "Monday",
                "observedStartTime": "09:20",
                "observedEndTime": "10:00",
                "subjectName": "Assembly",
            },  # no groupName in payload
        ]
    }
    s = parse_kingsinterhigh_week(data)
    by_subject = {b.subject: b for b in s.blocks}
    assert by_subject["Cycle Testing"].group_name == "10L CYCLE TEST AC-26/27"
    assert by_subject["Assembly"].group_name is None
    assert s.title_keywords() == ["10L CYCLE TEST AC-26/27"]

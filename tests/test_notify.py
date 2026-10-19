from familymon.notify import notify_argv


def test_notify_argv_identifies_as_known_desktop_app():
    """Plasma mutes popups for '[@other]' apps on the deployed laptop; the
    desktop-entry hint is what makes familymon a known application."""
    argv = notify_argv("Maths", "10:15–11:15 — time for Maths")
    assert argv[0] == "notify-send"
    assert "-a" in argv and argv[argv.index("-a") + 1] == "familymon"
    assert "string:desktop-entry:familymon" in argv
    assert argv[-2:] == ["Maths", "10:15–11:15 — time for Maths"]


def test_notify_argv_icon_and_urgency_are_overridable():
    """Live lesson warnings use a warning icon; schedule notices keep the default."""
    argv = notify_argv(
        "Maths — lesson in progress", "body", icon="dialog-warning", urgency="normal"
    )
    assert argv[argv.index("-i") + 1] == "dialog-warning"
    assert argv[argv.index("-u") + 1] == "normal"
    default = notify_argv("Maths", "body")
    assert default[default.index("-i") + 1] == "appointment-soon"

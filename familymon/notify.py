"""Desktop notification helper for the kid's session.

Plasma files notifications by application. One without a recognised desktop
entry falls under "[Applications][@other]", whose popups the user (or distro)
may have disabled — on the deployed laptop they were, so plain
`notify-send -a familymon` went silently to the history. Shipping
familymon.desktop and passing the `desktop-entry` hint makes familymon a
known app with its own (popups-on by default) settings.
"""

from __future__ import annotations

import subprocess

DESKTOP_ENTRY = "familymon"  # /usr/share/applications/familymon.desktop


def notify_argv(
    summary: str,
    body: str,
    *,
    timeout_ms: int = 60000,
    icon: str = "appointment-soon",
    urgency: str = "normal",
) -> list[str]:
    return [
        "notify-send",
        "-a",
        "familymon",
        "-h",
        f"string:desktop-entry:{DESKTOP_ENTRY}",
        "-u",
        urgency,
        "-i",
        icon,
        "-t",
        str(timeout_ms),
        summary,
        body,
    ]


def send(
    summary: str, body: str, *, icon: str = "appointment-soon", urgency: str = "normal"
) -> None:
    argv = notify_argv(summary, body, icon=icon, urgency=urgency)
    subprocess.run(argv, check=False, timeout=15)


def send_as(
    user: str,
    summary: str,
    body: str,
    *,
    icon: str = "appointment-soon",
    urgency: str = "normal",
) -> bool:
    """Notify another user's desktop session from root (system-scope timers
    such as the lesson web block). Needs that user's session bus; returns
    False when there is none (not logged in)."""
    import os
    import pwd

    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        return False
    runtime = f"/run/user/{pw.pw_uid}"
    if not os.path.exists(f"{runtime}/bus"):
        return False
    argv = [
        "runuser",
        "-u",
        user,
        "--",
        "env",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        f"XDG_RUNTIME_DIR={runtime}",
        f"HOME={pw.pw_dir}",
        *notify_argv(summary, body, icon=icon, urgency=urgency),
    ]
    subprocess.run(argv, check=False, timeout=15)
    return True

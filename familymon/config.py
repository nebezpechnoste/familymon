"""Runtime configuration: where state lives, default intervals.

Dev runs (non-root) put state under XDG_STATE_HOME (~/.local/state/familymon by
default). Production runs as the `familymon` system user with state under
/var/lib/familymon — when EUID is 0 or the FAMILYMON_STATE_DIR env var is set,
we honor that path.
"""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    override = os.environ.get("FAMILYMON_STATE_DIR")
    if override:
        return Path(override)
    if os.geteuid() == 0:
        return Path("/var/lib/familymon")
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "familymon"


def db_path() -> Path:
    return state_dir() / "captures.db"


def screenshots_dir() -> Path:
    return state_dir() / "screenshots"


def schedule_path() -> Path:
    return state_dir() / "schedule.yaml"


def policy_path() -> Path:
    """Parent-editable enforcement policy driving timekpr-next."""
    return state_dir() / "policy.yaml"


def user_state_dir(user: str) -> Path:
    """Another (monitored) user's default state dir — where *their* capture
    loop writes. Used by root-side reconcilers that must read the kid's
    captures. Honors the same XDG default as state_dir() for a non-root user."""
    import pwd

    home = Path(pwd.getpwnam(user).pw_dir)
    return home / ".local" / "state" / "familymon"


_DEFAULTS_DIR = Path(__file__).parent / "defaults"


def config_file(name: str) -> Path:
    """Resolve a bundled-config YAML with a parent-editable override layer.

    Order: $FAMILYMON_CONFIG_DIR/<name> (tests, custom setups) →
    /etc/familymon/<name> (deployed override, e.g. the messaging-app privacy
    contract, survives package upgrades) → the bundled default shipped inside
    the package. The bundled copy always exists, so this never returns a
    missing path unless the name itself is wrong.
    """
    override_dir = os.environ.get("FAMILYMON_CONFIG_DIR")
    candidates = [Path(override_dir) / name] if override_dir else []
    candidates.append(Path("/etc/familymon") / name)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return _DEFAULTS_DIR / name


def capture_interval_seconds() -> float:
    return float(os.environ.get("FAMILYMON_INTERVAL", "60"))

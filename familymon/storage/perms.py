"""Permissions on the monitoring state.

Production state lives in /var/lib/familymon, written by root. The dashboard
runs as the unprivileged `familymon` system user and only *reads*: the capture
DB and the schedule are made group-readable for that group; everything else
(screenshots, policy, grants, watermarks) stays root-only. The monitored user
gets no access to any of it.

Outside root (dev runs) there is no shared group: files are 0600, dirs 0700,
exactly as before.
"""

from __future__ import annotations

import grp
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_GROUP = "familymon"


def shared_gid() -> int | None:
    """The dashboard's group id, when running as root and the group exists."""
    if os.geteuid() != 0:
        return None
    try:
        return grp.getgrnam(STATE_GROUP).gr_gid
    except KeyError:
        return None


def secure(path: Path, *, shared: bool = False) -> None:
    """Owner-only (0600/0700), or group-readable by the dashboard group
    (0640/0750, root:familymon) when `shared` and that group exists. As root
    the owner is always set to root. Missing paths and symlinks are skipped;
    failures are logged, never raised: a permissions hiccup must not stop
    capture."""
    try:
        if path.is_symlink() or not path.exists():
            return
        is_dir = path.is_dir()
        gid = shared_gid() if shared else None
        if os.geteuid() == 0:
            # Root-written state is root-owned, including anything moved in
            # from the monitored user's home (a rename keeps the old owner).
            os.chown(path, 0, gid if gid is not None else 0, follow_symlinks=False)
        if gid is not None:
            os.chmod(path, 0o750 if is_dir else 0o640)
        else:
            os.chmod(path, 0o700 if is_dir else 0o600)
    except OSError as e:
        logger.warning("could not secure %s: %s", path, e)


def secure_tree(root: Path, *, shared: bool = False) -> None:
    """secure() on a directory and everything below it."""
    secure(root, shared=shared)
    if not root.is_dir() or root.is_symlink():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*dirnames, *filenames):
            secure(Path(dirpath) / name, shared=shared)

"""Screenshot storage hardening: the on-disk tree must be private (0700).

A deployed box showed the screenshots root created 0775 — `mkdir(mode=0o700)`
only applies to the leaf day dir, the root inherited the umask. The parent
state dir is 0700 so nothing leaked, but defense in depth says every level of
the screenshot tree is private, including a root that already exists with lax
bits from an older build.
"""

from __future__ import annotations

import stat
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from familymon.capture.backend import CaptureResult, WindowSnapshot
from familymon.capture.persist import CapturePersistence
from familymon.storage.models import Base


@pytest.fixture
def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _result() -> CaptureResult:
    return CaptureResult(
        captured_at=datetime(2026, 10, 1, 18, 52),
        active_window=WindowSnapshot(wm_class="google-chrome", title="x", active=True),
        other_windows=(),
        screenshot_bytes=b"\x89PNG fake",
    )


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_screenshot_tree_is_private_when_created_fresh(sessions, tmp_path):
    root = tmp_path / "screenshots"
    p = CapturePersistence(sessions, root)
    cap_id = p.write(_result(), same_as_prev=False, phash="abc")

    day = root / "2026-10-01"
    assert (day / f"{cap_id}.png").read_bytes() == b"\x89PNG fake"
    assert _mode(root) == 0o700
    assert _mode(day) == 0o700


def test_screenshot_root_with_lax_bits_is_tightened(sessions, tmp_path):
    """Pre-existing 0775 root (as seen on the deployed box) must end up 0700."""
    root = tmp_path / "screenshots"
    root.mkdir(mode=0o775)
    root.chmod(0o775)  # explicit, independent of the test runner's umask
    assert _mode(root) == 0o775

    CapturePersistence(sessions, root).write(_result(), same_as_prev=False, phash="abc")
    assert _mode(root) == 0o700
    assert _mode(root / "2026-10-01") == 0o700


def test_deduped_tick_writes_no_file_and_no_dirs(sessions, tmp_path):
    root = tmp_path / "screenshots"
    CapturePersistence(sessions, root).write(_result(), same_as_prev=True, phash="abc")
    assert not root.exists()

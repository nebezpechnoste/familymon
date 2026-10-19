"""Moving the monitored user's old state out of their home (storage/migrate.py).

Everything happens under tmp_path; the "home" is a fake directory.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from familymon.storage.migrate import MigrationError, marker_path, migrate_user_state
from familymon.storage.models import Event
from familymon.storage.session import make_engine, make_session_factory

WHEN = datetime(2026, 10, 19, 12, 0)


def _make_db(path: Path, *, captures: int, shots_dir: Path | None, events: list[str]) -> None:
    sessions = make_session_factory(make_engine(path))
    from familymon.storage.models import Capture

    with sessions() as s:
        for i in range(captures):
            shot = str(shots_dir / "2026-10-18" / f"{i + 1}.png") if shots_dir else None
            s.add(Capture(captured_at=WHEN, active_wm_class="x", active_title=f"t{i}",
                          windows=[], screenshot_path=shot))
        for kind in events:
            s.add(Event(at=WHEN, kind=kind, data={"k": kind}))
        s.commit()


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home" / "kid"
    src = home / ".local" / "state" / "familymon"
    dst = tmp_path / "var" / "lib" / "familymon"
    shots = src / "screenshots"
    (shots / "2026-10-18").mkdir(parents=True)
    for i in (1, 2):
        (shots / "2026-10-18" / f"{i}.png").write_bytes(b"png%d" % i)
    _make_db(src / "captures.db", captures=2, shots_dir=shots, events=["heartbeat"])
    (src / "schedule.yaml").write_text("blocks: []\n")
    (src / "notified.json").write_text("{}")
    dst.mkdir(parents=True)
    _make_db(dst / "captures.db", captures=0, shots_dir=None, events=["grant_hours", "web_block"])
    (dst / "policy.yaml").write_text("user: kid\n")
    return home, src, dst


def _rows(db: Path, sql: str):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_moves_everything_out_of_the_home(layout):
    home, src, dst = layout
    report = migrate_user_state("kid", src, dst, home=home, now=WHEN)

    assert report.captures == 2 and report.screenshots_moved == 2
    assert report.events_carried == 2
    assert sorted(report.copied) == ["notified.json", "schedule.yaml"]
    assert not src.exists(), "nothing of the monitoring state stays in the home"

    db = dst / "captures.db"
    paths = [p for (p,) in _rows(db, "SELECT screenshot_path FROM capture ORDER BY id")]
    assert paths == [str(dst / "screenshots" / "2026-10-18" / f"{i}.png") for i in (1, 2)]
    assert all(Path(p).read_bytes().startswith(b"png") for p in paths)
    kinds = sorted(k for (k,) in _rows(db, "SELECT kind FROM event"))
    assert kinds == ["grant_hours", "heartbeat", "web_block"]
    assert (dst / "schedule.yaml").read_text() == "blocks: []\n"

    backup = Path(report.backup_dir)
    assert (backup / "captures.db.root-before-migration").exists()
    assert (backup / "state" / "captures.db").exists(), "the old file is kept, not deleted"
    assert json.loads(marker_path(dst, "kid").read_text())["status"] == "complete"


def test_second_run_is_a_no_op(layout):
    home, src, dst = layout
    migrate_user_state("kid", src, dst, home=home, now=WHEN)
    assert migrate_user_state("kid", src, dst, home=home, now=WHEN) is None


def test_nothing_to_migrate(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    assert migrate_user_state("kid", home / "nope", tmp_path / "dst", home=home) is None


def test_existing_root_schedule_wins(layout):
    home, src, dst = layout
    (dst / "schedule.yaml").write_text("root: yes\n")
    report = migrate_user_state("kid", src, dst, home=home, now=WHEN)
    assert report.kept_root == ["schedule.yaml"]
    assert (dst / "schedule.yaml").read_text() == "root: yes\n"


def test_refuses_a_symlinked_state_dir(tmp_path):
    """The user controls their home: a link pointing root at /etc (or here,
    any other dir) must not be followed."""
    home = tmp_path / "home"
    (home / ".local" / "state").mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (home / ".local" / "state" / "familymon").symlink_to(target)
    with pytest.raises(MigrationError, match="symlink"):
        migrate_user_state("kid", home / ".local" / "state" / "familymon", tmp_path / "dst",
                           home=home)


def test_symlinked_files_inside_are_not_copied(layout, tmp_path):
    home, src, dst = layout
    secret = tmp_path / "secret.txt"
    secret.write_text("do not copy")
    (src / "schedule.yaml").unlink()
    (src / "schedule.yaml").symlink_to(secret)
    (src / "screenshots" / "2026-10-18" / "3.png").symlink_to(secret)
    report = migrate_user_state("kid", src, dst, home=home, now=WHEN)
    assert "schedule.yaml" not in report.copied
    assert not (dst / "schedule.yaml").exists()
    assert not (dst / "screenshots" / "2026-10-18" / "3.png").exists()
    assert report.screenshots_moved == 2


def test_resumes_after_the_database_step_without_duplicating_events(layout, monkeypatch):
    home, src, dst = layout
    import familymon.storage.migrate as m

    real_move = m.shutil.move

    def failing_move(a, b):
        if Path(a) == src:
            raise OSError("disk full")
        return real_move(a, b)

    monkeypatch.setattr(m.shutil, "move", failing_move)
    with pytest.raises(OSError):
        migrate_user_state("kid", src, dst, home=home, now=WHEN)
    assert json.loads(marker_path(dst, "kid").read_text())["status"] == "db"

    monkeypatch.setattr(m.shutil, "move", real_move)
    report = migrate_user_state("kid", src, dst, home=home, now=WHEN)
    assert report is not None and not src.exists()
    kinds = sorted(k for (k,) in _rows(dst / "captures.db", "SELECT kind FROM event"))
    assert kinds == ["grant_hours", "heartbeat", "web_block"]


def test_captures_root_took_before_the_migration_are_kept(layout):
    """If migration failed once, the root supervisor kept capturing into the
    root DB; a later migration must append those, classifications included."""
    from familymon.storage.models import Capture, Classification

    home, src, dst = layout
    sessions = make_session_factory(make_engine(dst / "captures.db"))
    with sessions() as s:
        c = Capture(captured_at=WHEN, active_wm_class="root", active_title="root-took",
                    windows=[])
        s.add(c)
        s.flush()
        s.add(Classification(capture_id=c.id, category="games", confidence=0.9,
                             evidence_text="e", source="heuristic", created_at=WHEN))
        s.commit()
    report = migrate_user_state("kid", src, dst, home=home, now=WHEN)
    assert report.root_captures_carried == 1
    rows = _rows(dst / "captures.db",
                 "SELECT c.active_title, k.category FROM capture c "
                 "LEFT JOIN classification k ON k.capture_id = c.id ORDER BY c.id")
    assert rows == [("t0", None), ("t1", None), ("root-took", "games")]

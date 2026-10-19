"""One-time move of a monitored user's state out of their home.

Releases before the root capture supervisor kept the capture DB, screenshots,
schedule and notification watermarks in the monitored user's own
~/.local/state/familymon, where that user could read, edit or delete them.
This moves them into the root state dir (/var/lib/familymon):

  * captures.db: copied with SQLite's backup API, screenshot paths rewritten,
    and the root DB's existing rows (audit events; captures the supervisor
    took if an earlier attempt failed) appended, so there is one database
    again. The previous root DB is kept in the backup dir.
  * screenshots/: moved; ownership becomes root, nobody else reads them.
  * schedule.yaml, notified.json, warned.json: copied when root has none.
  * Everything left in the old directory is moved to
    /var/lib/familymon/migrated/<user>-<timestamp>/ (root-only), so nothing
    stays in the user's home and nothing is deleted.

The source directory is controlled by the monitored user, so it is treated
as hostile: symlinks are never followed (a link to /etc must not get copied
into a group-readable place), and the move refuses a source path that is
itself a symlink at any level below the home directory.

Idempotent and resumable: a marker file per user records progress.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .perms import secure, secure_tree

logger = logging.getLogger(__name__)

_SIDE_FILES = ("schedule.yaml", "notified.json", "warned.json")


class MigrationError(RuntimeError):
    pass


@dataclass
class MigrationReport:
    user: str
    source: str
    captures: int = 0
    events_carried: int = 0
    root_captures_carried: int = 0
    screenshots_moved: int = 0
    copied: list[str] = field(default_factory=list)
    kept_root: list[str] = field(default_factory=list)
    backup_dir: str | None = None


def marker_path(dst: Path, user: str) -> Path:
    return dst / f".migrated-from-{user}"


def _check_not_symlinked(src: Path, home: Path) -> None:
    """Refuse when the source or any directory between it and the home is a
    symlink, so root never acts on a path the user redirected."""
    p = src
    while True:
        if p.is_symlink():
            raise MigrationError(f"{p} is a symlink; refusing to migrate from it")
        if p == home or p.parent == p:
            return
        p = p.parent


def _copy_db(src_db: Path, new_db: Path, src_shots: Path, dst_shots: Path) -> int:
    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(new_db)
        try:
            src.backup(dst)
            dst.execute(
                "UPDATE capture SET screenshot_path = ? || substr(screenshot_path, ?) "
                "WHERE screenshot_path LIKE ?",
                (str(dst_shots), len(str(src_shots)) + 1, f"{src_shots}/%"),
            )
            dst.commit()
            (ok,) = dst.execute("PRAGMA integrity_check").fetchone()
            if ok != "ok":
                raise MigrationError(f"copied database fails integrity_check: {ok}")
            return dst.execute("SELECT count(*) FROM capture").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()


def _columns(con: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA {schema}.table_info({table})")]


def _carry_root_rows(new_db: Path, old_root_db: Path) -> tuple[int, int]:
    """Append the old root DB's rows to the migrated DB: its events (audit:
    grants, web block, chargeback) and any captures it holds (written by the
    root supervisor if an earlier migration attempt failed), with their
    classifications re-pointed at the new capture ids.
    Returns (events, captures) carried."""
    con = sqlite3.connect(new_db)
    try:
        con.execute("ATTACH DATABASE ? AS old", (f"file:{old_root_db}?mode=ro",))
        tables = {r[0] for r in con.execute("SELECT name FROM old.sqlite_master")}
        events = captures = 0
        if "event" in tables:
            events = con.execute(
                "INSERT INTO event (at, kind, data) SELECT at, kind, data FROM old.event"
            ).rowcount
        if "capture" in tables:
            cap_cols = [c for c in _columns(con, "old", "capture")
                        if c != "id" and c in _columns(con, "main", "capture")]
            cls_cols = []
            if "classification" in tables:
                cls_cols = [c for c in _columns(con, "old", "classification")
                            if c not in ("id", "capture_id")
                            and c in _columns(con, "main", "classification")]
            cap_sel = ", ".join(cap_cols)
            for (old_id, *values) in con.execute(
                f"SELECT id, {cap_sel} FROM old.capture ORDER BY id"
            ).fetchall():
                new_id = con.execute(
                    f"INSERT INTO capture ({cap_sel}) VALUES ({', '.join('?' * len(cap_cols))})",
                    values,
                ).lastrowid
                captures += 1
                if cls_cols:
                    cls_sel = ", ".join(cls_cols)
                    con.execute(
                        f"INSERT INTO classification (capture_id, {cls_sel}) "
                        f"SELECT ?, {cls_sel} FROM old.classification WHERE capture_id = ?",
                        (new_id, old_id),
                    )
        con.commit()
        return events, captures
    finally:
        con.close()


def _move_screenshots(src_shots: Path, dst_shots: Path) -> int:
    """Move real files only (no symlinks), merging into an existing tree."""
    moved = 0
    dst_shots.mkdir(parents=True, exist_ok=True, mode=0o700)
    for day in sorted(src_shots.iterdir()):
        if day.is_symlink() or not day.is_dir():
            continue
        target_day = dst_shots / day.name
        target_day.mkdir(exist_ok=True, mode=0o700)
        for f in day.iterdir():
            if f.is_symlink() or not f.is_file():
                continue
            shutil.move(str(f), str(target_day / f.name))
            moved += 1
    return moved


def migrate_user_state(
    user: str,
    src: Path,
    dst: Path,
    *,
    home: Path,
    now: datetime | None = None,
) -> MigrationReport | None:
    """Move `src` (the user's old state dir) into `dst`. Returns None when
    there is nothing to do (already migrated, or no old state)."""
    marker = marker_path(dst, user)
    progress = _read_marker(marker)
    if progress.get("status") == "complete":
        return None
    if not src.exists() and not src.is_symlink():
        return None
    _check_not_symlinked(src, home)
    if not src.is_dir():
        raise MigrationError(f"{src} is not a directory")

    when = now or datetime.now()
    dst.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = MigrationReport(user=user, source=str(src))
    backup = Path(progress["backup_dir"]) if progress.get("backup_dir") else (
        dst / "migrated" / f"{user}-{when:%Y%m%d-%H%M%S}"
    )
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    secure(backup.parent)
    report.backup_dir = str(backup)

    src_db = src / "captures.db"
    dst_db = dst / "captures.db"
    src_shots = src / "screenshots"
    dst_shots = dst / "screenshots"

    # Order matters for a re-run after a failure: the source DB is only
    # read, screenshots are moved before the root DB is replaced, and the
    # marker records "db" right after the replacement so a re-run never
    # carries the root events twice.
    db_pending = progress.get("status") != "db" and src_db.is_file() and not src_db.is_symlink()
    new_db = dst / "captures.db.migrating"
    if db_pending:
        new_db.unlink(missing_ok=True)
        report.captures = _copy_db(src_db, new_db, src_shots, dst_shots)

    if src_shots.is_dir() and not src_shots.is_symlink():
        report.screenshots_moved = _move_screenshots(src_shots, dst_shots)
        secure_tree(dst_shots)

    if db_pending:
        if dst_db.exists():
            report.events_carried, report.root_captures_carried = _carry_root_rows(
                new_db, dst_db
            )
            shutil.move(str(dst_db), str(backup / "captures.db.root-before-migration"))
            for side in ("-journal", "-wal", "-shm"):
                p = dst_db.with_name(dst_db.name + side)
                if p.exists():
                    shutil.move(str(p), str(backup / (p.name + ".root-before-migration")))
        new_db.replace(dst_db)
        secure(dst_db, shared=True)
        _write_marker(marker, report, "db")

    for name in _SIDE_FILES:
        s = src / name
        if s.is_symlink() or not s.is_file():
            continue
        d = dst / name
        if d.exists():
            report.kept_root.append(name)
            continue
        shutil.copyfile(s, d, follow_symlinks=False)
        secure(d, shared=name == "schedule.yaml")
        report.copied.append(name)

    # Whatever remains (the old DB file, watermarks, empty screenshot dirs)
    # leaves the user's home for the root-only backup.
    shutil.move(str(src), str(backup / "state"))
    secure_tree(backup)
    secure(dst, shared=True)

    _write_marker(marker, report, "complete")
    return report


def _read_marker(marker: Path) -> dict:
    try:
        raw = json.loads(marker.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_marker(marker: Path, report: MigrationReport, status: str) -> None:
    marker.write_text(json.dumps({"status": status, **asdict(report)}, indent=1))
    secure(marker)

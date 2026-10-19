"""Self-healing for a damaged SQLite capture database.

The deployed laptop has a faulty power supply; unclean shutdowns are a fact of
life there. A damaged `captures.db` must not take monitoring down: every
component opens the DB through `make_engine`, which calls `ensure_intact`
first — so capture, classify and the dashboard all recover at startup, which
is exactly when crash damage is discovered.

Rules:
  * Never delete. The damaged file (and its -journal/-wal/-shm siblings) is
    renamed aside with a timestamp for forensics / manual salvage.
  * Salvage what's intact: `sqlite3 <damaged> .recover` (the CLI's recovery
    extension pulls surviving rows out of a broken file) piped into a fresh
    DB; if the CLI is missing, fall back to a best-effort Python `iterdump`;
    if even that fails, start fresh. Monitoring continues in every case.
  * Only *integrity* failures count as damage. A transient "database is
    locked" is never treated as corruption (quick_check doesn't take locks
    that matter, and a busy DB passes the check).
  * The recovery is recorded as an Event(kind="db_recovered") in the new DB
    so `familymonctl health` can surface it.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SIDECARS = ("-journal", "-wal", "-shm")


def check_integrity(db_path: Path) -> str:
    """SQLite quick_check as one line: "ok", or the error summary."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = [r[0] for r in conn.execute("PRAGMA quick_check(5)").fetchall()]
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        # Locked / busy / unreadable-for-permission reasons are not damage.
        if "locked" in str(e).lower() or "busy" in str(e).lower():
            return "ok"
        return f"OperationalError: {e}"[:300]
    except sqlite3.DatabaseError as e:
        return f"{type(e).__name__}: {e}"[:300]
    if rows == ["ok"]:
        return "ok"
    return "; ".join(str(r) for r in rows) or "unknown"


@dataclass(frozen=True)
class RecoveryResult:
    damaged_backup: Path
    method: str  # "sqlite3-recover" | "iterdump" | "fresh"
    detail: str  # the integrity finding that triggered it
    salvaged_tables: dict[str, int]


def _move_aside(db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.corrupt-{stamp}")
    db_path.rename(backup)
    for suffix in _SIDECARS:
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            side.rename(backup.with_name(backup.name + suffix))
    return backup


def _table_counts(db_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            names = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            ]
            for name in names:
                counts[name] = conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        pass
    return counts


def _recover_with_cli(damaged: Path, fresh: Path) -> bool:
    cli = shutil.which("sqlite3")
    if not cli:
        return False
    try:
        dump = subprocess.run(
            [cli, str(damaged), ".recover"],
            capture_output=True, text=True, timeout=600,
        )
        if not dump.stdout.strip():
            return False
        load = subprocess.run(
            [cli, str(fresh)], input=dump.stdout, capture_output=True, text=True, timeout=600,
        )
        if load.returncode != 0 and not fresh.exists():
            return False
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("sqlite3 .recover failed: %s", e)
        return False
    return check_integrity(fresh) == "ok"


def _recover_with_iterdump(damaged: Path, fresh: Path) -> bool:
    """Best effort: copies statements until the first unreadable page."""
    try:
        src = sqlite3.connect(f"file:{damaged}?mode=ro", uri=True)
        dst = sqlite3.connect(fresh)
        try:
            for stmt in src.iterdump():
                try:
                    dst.execute(stmt)
                except sqlite3.DatabaseError:
                    continue
            dst.commit()
        finally:
            src.close()
            dst.close()
    except sqlite3.DatabaseError as e:
        logger.warning("iterdump recovery stopped: %s", e)
    return fresh.exists() and check_integrity(fresh) == "ok"


def ensure_intact(db_path: Path) -> RecoveryResult | None:
    """Check the DB file; if damaged, move it aside and salvage into a fresh
    file at the same path. Returns what was done, or None if nothing was."""
    if not db_path.exists():
        return None
    finding = check_integrity(db_path)
    if finding == "ok":
        return None

    logger.error("capture DB failed integrity check (%s) — recovering", finding)
    backup = _move_aside(db_path)

    if _recover_with_cli(backup, db_path):
        method = "sqlite3-recover"
    else:
        if db_path.exists():
            db_path.unlink()  # a half-built attempt; the damaged original is safe aside
        if _recover_with_iterdump(backup, db_path):
            method = "iterdump"
        else:
            if db_path.exists():
                db_path.unlink()
            method = "fresh"

    result = RecoveryResult(
        damaged_backup=backup,
        method=method,
        detail=finding,
        salvaged_tables=_table_counts(db_path) if db_path.exists() else {},
    )
    logger.error(
        "capture DB recovered via %s; damaged file kept at %s; salvaged %s",
        method, backup, result.salvaged_tables,
    )
    return result

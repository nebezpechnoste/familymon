from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

logger = logging.getLogger(__name__)

# Columns added after the first schema shipped. SQLite is dynamically typed and
# create_all() never ALTERs existing tables, so we add any missing column
# idempotently — preserving accumulated dogfood data across upgrades.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "capture": {
        "messaging_present": "BOOLEAN DEFAULT 0",
        "screenshot_redacted": "BOOLEAN DEFAULT 0",
        "game_running": "BOOLEAN DEFAULT 0",
        "personal_time": "BOOLEAN DEFAULT 0",
    },
    "classification": {
        # JSON stored as TEXT in SQLite; SQLAlchemy's JSON type deserializes it.
        "present_categories": "TEXT DEFAULT '[]'",
    },
}


def _migrate(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
            }
            for col, decl in columns.items():
                if col not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {col} {decl}"
                    )


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Self-heal a crash-damaged file before anything opens it (flaky PSU on
    # the deployed box). The damaged original is kept aside; see recovery.py.
    from .recovery import ensure_intact

    recovery = ensure_intact(db_path)
    engine = create_engine(f"sqlite:///{db_path}", echo=False, future=True)
    Base.metadata.create_all(engine)
    _migrate(engine)
    if recovery is not None:
        _record_recovery(engine, recovery)
    # The DB holds sensitive monitoring metadata (window titles, classification
    # evidence). SQLite creates the file with default perms (0644 & ~umask), so
    # tighten it, and re-assert the parent, since mkdir(mode=) above only takes
    # effect when it freshly creates the dir. Only root and (in production) the
    # read-only dashboard group may read it; see perms.py. Journal/WAL
    # sidecars, if present, are covered too.
    from .perms import secure

    for name in (
        db_path,
        db_path.with_name(db_path.name + "-journal"),
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
        db_path.parent,
    ):
        secure(name, shared=True)
    return engine


def _record_recovery(engine: Engine, recovery) -> None:
    """Leave an Event row so `familymonctl health` can show what happened."""
    from datetime import datetime

    from .models import Event

    with Session(engine) as s:
        s.add(
            Event(
                at=datetime.now(),
                kind="db_recovered",
                data={
                    "method": recovery.method,
                    "finding": recovery.detail,
                    "damaged_backup": str(recovery.damaged_backup),
                    "salvaged_tables": recovery.salvaged_tables,
                },
            )
        )
        s.commit()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_readonly_engine(db_path: Path) -> Engine:
    """Open a capture DB without ever writing to it: no create_all, no
    migration, no recovery, no journal files. For readers that must not
    modify the history (the dashboard runs this way)."""
    # pysqlite needs `uri=true` IN THE URL to honor the file: URI (a connect
    # arg is ignored with a warning and the path is taken literally).
    return create_engine(f"sqlite:///file:{db_path}?mode=ro&uri=true", future=True)

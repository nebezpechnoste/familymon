"""A crash-damaged capture DB must self-heal at open, keeping the original."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from familymon.storage.models import Base, Capture, Event
from familymon.storage.recovery import check_integrity, ensure_intact
from familymon.storage.session import make_engine


def _build_db(path, rows: int = 60) -> None:
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)
    with s() as session:
        for i in range(rows):
            session.add(
                Capture(
                    captured_at=datetime(2026, 10, 1, 12, i % 60),
                    active_wm_class="firefox",
                    active_title="x" * 200,
                    windows=[],
                )
            )
        session.commit()
    engine.dispose()


def _smash(path, page_size: int = 4096) -> None:
    """Overwrite the TAIL of pages 2-6. SQLite stores cell content from the end
    of a page backwards, so this hits real row/index data; damage in a page's
    unused middle is invisible to quick_check (that's what made an earlier
    version of this test pass as 'healthy')."""
    data = bytearray(path.read_bytes())
    for page in range(2, 7):
        end = min(page * page_size, len(data))
        for off in range(max(end - 1024, 0), end):
            data[off] = 0xFF
    path.write_bytes(bytes(data))


def test_healthy_db_is_left_alone(tmp_path):
    db = tmp_path / "captures.db"
    _build_db(db)
    assert check_integrity(db) == "ok"
    assert ensure_intact(db) is None
    assert list(tmp_path.glob("captures.db.corrupt-*")) == []


def test_missing_db_is_not_an_error(tmp_path):
    assert ensure_intact(tmp_path / "nope.db") is None


def test_damaged_db_is_moved_aside_and_rebuilt(tmp_path):
    db = tmp_path / "captures.db"
    _build_db(db)
    _smash(db)
    assert check_integrity(db) != "ok"

    result = ensure_intact(db)
    assert result is not None
    assert result.method in ("sqlite3-recover", "iterdump", "fresh")
    # The damaged original is kept, never deleted.
    backups = list(tmp_path.glob("captures.db.corrupt-*"))
    assert len(backups) == 1 and backups[0] == result.damaged_backup
    # And the live path is now a healthy database.
    assert db.exists()
    assert check_integrity(db) == "ok"


def test_make_engine_recovers_and_records_event(tmp_path):
    db = tmp_path / "captures.db"
    _build_db(db)
    _smash(db)

    engine = make_engine(db)  # must not raise
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        events = list(s.scalars(select(Event).where(Event.kind == "db_recovered")))
        assert len(events) == 1
        data = dict(events[0].data)
        # schema is usable after recovery
        s.add(Capture(captured_at=datetime.now(), active_wm_class="x", windows=[]))
        s.commit()
    assert data["method"] in ("sqlite3-recover", "iterdump", "fresh")
    assert "captures.db.corrupt-" in data["damaged_backup"]
    assert check_integrity(db) == "ok"


def test_make_engine_on_healthy_db_records_nothing(tmp_path):
    db = tmp_path / "captures.db"
    _build_db(db)
    engine = make_engine(db)
    with sessionmaker(bind=engine)() as s:
        assert s.scalars(select(Event).where(Event.kind == "db_recovered")).first() is None

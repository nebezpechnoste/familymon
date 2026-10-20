"""Screenshot retention (storage/retention.py). Everything lives under tmp_path."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from familymon.storage.models import Base, Capture, Event
from familymon.storage.retention import prune_screenshots, screenshot_days

TODAY = date(2026, 10, 19)


@pytest.fixture
def sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _day(root: Path, age_days: int, files: int) -> Path:
    d = root / (TODAY - timedelta(days=age_days)).isoformat()
    d.mkdir(parents=True)
    for i in range(files):
        (d / f"{age_days}{i}.png").write_bytes(b"x" * 100)
    return d


def _row(sessions, path: Path | None, age_days: int) -> int:
    with sessions() as s:
        c = Capture(
            captured_at=datetime.combine(TODAY - timedelta(days=age_days), datetime.min.time()),
            active_wm_class="x",
            active_title="t",
            windows=[],
            screenshot_path=str(path) if path else None,
            screenshot_phash="abcd",
        )
        s.add(c)
        s.commit()
        return c.id


def _paths(sessions) -> list[str | None]:
    with sessions() as s:
        return list(s.scalars(select(Capture.screenshot_path).order_by(Capture.id)))


@pytest.fixture
def tree(tmp_path, sessions):
    root = tmp_path / "screenshots"
    old = _day(root, 100, 2)
    edge = _day(root, 90, 1)  # exactly keep_days old: dated ON the cutoff, kept
    fresh = _day(root, 89, 1)
    odd = root / "notes"
    odd.mkdir()
    (odd / "keep.txt").write_text("not a day dir")
    _row(sessions, old / "1000.png", 100)
    _row(sessions, old / "1001.png", 100)
    _row(sessions, edge / "900.png", 90)
    _row(sessions, fresh / "890.png", 89)
    _row(sessions, None, 89)
    return root


def test_prunes_old_days_clears_rows_and_records_an_event(sessions, tree):
    r = prune_screenshots(sessions, tree, keep_days=90, today=TODAY)
    assert r.cutoff == TODAY - timedelta(days=90)
    assert (r.files, r.bytes, r.dirs, r.rows, r.days) == (2, 200, 1, 2, ["2026-07-11"])
    assert not (tree / "2026-07-11").exists()
    assert (tree / "2026-07-21" / "900.png").exists()  # on the cutoff day: kept
    assert (tree / "2026-07-22" / "890.png").exists()
    assert (tree / "notes" / "keep.txt").exists()
    paths = _paths(sessions)
    assert paths[:2] == [None, None] and paths[2] and paths[3] and paths[4] is None
    with sessions() as s:
        [ev] = list(s.scalars(select(Event)))
    assert ev.kind == "screenshots_pruned"
    assert ev.data["files"] == 2 and ev.data["rows"] == 2 and ev.data["keep_days"] == 90


def test_dry_run_reports_and_changes_nothing(sessions, tree):
    before = _paths(sessions)
    r = prune_screenshots(sessions, tree, keep_days=90, today=TODAY, dry_run=True)
    assert (r.files, r.rows, r.dirs) == (2, 2, 0)
    assert (tree / "2026-07-11" / "1000.png").exists()
    assert _paths(sessions) == before
    with sessions() as s:
        assert s.scalar(select(Event)) is None


def test_zero_keeps_everything(sessions, tree):
    r = prune_screenshots(sessions, tree, keep_days=0, today=TODAY)
    assert r.cutoff is None and r.files == 0
    assert (tree / "2026-07-11" / "1000.png").exists()


def test_nothing_old_enough_records_no_event(sessions, tree):
    r = prune_screenshots(sessions, tree, keep_days=200, today=TODAY)
    assert r.files == 0 and r.days == []
    with sessions() as s:
        assert s.scalar(select(Event)) is None


def test_symlinks_are_not_followed_and_keep_their_directory(sessions, tmp_path):
    root = tmp_path / "screenshots"
    old = _day(root, 120, 1)
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"keep me")
    (old / "link.png").symlink_to(secret)
    r = prune_screenshots(sessions, root, keep_days=90, today=TODAY)
    assert r.files == 1 and r.dirs == 0
    assert secret.exists() and (old / "link.png").is_symlink() and old.exists()


def test_missing_screenshot_dir_is_fine(sessions, tmp_path):
    r = prune_screenshots(sessions, tmp_path / "nope", keep_days=90, today=TODAY)
    assert r.files == 0


def test_rows_are_matched_by_directory_even_with_sql_wildcards_in_the_path(sessions, tmp_path):
    root = tmp_path / "under_score%dir" / "screenshots"
    old = _day(root, 120, 1)
    _row(sessions, old / "1200.png", 120)
    other = tmp_path / "under-scoreXdir" / "screenshots" / "2026-06-21" / "x.png"
    _row(sessions, other, 120)  # a path the LIKE wildcards would wrongly match
    prune_screenshots(sessions, root, keep_days=90, today=TODAY)
    assert _paths(sessions) == [None, str(other)]


def test_screenshot_days_policy_parsing():
    assert screenshot_days({}) == 90
    assert screenshot_days({"retention": None}) == 90
    assert screenshot_days({"retention": {"screenshots_days": 30}}) == 30
    assert screenshot_days({"retention": {"screenshots_days": 0}}) == 0
    for bad in (
        {"retention": {"screenshots_days": -1}},
        {"retention": {"screenshots_days": "90"}},
        {"retention": {"screenshots_days": True}},
        {"retention": "90"},
    ):
        with pytest.raises(ValueError):
            screenshot_days(bad)

"""Screenshot retention: delete image files older than the configured age.

Screenshots are the bulky, sensitive part of the state (roughly 150 MB per
monitored day on a deployed laptop); the capture rows and classifications
that make the timeline are about a kilobyte each and are kept. Once a day
(familymon-retention.timer) this removes every screenshot in a day directory
older than `retention.screenshots_days` from policy.yaml (default 90, 0 = keep
forever), clears those rows' `screenshot_path` so the dashboard and `health`
stop expecting the files, removes the emptied day directories, and records a
`screenshots_pruned` event with the counts.

Day directories are named by capture date (`screenshots/<YYYY-MM-DD>/`), so
the date in the name decides, never a file timestamp: a migration or a
restore changes timestamps, not dates. Anything that is not a regular file
inside a date-named directory is left alone, and a directory that still has
something in it is kept.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from familymon.storage.models import Capture, Event

logger = logging.getLogger(__name__)

DEFAULT_SCREENSHOT_DAYS = 90


def screenshot_days(policy: dict) -> int:
    """`retention.screenshots_days` from policy.yaml: default 90; 0 = never."""
    section = policy.get("retention") or {}
    if not isinstance(section, dict):
        raise ValueError("retention must be a mapping")
    raw = section.get("screenshots_days", DEFAULT_SCREENSHOT_DAYS)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(
            f"retention.screenshots_days must be a whole number of days (0 = never), got {raw!r}"
        )
    return raw


@dataclass
class PruneResult:
    cutoff: date | None  # day directories dated before this are pruned; None = off
    files: int = 0
    bytes: int = 0
    dirs: int = 0
    rows: int = 0
    days: list[str] = field(default_factory=list)


def _day_dirs_before(root: Path, cutoff: date) -> list[Path]:
    out: list[Path] = []
    if root.is_symlink() or not root.is_dir():
        return out
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        try:
            day = date.fromisoformat(entry.name)
        except ValueError:
            continue
        if day < cutoff:
            out.append(entry)
    return out


def prune_screenshots(
    sessions: sessionmaker,
    screenshots_dir: Path,
    *,
    keep_days: int,
    today: date | None = None,
    dry_run: bool = False,
) -> PruneResult:
    """Delete the screenshots of day directories older than `keep_days`.

    `keep_days` 0 disables pruning. A file that can't be deleted is logged
    and skipped; its directory then stays."""
    if keep_days <= 0:
        return PruneResult(cutoff=None)
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    result = PruneResult(cutoff=cutoff)

    for day_dir in _day_dirs_before(screenshots_dir, cutoff):
        prefix = f"{day_dir}/"
        for f in sorted(day_dir.iterdir()):
            if f.is_symlink() or not f.is_file():
                continue
            size = f.stat().st_size
            if not dry_run:
                try:
                    f.unlink()
                except OSError as e:
                    logger.warning("retention: could not delete %s: %s", f, e)
                    continue
            result.files += 1
            result.bytes += size

        with sessions() as s:
            rows_here = s.scalar(
                select(func.count()).where(
                    Capture.screenshot_path.startswith(prefix, autoescape=True)
                )
            ) or 0
            if not dry_run and rows_here:
                s.execute(
                    update(Capture)
                    .where(Capture.screenshot_path.startswith(prefix, autoescape=True))
                    .values(screenshot_path=None)
                )
                s.commit()
        result.rows += rows_here

        if not dry_run:
            try:
                day_dir.rmdir()
                result.dirs += 1
            except OSError:
                pass  # something non-prunable is still inside; keep it
        result.days.append(day_dir.name)

    if not dry_run and (result.files or result.rows):
        with sessions() as s:
            s.add(
                Event(
                    at=datetime.now(),
                    kind="screenshots_pruned",
                    data={
                        "keep_days": keep_days,
                        "cutoff": cutoff.isoformat(),
                        "files": result.files,
                        "bytes": result.bytes,
                        "dirs": result.dirs,
                        "rows": result.rows,
                        "first_day": result.days[0],
                        "last_day": result.days[-1],
                    },
                )
            )
            s.commit()
        logger.info(
            "retention: deleted %d screenshot(s), %.1f MB, from %d day(s) before %s",
            result.files, result.bytes / 1e6, len(result.days), cutoff,
        )
    return result

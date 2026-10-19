"""SQLite + filesystem persistence for capture results.

We deliberately do *not* store screenshot bytes inside the DB — only file paths.
That keeps the SQLite WAL/journal small and means `VACUUM` doesn't leave deleted
image blobs in freelist pages. See the privacy section in the README.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from familymon.storage.models import Capture as CaptureRow

from .backend import CaptureResult

logger = logging.getLogger(__name__)


class CapturePersistence:
    def __init__(
        self,
        session_factory: sessionmaker,
        screenshots_dir: Path,
        *,
        save_screenshots: bool = True,
    ) -> None:
        self._session = session_factory
        self._dir = screenshots_dir
        self._save_screenshots = save_screenshots

    def write(
        self,
        result: CaptureResult,
        *,
        same_as_prev: bool,
        phash: str | None,
        game_running: bool = False,
    ) -> int:
        active = result.active_window
        row = CaptureRow(
            captured_at=result.captured_at,
            active_wm_class=active.wm_class if active else None,
            active_title=active.title if active else None,
            active_pid=active.pid if active else None,
            active_exe=active.exe if active else None,
            windows=[w.model_dump() for w in result.other_windows],
            screenshot_path=None,
            screenshot_phash=phash,
            screenshot_skipped_reason=result.screenshot_skipped_reason,
            messaging_present=result.messaging_present,
            screenshot_redacted=result.screenshot_redacted,
            game_running=game_running,
            same_as_prev=same_as_prev,
        )

        with self._session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            cap_id = row.id

            should_save = (
                self._save_screenshots
                and result.screenshot_bytes is not None
                and not same_as_prev
            )
            if should_save:
                # mkdir's `mode` only applies to the leaf; the screenshots
                # root would otherwise inherit the umask (0775 was observed on
                # a deployed box). Create it explicitly and chmod every time so
                # a root that already exists with lax bits gets tightened too.
                self._dir.mkdir(parents=True, exist_ok=True)
                self._dir.chmod(0o700)
                day_dir = self._dir / result.captured_at.date().isoformat()
                day_dir.mkdir(exist_ok=True, mode=0o700)
                day_dir.chmod(0o700)
                shot_path = day_dir / f"{cap_id}.png"
                shot_path.write_bytes(result.screenshot_bytes)
                row.screenshot_path = str(shot_path)
                session.commit()

            return cap_id

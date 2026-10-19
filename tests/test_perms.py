from __future__ import annotations

import os
import stat

import pytest

from familymon.storage.perms import secure, secure_tree


def _mode(p) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@pytest.mark.skipif(os.geteuid() == 0, reason="non-root behaviour")
def test_non_root_is_owner_only(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o777)
    f = d / "f"
    f.write_text("x")
    f.chmod(0o666)
    secure_tree(d, shared=True)  # no shared group outside root
    assert _mode(d) == 0o700 and _mode(f) == 0o600


def test_symlinks_are_left_alone(tmp_path):
    target = tmp_path / "target"
    target.write_text("x")
    target.chmod(0o644)
    link = tmp_path / "link"
    link.symlink_to(target)
    secure(link)
    assert _mode(target) == 0o644


def test_missing_path_is_ignored(tmp_path):
    secure(tmp_path / "nope")

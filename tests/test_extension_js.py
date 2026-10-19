"""Run the extension's node tests (tests/js) under pytest; skipped without node."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_extension_rules_under_node():
    r = subprocess.run(
        # Files, not the directory: older node treats a directory as a module.
        ["node", "--test", "--test-reporter=tap",
         *sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# fail 0" in r.stdout

"""Print distinct recent window titles for a class substring. Dev helper for
designing title-pattern redaction rules.

    uv run python scripts/peek_titles.py firefox
"""

from __future__ import annotations

import json
import sqlite3
import sys

from familymon.config import db_path

needle = (sys.argv[1] if len(sys.argv) > 1 else "firefox").lower()
con = sqlite3.connect(db_path())
seen: set[str] = set()

for wm_class, title in con.execute(
    "select active_wm_class, active_title from capture order by id desc limit 120"
):
    if wm_class and needle in wm_class.lower() and title and title not in seen:
        seen.add(title)
        print(f"ACTIVE  {title!r}")

for (windows_json,) in con.execute(
    "select windows from capture order by id desc limit 120"
):
    for w in json.loads(windows_json) if windows_json else []:
        cls = (w.get("wm_class") or "").lower()
        title = w.get("title")
        if needle in cls and title and title not in seen:
            seen.add(title)
            print(f"WINDOW  {title!r}")

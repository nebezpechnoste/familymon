"""Print the most recent captures + classifications. Dev inspection helper.

uv run python scripts/peek_recent.py [N]
"""

from __future__ import annotations

import json
import sqlite3
import sys

from familymon.config import db_path

n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
con = sqlite3.connect(db_path())
rows = con.execute(
    "select c.id, c.active_wm_class, substr(c.active_title, 1, 36),"
    " c.screenshot_path is not null, c.same_as_prev,"
    " c.screenshot_skipped_reason, cls.category, cls.present_categories"
    " from capture c left join classification cls on cls.capture_id = c.id"
    " order by c.id desc limit ?",
    (n,),
).fetchall()
hdr = f"{'id':>4} {'disk':4} {'dup':4} {'skip':13} {'category':14} {'present':24} title"
print(hdr)
for cid, wm, title, on_disk, dup, skip, cat, present in reversed(rows):
    present_list = json.loads(present) if present else []
    present_str = ",".join(present_list)
    print(
        f"{cid:>4} {('yes' if on_disk else '-'):4} {('Y' if dup else '-'):4} "
        f"{(skip or '-'):13} {(cat or '-'):14} {present_str:24} {title}"
    )

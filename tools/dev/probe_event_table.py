"""看实时对局的事件表全貌：种类分布 + 最早/最新条目。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.model import Store  # noqa: E402
from sts2mon import paths

s = Store(paths.db_path())
rows = s.all_run_rows()
live = [r for r in rows if r["live"]] or rows[:1]
rid = live[0]["run_id"]
print(f"对局 {rid}")

allrows = s.query("SELECT kind, COUNT(*) n FROM events WHERE run_id=? GROUP BY kind", (rid,))
print("事件种类分布:", {r["kind"]: r["n"] for r in allrows})

oldest = s.query("SELECT * FROM events WHERE run_id=? ORDER BY id LIMIT 14", (rid,))
print("\n最早的 14 条（首读那批）：")
for r in oldest:
    print(f"  id={r['id']:<4} T{r['turn']} [{r['side']:<7}] {r['kind']:<10} {r['label']}")

newest = s.query("SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT 6", (rid,))
print("\n最新的 6 条：")
for r in newest:
    print(f"  id={r['id']:<4} T{r['turn']} [{r['side']:<7}] {r['kind']:<10} {r['label']}")

print(f"\n总条数: {s.query('SELECT COUNT(*) n FROM events WHERE run_id=?', (rid,))[0]['n']}")
s.close()

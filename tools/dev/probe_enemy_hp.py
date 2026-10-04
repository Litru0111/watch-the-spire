"""验证「从对局数据反推怪物 HP」是否可行。

原理：单体战斗打赢时，我方总输出 == 该怪物的最大 HP。
如果不同对局里同一怪物的这个值稳定，说明可以建表。
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.history import find_run_files  # noqa: E402

by_enc: dict[str, Counter] = defaultdict(Counter)
n_multi = 0
n_single = 0

files = find_run_files()
print(f"扫描 {len(files)} 个对局存档…")
for p in files:
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    for act in d.get("map_point_history") or []:
        for pt in act or []:
            ptype = str(pt.get("map_point_type") or "")
            if ptype not in ("monster", "elite", "boss"):
                continue
            rooms = pt.get("rooms") or []
            if not rooms:
                continue
            r0 = rooms[0]
            enc = str(r0.get("model_id") or "").split(".")[-1]
            if not enc:
                continue
            # 怪物数量：rooms[0] 里有没有 monster_ids / monsters
            mons = r0.get("monster_ids") or r0.get("monsters") or []
            n_mon = len(mons) if isinstance(mons, list) else 0
            # 我方总输出（多人在场时只取第一个玩家）
            stats = pt.get("player_stats") or [{}]
            dealt = 0
            for s in stats:
                dealt += int(s.get("damage_dealt") or s.get("damage_dealt_total") or 0)
            if dealt <= 0:
                continue
            if n_mon > 1:
                n_multi += 1
                continue
            n_single += 1
            by_enc[enc][dealt] += 1

print(f"单体样本 {n_single} · 多体样本 {n_multi}（多体的总输出不能直接当个体 HP）")
print()
print("同一遭遇的『总输出』分布（若稳定=可当 HP）：")
shown = 0
for enc, cnt in sorted(by_enc.items(), key=lambda x: -sum(x[1].values())):
    if sum(cnt.values()) < 2:
        continue
    top = cnt.most_common(4)
    total = sum(cnt.values())
    agree = top[0][1] / total
    flag = "稳定" if agree >= 0.6 else ("较散" if agree >= 0.3 else "很散")
    print(f"   {enc[:34]:<36} n={total:<3} {flag:<4} 取值={top}")
    shown += 1
    if shown >= 18:
        break

print()
print("rooms[0] 里有哪些字段（看能否知道怪物数量）:")
for p in files[:60]:
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    for act in d.get("map_point_history") or []:
        for pt in act or []:
            for r in (pt.get("rooms") or []):
                if r.get("model_id"):
                    print("   ", json.dumps(r, ensure_ascii=False)[:260])
                    sys.exit(0)

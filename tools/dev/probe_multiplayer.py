"""摸清多人对局的 .run 结构：players 数组里每位玩家各有什么数据。"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

R = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
files = sorted(R.rglob("history/*.run"), key=lambda p: p.stat().st_size, reverse=True)

print("=== 各存档的玩家数量分布 ===")
sizes = Counter()
mp_files = []
for f in files:
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    ps = d.get("players") or []
    sizes[len(ps)] += 1
    if len(ps) > 1:
        mp_files.append((f, d))
print("   ", dict(sorted(sizes.items())), "  ← 键=玩家人数，值=对局数")

if not mp_files:
    print("\n没有多人对局样本")
    raise SystemExit

f, d = max(mp_files, key=lambda t: len(t[1].get("players") or []))
ps = d["players"]
print(f"\n=== 多人样本: {f.name}  玩家数={len(ps)} ===")
print(f"seed={d.get('seed')}  win={d.get('win')}  ascension={d.get('ascension')}"
      f"  game_mode={d.get('game_mode')}  killed_by={d.get('killed_by_encounter')}")

for i, p in enumerate(ps):
    print(f"\n--- players[{i}] 的键 ---")
    print("   ", sorted(p.keys()))
    for k, v in p.items():
        if isinstance(v, list):
            print(f"    {k}: list[{len(v)}]  首项={json.dumps(v[0], ensure_ascii=False)[:120] if v else '空'}")
        elif isinstance(v, dict):
            print(f"    {k}: dict{{{', '.join(list(v.keys())[:8])}}}")
        else:
            print(f"    {k}: {json.dumps(v, ensure_ascii=False)[:120]}")

# 每层 player_stats 是否也是「每人一条」
mph = d.get("map_point_history") or []
print(f"\n=== 每层 player_stats 与 players 的对应关系 ===")
for ai, act in enumerate(mph[:1]):
    for pi, pt in enumerate(act[:4]):
        stats = pt.get("player_stats") or []
        ids = [s.get("player_id") for s in stats]
        print(f"   act{ai} point{pi} type={pt.get('map_point_type'):<10} "
              f"player_stats={len(stats)}  player_id={ids}")
        for s in stats:
            print(f"        id={s.get('player_id')} hp={s.get('current_hp')}/{s.get('max_hp')} "
                  f"dmg_taken={s.get('damage_taken')} gold={s.get('current_gold')} "
                  f"cards_gained={len(s.get('cards_gained') or [])}")

# 顶层是否还有别的玩家相关字段
print("\n=== 顶层键 ===")
print("   ", sorted(d.keys()))
for k in ("game_mode", "modifiers", "platform_type"):
    print(f"   {k} = {json.dumps(d.get(k), ensure_ascii=False)[:200]}")

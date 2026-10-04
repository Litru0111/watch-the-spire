"""查对局数据里有没有「读档重打」的痕迹。

思路：如果读档重打了同一层，那么在逐层序列里会看到
  * 同一层号出现多次（`map_point_history` 长度 > 最大层号）
  * 同一层的遭遇/地图点被重复记录
  * 同一场战斗被记两遍
这些是**可验证的**信号；查不到就说明这条路不通。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.history import find_run_files  # noqa: E402

print("① 每个存档：楼层序号是否有异常（重复 / 跳号 / 回退）")
print(f"{'对局':<14} {'点数':>5} {'最大层':>6} {'放弃':>5}  异常")
files = find_run_files()
suspicious = 0
for f in files:
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    pts = [pt for a in (d.get("map_point_history") or []) for pt in (a or [])]
    n = len(pts)
    if not n:
        continue
    # 各 Act 的点数（正常每个 Act 的点数 == 该 Act 走过的层数）
    acts = [len(a or []) for a in (d.get("map_point_history") or [])]
    coord = d.get("visited_map_coords") or []
    notes = []
    # 正常：单 Act 时点数 == 层数；多 Act 时应等于各 Act 之和
    if len(coord) and n != len(coord):
        notes.append(f"点数{n}≠访问坐标{len(coord)}")
    # 同一层出现两次的遭遇
    encs = []
    for pt in pts:
        for r in (pt.get("rooms") or []):
            if r.get("model_id"):
                encs.append(str(r["model_id"]))
    dup = [k for k, v in Counter(encs).items() if v > 1]
    if dup:
        notes.append(f"遭遇重复:{len(dup)}")
    if d.get("was_abandoned"):
        notes.append("主动放弃")
    if notes:
        suspicious += 1
        print(f"{f.stem:<14} {n:>5} {len(coord):>6} {str(bool(d.get('was_abandoned'))):>5}  "
              f"{' · '.join(notes)}")
print(f"\n  有异常标记的对局 {suspicious} / {len(files)}")

print()
print("② 逐层血量曲线是否有「突然满血」或「层数回退」")
for f in files[-6:]:
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    hps = []
    for i, act in enumerate(d.get("map_point_history") or [], 1):
        for pt in act or []:
            ps = (pt.get("player_stats") or [{}])[0]
            hp, mx = ps.get("current_hp"), ps.get("max_hp")
            if hp is not None:
                hps.append((i, hp, mx, int(ps.get("damage_taken") or 0),
                            int(ps.get("hp_healed") or 0)))
    if not hps:
        continue
    flags = []
    for k in range(1, len(hps)):
        _a, hp0, mx0, _d0, _h0 = hps[k - 1]
        _b, hp1, mx1, d1, h1 = hps[k]
        # 血量上升但没有治疗记录
        if hp1 > hp0 and h1 == 0:
            flags.append(f"f{hps[k][0]}: {hp0}→{hp1} 无治疗记录")
    print(f"  {f.stem}: {len(hps)} 个点"
          + (f"  ⚠ {' · '.join(flags[:3])}" if flags else "  无异常"))

print()
print("③ 结论：这些字段能否证明「读过档」")
print("   · was_abandoned —— 只表示「主动放弃」，不等于读档")
print("   · 点数/坐标一致 —— 说明逐层记录是连续的，没有重复楼层")
print("   · 血量为 0 后还有后续楼层 —— 这才是「死亡后继续」的铁证")
for f in files:
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    hps = []
    for act in d.get("map_point_history") or []:
        for pt in act or []:
            ps = (pt.get("player_stats") or [{}])[0]
            if ps.get("current_hp") is not None:
                hps.append(int(ps["current_hp"]))
    if 0 in hps[:-1]:
        print(f"   ⚠ {f.stem}: 血量为 0 之后还有 {len(hps) - hps.index(0) - 1} 个点")

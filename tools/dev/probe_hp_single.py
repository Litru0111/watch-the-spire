"""验证最干净的情况：单体遭遇 + 打赢 → 总输出应等于该怪物的最大 HP。

用 rdps 的 RunId（= 种子）与 .run 对齐，再按「遭遇顺序」把两者配对。
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.history import find_run_files  # noqa: E402

APPDATA = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
RDPS = APPDATA / "rdps_meter"

runs = {p.stem: p for p in find_run_files()}
print(f".run 存档 {len(runs)} 个 · rdps 文件 {len(list(RDPS.glob('*.json')))} 个")

# 按种子对齐：rdps 的 RunId 是种子吗？
sample = sorted(RDPS.glob("*.json"))[-1]
rd = json.loads(sample.read_text(encoding="utf-8-sig"))
print(f"rdps 样例 {sample.name}: RunId={rd.get('RunId')!r}")
print(f"  对应 .run 存在: {str(rd.get('RunId')) in runs}")

# 单体遭遇 + 打赢 的样本
singles: dict[str, Counter] = defaultdict(Counter)
n = 0
for seed in runs:
    if seed not in runs:
        continue
    rf = RDPS / f"{seed}.json"
    if not rf.is_file():
        continue
    try:
        r = json.loads(rf.read_text(encoding="utf-8-sig"))
        h = json.loads(runs[seed].read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    # 按顺序收集 .run 里的战斗点（含怪物构成、是否打赢=后续还有楼层）
    pts = [pt for a in (h.get("map_point_history") or []) for pt in (a or [])]
    stages = []
    for i, pt in enumerate(pts):
        rooms = pt.get("rooms") or []
        if not rooms:
            continue
        enc = str(rooms[0].get("model_id") or "").split(".")[-1]
        mons = [str(m).split(".")[-1] for m in (rooms[0].get("monster_ids") or [])]
        if enc and mons:
            # 打赢的判据：这一层之后还有楼层（且最后是 win）
            alive_after = i < len(pts) - 1
            stages.append((enc, mons, alive_after))
    combats = r.get("Combats") or []
    if len(combats) != len(stages):
        continue
    for (enc, mons, alive), c in zip(stages, combats):
        total = sum(int(x.get("Amount") or 0)
                    for pl in (c.get("Players") or [])
                    for x in (pl.get("Dealt") or []))
        if total <= 0 or not alive:
            continue
        if len(mons) == 1:
            singles[mons[0]][total] += 1
            n += 1

print(f"\n单体+打赢 样本 {n} 个，覆盖 {len(singles)} 种怪物")
print(f"{'怪物':<28} {'样本':>4}  取值分布（越集中越像 HP）")
for m, cnt in sorted(singles.items(), key=lambda x: -sum(x[1].values()))[:20]:
    total = sum(cnt.values())
    top = cnt.most_common(4)
    agree = top[0][1] / total
    flag = "★稳定" if agree >= 0.6 else ("较集中" if agree >= 0.3 else "散")
    print(f"   {m[:26]:<28} {total:>4}  {flag:<6} {top}")

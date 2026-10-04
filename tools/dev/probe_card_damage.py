"""对比两种「卡牌强度」信号的数据量：
  A. 胜率（二值，样本 = 对局数，通关只有 13 局）
  B. 每次打出的平均伤害（连续，样本 = 打出次数，可能多得多）
"""
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.model import Store  # noqa: E402
from sts2mon import paths

G = default_glossary()
store = Store(paths.db_path())

# B：从 card_stats 汇总每张牌的 打出次数 / 总伤害
plays: dict[str, int] = defaultdict(int)
dmg: dict[str, int] = defaultdict(int)
runs_with = 0
for r in store.all_run_rows():
    rows = store.card_stats(r["run_id"])
    if rows:
        runs_with += 1
    for x in rows:
        cid = x.get("card_id") or ""
        if not cid:
            continue
        plays[cid] += int(x.get("plays") or 0)
        dmg[cid] += int(x.get("damage") or 0)

print(f"有 card_stats 的对局 {runs_with} 局 · 涉及卡牌 {len(plays)} 张")
print()
rows = []
for cid, p in plays.items():
    if p <= 0:
        continue
    rows.append((cid, p, dmg[cid], dmg[cid] / p))
rows.sort(key=lambda x: -x[1])
print(f"{'卡牌':<20} {'打出次数':>8} {'总伤害':>8} {'每次伤害':>8}")
for cid, p, d, avg in rows[:20]:
    print(f"{G.label(cid)[:18]:<20} {p:>8} {d:>8} {avg:>8.1f}")

print()
for thr in (10, 30, 100, 300):
    k = sum(1 for x in rows if x[1] >= thr)
    print(f"  打出次数 >= {thr} 的卡牌: {k} 种")

print()
print("两种信号的样本量对比：")
print(f"  A 胜率   : 最大样本 = 23 局（通关 13 局）")
print(f"  B 每次伤害: 最大样本 = {rows[0][1] if rows else 0} 次打出")
store.close()

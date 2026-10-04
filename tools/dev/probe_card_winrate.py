"""看「按卡牌胜率评估牌组」在现有数据上是否可行。

思路：某张牌的「胜率」= 抓了这张牌的对局里，通关的比例。
      一副牌的质量 = 其中各牌胜率的加权平均。
先统计每张牌被抓取的次数与对应胜率，看看样本够不够。
"""
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.model import Store  # noqa: E402
from sts2mon.summary import _card_drafts  # noqa: E402
from sts2mon import paths

G = default_glossary()
store = Store(paths.db_path())
runs = store.all_run_rows()
print(f"对局 {len(runs)} 局")

# 每张牌：被抓取的对局集合 / 其中通关的
picked_in: dict[str, set[str]] = defaultdict(set)
won_with: dict[str, set[str]] = defaultdict(set)
picked_n = Counter()
total_picks = 0
runs_with_draft = 0
for r in runs:
    rid = r["run_id"]
    drafts = _card_drafts(store, rid, G, None)
    picks = [d for d in drafts if d.get("picked")
             and d.get("kind") in ("picked", "gained", "upgraded")]
    if not picks:
        continue
    runs_with_draft += 1
    won = bool(r["win"])
    for d in picks:
        cid = d["card_id"]
        picked_n[cid] += 1
        total_picks += 1
        picked_in[cid].add(rid)
        if won:
            won_with[cid].add(rid)

print(f"有抓牌记录的对局 {runs_with_draft} · 抓牌总次数 {total_picks} · 涉及卡牌 {len(picked_n)} 种")
print()
wins = sum(1 for r in runs if r["win"])
print(f"总体通关率: {wins}/{len(runs)} = {wins/len(runs)*100:.1f}%")
print()

rows = []
for cid, n in picked_n.items():
    denom = len(picked_in[cid])
    w = len(won_with[cid])
    rows.append((cid, n, denom, w, w / denom if denom else 0))

rows.sort(key=lambda x: -x[2])
print(f"{'卡牌':<22} {'抓取次数':>6} {'对局数':>6} {'其中通关':>6} {'胜率':>7}")
for cid, n, denom, w, wr in rows[:22]:
    print(f"{G.label(cid)[:20]:<22} {n:>6} {denom:>6} {w:>6} {wr*100:>6.0f}%")

print()
for thr in (2, 3, 5, 8):
    k = sum(1 for x in rows if x[2] >= thr)
    print(f"  对局数 >= {thr} 的卡牌: {k} 种")

store.close()

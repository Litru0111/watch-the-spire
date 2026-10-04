"""验证「卡牌胜率」在 105 局样本下的可靠性。

只看胜率会因为样本小而剧烈波动（赢 1 局和赢 3 局差别巨大）。
用「收缩估计」检验：把每张牌的胜率朝总体均值拉，看需要多少样本才稳。
"""
import sys
from collections import defaultdict
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
n_runs = len(runs)
prior = sum(1 for r in runs if r["win"]) / n_runs

picked: dict[str, set[str]] = defaultdict(set)
won: dict[str, set[str]] = defaultdict(set)
for r in runs:
    rid = r["run_id"]
    for d in _card_drafts(store, rid, G, None):
        if not d.get("picked") or d.get("kind") not in ("picked", "gained", "upgraded"):
            continue
        picked[d["card_id"]].add(rid)
        if r["win"]:
            won[d["card_id"]].add(rid)

print(f"总体通关率（先验） = {prior*100:.1f}%  （{sum(1 for r in runs if r['win'])}/{n_runs}）")
print()

# 收缩估计：posterior = (wins + k*prior) / (n + k)
def shrunk(cid: str, k: float) -> tuple[float, int]:
    n = len(picked[cid])
    w = len(won[cid])
    return ((w + k * prior) / (n + k) if n + k else prior), n

for k in (0.0, 2.0, 5.0, 10.0, 20.0):
    vals = [shrunk(c, k)[0] for c in picked if len(picked[c]) >= 1]
    lo, hi = min(vals), max(vals)
    print(f"  k={k:>5}: 估计值范围 {lo*100:>5.1f}% ~ {hi*100:>5.1f}%  （跨度 {100*(hi-lo):.1f}%）")

print()
print("收缩后（k=8）样本最多的 20 张牌：")
rows = sorted(picked, key=lambda c: -len(picked[c]))[:20]
print(f"  {'卡牌':<20} {'对局':>4} {'通关':>4} {'原始':>6} {'收缩后':>7}")
for cid in rows:
    n = len(picked[cid])
    w = len(won[cid])
    s, _ = shrunk(cid, 8.0)
    print(f"  {G.label(cid)[:18]:<20} {n:>4} {w:>4} {w/n*100:>5.0f}% {s*100:>6.1f}%")

print()
print("结论：")
print(f"  · 通关样本只有 {sum(1 for r in runs if r['win'])} 局，任何单卡胜率的置信区间都很宽")
print(f"  · 收缩后（k=8）几乎全部牌都挤在 {prior*100:.0f}% 附近，说明**当前样本量还不足以给单卡定级**")
print(f"  · 但作为「整副牌的平均强度」指标可用：多张牌平均后噪声会抵消")
store.close()

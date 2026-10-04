"""验证「卡牌胜率」的偏差：是牌强，还是「能撑到那一层」？

做法：把「抓这张牌时的楼层」也记下来，对比
  原始胜率        = 抓了这牌的对局中通关比例
  同层基准胜率    = 所有「走到过这一层」的对局的通关比例
  净贡献          = 原始胜率 − 同层基准胜率
后者的绝对值小得多，才是牌本身的贡献。
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

# ① 生存曲线：走到第 f 层的对局里，有多少最终通关
reached: dict[int, int] = defaultdict(int)
reached_won: dict[int, int] = defaultdict(int)
picks: list[tuple[str, int, bool]] = []      # (卡, 层, 该局是否通关)
for r in runs:
    rid = r["run_id"]
    won = bool(r["win"])
    nodes = [n for n in store.nodes(rid, None) if n.get("visited")]
    for f in range(1, len(nodes) + 1):
        reached[f] += 1
        if won:
            reached_won[f] += 1
    for d in _card_drafts(store, rid, G, None):
        if not d.get("picked") or d.get("kind") not in ("picked", "gained", "upgraded"):
            continue
        picks.append((d["card_id"], int(d.get("floor") or 1), won))

print("① 生存曲线（走到第 f 层的对局，最终通关比例）")
for f in range(1, 21, 2):
    if reached[f]:
        print(f"   第 {f:>2} 层: {reached_won[f]:>2}/{reached[f]:<3} = "
              f"{reached_won[f]/reached[f]*100:>5.1f}%")

print()
print("② 卡牌胜率：原始 vs 扣除「层数优势」后")
agg: dict[str, dict] = defaultdict(lambda: {"n": 0, "w": 0, "base": 0.0})
for cid, floor, won in picks:
    a = agg[cid]
    a["n"] += 1
    a["w"] += 1 if won else 0
    base = reached_won.get(floor, 0) / reached[floor] if reached.get(floor) else 0.0
    a["base"] += base

rows = []
for cid, a in agg.items():
    if a["n"] < 3:
        continue
    raw = a["w"] / a["n"]
    exp = a["base"] / a["n"]
    rows.append((cid, a["n"], raw, exp, raw - exp))

rows.sort(key=lambda x: -x[1])
print(f"{'卡牌':<20} {'次数':>4} {'原始胜率':>8} {'同层基准':>8} {'净贡献':>8}")
for cid, n, raw, exp, net in rows[:18]:
    print(f"{G.label(cid)[:18]:<20} {n:>4} {raw*100:>7.0f}% {exp*100:>7.0f}% "
          f"{net*100:>+7.0f}%")

print()
print("③ 偏差有多大")
if rows:
    import statistics
    raws = [x[2] for x in rows]
    exps = [x[3] for x in rows]
    nets = [x[4] for x in rows]
    print(f"   原始胜率 标准差 = {statistics.pstdev(raws)*100:.1f}%")
    print(f"   同层基准 标准差 = {statistics.pstdev(exps)*100:.1f}%")
    print(f"   净贡献   标准差 = {statistics.pstdev(nets)*100:.1f}%")
    print(f"   → 原始胜率的差异里，大部分来自「层数」而不是牌本身"
          if statistics.pstdev(exps) > statistics.pstdev(nets) else
          f"   → 牌本身的影响占主导")

store.close()

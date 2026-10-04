"""确认 DamageMeter events[] 里 t=0 / t=1 的配对关系（用真机数据）。"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REAL = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "DamageMeter_combat_state.json"

if not REAL.is_file():
    print(f"真机文件不存在: {REAL}")
    sys.exit(0)

st = json.loads(REAL.read_text(encoding="utf-8-sig"))
evs = st.get("events") or []
print(f"events[] 共 {len(evs)} 条 · 类型分布: {dict(Counter(e.get('t') for e in evs))}")
print()
print(f"  {'t':>2} {'turn':>4} {'p':>2} {'v':>5}  label")
print("  " + "-" * 74)
for e in evs[:24]:
    print(f"  {e.get('t'):>2} {e.get('turn'):>4} {str(e.get('p')):>2} "
          f"{str(e.get('v')):>5}  {e.get('l')}")

# 统计：t=1 的数值之和 vs 玩家累计承受
t1 = sum(int(e.get("v") or 0) for e in evs if e.get("t") == 1)
t0 = sum(int(e.get("v") or 0) for e in evs if e.get("t") == 0)
pl = (st.get("players") or {}).get("1") or {}
print()
print(f"t=1 数值合计 = {t1}   t=0 数值合计 = {t0}")
print(f"玩家累计 dmg_taken = {pl.get('dmg_taken')}   dmg_dealt = {pl.get('dmg_dealt')}")
print()
print("结论：")
if t1 == pl.get("dmg_taken"):
    print("  ✔ t=1 的数值合计 == 我方累计承伤 → t=1 是「这一击的伤害量」，t=0 是它的来源")
else:
    print(f"  ? t=1 合计 {t1} 与承伤 {pl.get('dmg_taken')} 不一致，需再判断")
# t=0 的 label 是卡牌还是怪物
from sys import path
path.insert(0, str(Path(__file__).resolve().parent.parent))
from sts2mon.glossary import default_glossary  # noqa: E402
g = default_glossary()
srcs = [str(e.get("l") or "").split("→")[0].strip() for e in evs if e.get("t") == 0]
cards = [s for s in srcs if g.is_known(g.canonical(s))]
print(f"  t=0 来源里：卡牌名 {len(cards)} 个 / 非卡牌（怪物名）{len(srcs) - len(cards)} 个")

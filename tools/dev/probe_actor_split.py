"""检查事件流的敌我区分：行动方 actor / 目标 target / 数值 value。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from sample_state import make_state  # noqa: E402
from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402

g = default_glossary()
td = tempfile.mkdtemp()
p = Path(td) / "s.json"
st = make_state(
    seed=1, turn=3, enc="SLIMES_WEAK", dealt=30, taken=8, cards_played=6,
    events=[
        {"t": 0, "turn": 1, "p": "1", "l": "打击 → 12", "v": 12, "card": "STRIKE_SILENT"},
        {"t": 1, "turn": 1, "p": "1", "l": "打击 → 12", "v": 12},
        {"t": 2, "turn": 1, "p": "1", "l": "+5 (防御)", "v": 5},
        {"t": 0, "turn": 2, "p": "1", "l": "树枝史莱姆（中） → 6", "v": 6},
        {"t": 1, "turn": 2, "p": "1", "l": "树枝史莱姆（中） → 6", "v": 6},
        {"t": 0, "turn": 2, "p": "1", "l": "小刀 → 19", "v": 19, "card": "SHIV"},
        {"t": 1, "turn": 2, "p": "1", "l": "小刀 → 19", "v": 19},
        {"t": 5, "turn": 3, "p": "1", "l": "虚弱 x1", "v": 1},
    ])
p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")

ad = CombatStateAdapter(p, g)
print("首读（initial_events）里带敌我标注的事件：")
print(f"  {'kind':<10} {'side':<8} {'actor':<20} {'target':<8} {'value':>6}  label")
print("  " + "-" * 84)
for e in ad.initial_events(st):
    d = e.data
    if not d.get("actor_kind"):
        continue
    actor = str(d.get("actor") or "")
    target = str(d.get("target") or "")
    print(f"  {e.kind:<10} {e.side:<8} {actor[:18]:<20} {target[:6]:<8} "
          f"{e.value:>6}  {e.label}")

print("\n按「我方回合 / 敌方回合」归类（这就是前端要渲染的结构）：")
by_side: dict[str, list[str]] = {"player": [], "enemy": []}
for e in ad.initial_events(st):
    d = e.data
    if not d.get("actor_kind"):
        continue
    if e.kind == "damage" or d.get("damage"):
        by_side.setdefault(e.side, []).append(
            f"T{e.turn} {d.get('actor')} → {e.value}")
for side, label in (("player", "我方出手"), ("enemy", "敌方出手")):
    print(f"  {label}: {by_side.get(side) or '（无）'}")

# 校验：我方出手的伤害之和 == dealt，敌方出手之和 == taken
p_dealt = sum(e.value for e in ad.initial_events(st)
              if e.data.get("actor_kind") == "player" and e.value and e.kind == "damage")
e_taken = sum(e.value for e in ad.initial_events(st)
              if e.data.get("actor_kind") == "enemy" and e.kind == "damage")
print(f"\n我方出手合计 {p_dealt}（源输出 {st['players']['1']['dmg_dealt']}）")
print(f"敌方出手合计 {e_taken}（源承伤 {st['players']['1']['dmg_taken']}）")

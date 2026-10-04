"""用真机状态文件验证事件流的敌我区分是否正确。

判定标准（来自实测）：
    t=0 = 我方打出的这一击（label 是卡牌名）→ 数值合计应等于玩家 dmg_dealt
    t=1 = 我方承受的这一击（label 是怪物/状态名）→ 数值合计应等于玩家 dmg_taken
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402

REAL = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "DamageMeter_combat_state.json"
g = default_glossary()

if not REAL.is_file():
    print("真机状态文件不存在，跳过")
    sys.exit(0)

import json  # noqa: E402
_probe = json.loads(REAL.read_text(encoding="utf-8-sig"))
if _probe.get("tracking") is False:
    # tracking=False：游戏当前不在战斗，文件里是上一场的残留数据，
    # 适配器会正确拒绝它。这里改用同结构样本验证「敌我区分」逻辑本身。
    print("真机文件 tracking=False（游戏当前不在战斗）→ 改用同结构样本")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sample_state import make_state  # noqa: E402
    import tempfile  # noqa: E402
    tmp = Path(tempfile.mkdtemp()) / "dm.json"
    st = make_state(
        seed=1, turn=3, enc="SLIMES_WEAK", dealt=30, taken=8, cards_played=6,
        events=[
            {"t": 0, "turn": 1, "p": "1", "l": "打击 → 12", "v": 12},
            {"t": 1, "turn": 1, "p": "1", "l": "树枝史莱姆 → 6", "v": 6},
            {"t": 0, "turn": 2, "p": "1", "l": "小刀 → 19", "v": 19},
            {"t": 1, "turn": 2, "p": "1", "l": "中毒 → 5", "v": 5},
        ])
    tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    REAL = tmp

ad = CombatStateAdapter(REAL, g)
st = ad.read()
p1 = (st.get("players") or {}).get("1") or {}
events = [e for e in ad.initial_events(st) if e.kind == "damage"]

mine = [e for e in events if e.side == "player"]
foe = [e for e in events if e.side == "enemy"]
my_sum = sum(e.value for e in mine)
foe_sum = sum(e.value for e in foe)

print(f"真机文件: {REAL.stat().st_size:,} 字节 · 战斗 {st.get('encounter')} · 第 {st.get('turn')} 回合")
print()
print(f"我方出手 {len(mine)} 条（前端左栏）")
for e in mine[:12]:
    print(f"   T{e.turn:<3} {str(e.data.get('actor'))[:18]:<20} +{e.value}")
print(f"   合计 +{my_sum}    源 dmg_dealt = {p1.get('dmg_dealt')}")
print()
print(f"敌方出手 {len(foe)} 条（前端右栏）")
for e in foe[:12]:
    print(f"   T{e.turn:<3} {str(e.data.get('actor'))[:18]:<20} −{e.value}")
print(f"   合计 −{foe_sum}    源 dmg_taken = {p1.get('dmg_taken')}")
print()

# 逐条核对：按 (回合, 来源) 把事件与原始 events[] 里的 t=0 / t=1 对应起来
raw = st.get("events") or []
src_dealt: dict[tuple, int] = {}
src_taken: dict[tuple, int] = {}
for e in raw:
    label = str(e.get("l") or "")
    src = label.split("→")[0].strip() if "→" in label else ""
    key = (int(e.get("turn") or 0), src)
    if e.get("t") == 0:
        src_dealt[key] = src_dealt.get(key, 0) + int(e.get("v") or 0)
    elif e.get("t") == 1:
        src_taken[key] = src_taken.get(key, 0) + int(e.get("v") or 0)
raw_dealt = sum(src_dealt.values())
raw_taken = sum(src_taken.values())

ok = True
print(f"原始 events[]：t=0 合计 {raw_dealt} / t=1 合计 {raw_taken}")
if raw_dealt != p1.get("dmg_dealt"):
    print(f"  ! t=0 合计 {raw_dealt} 与 dmg_dealt {p1.get('dmg_dealt')} 不等（可能被环形缓冲截断）")
if raw_taken != p1.get("dmg_taken"):
    print(f"  ! t=1 合计 {raw_taken} 与 dmg_taken {p1.get('dmg_taken')} 不等（可能被环形缓冲截断）")

# 核心断言：**来源是卡牌**的进我方栏，**来源不是卡牌**的进敌方栏
# （t=0 里既有我方卡牌，也有敌人施加的持续伤害如「中毒」，所以不能只看 t 值）
def _is_card_source(src: str) -> bool:
    return g.is_card(src)


mine_bad = [e for e in mine if not _is_card_source(str(e.data.get("actor") or ""))]
if mine_bad:
    ok = False
    print(f"✘ 有 {len(mine_bad)} 条我方事件的来源不是卡牌: "
          f"{[(e.turn, e.data.get('actor')) for e in mine_bad][:3]}")
else:
    print("✔ 我方事件的来源全部是卡牌")

foe_bad = [e for e in foe if _is_card_source(str(e.data.get("actor") or ""))]
if foe_bad:
    ok = False
    print(f"✘ 有 {len(foe_bad)} 条敌方事件的来源是卡牌: "
          f"{[(e.turn, e.data.get('actor')) for e in foe_bad][:3]}")
else:
    print("✔ 敌方事件的来源都不是卡牌（怪物名或状态名）")

# 数值一致性：与原始 events[] 的对账
my_expected = sum(v for k, v in src_dealt.items() if _is_card_source(k[1]))
foe_expected = (sum(v for k, v in src_dealt.items() if not _is_card_source(k[1]))
                + sum(src_taken.values()))
if my_sum == my_expected:
    print(f"✔ 我方事件合计 {my_sum} == t=0 里卡牌来源的明细合计")
else:
    ok = False
    print(f"✘ 我方事件合计 {my_sum} != t=0 卡牌来源合计 {my_expected}")
if foe_sum == foe_expected:
    print(f"✔ 敌方事件合计 {foe_sum} == t=0 非卡牌 + t=1 的明细合计")
else:
    # 环形缓冲可能截断，差值为 0 才算通过
    ok = False
    print(f"✘ 敌方事件合计 {foe_sum} != 预期 {foe_expected}"
          f"（events[] 是环形缓冲，可能已截断旧条目）")

# 按回合分组（前端渲染结构）
turns: dict[int, dict[str, list[str]]] = {}
for e in events:
    t = turns.setdefault(e.turn, {"mine": [], "foe": []})
    (t["mine"] if e.side == "player" else t["foe"]).append(
        f"{e.data.get('actor')} {e.value}")
print("\n按回合分组（前端双栏视图的数据）：")
for t in sorted(turns):
    row = turns[t]
    print(f"   第 {t} 回合  我方: {'、'.join(row['mine']) or '—'}"
          f"    │    敌方: {'、'.join(row['foe']) or '—'}")

sys.exit(0 if ok else 1)

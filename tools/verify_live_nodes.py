"""验证实时链路的两个修复：
1) 多场遭遇应产生多个「不同节点」，且回合数正确记录
2) 事件落库不重复（水位线增量写入）
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from sample_state import REAL, real_state, write_state  # noqa: E402
from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon.model import Store  # noqa: E402

g = default_glossary()
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


def _real_is_live() -> bool:
    """真机文件存在且游戏当前确实在战斗（tracking 不为 False）。

    tracking=False 时文件里是上一场的残留数据，适配器会（正确地）拒绝它，
    这种情况必须改用同结构样本，否则测的就不是链路而是「拒绝逻辑」。
    """
    if not REAL.is_file():
        return False
    try:
        return json.loads(REAL.read_text(encoding="utf-8-sig")).get("tracking") is not False
    except Exception:
        return False


USE_REAL = _real_is_live()
print(f"样本来源: {'真机状态文件' if USE_REAL else '内置生成器（真机文件不存在或游戏不在战斗中）'}")

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td) / "state.json"
    if USE_REAL:
        shutil.copy2(REAL, tmp)
    else:
        write_state(tmp, seed=7654321, turn=15, enc="SOUL_FYSH_BOSS",
                    dealt=149, taken=44, cards_played=76,
                    events=[{"t": 1, "turn": 3, "p": "1", "l": "灵魂异鱼 → 8", "v": 8}],
                    cards={"STRIKE_SILENT": 8, "SHIV": 22})
    ad = CombatStateAdapter(tmp, g)
    live = LiveState(g)
    bus = EventBus()
    live.attach(bus)

    base = json.loads(tmp.read_text(encoding="utf-8"))
    for e in ad.initial_events(base):
        bus.emit(e)

    print("① 依次模拟 3 场遭遇（每场打几回合、都不带 floor 字段）")
    encounters = ["SEAPUNK_WEAK", "CORPSE_SLUGS_WEAK", "TOADPOLES_WEAK"]
    prev = base
    for enc_i, enc in enumerate(encounters, start=1):
        cur = json.loads(json.dumps(prev))
        cur["encounter"] = enc
        cur["segments"] = [{"turn_count": 0, "encounter": enc,
                            "room_label": f"房间{enc_i}", "point_type": "monster"}]
        p = cur["players"]["1"]
        p["dpt"] = {}
        p["cpc"] = {}
        p["dmg_dealt"] = 0
        p["dmg_taken"] = 0
        cur["events"] = []
        for t in range(1, 4):                       # 每场 3 回合
            cur["turn"] = t
            p["dpt"][str(t)] = 10 + t
            p["dmg_dealt"] = int(p["dmg_dealt"]) + 10 + t
            p["dmg_taken"] = int(p["dmg_taken"]) + 2
            p["cards_played"] = int(p.get("cards_played", 0)) + 1
            known = [k for k in g.by_kind.get(1, {})][t % 5]
            p["cpc"][known] = int(p["cpc"].get(known, 0)) + 1
            cur["events"].append({"t": 1, "turn": t, "p": "1",
                                  "l": f"{enc} → 2", "v": 2})
            for e in ad.diff(prev, cur):
                bus.emit(e)
            prev = json.loads(json.dumps(cur))
        # 换场次的那一拍（走 combat_end + combat_start 分支）
        nxt = json.loads(json.dumps(prev))
        nxt["encounter"] = f"NEXT_{enc_i}"
        nxt["turn"] = 1
        nxt["players"]["1"]["dpt"] = {}
        for e in ad.diff(prev, nxt):
            bus.emit(e)
        prev = nxt

    run = live.tracker.run
    nodes = run.floors
    cmb = run.combats
    ids = [n.node_id for n in nodes]
    print(f"\n② 结果：节点 {len(nodes)} 个，战斗 {len(cmb)} 场")
    for n in nodes:
        print(f"     node={n.node_id} ord={n.order} {n.point_type} {n.room_label}"
              f"  战斗={'有' if n.combat else '无'} turns={n.combat.turns if n.combat else '-'}")
    check(len(set(ids)) == len(ids), "节点 ID 唯一（不再互相覆盖）",
          f"{len(ids)} 个 ID，去重后 {len(set(ids))} 个")
    check(len(nodes) > 1, "多场遭遇产生多个楼层节点", f"{len(nodes)} 个")
    turn_list = [n.combat.turns for n in nodes if n.combat]
    check(all(t > 0 for t in turn_list), "每场战斗都记录了回合数",
          f"回合数={turn_list}")
    check(all(n.order for n in nodes), "每个节点都有楼层号",
          f"楼层={[n.order for n in nodes]}")

    print("\n③ 事件落库：水位线增量写入不应重复")
    db = Path(td) / "t.db"
    st = Store(db)
    st.save_run(run)
    all_ev = [r for c in run.combats for r in c.events]
    st.add_events(run.run_id, all_ev)
    n1 = st.query("SELECT COUNT(*) n FROM events")[0]["n"]
    st.add_events(run.run_id, [])              # 空批次
    n2 = st.query("SELECT COUNT(*) n FROM events")[0]["n"]
    check(n1 == n2 == len(all_ev), "写入条数与事件数一致，空批次不新增",
          f"{n1} / {n2} / 事件 {len(all_ev)}")
    removed = st.prune_events(run.run_id, keep=5)
    n3 = st.query("SELECT COUNT(*) n FROM events")[0]["n"]
    check(n3 == 5, "prune_events 只保留最近 N 条", f"删除 {removed}，剩 {n3}")
    st.close()

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

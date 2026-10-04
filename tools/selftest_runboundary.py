"""验证「开局即监测」：一个 watch 进程能自动识别游戏里开的每一局。

覆盖：
1. 中途启动监控（对局已进行到一半）→ 应能立刻建立当前状态
2. 游戏里开新一局（run_seed 变化）→ 应发 run_start、状态机重置、旧局被收尾
3. 连续三局 → 三局各自独立、互不污染
4. 游戏退回主菜单（tracking=false 且无 encounter）→ 不再继续记账
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402

REAL = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "DamageMeter_combat_state.json"
g = default_glossary()
results: list[tuple[bool, str]] = []
finished: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


def make_state(seed, turn, enc, dealt, taken, cards_played, hps, *, tracking=True):
    """伪造一份 DamageMeter 风格的状态。"""
    players = {}
    for i, key in enumerate(hps, start=1):
        players[str(i)] = {
            "name": f"P{key}", "key": str(i), "char_id": key, "color": [1, 1, 1, 1],
            "dmg_dealt": dealt, "dmg_taken": taken, "blocked": 0, "overkill": 0,
            "hits": cards_played, "cards_played": cards_played, "block_total": 0,
            "energy_spent": 0, "energy_wasted": 0, "potions": 0, "drawn": 0,
            "discarded": 0, "exhausted": 0, "orbs": 0, "stars": 0, "summons": 0,
            "max_hit": 9, "max_hit_card": "", "assist_dmg": 0, "assist_block": 0,
            "dmg_prevented": 0, "dmg_boosted": 0,
            "dpt": {str(t): dealt // max(turn, 1) for t in range(1, turn + 1)},
            "dbc": {}, "dbs": {}, "cpc": {}, "ctc": {}, "bbc": {}, "esbc": {},
            "debuffs": {}, "dpp": {}, "draw_ct": {}, "disc_ct": {}, "exh_ct": {},
        }
    return {
        "schema": 1, "run_seed": seed, "turn": turn, "encounter": enc,
        "tracking": tracking, "players": players,
        "segments": [{"turn_count": turn, "encounter": enc,
                      "room_label": "测试房间", "point_type": "monster"}],
        "events": [],
    }


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td) / "state.json"
    ad = CombatStateAdapter(tmp, g)

    # ---------- ① 中途启动：对局已经打到第 7 回合 ----------
    print("① 中途启动监控（对局已到 turn=7）")
    mid = make_state(1001, 7, "SEAPUNK_WEAK", 70, 12, 10, ["SILENT"])
    tmp.write_text(json.dumps(mid, ensure_ascii=False), encoding="utf-8")
    live = LiveState(g)
    live.set_finish_hook(lambda run: finished.append(run.run_id))
    bus = EventBus()
    live.attach(bus)
    prev = None
    for e in ad.diff(prev, tmp and ad.read()):
        bus.emit(e)
    prev = ad.read()
    snap = live.snapshot()
    check(snap["available"], "中途启动也能立刻建立状态")
    check(snap["turn"] == 7, "回合数与文件一致", f"{snap['turn']}")
    check(snap["damage_dealt"] == 70, "累计输出与文件一致", f"{snap['damage_dealt']}")
    check(len(snap["dpt_by_turn"]) == 7, "逐回合曲线补齐 7 个点",
          f"{len(snap['dpt_by_turn'])}")

    # 这一局继续推进两回合
    for t, d in ((8, 80), (9, 95)):
        nxt = make_state(1001, t, "SEAPUNK_WEAK", d, 12 + t, 10 + t, ["SILENT"])
        nxt["players"]["1"]["cpc"] = {"STRIKE_SILENT": t}
        tmp.write_text(json.dumps(nxt, ensure_ascii=False), encoding="utf-8")
        for e in ad.diff(prev, ad.read()):
            bus.emit(e)
        prev = ad.read()
    check(live.snapshot()["turn"] == 9, "同局内正常推进", f"turn={live.snapshot()['turn']}")

    # ---------- ② 游戏里开新一局 ----------
    print("\n② 在游戏里开新一局（run_seed 变化）")
    new = make_state(2002, 1, "CORPSE_SLUGS_WEAK", 5, 0, 1, ["IRONCLAD"])
    tmp.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    events = ad.diff(prev, ad.read())
    kinds = [e.kind for e in events]
    for e in events:
        bus.emit(e)
    prev = ad.read()
    check("run_start" in kinds, "换局产出 run_start 事件", ",".join(kinds))
    snap = live.snapshot()
    check(snap["run_id"] == "live-2002", "监控目标切到新对局", snap["run_id"])
    check(snap["turn"] == 1 and snap["damage_dealt"] == 5, "新对局数据未被旧局污染",
          f"turn={snap['turn']} 输出={snap['damage_dealt']}")
    check(len(live.tracker.run.floors) <= 1, "新对局楼层从零开始",
          f"{len(live.tracker.run.floors)} 层")
    check("live-1001" in finished, "上一局被收尾回调（用于落库）", str(finished))

    # ---------- ③ 接连三局 ----------
    print("\n③ 连续三局：各自独立")
    for i, seed in enumerate((3003, 4004, 5005), start=1):
        s = make_state(seed, i * 2, f"ENC_{seed}", i * 11, i, i * 2, ["SILENT"])
        tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
        for e in ad.diff(prev, ad.read()):
            bus.emit(e)
        prev = ad.read()
        sn = live.snapshot()
        ok = sn["run_id"] == f"live-{seed}" and sn["turn"] == i * 2
        check(ok, f"第 {i} 局切换正确", f"{sn['run_id']} turn={sn['turn']}")
    check(len(finished) >= 3, "每局都被收尾", f"收尾 {len(finished)} 局: {finished}")

    # ---------- ④ 退回主菜单 ----------
    print("\n④ 退回主菜单（tracking=false，无 encounter）")
    menu = make_state(5005, 6, "", 33, 3, 6, ["SILENT"], tracking=False)
    tmp.write_text(json.dumps(menu, ensure_ascii=False), encoding="utf-8")
    evs = ad.diff(prev, ad.read())
    before = live.snapshot()["damage_dealt"]
    for e in evs:
        bus.emit(e)
    after = live.snapshot()["damage_dealt"]
    check(len(evs) == 0, "主菜单状态不产生事件", f"{len(evs)} 个")
    check(before == after, "倒退主菜单不会篡改已有数据", f"{before} -> {after}")

    # ---------- ⑤ 幂等：同一份状态反复读 ----------
    print("\n⑤ 幂等：同一份状态反复读不应重复计数")
    same = ad.read()
    n1 = live.snapshot()["damage_dealt"]
    for _ in range(5):
        for e in ad.diff(same, same):
            bus.emit(e)
    check(live.snapshot()["damage_dealt"] == n1, "重复读同一状态输出不变",
          f"{n1} -> {live.snapshot()['damage_dealt']}")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

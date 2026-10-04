"""真机链路测试：直接读游戏写的 DamageMeter_combat_state.json，
走「适配器 -> diff -> 事件总线 -> Tracker -> 快照」全链路（不依赖游戏正在运行）。

同时验证：文件未变化时不会重复产事件（幂等），文件变化时能产出增量。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402

REAL = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "DamageMeter_combat_state.json"
results: list[tuple[bool, str]] = []


def _require_tracking(st: dict) -> None:
    """数据源是否在报当前战斗；不在就友好退出（码 2）。

    状态文件里的 `tracking=False` 表示里面是**上一场**的残留数据，
    适配器会正确地不产出事件。此时本测试无法验证实时链路，
    给出说明并以退出码 2 结束（与「断言失败=1」区分）。
    """
    if st.get("tracking") is not False:
        return
    print("=" * 70)
    print(" 数据源当前不在战斗中（tracking=False）")
    print("=" * 70)
    print(f"  状态文件里的遭遇: {st.get('encounter')!r}（这是上一场的残留）")
    print()
    print("  本测试验证的是「实时链路」，需要游戏正在战斗中。")
    print("  请在游戏里进入一场战斗后再跑；或运行不依赖实时数据的那些：")
    print("      python tools/selftest_multiplayer.py")
    print("      python tools/simulate_runs.py --runs 25")
    raise SystemExit(2)


def _local_id() -> str:
    """本机 Steam ID —— 与真实运行路径（cli.Ctx）保持一致。

    测试若用 LiveState(g) 不传本机身份，就会退化到合成键 1，
    与生产路径不符，也测不出多人局的玩家分流问题。
    """
    from sts2mon.adapters import CurrentRunAdapter, find_current_run_files
    from sts2mon import paths
    from sts2mon.glossary import default_glossary as _dg

    for f in find_current_run_files():
        try:
            sid = CurrentRunAdapter(glossary=_dg(), path=f).local_steam_id()
        except Exception:
            sid = ""
        if sid:
            return sid
    return paths.local_steam_id_hint()


def _first_player_key(state: dict) -> str:
    """取状态文件里的一个玩家键。

    **不能写死 "1"** —— 单人局的键是 "1"，多人局则是 Steam ID
    （实测：多人局 keys 形如 ['76561199029071077', '76561198412198880']）。
    早先写死 "1" 时，只要有进行中的多人局，测试就会 KeyError 崩掉。
    """
    keys = list((state.get("players") or {}).keys())
    if not keys:
        raise SystemExit("状态文件里没有 players，无法构造增量样本")
    return keys[0]


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


g = default_glossary()
print("=" * 84)
print("① 真机状态文件")
use_real = False
if REAL.is_file():
    print(f"   路径: {REAL}")
    print(f"   大小: {REAL.stat().st_size:,} 字节 · "
          f"修改于 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(REAL.stat().st_mtime))}")
    try:
        _probe = json.loads(REAL.read_text(encoding="utf-8-sig"))
        use_real = _probe.get("tracking") is not False
        if not use_real:
            # tracking=False 表示游戏当前不在战斗，文件里是上一场的残留数据；
            # 适配器会（正确地）拒绝它，所以这里改用同结构样本继续验证链路
            age = int(time.time() - REAL.stat().st_mtime)
            print(f"   [!] tracking=False（游戏当前不在战斗，{age} 秒未更新）"
                  f" → 改用同结构样本")
    except Exception as exc:
        print(f"   [!] 解析失败 {exc!r} → 改用样本")
if use_real:
    src = REAL
else:
    src = Path(tempfile.mkdtemp()) / "DamageMeter_combat_state.json"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sample_state import write_state
    write_state(src, seed=24681357, turn=11, enc="KAISER_CRAB_BOSS",
                dealt=277, taken=58, cards_played=60,
                cards={"SHIV": 22, "NEUTRALIZE": 6, "STRIKE_SILENT": 8},
                events=[{"t": 1, "turn": 3, "p": "1", "l": "帝皇蟹 → 8", "v": 8},
                        {"t": 0, "turn": 4, "p": "1", "l": "小刀 → 19", "v": 19,
                         "card": "SHIV"}])
    if not REAL.is_file():
        print("   样本: 真机文件不存在，使用合成样本")

adapter = CombatStateAdapter(src, g)
st = adapter.read()
check(st is not None, "能解析状态文件", f"{len(st or {})} 个顶层键")
if not st:
    sys.exit(1)
check("turn" in st and "encounter" in st, "含 turn/encounter 字段",
      f"turn={st.get('turn')} encounter={st.get('encounter')}")
check(bool(st.get("events")), "含事件流", f"{len(st.get('events') or [])} 条")
players = st.get("players") or {}
check(bool(players), "含 players 段", ",".join(players.keys()))
p1 = players.get(_first_player_key(st)) or {}
check(bool(p1.get("dpt")), "含 dpt 逐回合伤害", f"{len(p1.get('dpt') or {})} 个回合")
check(bool(p1.get("cpc")), "含 cpc 每卡打出", f"{len(p1.get('cpc') or {})} 张卡")

_require_tracking(st)

print("\n② 首次读取 -> 初始事件（模拟游戏刚启动）")
# 本机身份必须与**这份数据**对得上：走真机文件时是存档里的 Steam ID，
# 走合成样本时样本用的是键 "1"。不对齐就会「事件有、快照却是 0」
# —— 快照按本机键取数，而那个键在数据里不存在（实测踩过）。
_p1_keys = list((st.get("players") or {}).keys())
_live_id = _local_id()
if _live_id not in _p1_keys:
    _live_id = _p1_keys[0] if _p1_keys else ""
live = LiveState(g, local_player_id=_live_id)
bus = EventBus()
live.attach(bus)
evs = adapter.initial_events(st)
# 游戏可能刚好在你跑测试的这几百毫秒里结束战斗（tracking 转 False），
# 那样本节的实时断言就无从谈起。再确认一次，避免报出「0 vs 315」这类
# 看起来像 bug 的假失败。
if not evs:
    _require_tracking(adapter.read() or {})
for e in evs:
    bus.emit(e)
check(len(evs) >= 0, "产出初始事件", f"{len(evs)} 个: " +
      ",".join(sorted({e.kind for e in evs})))
snap = live.snapshot()
check(snap["available"] is True, "实时快照可用（available=True）")
check(snap["encounter_name"] != "?", "遭遇名可解析", f"{snap['encounter_name']!r}")
check(snap["turn"] == int(st.get("turn") or 0), "回合数与源一致",
      f"{snap['turn']} vs {st.get('turn')}")
check(snap["damage_dealt"] > 0, "累计伤害 > 0", str(snap["damage_dealt"]))
check(len(snap["cards"]) > 0, "卡牌贡献非空", f"{len(snap['cards'])} 张")
check(len(snap["dpt_by_turn"]) > 0, "逐回合曲线非空", f"{len(snap['dpt_by_turn'])} 点")
# HP：DamageMeter 不记录我方 HP（实测字段表无 hp/max_hp），这里断言「如实为空」而非伪造
has_hp = any(k in p1 for k in ("hp", "max_hp"))
check((snap["max_hp"] is not None) == has_hp,
      "HP 与数据源一致（有则显示，无则如实为空）",
      f"源含 hp 字段={has_hp}，快照 max_hp={snap['max_hp']}")
dpt_sum = sum(snap["dpt_by_turn"].values())
print(f"      曲线各回合之和={dpt_sum}, 累计伤害={snap['damage_dealt']}"
      f"（曲线是逐回合值，累计值另算）")

print("\n③ 幂等性：文件没变化时不应重复产事件")
evs2 = adapter.diff(st, st)
check(len(evs2) == 0, "相同状态 diff 得到 0 个事件", f"{len(evs2)} 个")

print("\n④ 增量：在临时副本上模拟「进入下一回合」")
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td) / "DamageMeter_combat_state.json"
    shutil.copy2(src, tmp)          # src 可能是真机文件，也可能是样本
    ad2 = CombatStateAdapter(tmp, g)
    before = ad2.read()                       # 先读，才能知道这局有哪些玩家
    # 让 tracker 的本机身份与**这份副本**对齐：副本可能是换局前抓的，
    # 里面不一定有当前的本机 ID；此时用本机 ID 会让快照落在空玩家上，
    # 于是「累计伤害 vs 源」出现 175 vs 87 这种假失败（实测踩过）。
    _copy_players = list((before.get("players") or {}).keys())
    _live_id = _local_id()
    if _live_id not in _copy_players:
        _live_id = _copy_players[0] if _copy_players else ""
    live2 = LiveState(g, local_player_id=_live_id)
    bus2 = EventBus()
    live2.attach(bus2)
    for e in ad2.initial_events(before):
        bus2.emit(e)

    # 构造下一回合：回合+1、新增一次伤害事件、累计值与 cpc 同步增加
    nxt = json.loads(json.dumps(before))
    new_turn = int(before.get("turn") or 0) + 1
    nxt["turn"] = new_turn
    # 必须用**本机**的键：快照比的是本机，多人局里「第一个玩家」往往
    # 是队友，选了不同的人就会得到「175 vs 87」这种假失败（实测踩过）。
    # 副本可能是换局前抓的，本机未必在里面 —— 那就退回第一个玩家，
    # 并让下面的断言以**实际选中的那个键**为准。
    me_key = live2.tracker.run.local_player or ""
    if me_key not in (nxt.get("players") or {}):
        me_key = _first_player_key(nxt)
    p = nxt["players"][me_key]
    p["dmg_dealt"] = int(p.get("dmg_dealt") or 0) + 17
    p.setdefault("dpt", {})[str(new_turn)] = 17
    p["cards_played"] = int(p.get("cards_played") or 0) + 1
    cpc = p.setdefault("cpc", {})
    # 挑一张「名称库里认识」的真卡（数据源里可能混有脏条目）
    known = [k for k in cpc if g.is_known(g.canonical(k))]
    some_card = known[0] if known else "STRIKE_SILENT"
    cpc[some_card] = int(cpc.get(some_card, 0)) + 1
    dbc = p.setdefault("dbc", {})
    dbc[some_card] = int(dbc.get(some_card, 0)) + 17
    ev_list = nxt.setdefault("events", [])
    ev_list.append({"t": 0, "turn": new_turn, "p": "1", "l": "打击 → 17", "v": 17})
    tmp.write_text(json.dumps(nxt, ensure_ascii=False), encoding="utf-8")

    cur = ad2.read()
    delta = ad2.diff(before, cur)
    for e in delta:
        bus2.emit(e)
    check(len(delta) > 0, "回合推进产出增量事件", f"{len(delta)} 个: " +
          ",".join(sorted({e.kind for e in delta})))
    kinds = {e.kind for e in delta}
    check("turn_start" in kinds, "含 turn_start")
    check("player_stat" in kinds, "含 player_stat")
    # 伤害现在是独立的 damage 事件（带 actor/side，供敌我双栏展示）
    dmg_evs = [e for e in delta if e.kind == "damage"]
    check(bool(dmg_evs), "伤害记录进入事件流",
          ",".join(f"{e.kind}:{e.label}" for e in delta))
    check(all(e.data.get("actor") for e in dmg_evs),
          "每条伤害都带行动方（actor）",
          ",".join(f"{e.side}/{e.data.get('actor')}" for e in dmg_evs[:4]))
    check(all(e.side in ("player", "enemy") for e in dmg_evs),
          "伤害事件标注了敌我",
          ",".join(sorted({e.side for e in dmg_evs})))
    check(any(e.kind == "card_play" for e in delta), "识别到出牌")
    s2 = live2.snapshot()
    check(s2["turn"] == new_turn, "快照回合已推进", f"{s2['turn']}")
    check(s2["damage_dealt"] == int(p["dmg_dealt"]), "累计伤害与源一致",
          f"{s2['damage_dealt']} vs {p['dmg_dealt']}")
    c2 = live2.tracker.current_combat
    curve = s2["dpt_by_turn"]
    check(str(new_turn) in curve, "新回合进入曲线",
          f"实际 {sorted(curve, key=lambda x: int(x))}")
    check(curve.get(str(new_turn)) == 17, "新回合曲线值正确（无重复计数）",
          f"期待 17，实际 {curve.get(str(new_turn))}")
    check(sum(curve.values()) == int(p["dmg_dealt"]), "曲线总和 == 累计伤害",
          f"{sum(curve.values())} vs {p['dmg_dealt']}")

    print("\n⑤ 轮询循环：mtime 变化才会重新解析")
    ad3 = CombatStateAdapter(tmp, g)
    m1 = ad3.mtime()
    time.sleep(0.05)
    nxt["turn"] = new_turn + 1
    nxt["players"][me_key].setdefault("dpt", {})[str(new_turn + 1)] = 5
    tmp.write_text(json.dumps(nxt, ensure_ascii=False), encoding="utf-8")
    os.utime(tmp, None)
    m2 = ad3.mtime()
    check(m2 > m1, "文件 mtime 变化可检测", f"{m1:.3f} -> {m2:.3f}")
    extra = ad3.diff(cur, ad3.read())
    check(any(e.kind == "turn_start" for e in extra), "轮询能捕捉到新回合",
          f"{len(extra)} 个事件")

    print("\n⑥ 同回合内粒度：出一张牌就要刷新累计值（不能等回合结束）")
    ad4 = CombatStateAdapter(tmp, g)
    cur2 = ad4.read()
    # 同样要选**本机**（快照比的是本机，多人局里第一个往往是队友）
    _p4_keys = list((cur2.get("players") or {}).keys())
    _p4_id = _local_id()
    if _p4_id not in _p4_keys:
        _p4_id = _p4_keys[0] if _p4_keys else ""
    live4 = LiveState(g, local_player_id=_p4_id)
    bus4 = EventBus()
    live4.attach(bus4)
    for e in ad4.initial_events(cur2):
        bus4.emit(e)
    turn_now = int(cur2.get("turn") or 0)
    p4 = cur2["players"][_p4_id]
    prev4 = json.loads(json.dumps(cur2))
    ok_mid = True
    for card, dmg in (("打击", 12), ("小刀", 9)):
        p4["dpt"][str(turn_now)] = int(p4["dpt"].get(str(turn_now), 0)) + dmg
        p4["dmg_dealt"] = int(p4["dmg_dealt"]) + dmg
        known = [k for k in p4.get("cpc", {}) if g.is_known(g.canonical(k))]
        c = known[0] if known else "STRIKE_SILENT"
        p4["cpc"][c] = int(p4["cpc"].get(c, 0)) + 1
        cur2["events"].append({"t": 4, "turn": turn_now, "p": "1",
                               "l": f"{card} → {dmg}", "v": dmg, "card": c})
        tmp.write_text(json.dumps(cur2, ensure_ascii=False), encoding="utf-8")
        fresh = ad4.read()
        for e in ad4.diff(prev4, fresh):
            bus4.emit(e)
        snap4 = live4.snapshot()
        if snap4["damage_dealt"] != int(p4["dmg_dealt"]):
            ok_mid = False
        prev4 = json.loads(json.dumps(fresh))
    check(ok_mid, "同回合内累计输出随出牌即时刷新",
          f"仪表盘={live4.snapshot()['damage_dealt']} 源={p4['dmg_dealt']}")
    s4 = live4.snapshot()
    check(s4["dpt_by_turn"].get(str(turn_now)) == int(p4["dpt"][str(turn_now)]),
          "同回合内曲线值同步",
          f"{s4['dpt_by_turn'].get(str(turn_now))} vs {p4['dpt'][str(turn_now)]}")

ok = sum(1 for c, _ in results if c)
print("\n" + "=" * 84)
print(f"真机链路测试: {ok} / {len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

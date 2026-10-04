"""黑箱对局模拟器 —— 不需要游戏，用合成数据驱动完整链路并断言不变量。

设计要点
--------
模拟器**自己就是真值来源**：它按脚本编排每一回合发生了什么，
再把这些事实翻译成两个数据源的真实文件格式：

    current_run.save              —— 权威 HP / 金币 / 逐层数值
    DamageMeter_combat_state.json —— 伤害、出牌、每回合曲线

然后按监控循环的真实顺序喂给 Tracker / LiveState / Store，
最后拿「模拟器知道的真值」去核对链路输出。

这样能抓到的问题类型
--------------------
* 多源合并失败（HP 丢失、节点分裂）
* 事件归属错乱（上一场战斗的事件混进当前场）
* 重复计数 / 幂等性（反复读同一状态、环形缓冲淘汰后重发）
* 回合曲线与累计值不一致
* 换局、换幕、休息/药水回血等状态变化处理

用法
----
    python tools\\simulate_runs.py                # 默认跑 40 局
    python tools\\simulate_runs.py --runs 200     # 跑 200 局
    python tools\\simulate_runs.py --seed 7 --verbose
    python tools\\simulate_runs.py --stop-on-bug  # 首个失败就停（调试用）
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon.model import Store  # noqa: E402

G = default_glossary()

# DamageMeter 的事件类型（与适配器里的 T_* 常量一致）
T_DAMAGE, T_DAMAGE_DEALT, T_BLOCK, T_CARD, T_LOG, T_DEBUFF = 0, 1, 2, 3, 4, 5

RING_BUFFER = 60          # events[] 是环形缓冲，模拟真实淘汰行为

ACT_TYPES = ["monster", "monster", "event", "monster", "rest_site",
             "monster", "elite", "treasure", "monster", "rest_site",
             "monster", "shop", "elite", "monster", "boss"]


# ----------------------------------------------------------------- 真值模型
@dataclass
class TrueTurn:
    """一个回合的真值。"""
    turn: int
    dealt: int = 0                      # 我方打出的总伤害
    taken: int = 0                      # 我方承受的总伤害
    cards: dict[str, int] = field(default_factory=dict)   # 牌 -> 本回合打出次数
    card_damage: dict[str, int] = field(default_factory=dict)
    block: int = 0


@dataclass
class TrueRoom:
    """一层（一个房间）的真值。"""
    floor: int
    act: int
    kind: str
    encounter: str = ""
    turns: list[TrueTurn] = field(default_factory=list)
    hp_after: int = 0
    max_hp_after: int = 0
    gold_after: int = 0
    damage_taken: int = 0               # 本层我方总承伤（对局真值）
    dealt: int = 0                      # 本层我方总输出（对局真值）

    @property
    def node_id(self) -> str:
        return f"f{self.floor:03d}"


@dataclass
class TrueRun:
    """一整局的真值。"""
    run_id: str
    seed: int
    character: str
    max_hp: int
    rooms: list[TrueRoom] = field(default_factory=list)
    win: bool = False
    # 每层结束时的真实 HP（模拟器自己算的，考虑休息/药水/事件回血）
    hp_by_floor: dict[int, int] = field(default_factory=dict)
    gold_by_floor: dict[int, int] = field(default_factory=dict)


# ----------------------------------------------------------------- 造数据
# 卡牌池：从**真实名称库**里取（这样卡名能解析成中文、is_card 判定成立）。
# 早先用写死的英文卡 ID，结果名称库里没有 → 适配器把它当成了「非卡牌」，
# 造出一堆假 bug。
def _card_pool() -> list[str]:
    cards: list[str] = []
    for kind, table in getattr(G, "by_kind", {}).items():
        if kind == 1:                     # KIND_CARD
            cards = [cid for cid, name in table.items()
                     if name and not name.isascii()]
    if len(cards) >= 8:
        return cards
    return ["SHIV", "NEUTRALIZE", "SURVIVOR"]     # 兜底（库里卡太少时）


CARD_POOL = _card_pool()
MONSTER_POOL = ["NIBBIT", "LEAF_SLIME_S", "TWIG_SLIME_M", "INKLET", "MAWLER",
                "SHRINKER_BEETLE", "FLYCONID"]
ENCOUNTERS = ["NIBBITS_WEAK", "LEAF_SLIME_WEAK", "MAWLER_NORMAL", "INKLETS_WEAK",
              "SLIMES_WEAK", "BYRDONIS_ELITE", "VANTOM_BOSS"]


def _card_label(cid: str) -> str:
    """卡牌的中文名（适配器靠它判断「来源是卡牌」）。"""
    return G.card(cid) or cid


def _monster_label(mid: str) -> str:
    """怪物的中文名（必须是**非卡牌**，适配器靠它判断「来源是敌人」）。"""
    return G.name(mid) or mid


def make_truth(rng: random.Random, seed: int, *, acts: int = 1,
               multiplayer: bool = False, max_rooms: int = 15) -> TrueRun:
    """生成一局的「真值」。"""
    character = rng.choice(["IRONCLAD", "SILENT", "DEFECT"])
    base = {"IRONCLAD": 80, "SILENT": 70, "DEFECT": 75}[character]
    max_hp = base + rng.randint(-6, 10)
    run = TrueRun(run_id=f"run-{seed}", seed=seed, character=character, max_hp=max_hp)
    hp = max_hp
    gold = rng.randint(90, 130)

    floor = 0
    for act in range(1, acts + 1):
        n_rooms = rng.randint(6, max_rooms)
        for i in range(n_rooms):
            floor += 1
            if floor == 1:
                kind = "ancient"
            elif i == n_rooms - 1:
                kind = "boss"
            else:
                kind = rng.choice(ACT_TYPES[:-1])
            room = TrueRoom(floor=floor, act=act, kind=kind,
                            encounter=rng.choice(ENCOUNTERS) if kind in
                            ("monster", "elite", "boss") else "")
            room.damage_taken = 0
            room.dealt = 0
            # 战斗类房间：生成若干回合
            if kind in ("monster", "elite", "boss", "ancient"):
                if kind == "ancient":
                    n_turns = 0
                else:
                    n_turns = rng.randint(1, 8)
                monster = _monster_label(rng.choice(MONSTER_POOL))
                for t in range(1, n_turns + 1):
                    tt = TrueTurn(turn=t)
                    # 我方出 1~4 张牌
                    for _ in range(rng.randint(1, 4)):
                        cid = rng.choice(CARD_POOL)
                        tt.cards[cid] = tt.cards.get(cid, 0) + 1
                        if rng.random() < 0.75:      # 有的牌不打伤害（技能牌）
                            dmg = rng.randint(3, 18)
                            tt.card_damage[cid] = tt.card_damage.get(cid, 0) + dmg
                            tt.dealt += dmg
                    # 敌人打我方
                    if rng.random() < 0.7:
                        tt.taken = rng.randint(1, 14)
                    tt.block = rng.randint(0, 8)
                    room.turns.append(tt)
                    room.dealt += tt.dealt
                    room.damage_taken += tt.taken
            # 结算：扣血、回血、拿钱
            if room.damage_taken:
                hp = max(hp - room.damage_taken, 1)
            if room.kind == "rest_site":
                hp = min(max_hp, hp + rng.randint(10, 25))       # 休息处回血
            if rng.random() < 0.12:
                # 药水/事件回血：**关键**，这就是让「按承伤推算 HP」失效的场景
                hp = min(max_hp, hp + rng.randint(5, 20))
            if rng.random() < 0.08 and max_hp > 30:
                max_hp += 5                                       # 生命上限提升
                hp = min(max_hp, hp + 5)
            gold += rng.randint(10, 60)
            if room.kind == "shop":
                gold = max(gold - rng.randint(20, 120), 0)
            room.hp_after = hp
            room.max_hp_after = max_hp
            room.gold_after = gold
            run.rooms.append(room)
            run.hp_by_floor[floor] = hp
            run.gold_by_floor[floor] = gold
    return run


# ----------------------------------------------------------- 渲染成数据源
class SimSource:
    """把真值渲染成两个数据源的文件内容。"""

    def __init__(self, truth: TrueRun, *, multiplayer: bool = False,
                 players: int = 1, ring: int = RING_BUFFER) -> None:
        self.t = truth
        self.ring = ring
        self.players = players if multiplayer else 1
        # 每个玩家的累计状态（单人局只有 "1"）
        self.cum: list[dict[str, Any]] = [
            {"dmg_dealt": 0, "dmg_taken": 0, "cards_played": 0, "blocked": 0,
             "dpt": {}, "cpc": {}, "dbc": {}}
            for _ in range(self.players)]
        self.events: list[dict[str, Any]] = []
        # 已累计过的 (层, 回合)：dm_state 会被反复调用（每回合都渲染一次），
        # 必须记住进度，否则同一回合会被累计很多遍（模拟器自己的坑）
        self._done: set[tuple[int, int]] = set()
        # 当前场次：实测 DamageMeter 的 dmg_dealt 是**按场次**统计的
        # （换遭遇就归零），所以要跟着房间切换。
        self._cur_floor: int = -1

    # ---------- DamageMeter ----------
    def dm_state(self, room: TrueRoom, turn: int) -> dict[str, Any]:
        """生成某一回合的 DamageMeter 状态（含环形缓冲淘汰）。

        只累计「还没累计过」的回合：这个方法每个回合都会被调用一次，
        重复累计会让数值翻好几倍（模拟器最初就踩了这个坑）。
        另外换房间（= 换遭遇）时累计值要归零，因为实测 dmg_dealt 是按场次算的。
        """
        if room.floor != self._cur_floor:
            self._cur_floor = room.floor
            for c in self.cum:
                for k in ("dmg_dealt", "dmg_taken", "cards_played", "blocked"):
                    c[k] = 0
                c["dpt"] = {}
                c["cpc"] = {}
                c["dbc"] = {}
            # 事件缓冲也要清空：DamageMeter 只记录**当前遭遇**的事件，
            # 不清的话上一场的条目会跟着累积，历史补发时整批重放（假重复）
            self.events.clear()
        for tt in room.turns:
            if tt.turn > turn:
                break
            key = (room.floor, tt.turn)
            if key in self._done:
                continue
            self._done.add(key)
            c = self.cum[0]
            c["dmg_dealt"] += tt.dealt
            c["dmg_taken"] += tt.taken
            for cid, n in tt.cards.items():
                c["cards_played"] += n
                c["cpc"][cid] = c["cpc"].get(cid, 0) + n
            for cid, d in tt.card_damage.items():
                c["dbc"][cid] = c["dbc"].get(cid, 0) + d
            c["dpt"][str(tt.turn)] = tt.dealt
            # 事件流：我方卡牌伤害（t=0）
            for cid, d in tt.card_damage.items():
                self._push({"t": T_DAMAGE, "turn": tt.turn, "p": "1",
                            "l": f"{_card_label(cid)} → {d}", "v": d})
            # 敌人伤害（t=1）
            if tt.taken:
                mon = _monster_label("MAWLER")
                self._push({"t": T_DAMAGE_DEALT, "turn": tt.turn, "p": "1",
                            "l": f"{mon} → {tt.taken}", "v": tt.taken})
            # 出牌（t=3）
            for cid, n in tt.cards.items():
                for _ in range(n):
                    self._push({"t": T_CARD, "turn": tt.turn, "p": "1",
                                "l": _card_label(cid), "v": 1})
            # 格挡（t=2）
            if tt.block:
                self._push({"t": T_BLOCK, "turn": tt.turn, "p": "1",
                            "l": f"+{tt.block} (Other)", "v": tt.block})
        c = self.cum[0]
        players = {"1": {
            "name": "模拟玩家", "key": "1", "char_id": self.t.character,
            "color": [1, 1, 1, 1],
            "dmg_dealt": c["dmg_dealt"], "dmg_taken": c["dmg_taken"],
            "blocked": 0, "overkill": 0, "hits": 0,
            "cards_played": c["cards_played"], "block_total": 0,
            "energy_spent": 0, "energy_wasted": 0, "potions": 0,
            "drawn": 0, "discarded": 0, "exhausted": 0,
            "orbs": 0, "stars": 0, "summons": 0,
            "max_hit": max([v for v in c["dbc"].values()] or [0]),
            "max_hit_card": "", "assist_dmg": 0, "assist_block": 0,
            "dmg_prevented": 0, "dmg_boosted": 0,
            "dpt": dict(c["dpt"]), "dbc": dict(c["dbc"]), "cpc": dict(c["cpc"]),
            "ctc": {}, "bbc": {}, "esbc": {}, "debuffs": {}, "dpp": {},
            "draw_ct": {}, "disc_ct": {}, "exh_ct": {},
        }}
        return {
            "schema": 1, "run_seed": self.t.seed, "turn": turn,
            "encounter": room.encounter or f"ROOM_{room.floor}",
            "tracking": True, "players": players,
            "segments": [{"turn_count": turn, "encounter": room.encounter or "",
                          "room_label": "", "point_type": room.kind}],
            "events": list(self.events),
        }

    def _push(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)
        if len(self.events) > self.ring:
            del self.events[:len(self.events) - self.ring]   # 环形缓冲淘汰

    # ---------- current_run.save ----------
    def save_state(self, room: TrueRoom, *, include_history: bool = True
                   ) -> dict[str, Any]:
        """生成某一层结束后的进行中存档。"""
        hist: list[list[dict[str, Any]]] = []
        acts: dict[int, list[dict[str, Any]]] = {}
        for r in self.t.rooms:
            if r.floor > room.floor:
                break
            stats = [{
                "player_id": "1", "current_hp": r.hp_after,
                "max_hp": r.max_hp_after, "current_gold": r.gold_after,
                "damage_taken": r.damage_taken, "hp_healed": 0,
                "gold_gained": 0, "gold_spent": 0,
                "cards_gained": [], "cards_removed": [],
                "card_choices": [], "relic_choices": [], "potion_choices": [],
                "event_choices": [],
            }]
            acts.setdefault(r.act, []).append({
                "map_point_type": r.kind, "player_stats": stats,
                # 注意：这里要和 DamageMeter 的 encounter 写法一致（不带前缀）。
                # 真机存档写的是 ENCOUNTER.XXX，但适配器会归一化掉前缀；
                # 模拟器如果把两种写法混用，会造出两条战斗记录（假 bug）。
                "rooms": [{"model_id": r.encounter,
                           "room_type": r.kind, "turns_taken": len(r.turns)}],
            })
            # 当前楼层往往还没写 player_stats（实测是 0/0），这里也模拟
        for a in sorted(acts):
            hist.append(acts[a])
        if hist and hist[-1] and room.turns:
            # 当前层未结算：把它标成 0/0（真机就是这个行为）
            last = hist[-1][-1]
            last["player_stats"] = [{**last["player_stats"][0],
                                     "current_hp": 0, "max_hp": 0,
                                     "current_gold": 0, "damage_taken": 0}]
        return {
            "schema_version": 6,
            "start_time": self.t.seed,
            "current_act_index": room.act - 1,
            "visited_map_coords": [[i % 7, i // 7] for i in range(room.floor)],
            "players": [{
                "net_id": 1, "character_id": f"CHARACTER.{self.t.character}",
                "current_hp": room.hp_after, "max_hp": room.max_hp_after,
                "gold": room.gold_after, "deck": [], "relics": [], "potions": [],
            }],
            "map_point_history": hist,
            "save_time": 1000 + room.floor,
        }


# ----------------------------------------------------------------- 驱动
@dataclass
class Bug:
    run_id: str
    floor: int
    what: str
    detail: str


class SimRunner:
    """把一局模拟对局喂给完整链路，并在每一步核对不变量。"""

    def __init__(self, tmpdir: Path, *, verbose: bool = False) -> None:
        self.tmp = tmpdir
        self.verbose = verbose
        self.save_path = tmpdir / "current_run.save"
        self.dm_path = tmpdir / "DamageMeter_combat_state.json"
        self.store_path = tmpdir / "sim.db"
        self.bugs: list[Bug] = []
        self.checks = 0

    def bug(self, run_id: str, floor: int, what: str, detail: str) -> None:
        self.bugs.append(Bug(run_id, floor, what, detail))
        print(f"   [BUG] {run_id} 第{floor}层 {what}\n         {detail}")

    def check(self, cond: bool, run_id: str, floor: int, what: str,
              detail: str = "") -> bool:
        self.checks += 1
        if not cond:
            self.bug(run_id, floor, what, detail)
        return bool(cond)

    def run_one(self, truth: TrueRun, *, players: int = 1, ring: int = RING_BUFFER,
                duplicate_polls: bool = True) -> None:
        """跑一局，逐层逐回合喂事件并核对。"""
        src = SimSource(truth, players=players, ring=ring)
        save = CurrentRunAdapter(self.save_path, G)
        dm = CombatStateAdapter(self.dm_path, G)
        live = LiveState(G)
        bus = EventBus()
        live.attach(bus)
        store = Store(self.store_path)

        run_id = truth.run_id
        dm.set_run_id_hint(run_id)
        prev_save: dict | None = None
        prev_dm: dict | None = None
        last_room_floor = -1

        for room in truth.rooms:
            # ---- 1. 存档源：进入该层 ----
            st_save = src.save_state(room)
            self.save_path.write_text(json.dumps(st_save, ensure_ascii=False),
                                      encoding="utf-8")
            dm.set_floor_hint(room.floor)
            dm.set_act_hint(room.act)
            for e in (save.initial_events(st_save) if prev_save is None
                      else save.diff(prev_save, st_save)):
                bus.emit(e)
            prev_save = st_save

            # 不变量：实时 HP 必须等于存档真值
            snap = live.snapshot()
            self.check(snap["hp"] == room.hp_after, run_id, room.floor,
                       "实时 HP 与存档不一致",
                       f"链路={snap['hp']} 真值={room.hp_after}")
            self.check(snap.get("hp_authoritative") is True, run_id, room.floor,
                       "HP 未被标记为实测",
                       f"hp_authoritative={snap.get('hp_authoritative')}")

            # ---- 2. 战斗源：逐回合 ----
            # 换房间时战斗源是全新的（它只记录当前遭遇），所以重置 diff 基线，
            # 否则会把上一场的累计值和事件流整批重放（重复计数）
            if room.floor != last_room_floor:
                prev_dm = None
                last_room_floor = room.floor
            if room.turns:
                for tt in room.turns:
                    dm.set_floor_hint(room.floor)      # 每回合都同步，别用上层的
                    dm.set_act_hint(room.act)
                    st_dm = src.dm_state(room, tt.turn)
                    self.dm_path.write_text(json.dumps(st_dm, ensure_ascii=False),
                                            encoding="utf-8")
                    evs = (dm.initial_events(st_dm) if prev_dm is None
                           else dm.diff(prev_dm, st_dm))
                    for e in evs:
                        bus.emit(e)
                    # 重复轮询：真实监控会遇到「文件没变但再读一次」
                    if duplicate_polls:
                        for e in dm.diff(st_dm, st_dm):
                            bus.emit(e)
                    prev_dm = st_dm

                # 不变量：累计输出必须等于真值
                snap = live.snapshot()
                self.check(snap["damage_dealt"] == room.dealt, run_id, room.floor,
                           "累计输出与真值不一致",
                           f"链路={snap['damage_dealt']} 真值={room.dealt}")

            # ---- 3. 落库 ----
            live.flush_events_to(store, run_id)
            # 幂等：再 flush 一次不应产生任何新事件
            # （这条断言抓到过真 bug：LiveState.flush_events_to 当初每调一次
            #   就把该战斗的全部事件重写一遍，条数会翻好几倍）
            before = store.query(
                "SELECT COUNT(*) c FROM events WHERE run_id=?", (run_id,))[0]["c"]
            live.flush_events_to(store, run_id)
            after = store.query(
                "SELECT COUNT(*) c FROM events WHERE run_id=?", (run_id,))[0]["c"]
            self.check(after == before, run_id, room.floor,
                       "重复 flush 写入了重复事件（落库不幂等）",
                       f"{before} -> {after}")

        # ---- 4. 整局不变量 ----
        self._check_whole_run(truth, store, run_id)
        store.close()

    def run_sequence(self, truths: list[TrueRun], *, ring: int = RING_BUFFER) -> None:
        """连续多局：模拟「一个 watch 进程挂着打好几局」。

        重点验证换局处理：新局开始时旧局要被收尾、新局从零建状态、
        上一局的楼层/事件不会混进新局。
        """
        store = Store(self.store_path)
        save = CurrentRunAdapter(self.save_path, G)
        dm = CombatStateAdapter(self.dm_path, G)
        live = LiveState(G)
        bus = EventBus()
        live.attach(bus)
        # 一局结束时把旧局落库（与 CLI 的完成回调等价）
        live.set_finish_hook(lambda run: live.flush_events_to(store, run.run_id))

        prev_save: dict | None = None
        for truth in truths:
            src = SimSource(truth, ring=ring)
            run_id = truth.run_id
            for room in truth.rooms:
                st_save = src.save_state(room)
                self.save_path.write_text(json.dumps(st_save, ensure_ascii=False),
                                          encoding="utf-8")
                for e in (save.initial_events(st_save) if prev_save is None
                          else save.diff(prev_save, st_save)):
                    bus.emit(e)
                prev_save = st_save
                if room.turns:
                    prev_dm = None
                    for tt in room.turns:
                        dm.set_floor_hint(room.floor)
                        dm.set_act_hint(room.act)
                        dm.set_run_id_hint(run_id)
                        st_dm = src.dm_state(room, tt.turn)
                        self.dm_path.write_text(
                            json.dumps(st_dm, ensure_ascii=False), encoding="utf-8")
                        for e in (dm.initial_events(st_dm) if prev_dm is None
                                  else dm.diff(prev_dm, st_dm)):
                            bus.emit(e)
                        prev_dm = st_dm
                live.flush_events_to(store, run_id)
                snap = live.snapshot()
                self.check(snap["hp"] == room.hp_after, run_id, room.floor,
                           "[序列] 实时 HP 与存档不一致",
                           f"链路={snap['hp']} 真值={room.hp_after}")
                self.check(snap["run_id"] == run_id, run_id, room.floor,
                           "[序列] 实时对局没有切到当前局",
                           f"链路={snap['run_id']} 期望={run_id}")
                # 当前局的节点必须只属于当前局
                nodes = [n for n in store.nodes(run_id) if n.get("visited")]
                ids = [n["node_id"] for n in nodes]
                self.check(len(ids) == len(set(ids)), run_id, room.floor,
                           "[序列] 当前局出现重复节点",
                           f"{ids}")

        # 收尾后：live 标记只应留在最后一局
        live_runs = [r["run_id"] for r in store.all_run_rows() if r["live"]]
        self.check(live_runs in ([], [truths[-1].run_id]), truths[-1].run_id, 0,
                   "[序列] 旧局没有收尾（仍标记进行中）", f"live={live_runs}")
        for i, truth in enumerate(truths):
            rows = [r for r in store.all_run_rows() if r["run_id"] == truth.run_id]
            self.check(bool(rows), truth.run_id, 0, "[序列] 对局未落库")
            if rows:
                n_rooms = len([n for n in store.nodes(truth.run_id)
                               if n.get("visited")])
                self.check(n_rooms == len(truth.rooms), truth.run_id, 0,
                           "[序列] 落库层数与真值不符",
                           f"链路={n_rooms} 真值={len(truth.rooms)}")
        store.close()

    def _check_whole_run(self, truth: TrueRun, store: Store, run_id: str) -> None:
        nodes = store.nodes(run_id)
        real_nodes = [n for n in nodes if n.get("visited")]
        # 节点唯一：每层一个
        ids = [n["node_id"] for n in real_nodes]
        self.check(len(ids) == len(set(ids)), run_id, 0,
                   "同一层出现重复节点（多源分裂）",
                   f"{len(ids)} 个节点，去重后 {len(set(ids))} 个")
        self.check(len(ids) == len(truth.rooms), run_id, 0,
                   "节点数与真实层数不符",
                   f"链路={len(ids)} 真值={len(truth.rooms)}")

        # 事件归属：每个节点的事件只能来自它自己的那场战斗
        for room in truth.rooms:
            if not room.turns:
                continue
            evs = store.events(run_id, limit=10000, node_id=room.node_id)
            other = [e for e in evs if e.get("node_id") != room.node_id]
            self.check(not other, run_id, room.floor,
                       "节点事件里混入了其它战斗",
                       f"{len(other)} 条来自 {sorted({e.get('node_id') for e in other})[:3]}")
            if evs:
                self.check(all(e.get("encounter_id") for e in evs), run_id, room.floor,
                           "事件缺少 encounter_id")

        # 每层输出：用战斗记录核对
        for room in truth.rooms:
            if not room.turns:
                continue
            cs = [c for c in store.combats(run_id) if c["node_id"] == room.node_id]
            self.check(bool(cs), run_id, room.floor, "该层没有战斗记录")
            if cs:
                self.check(cs[0]["dmg_dealt"] == room.dealt, run_id, room.floor,
                           "该层累计输出与真值不符",
                           f"链路={cs[0]['dmg_dealt']} 真值={room.dealt}")

            # 事件流里的伤害之和必须等于该层真值 —— 这条能抓到
            # 「环形缓冲淘汰后旧事件被当新增重发」导致的重复计数
            evs = store.query(
                """SELECT side, label, value FROM events
                   WHERE run_id=? AND node_id=? AND kind='damage'""",
                (run_id, room.node_id))
            ev_dealt = sum(r["value"] or 0 for r in evs if r["side"] == "player")
            ev_taken = sum(r["value"] or 0 for r in evs if r["side"] == "enemy")
            true_taken = sum(tt.taken for tt in room.turns)
            # 事件流是「记录」性质，环形缓冲会截断最早的条目，所以只断言上界：
            # 记录到的伤害不能**超过**真值（超了就是重复计数）
            self.check(ev_dealt <= room.dealt, run_id, room.floor,
                       "事件流里的输出超过真值（重复计数）",
                       f"事件合计={ev_dealt} 真值={room.dealt}")
            self.check(ev_taken <= true_taken, run_id, room.floor,
                       "事件流里的承伤超过真值（重复计数）",
                       f"事件合计={ev_taken} 真值={true_taken}")

            # 出牌事件必须都有牌名，且都是真实卡牌（不是占位符/其它类别）
            plays = store.query(
                """SELECT label FROM events
                   WHERE run_id=? AND node_id=? AND kind='card_play'""",
                (run_id, room.node_id))
            unnamed = [p["label"] for p in plays if not p["label"]]
            self.check(not unnamed, run_id, room.floor,
                       "出牌事件缺牌名", f"{len(unnamed)} 条无名: {unnamed[:3]}")
            bad = [p["label"] for p in plays if not G.is_card(p["label"])]
            self.check(not bad, run_id, room.floor,
                       "出牌事件的牌名不是卡牌", f"{bad[:4]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="黑箱对局模拟器")
    ap.add_argument("--runs", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--acts", type=int, default=0,
                    help="0 = 随机 1~2 幕")
    ap.add_argument("--multiplayer-every", type=int, default=5,
                    help="每 N 局里安排一局多人（0 = 不测多人）")
    ap.add_argument("--ring", type=int, default=RING_BUFFER)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--stop-on-bug", action="store_true")
    ap.add_argument("--sequences", type=int, default=3,
                    help="额外跑几组「连续多局」（验证换局生命周期）")
    ap.add_argument("--seq-runs", type=int, default=3,
                    help="每组序列里包含几局")
    args = ap.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="sts2sim-"))
    runner = SimRunner(tmpdir, verbose=args.verbose)
    print(f"模拟 {args.runs} 局 · 临时目录 {tmpdir}\n")

    failed_runs: set[str] = set()
    for i in range(args.runs):
        rng = random.Random(args.seed * 100003 + i)
        acts = args.acts or rng.choice([1, 1, 2])
        mp = (args.multiplayer_every and i % args.multiplayer_every == 0
              and i > 0)
        truth = make_truth(rng, seed=args.seed * 1000 + i, acts=acts,
                           multiplayer=bool(mp))
        n_before = len(runner.bugs)
        try:
            runner.run_one(truth, players=2 if mp else 1, ring=args.ring)
        except Exception as exc:                       # 崩溃也是 bug
            runner.bug(truth.run_id, 0, "模拟过程抛异常", repr(exc))
            if args.verbose:
                import traceback
                traceback.print_exc()
        if len(runner.bugs) > n_before:
            failed_runs.add(truth.run_id)
            if args.stop_on_bug:
                break
        if args.verbose or (i + 1) % 10 == 0:
            print(f"  第 {i + 1}/{args.runs} 局完成 · 累计 bug {len(runner.bugs)}")

    print(f"\n{'=' * 78}")
    print("连续多局序列（换局生命周期）…")
    for s in range(args.sequences):
        tmpdir2 = Path(tempfile.mkdtemp(prefix=f"sts2seq{s}-"))
        runner2 = SimRunner(tmpdir2, verbose=args.verbose)
        truths = []
        for k in range(args.seq_runs):
            rng = random.Random(args.seed * 7919 + s * 131 + k)
            truths.append(make_truth(rng, seed=args.seed * 5000 + s * 100 + k,
                                     acts=1))
        n_before = len(runner2.bugs)
        try:
            runner2.run_sequence(truths, ring=args.ring)
        except Exception as exc:
            runner2.bug(truths[-1].run_id, 0, "[序列] 抛异常", repr(exc))
            if args.verbose:
                import traceback
                traceback.print_exc()
        runner.checks += runner2.checks
        runner.bugs.extend(runner2.bugs)
        print(f"  序列 {s + 1}/{args.sequences}: {args.seq_runs} 局 · "
              f"新增 bug {len(runner2.bugs) - n_before}")
        shutil.rmtree(tmpdir2, ignore_errors=True)

    print(f"\n{'=' * 78}")
    print(f"模拟完成: {args.runs} 局 · 断言 {runner.checks} 次 · "
          f"失败对局 {len(failed_runs)} 局 · 发现 bug {len(runner.bugs)} 个")
    if runner.bugs:
        kinds: dict[str, int] = {}
        for b in runner.bugs:
            kinds[b.what] = kinds.get(b.what, 0) + 1
        print("\n按类型统计:")
        for what, n in sorted(kinds.items(), key=lambda x: -x[1]):
            print(f"  {n:>4}  {what}")
            ex = next(b for b in runner.bugs if b.what == what)
            print(f"        例: {ex.run_id} 第{ex.floor}层 — {ex.detail[:100]}")
    else:
        print("没有发现 bug ✔")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 1 if runner.bugs else 0


if __name__ == "__main__":
    sys.exit(main())

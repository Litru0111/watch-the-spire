"""实时适配器：轮询 Slay the Spire 2 的 mod 状态文件，diff 出事件流。

主数据源
--------
`%APPDATA%/SlayTheSpire2/DamageMeter_combat_state.json`
    由 DamageMeter 这个 mod 实时写盘，包含：
      run_seed / turn / encounter / tracking / players{...} / segments[...] / events[...]
    * players.<id>：dmg_dealt、dmg_taken、blocked、dpt(每回合伤害)、dbc(每卡伤害)、
      cpc(每卡打出次数)、bbc(每卡格挡)、energy_spent ...  —— 我方全量统计
    * events[]：{t, turn, p, l, v} —— 带回合号与中文标签的事件流（伤害/上 Buff）
    * segments[]：每场遭遇的分段快照（encounter / turn_count / players）

次要数据源
----------
`DanmakuSpire/logs/latest.log`（GBK 编码）与 `national_spire_diagnostics/*.log`：
    纯文本，用于在缺少结构化数据时标记「游戏正在运行」。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from ..events import Event, EventBus, K
from ..glossary import KIND_CARD, Glossary, default_glossary
from .. import paths

DEFAULT_ROOT = paths.sts2_appdata()

# 逐局令牌：另一款统计 mod 会在「新一局开始」时写这个文件。
# 它比战斗状态文件更早出现（开局就有），所以用作「新对局」的第一信号。
RUN_TOKEN = "skada_run_token.json"

# events[].t 的语义（由实测样本推断）
T_DAMAGE = 0        # 伤害来源，label 形如 "打击 → 6"
T_DAMAGE_DEALT = 1  # 造成的伤害记账
T_BLOCK = 2         # 获得格挡，label 形如 "+6 (防御)"
T_CARD = 3          # 出牌/启动，label 为中文卡名
T_LOG = 4           # 其他记录（回放用它记录「某张卡打了多少」）
T_DEBUFF = 5        # 上状态，label 形如 "虚弱 x1"

# players.<id> 里「累计型」字段 -> Combat 字段
PLAYER_CUMULATIVE = {
    "dmg_dealt": "player_damage_dealt",
    "dmg_taken": "player_damage_taken",
    "blocked": "player_blocked",
    "block_total": "player_block",
    "energy_spent": "player_energy_spent",
    "energy_wasted": "player_energy_wasted",
    "cards_played": "cards_played",
    "drawn": "cards_drawn",
    "discarded": "cards_discarded",
    "exhausted": "cards_exhausted",
    "overkill": "overkill",
    "max_hit": "max_hit",
}

# 每卡字典字段 -> CardStat 字段
CARD_MAPS = {
    "dbc": "damage",
    "cpc": "plays",
    "bbc": "block",
    "esbc": "energy",
    "draw_ct": "drawn",
    "disc_ct": "discarded",
    "exh_ct": "exhausted",
}


class CombatStateAdapter:
    """把一个 mod 状态 JSON 文件变成事件流。"""

    def __init__(self, path: str | Path | None = None,
                 glossary: Glossary | None = None,
                 player_key: str = "1") -> None:
        self.path = Path(path) if path else DEFAULT_ROOT / "DamageMeter_combat_state.json"
        self.g = glossary or default_glossary()
        self.player_key = player_key
        # cpc 增量基线（见 _combo_diff）：快照与增量共用同一条基线，避免重复计数
        self._cpc_base: dict[str, int] = {}
        self._cpc_encounter = ""
        # 楼层提示：DamageMeter 自己**不记录楼层**（实测顶层只有 8 个键），
        # 由 current_run.save 适配器在监控循环里注入，两个数据源才能落到同一节点。
        self.floor_hint: int = 0
        # 幕（Act）提示：同上，DamageMeter 也不知道当前是第几个 Act，
        # 不注入的话它会硬编码成 1，同一层会被建成两个节点。
        self.act_hint: int = 0
        # 逐局令牌提示：多个数据源必须用**同一个** run_id，
        # 否则 LiveState 会以为「换局了」而重建 tracker，把别处拿到的 HP 丢掉。
        self.run_id_hint: str = ""
        # 跨轮询的事件去重签名（events[] 是环形缓冲，只比上一帧会重复计数）
        self._seen_sigs: set[tuple] = set()
        self._seen_encounter: str = ""

    def set_run_id_hint(self, run_id: str | None) -> None:
        if run_id:
            self.run_id_hint = str(run_id)

    def set_floor_hint(self, floor: int | None) -> None:
        if floor:
            self.floor_hint = int(floor)

    def set_act_hint(self, act: int | None) -> None:
        if act:
            self.act_hint = int(act)

    def _act(self) -> int:
        """当前 Act：DamageMeter 不知道，用注入值，兜底 1。"""
        return self.act_hint or 1

    def _floor_of(self, seg: dict[str, Any]) -> int:
        """楼层：优先用 segment 自带的，否则用注入的提示值。"""
        try:
            f = int(seg.get("floor") or 0)
        except (TypeError, ValueError):
            f = 0
        return f or self.floor_hint

    # ------------------------------------------------------------ 读文件
    def read(self) -> dict[str, Any] | None:
        """读一次状态；文件正在被写入时容忍半截 JSON。"""
        if not self.path.is_file():
            return None
        for attempt in range(3):
            try:
                return json.loads(self.path.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                time.sleep(0.05 * (attempt + 1))
            except OSError:
                time.sleep(0.05)
        return None

    def exists(self) -> bool:
        return self.path.is_file()

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    # ------------------------------------------------------------ 转换
    def _segment(self, st: dict[str, Any]) -> dict[str, Any]:
        """segments[0] 提供楼层/房间信息（回放会写；真实 mod 可能没有）。"""
        segs = st.get("segments") or []
        return segs[0] if segs and isinstance(segs[0], dict) else {}

    def initial_events(self, st: dict[str, Any]) -> list[Event]:
        """首次看到的完整状态：补出 run/act/map/combat 的起始事件与累计值。

        `tracking=False` 时文件里是上一场战斗的残留数据，不发任何事件。
        """
        if st.get("tracking") is False:
            return []
        seed = str(st.get("run_seed") or "")
        run_id = self.run_id_hint or (f"live-{seed}" if seed else "live")
        enc = str(st.get("encounter") or "")
        p = self._player(st) or {}
        seg = self._segment(st)
        floor = self._floor_of(seg)
        node_id = f"f{floor:03d}" if floor else ""
        events: list[Event] = [
            Event(kind=K.RUN_START, run_id=run_id, seed=seed, ts=time.time(),
                  character=str(p.get("char_id") or ""), source="damage_meter",
                  # 本适配器**不掌握**对局模式（DamageMeter 的战斗文件里没有），
                  # 所以绝不声明 game_mode —— 早先写死 "standard"，
                  # 会在每次进战斗时把权威的 "custom" 覆盖掉（实测踩过）。
                  data={}),
            Event(kind=K.ACT_ENTER, act=self._act(), run_id=run_id,
                      source="damage_meter"),
        ]
        if floor or enc:
            events.append(Event(
                kind=K.MAP_NODE, run_id=run_id, act=self._act(), floor=floor,
                node_id=node_id,
                source="damage_meter",
                data={"point_type": seg.get("point_type") or _encounter_type(enc),
                      "room_id": enc, "room_label": seg.get("room_label") or "",
                      "floor": floor, "visited": True}))
        if enc:
            events.append(Event(kind=K.COMBAT_START, run_id=run_id, encounter_id=enc,
                                turn=int(st.get("turn") or 0), node_id=node_id,
                                source="damage_meter",
                                data={"point_type": _encounter_type(enc), "floor": floor}))
            # **逐玩家**发 PLAYER_STAT。DamageMeter 把每人数据分块存在
            # players.<steamid>，只发一条（取 self.player_key，多人局里
            # 该键不存在 → 退回第一个玩家）会让事件流里只有一个人，
            # 本机的伤害/出牌全丢（实测踩过）。
            keys = self.player_keys(st) or [""]
            taken = self._enemy_damage_by_turn(st)
            for pk in keys:
                data = {**self._player_state(st, pk), "floor": floor}
                if taken:
                    data["taken_by_turn"] = taken
                events.append(Event(kind=K.PLAYER_STAT, run_id=run_id, encounter_id=enc,
                                    turn=int(st.get("turn") or 0), node_id=node_id,
                                    key=pk, source="damage_meter", data=data))
            # 首次读取要补齐「整场战斗到目前为止」的卡牌贡献，
            # 否则实时面板的卡牌表在开局是空的（增量路径只反映之后的变化）。
            events.extend(self.card_snapshot(st, run_id, enc, int(st.get("turn") or 0),
                                             node_id))
            # 同时把游戏文件里已有的历史事件补进「事件流」。
            # 这里包含伤害明细，所以**中途启动监控**也能看到已经打过的回合
            # （就是下面这个路径）。重复条目由前端按「动作+对象+数值」去重。
            #
            # 关键：把它们登记进「已见」集合。否则下一回合 _event_delta
            # 会把同一批历史事件当成新增再发一遍 —— 每回合重发全量，
            # 事件流的伤害合计会涨到真值的十几倍（实测踩过）。
            self._seen_encounter = enc
            self._seen_sigs = {
                (e.get("turn"), e.get("t"), e.get("p"), e.get("l"), e.get("v"))
                for e in (st.get("events") or [])}
            events.extend(self._history_events(st, run_id, enc, node_id))
        return events

    def _history_events(self, st: dict[str, Any], run_id: str, enc: str,
                        node_id: str = "") -> list[Event]:
        """把状态文件里已积累的事件转成「仅供展示」的事件流条目。

        重要：events[] 是**环形缓冲**，旧条目会被挤掉，所以这里每次都给全量、
        让「中途启动监控」也能看到已打过的回合；重复条目由前端按
        (动作, 对象, 数值) 去重。

        但这些条目一律带 `record_only=True`：Tracker 只把它们记进事件流，
        **不做任何累加**——累计值与逐回合曲线由 PLAYER_STAT 快照 / _event_delta 负责，
        否则每一击都会被算两遍（实测会导致曲线值翻倍）。
        """
        out: list[Event] = []
        for e in st.get("events") or []:
            ev = self._one_event(e, run_id, enc, node_id)
            if ev is None:
                continue
            ev.data["record_only"] = True
            out.append(ev)
        return out

    def card_snapshot(self, st: dict[str, Any], run_id: str, enc: str = "",
                      turn: int = 0, node_id: str = "") -> list[Event]:
        """把 players.<id> 里各「每卡字典」的累计值，作为绝对值快照发出来。

        同时把 cpc 记为增量基线：快照已经反映了「到目前为止的总次数」，
        之后的增量必须从这条基线往上加。
        """
        out: list[Event] = []
        # 逐玩家：多人局每人一条卡牌贡献，否则只有一个人的表
        keys = self.player_keys(st) or [""]
        self._cpc_encounter = enc
        # 基线按 (玩家, 卡牌) 记，与 _combo_diff 的增量和保持一致
        self._cpc_base = {}
        for pk in keys:
            p = self._player(st, pk) or {}
            for card_id, val in (p.get("cpc") or {}).items():
                self._cpc_base[(pk, card_id)] = int(val or 0)
            for field, attr in CARD_MAPS.items():
                for card_id, val in (p.get(field) or {}).items():
                    cid = self.g.canonical(card_id)
                    if not cid or not self.g.is_known(cid):
                        continue        # 跳过脏数据条目
                    out.append(Event(kind=K.CARD_STAT, run_id=run_id, encounter_id=enc,
                                     turn=turn, node_id=node_id, key=cid,
                                     name=self.g.card(cid), label=self.g.card(cid),
                                     value=int(val or 0), source="damage_meter",
                                     data={attr: int(val or 0),
                                           "player_key": pk}))
        return out

    def diff(self, prev: dict[str, Any] | None, cur: dict[str, Any]) -> list[Event]:
        """对比两次状态，产出增量事件。

        「换局」是第一优先判断：run_seed 变了说明游戏里开了新的一局，
        此时必须发 run_start 让下游重置状态机，否则新一局的数据会灌进旧对局。
        """
        if prev is not None:
            prev_seed = prev.get("run_seed")
            cur_seed = cur.get("run_seed")
            if cur_seed is not None and prev_seed != cur_seed:
                return self.initial_events(cur)

        # tracking=False 表示游戏**当前没有在追踪战斗**（战斗已结束、正在地图/事件/
        # 商店里）。此时文件里残留的还是上一场战斗的数据，**顶层 encounter 字段
        # 不会被清空**，所以不能只看 encounter 是否为空 —— 否则过时数据会被当成
        # 新楼层的战斗，事件房会被标成上一场的怪名（实测踩过）。
        if cur.get("tracking") is False:
            return []

        run_id = self.run_id_hint or \
            f"live-{cur.get('run_seed') or ''}".rstrip("-")
        events: list[Event] = []
        turn = int(cur.get("turn") or 0)
        enc = str(cur.get("encounter") or "")
        prev_enc = str((prev or {}).get("encounter") or "")
        prev_turn = int((prev or {}).get("turn") or 0)
        seg = self._segment(cur)
        prev_seg = self._segment(prev or {})
        floor = self._floor_of(seg)
        node_id = f"f{floor:03d}" if floor else ""

        if prev is None:
            return self.initial_events(cur)

        # 换楼层：登记新的地图点
        if floor and floor != int(prev_seg.get("floor") or 0):
            events.append(Event(
                kind=K.MAP_NODE, run_id=run_id, act=self._act(), floor=floor,
                node_id=node_id,
                source="damage_meter",
                data={"point_type": seg.get("point_type") or _encounter_type(enc),
                      "room_id": enc, "room_label": seg.get("room_label") or "",
                      "floor": floor, "visited": True}))

        # 换场次：上一场结束 + 新一场开始（注意不能提前 return，
        # 否则同一拍里新场次的卡牌/伤害事件会被丢掉）
        if enc and enc != prev_enc:
            if prev_enc:
                events.append(Event(kind=K.COMBAT_END, run_id=run_id, encounter_id=prev_enc,
                                    turn=prev_turn, node_id=node_id, source="damage_meter",
                                    data={"outcome": "win", "turns": prev_turn}))
            events.append(Event(kind=K.COMBAT_START, run_id=run_id, encounter_id=enc,
                                turn=turn, node_id=node_id, source="damage_meter",
                                data={"point_type": _encounter_type(enc), "floor": floor}))
            # 逐玩家发（多人局每人一条），见 initial_events 里的说明
            for pk in (self.player_keys(cur) or [""]):
                events.append(Event(kind=K.PLAYER_STAT, run_id=run_id, encounter_id=enc,
                                    turn=turn, node_id=node_id, key=pk,
                                    source="damage_meter",
                                    data={**self._player_state(cur, pk), "floor": floor}))
            # 新场次第一回合的明细事件照常补上
            events.extend(self._event_delta(prev, cur, run_id, enc, node_id))
            events.extend(self._combo_diff(prev, cur, run_id, enc, turn, node_id))
            return events

        # 同一场次：回合推进（回放会一次跳多回合，同样按快照处理，保证数值权威）
        if turn != prev_turn:
            events.append(Event(kind=K.TURN_START, run_id=run_id, encounter_id=enc,
                                turn=turn, node_id=node_id, source="damage_meter",
                                data={"floor": floor}))
            events.extend(self._turn_delta(prev, cur, run_id, enc, node_id))
            return events

        # 同回合内：事件流在变，累计数值也可能在变（每打出一张牌都会涨）。
        # 这里补发一次绝对值快照，让仪表盘的累计数字在同一回合内就跟着动，
        # 而不是等到回合结束；同时把新增的出牌计入卡牌贡献。
        events.extend(self._stat_delta(prev, cur, run_id, enc, turn, node_id))
        events.extend(self._event_delta(prev, cur, run_id, enc, node_id))
        events.extend(self._combo_diff(prev, cur, run_id, enc, turn, node_id))
        return events

    def _stat_delta(self, prev: dict[str, Any], cur: dict[str, Any], run_id: str,
                    enc: str, turn: int, node_id: str = "") -> list[Event]:
        """累计数值有变化时才补发一次玩家状态快照（绝对值，幂等）。

        **逐玩家**比对与发事件 —— 多人局里各人的数值独立变化，
        只发一条（取第一个玩家）会让本机的变化被漏掉。
        """
        watched = ("player_damage_dealt", "player_damage_taken", "player_block",
                   "player_blocked", "player_energy_spent", "player_energy_wasted",
                   "cards_played", "cards_drawn", "cards_discarded", "cards_exhausted",
                   "overkill", "max_hit", "hp", "max_hp", "gold")
        seg = self._segment(cur)
        taken = self._enemy_damage_by_turn(cur)
        out: list[Event] = []
        for pk in (self.player_keys(cur) or self.player_keys(prev) or [""]):
            a = self._player_state(prev, pk)
            b = self._player_state(cur, pk)
            changed = any(a.get(k) != b.get(k) for k in watched)
            changed = changed or (a.get("dpt") != b.get("dpt"))
            if not changed:
                continue
            data = {**b, "floor": self._floor_of(seg)}
            # 数据源不提供我方 HP，但提供了「每次挨打多少」的明细，
            # 按回合汇总后交给 Tracker，这样逐回合承伤就是真实值而不是估算。
            if taken:
                data["taken_by_turn"] = taken
            out.append(Event(kind=K.PLAYER_STAT, run_id=run_id, encounter_id=enc,
                             turn=turn, node_id=node_id, key=pk,
                             source="damage_meter", data=data))
        return out

    def _enemy_damage_by_turn(self, st: dict[str, Any]) -> dict[str, int]:
        """从事件流里汇总「我方每个回合分别挨了多少伤害」。

        实测口径（真机验证）：
            t=0 合计 == 玩家 dmg_dealt  → 里面的**卡牌名**条目是我方打出的
            t=1 合计 == 玩家 dmg_taken  → 全是我方承受的
        但 t=0 里**也混着非卡牌来源**（如「中毒」持续伤害，由敌人施加），
        这部分同样属于我方承伤，必须算进来。
        另外 `p` 字段恒为本机玩家 id，**不能**用它区分敌我；
        真正的依据是来源名的**类别**（卡牌 vs 怪物/状态）。
        """
        out: dict[str, int] = {}
        for e in st.get("events") or []:
            value = int(e.get("v") or 0)
            if value <= 0:
                continue
            source = str(e.get("l") or "").split("→")[0].strip()
            t = e.get("t")
            if t == T_DAMAGE_DEALT:
                is_taken = True             # t=1 全是我方承受
            elif t == T_DAMAGE:
                is_taken = not self._is_player_card(source)   # t=0 里非卡牌来源=敌人
            else:
                continue
            if not is_taken:
                continue
            turn_key = str(int(e.get("turn") or 0))
            out[turn_key] = out.get(turn_key, 0) + value
        return out

    def _is_player_card(self, source: str) -> bool:
        """来源名是否为我方卡牌。

        必须用「类别」判断：怪物名（如「墨影幻灵」）也在名称库里，
        早期版本用「能否解析成 ID」判断，结果把敌人伤害当成了我方输出，
        逐回合承伤曲线因此一直是空的（已修）。
        """
        return self.g.is_card(source)

    # ---- 各类增量 ----
    def _player_state(self, st: dict[str, Any] | None,
                      key: str = "") -> dict[str, Any]:
        """把 players.<id> 里的累计字段整理成 PLAYER_STAT 的 data。

        注意：DamageMeter **不记录我方 HP/金币**（实测字段表里没有 hp/max_hp），
        所以这里不会伪造这些值；界面会如实标注。

        `key` 指定取哪位玩家；同时把 `player_key` 放进 data，
        这样 tracker 能确定这条事件属于谁（否则会归到「第一位」）。
        """
        p = self._player(st, key) or {}
        data: dict[str, Any] = {src: int(p.get(k) or 0) for k, src in PLAYER_CUMULATIVE.items()}
        data["max_hit_card"] = p.get("max_hit_card") or ""
        for k in ("hp", "max_hp", "gold", "block", "turn_taken", "block_total"):
            if p.get(k) is not None:
                data[k] = int(p[k])
        # dpt：每个回合各自的伤害值（不是增量），供逐回合曲线使用
        dpt = p.get("dpt")
        if isinstance(dpt, dict) and dpt:
            data["dpt"] = {str(k): int(v or 0) for k, v in dpt.items()}
        if p.get("char_id"):
            data["char_id"] = p["char_id"]
        if p.get("name"):
            data["player_name"] = p["name"]
        pk = str(key or p.get("key") or self.player_key)
        if pk:
            data["player_key"] = pk
        return data

    def all_players(self, st: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        """状态文件里记录的全部玩家（多人对局会有多条）。

        键是 DamageMeter 的玩家 key（单人局通常为 "1"）。
        """
        if not st:
            return {}
        return dict(st.get("players") or {})

    def _turn_delta(self, prev: dict[str, Any], cur: dict[str, Any], run_id: str,
                    enc: str, node_id: str = "") -> list[Event]:
        """回合推进。

        dmg_dealt / dmg_taken 是**整场累计值**（不是本回合增量），所以这里发绝对值快照，
        由 Tracker 直接赋值；逐回合伤害由 dpt 字典给出。
        """
        events: list[Event] = []
        turn = int(cur.get("turn") or 0)
        seg = self._segment(cur)
        floor = self._floor_of(seg)
        # 逐玩家（多人局每人一条），理由见 initial_events
        for pk in (self.player_keys(cur) or [""]):
            data = self._player_state(cur, pk)
            events.append(Event(kind=K.PLAYER_STAT, run_id=run_id, encounter_id=enc,
                                turn=turn, node_id=node_id, key=pk,
                                source="damage_meter",
                                data={**data, "floor": floor}))
        events.extend(self._event_delta(prev, cur, run_id, enc, node_id))
        events.extend(self._combo_diff(prev, cur, run_id, enc, turn, node_id))
        return events

    def _event_delta(self, prev: dict[str, Any], cur: dict[str, Any], run_id: str,
                     enc: str, node_id: str = "") -> list[Event]:
        """events[] 是环形缓冲：找出**从未见过**的新事件。

        坑：只跟「上一帧」比较是不够的。环形缓冲会把最早的事件挤出去，
        之后列表里再也看不到它；等别的事件进来、列表再次变化时，
        同一击可能被重新当成「新增」——实测会让事件流的伤害合计涨到真值的十几倍。
        所以这里维护一份**跨轮询的已见签名集合**，且按场次清空。
        """
        prev_ev = (prev or {}).get("events") or []
        cur_ev = cur.get("events") or []

        def sig(e: dict[str, Any]) -> tuple:
            return (e.get("turn"), e.get("t"), e.get("p"), e.get("l"), e.get("v"))

        if enc != self._seen_encounter:
            self._seen_encounter = enc
            self._seen_sigs.clear()
        # 上一帧的全部先记为已见（即使它已被挤出签名集合）
        for e in prev_ev:
            self._seen_sigs.add(sig(e))
        old_sources = {(e.get("turn"), str(e.get("l") or "").split("→")[0].strip())
                       for e in prev_ev if e.get("t") == T_DAMAGE}
        new_sources = {(e.get("turn"), str(e.get("l") or "").split("→")[0].strip())
                       for e in cur_ev if e.get("t") == T_DAMAGE}
        fresh_sources = new_sources - old_sources
        out: list[Event] = []
        for e in cur_ev:
            key = sig(e)
            if key in self._seen_sigs:
                continue
            self._seen_sigs.add(key)
            ev = self._one_event(e, run_id, enc, node_id, fresh_sources=fresh_sources)
            if ev is not None:
                out.append(ev)
        return out

    def _damage_events(self, st: dict[str, Any], run_id: str, enc: str,
                       node_id: str = "") -> list[Event]:
        """把 events[] 里全部「这一击造成了多少伤害」转成伤害事件（首读用）。

        `t=0` 给出这一击的来源（卡牌名或怪物名），`t=1` 给出数值；
        两者按 (回合, 来源) 配对，就能得到「我方打出的」与「我方承受的」。
        """
        out: list[Event] = []
        for e in st.get("events") or []:
            if e.get("t") != T_DAMAGE_DEALT:
                continue
            out.append(self._one_event(e, run_id, enc, node_id))
        return out

    def _one_event(self, e: dict[str, Any], run_id: str, enc: str,
                   node_id: str = "", fresh_sources: set[tuple] | None = None,
                   ) -> Event | None:
        """把一条原始事件翻译成归一化事件（None = 这条不单独成事件）。

        关键：`p` 字段恒为本机玩家 id，**不能**用来分敌我；
        敌方动作只能通过「来源名」识别——名字是已知卡牌 = 我方出手，
        否则（怪物名）就是敌方打我。由此得到 actor / target / side。
        """
        kind_t = e.get("t")
        label = str(e.get("l") or "")
        value = int(e.get("v") or 0)
        turn = int(e.get("turn") or 0)
        source = label.split("→")[0].strip() if "→" in label else ""
        is_card = self._is_player_card(source)
        base = dict(run_id=run_id, encounter_id=enc, turn=turn, ts=time.time(),
                    key=str(e.get("p") or ""), label=label, value=value,
                    node_id=node_id, source="damage_meter")

        if kind_t == T_DAMAGE:
            # t=0：我方打出的伤害（来源是卡牌），**也混有**敌人施加的持续伤害
            # （如「中毒」，来源不是卡牌）。实测 t=0 合计 == dmg_dealt。
            # 敌我判定用「来源名类别」，不能用 t 值硬套。
            actor_kind = "player" if self.g.is_card(source) else "enemy"
            data = {
                "actor": source or "我方",
                "actor_kind": actor_kind,
                "target": "敌人" if actor_kind == "player" else "我方",
                "damage": value,
                "is_new": (turn, source) in (fresh_sources or set()),
            }
            if actor_kind == "player":
                data["dealt"] = value
            else:
                data["damage_taken"] = value
            ev = Event(kind=K.DAMAGE, name=label, side=actor_kind,
                       data=data, **base)
            ev.value = value
            return ev
        if kind_t == T_DAMAGE_DEALT:
            # t=1：我方承受的伤害（怪物名或状态名）。实测 t=1 合计 == dmg_taken。
            data = {
                "actor": source or "敌方",
                "actor_kind": "enemy",
                "target": "我方",
                "damage": value,
                "damage_taken": value,
            }
            ev = Event(kind=K.DAMAGE, name=label, side="enemy", data=data, **base)
            ev.value = value
            return ev
        if kind_t == T_BLOCK:
            return Event(kind=K.BLOCK, name=label, side="player",
                         data={"actor": "player", "block": value, "raw": e}, **base)
        if kind_t == T_CARD:
            return Event(kind=K.CARD_PLAY, side="player",
                         data={"actor": "player", "card_id": source, "raw": e}, **base)
        if kind_t == T_LOG:
            card = str(e.get("card") or "")
            if not card and "→" in label:
                card = source
            return Event(kind=K.LOG, name=label,
                         side="player" if is_card else "enemy",
                         data={"actor": card or source,
                               "actor_kind": "player" if is_card else "enemy",
                               "card_id": card, "damage": value, "raw": e}, **base)
        if kind_t == T_DEBUFF:
            # "虚弱 x1" / "中毒 x5" 这类状态记录：**来源不明**（可能是我方施加给敌人，
            # 也可能是敌人施加给我方），实测数据里无法区分，因此标为中立，
            # 前端用灰色小标签展示，不硬塞进任何一栏。
            return Event(kind=K.LOG, name=label, side="neutral",
                         data={"actor": "", "actor_kind": "neutral",
                               "effect": source or label, "stacks": value,
                               "raw": e}, **base)
        return Event(kind=K.LOG, name=label, side="enemy",
                     data={"actor": source or "enemy", "actor_kind": "enemy",
                           "raw": e}, **base)

    def _combo_diff(self, prev: dict[str, Any], cur: dict[str, Any], run_id: str,
                    enc: str, turn: int, node_id: str = "") -> list[Event]:
        """每卡打出次数的增量（**逐玩家分别记账**）。

        基线由适配器自己维护（`_cpc_base`）：初始快照已把当时的总次数计入，
        之后的增量必须是「当前值 - 基线」，否则会重复计数。
        只处理名称库里认识的规范 ID，过滤数据源里的脏条目。

        键必须是 `(玩家, 卡牌)`：多人局里两人都可能打出同一张牌，
        只按卡牌记基线会把两个人的次数混在一起（实测踩过）。
        """
        out: list[Event] = []
        if self._cpc_encounter != enc:
            # 换场次：基线重来
            self._cpc_encounter = enc
            self._cpc_base = {}
        for pk in (self.player_keys(cur) or self.player_keys(prev) or [""]):
            p_cur = self._player(cur, pk) or {}
            cpc_cur = p_cur.get("cpc") or {}
            if not cpc_cur:
                continue
            if not any(k[0] == pk for k in self._cpc_base):
                # 首读没有走 initial_events 时，用 prev 状态铺一次基线
                p_prev = self._player(prev, pk) or {}
                for k, v in (p_prev.get("cpc") or {}).items():
                    self._cpc_base[(pk, k)] = int(v or 0)
            for card_id, val in cpc_cur.items():
                total = int(val or 0)
                bkey = (pk, card_id)
                base = int(self._cpc_base.get(bkey, 0))
                delta = total - base
                if delta <= 0:
                    continue
                cid = self.g.canonical(card_id)
                if not cid or not self.g.is_known(cid):
                    self._cpc_base[bkey] = total   # 脏条目也推进基线，避免反复触发
                    continue
                self._cpc_base[bkey] = total
                out.append(Event(kind=K.CARD_PLAY, run_id=run_id, encounter_id=enc,
                                 turn=turn, key=cid, value=delta, node_id=node_id,
                                 name=self.g.card(cid), label=self.g.card(cid),
                                 source="damage_meter",
                                 data={"plays": total, "player_key": pk}))
        return out

    def _player(self, st: dict[str, Any] | None,
                key: str = "") -> dict[str, Any] | None:
        """取某个玩家的数据。

        `key` 为空时退回 `self.player_key`（最后退回第一个玩家）。
        **多人局必须显式传 key**：本文件按玩家分块存 `players.<steamid>`，
        而 `self.player_key` 的默认值 `"1"` 是单人局的键；多人局里没有它，
        于是会「退回第一个玩家」—— 实测那一局里第一个恰好是队友，
        导致事件流里**只有队友的数据**，本机的伤害/出牌全丢，
        界面上本机一栏永远是空的。所以调用方要**遍历所有玩家**分别取。
        """
        if not st:
            return None
        players = st.get("players") or {}
        if key:
            return players.get(str(key))
        return players.get(self.player_key) or (next(iter(players.values())) if players else None)

    def player_keys(self, st: dict[str, Any] | None) -> list[str]:
        """本场出现过的所有玩家键（多人局逐人发事件用）。"""
        if not st:
            return []
        return [str(k) for k in (st.get("players") or {})]

    # ------------------------------------------------------------ 实时轮询
    def watch(self, bus: EventBus, interval: float = 0.5,
              should_stop: Callable[[], bool] | None = None,
              verbose: bool = True) -> None:
        """阻塞轮询：文件一变就 diff 出事件并 emit。"""
        prev: dict[str, Any] | None = None
        last_mtime = 0.0
        while not (should_stop and should_stop()):
            cur = self.read()
            mt = self.mtime()
            if cur is not None and (mt != last_mtime or prev is None):
                last_mtime = mt
                for ev in self.diff(prev, cur):
                    bus.emit(ev)
                if verbose:
                    print(f"[live] turn={cur.get('turn')} enc={cur.get('encounter')} "
                          f"events={len(cur.get('events') or [])}")
                prev = cur
            time.sleep(interval)


def _encounter_type(enc: str) -> str:
    e = enc.upper()
    if "BOSS" in e:
        return "boss"
    if "ELITE" in e:
        return "elite"
    return "monster"


class RunTokenAdapter:
    """读取 `skada_run_token.json`（逐局令牌）。

    游戏里一开新局这个文件就会被写，字段：
        {"StartTime": unix秒, "CharacterId": "CHARACTER.X", "Ascension": n,
         "PlayerCount": n, "MaxActIndex": n, "MaxVisitedMapCoords": n}

    用途：即使战斗状态文件还没出现（开局第一场战斗之前），也能立刻发现「开了新一局」，
    从而马上把监控目标切过去——这就是「开局即监测」的关键补充。
    """

    def __init__(self, path: str | Path | None = None,
                 glossary: Glossary | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_ROOT / RUN_TOKEN
        self.g = glossary or default_glossary()

    def read(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None

    def exists(self) -> bool:
        return self.path.is_file()

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def events(self, token: dict[str, Any] | None, prev_token: dict[str, Any] | None,
               ) -> list[Event]:
        """令牌变化时产出 run_start / act_enter（Boss 前不产生数据事件）。"""
        if not token:
            return []
        start = token.get("StartTime")
        prev_start = (prev_token or {}).get("StartTime")
        if prev_token is not None and start == prev_start:
            return []
        char = str(token.get("CharacterId") or "")
        out = [Event(kind=K.RUN_START, run_id=f"run-{start}", seed=str(start),
                     character=char, ascension=int(token.get("Ascension") or 0),
                     source="run_token",
                     data={"player_count": int(token.get("PlayerCount") or 1),
                           "token": token})]
        return out

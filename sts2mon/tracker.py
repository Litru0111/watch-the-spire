"""Tracker：把事件流增量重建为 RunState。

「实时监控」和「事后解析」共用这一个状态机：
    * 实时：DamageMeter 适配器 diff 出事件 -> Tracker
    * 事后：历史存档适配器直接产事件 -> Tracker（或直接产出 RunState）
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .events import Event, K
from .glossary import Glossary, default_glossary
from .model import (ActState, CardStat, Combat, EventRecord, MapNode, PlayerStat,
                    RunPlayer, RunState, TurnStat)


class Tracker:
    def __init__(self, run_id: str = "", glossary: Glossary | None = None,
                 on_update: Callable[[RunState, Event], None] | None = None) -> None:
        self.g = glossary or default_glossary()
        self.run = RunState(run_id=run_id or "live", live=True, source="live")
        self._node_seq = 0
        self._order = 0
        self._stage_floor = 0     # 数据源自报的楼层（回放会写进 segments[0].floor）
        self._current_turn = 0    # 当前正在进行（或最近完成）的回合号
        self._plays_applied: dict[tuple[str, str], int] = {}   # (encounter, card) -> 已计入的 cpc
        self._cur: MapNode | None = None
        # 多人对局：每位玩家各有一条战斗（键 = 玩家 key）
        self._combats: dict[str, Combat] = {}
        self._seen_events: set[tuple] = set()
        self.on_update = on_update

    # ------------------------------------------------------------ 入口
    def apply(self, ev: Event) -> RunState:
        if ev.turn:
            self._current_turn = ev.turn
        handler = getattr(self, f"_on_{ev.kind}", None)
        if handler:
            handler(ev)
        self._sync_combat_turns()
        self.run.updated_at = time.time()
        if self.on_update:
            self.on_update(self.run, ev)
        return self.run

    def _sync_combat_turns(self) -> None:
        """让回合数始终反映「数据源里见过的最新回合」。

        数据源在回合推进时会发 turn_start，但**首读（initial_events）不会**，
        此时逐回合数据已经写进 turn_stats —— 这里统一以两者最大值兜底，
        否则中途开始监控时实时页会一直显示「第 0 回合」。
        """
        latest = 0
        for c in self._combats.values():
            top = max((t.turn or 0) for t in c.turn_stats) if c.turn_stats else 0
            c.turns = max(c.turns, top, self._current_turn if c.turn_stats else 0)
            latest = max(latest, c.turns)
        if latest:
            self._current_turn = max(self._current_turn, latest)
            if self._cur is not None:
                self._cur.turns_taken = max(self._cur.turns_taken, latest)

    # ---- 玩家分流（多人对局的关键） ----
    # 只有「玩家相关」的事件才带玩家 key：
    #   PLAYER_STAT / DAMAGE / BLOCK / LOG / TURN_START / COMBAT_* → key 或 data.player_key
    # 而 CARD_PLAY / CARD_STAT 的 key 是**卡牌 ID**，绝不能当玩家。
    _PLAYER_SCOPED = {K.PLAYER_STAT, K.DAMAGE, K.BLOCK, K.LOG, K.TURN_START,
                      K.COMBAT_START, K.COMBAT_END, K.RUN_START}

    def _pkey(self, ev: Event) -> str:
        """这条事件属于哪位玩家（实时源的玩家 key，单人局通常是 "1"）。

        注意与 RunState.local_player 的区别：那个是**对局级**的本机标记，
        由存档/历史给定；这里只是「事件带的玩家 key」，两者在多数据源下
        可能取不同的值，所以不能混用。
        """
        explicit = ev.data.get("player_key")
        if explicit:
            return str(explicit)
        if ev.kind in self._PLAYER_SCOPED and ev.key:
            return str(ev.key)
        # 事件没带玩家信息时，归到本机玩家（或第一位）
        run = self.run
        return run.local_player or (run.players[0].player_id if run.players else "1")

    def _char_name(self, char_id: str) -> str:
        """角色中文名；解析不出来时返回**空串**，绝不写占位符。

        词典的兜底是 `'?'`，早先直接把它落库了，于是界面上出现一堆
        「角色 = ?」的记录（实测踩过）。占位符不该进数据库 ——
        留空才能让界面回退到别的字段、也才能在拿到真角色后补上。
        """
        nm = self.g.char(char_id) if char_id else ""
        return "" if nm in ("?", "？", "未知", "None") else nm

    def _track_player(self, key: str, char_id: str = "", name: str = "") -> RunPlayer:
        """登记/更新一名玩家（实时源只给 key 与角色，Steam ID 靠后续对局存档补）。"""
        p = next((x for x in self.run.players if x.player_id == key), None)
        if p is None:
            p = RunPlayer(player_id=key, name=name or f"玩家{key}",
                          character=char_id, character_name=self._char_name(char_id),
                          is_local=(not self.run.players))
            self.run.players.append(p)
            if p.is_local and not self.run.local_player:
                self.run.local_player = key
        else:
            if char_id and not p.character:
                p.character = char_id
                p.character_name = self._char_name(char_id)
            if name and not p.name:
                p.name = name
        # 兼容字段：队长视角
        if self.run.players and self.run.players[0] is p:
            self.run.character = p.character
            self.run.character_name = p.character_name
        return p

    def _combat_for(self, ev: Event) -> Combat:
        """取（或新建）这条事件所属玩家的战斗记录。"""
        return self._ensure_combat(ev)

    def apply_all(self, events: list[Event]) -> RunState:
        for ev in events:
            self.apply(ev)
        return self.run

    # ------------------------------------------------------------ 对局
    def _on_run_start(self, ev: Event) -> None:
        if ev.run_id:
            self.run.run_id = ev.run_id
        self.run.seed = ev.seed or self.run.seed
        self.run.character = ev.character or self.run.character
        # 角色名只在能解析出真名时才覆盖（占位符不落库，见 _char_name）
        nm = self._char_name(ev.character) if ev.character else ""
        if nm:
            self.run.character_name = nm
        self.run.ascension = ev.ascension or self.run.ascension
        self.run.start_time = self.run.start_time or ev.ts
        # 对局模式：**只能被更权威的值覆盖**。
        # 存档里 game_mode 是权威的（"standard" / "custom"）；
        # DamageMeter 与逐局令牌都不掌握模式，绝不能拿它们的 "standard" 去覆盖
        # 已经确认为 custom 的值（实测踩过：一进战斗就退回标准局）。
        gm = str(ev.data.get("game_mode") or "")
        if gm and (gm != "standard" or self.run.game_mode in ("", "standard")):
            self.run.game_mode = gm
        if ev.data.get("build_id"):
            self.run.build_id = ev.data["build_id"]
        # 自定义对局的修正项（如 DRAFT）。同样只在非空时覆盖，
        # 避免后到的、不掌握修正项的数据源把它清空。
        mods = ev.data.get("modifiers")
        if mods:
            self.run.modifiers = [str(m) for m in mods]
        self.run.live = True

    def _on_run_end(self, ev: Event) -> None:
        self.run.live = False
        self.run.end_time = ev.ts
        if "win" in ev.data:
            self.run.win = bool(ev.data["win"])
        self.run.run_time = int(ev.data.get("run_time") or self.run.run_time)
        self.run.abandoned = bool(ev.data.get("abandoned", self.run.abandoned))
        self.run.killed_by = ev.data.get("killed_by", self.run.killed_by)
        for n in self.run.floors:
            n.current = False
        if self.run.deck and "deck" not in ev.data:
            ev.data["deck"] = self.run.deck

    def _on_act_enter(self, ev: Event) -> None:
        act = ev.act or self.run.current_act
        self.run.current_act = act
        self.run.ensure_act(act, ev.data.get("act_id", ""))
        if ev.data.get("act_id"):
            self.run.ensure_act(act).act_id = ev.data["act_id"]

    # ------------------------------------------------------------ 地图 / 楼层
    def _node_id(self, ev: Event) -> str:
        """事件该落到哪个节点。

        优先级：事件显式带的 node_id > 楼层派生的 f{floor:03d} > 顺序号 n{seq}。

        多个数据源（DamageMeter 不知道楼层、current_run.save 知道）必须落到
        **同一个节点**，否则会出现「HP 挂在 A 节点、战斗挂在 B 节点」的分裂。
        所以一律用楼层派生 id，谁先到都一样。
        """
        if ev.node_id:
            return ev.node_id
        floor = ev.floor or (ev.data.get("floor") if ev.data else 0)
        if floor:
            return f"f{int(floor):03d}"
        return f"n{self._order:03d}"

    def _on_map_node(self, ev: Event) -> None:
        """记录地图点（可来自历史存档，也可来自实时地图快照）。"""
        act = ev.act or self.run.current_act
        a = self.run.ensure_act(act, ev.data.get("act_id", ""))
        nid = self._node_id(ev)
        node = next((n for n in a.nodes if n.node_id == nid), None)
        if node is None:
            # 跨 Act 找同 id 的节点：不同数据源对「第几个 Act」可能说法不一
            # （存档知道真实 Act，DamageMeter 只能硬编码 1）。
            # 不这样兜底的话，同一层会被建成两个节点，HP 与战斗就分家了。
            node = next((n for n in self.run.floors if n.node_id == nid), None)
            if node is not None and act and node.act != act:
                # 以带 Act 信息的数据源为准，把节点挪到正确的 Act
                old = self.run.act(node.act)
                if old is not None and node in old.nodes:
                    old.nodes.remove(node)
                node.act = act
                a.nodes.append(node)
        if node is None:
            node = MapNode(node_id=nid, act=act)
            a.nodes.append(node)
        # 切换到「新的楼层」时收尾上一场战斗，避免把新战斗挂到旧节点上
        if self._cur is not None and node is not self._cur \
                and int(node.order or 0) != int(self._cur.order or 0):
            self._combats.clear()
            self._cur = None
        if ev.floor:
            node.order = ev.floor
            self._order = max(self._order, ev.floor)
        elif not node.order:
            self._order += 1
            node.order = self._order
        # 只在事件**确实带了**类型时才覆盖，否则空值会把已有类型抹掉。
        # 实测：回填时最后一个点可能没有类型，它一来就把所有节点的 point_type
        # 清成空串，界面上全变成「?」（踩过）。
        new_type = str(ev.data.get("point_type") or "")
        if new_type:
            node.point_type = new_type
            node.type_label = self.g.name(new_type)
        elif not node.type_label:
            node.type_label = self.g.name(node.point_type)
        if "col" in ev.data:
            node.col = ev.data["col"]
        if "row" in ev.data:
            node.row = ev.data["row"]
        node.children = ev.data.get("children", node.children)
        node.parents = ev.data.get("parents", node.parents)
        # 房间信息只能来自「楼层一致」的数据源。
        # DamageMeter 的状态文件在离开战斗后**不会更新**（还停在上一次战斗），
        # 如果把它贴到新楼层，就会出现「第 6 层的事件被标成第 5 层的怪」——实测踩过。
        room_id = ev.data.get("room_id") or ""
        room_label = ev.data.get("room_label") or ""
        ev_floor = int(ev.floor or (ev.data.get("floor") or 0) or 0)
        same_floor = (not ev_floor) or (ev_floor == int(node.order or 0))
        # 只有「真正的 MAP_NODE」才有资格清空房间信息。
        # _node_for 内部会合成一个 MAP_NODE 事件来定位节点，那种合成事件不带房间信息，
        # 若也走清空分支，就会把刚写好的房间名立刻抹掉（实测踩过）。
        authoritative = bool(ev.data.get("_authoritative_room"))
        if room_id and same_floor:
            node.room_id = room_id
            node.room_label = room_label or self.g.name(room_id)
        elif authoritative and ev.floor and ev.floor == int(node.order or 0):
            # 权威事件明确说这一层没有房间信息 —— 覆盖掉旧值，
            # 否则上一轮会话残留的房间名会一直挂着
            node.room_id = room_id
            node.room_label = room_label
        elif room_id and not same_floor:
            # 过时数据：只补 point_type（新楼层通常还没有类型），不动房间名
            if not node.point_type:
                node.point_type = "monster"
                node.type_label = node.type_label or self.g.name("monster")
        self.run.current_floor = node.order
        self.run.current_act = act
        if ev.data.get("visited", True):
            node.visited = True
            self._set_current(node)

    def _on_node_enter(self, ev: Event) -> None:
        self._on_map_node(ev)

    def _on_room(self, ev: Event) -> None:
        if self._cur is None:
            self._on_map_node(ev)
        if self._cur is not None:
            if ev.data.get("room_id"):
                self._cur.room_id = ev.data["room_id"]
                self._cur.room_label = ev.data.get("room_label") or self.g.name(ev.data["room_id"])
            if ev.data.get("encounter_id"):
                self._cur.room_id = self._cur.room_id or ev.data["encounter_id"]
                self._cur.room_label = self._cur.room_label or self.g.encounter(ev.data["encounter_id"])
            if ev.data.get("point_type"):
                self._cur.point_type = ev.data["point_type"]
                self._cur.type_label = self.g.name(ev.data["point_type"])

    def _set_current(self, node: MapNode) -> None:
        for n in self.run.floors:
            n.current = n.node_id == node.node_id
        self._cur = node
        self._stage_floor = int(node.order or self._stage_floor)

    # ------------------------------------------------------------ 战斗
    def _apply_choices(self, ps: PlayerStat, choices: Any) -> None:
        """把存档解析出的「选择」写入该玩家在该层的数据（幂等覆盖）。

        覆盖的是 card_choices / relics_gained / potions —— 界面上的
        「卡牌抓取监控」「事件选择监控」就是读这些字段。
        开场事件（ancient）的候选项也一并记入，这样涅奥三选一也能看到。
        """
        if not isinstance(choices, dict) or not choices:
            return
        cards = choices.get("card_choices") or []
        if cards:
            ps.card_choices = list(cards)
        relics = choices.get("relics_gained") or []
        if relics:
            ps.relics_gained = list(relics)
        potions = choices.get("potions") or []
        if potions:
            ps.potions = list(potions)
        ancient = choices.get("ancient")
        if isinstance(ancient, dict) and ancient.get("options"):
            self.run.notes["ancient_choice"] = ancient
        rest = choices.get("rest_site_choices")
        if rest:
            # 休息处的选择也记进 card_choices（带 rest_site_choices 字段），
            # 否则界面上看不出这层是「升级」还是「睡觉」
            existing = next((c for c in ps.card_choices if c.get("rest_site_choices")), None)
            if existing:
                existing["rest_site_choices"] = list(rest)
            else:
                ps.card_choices.append({"rest_site_choices": list(rest),
                                        "name": "升级" if "SMITH" in rest else "休息",
                                        "picked": True, "kind": "rest"})

    def _node_for(self, ev: Event) -> MapNode | None:
        """取（必要时建）这条事件所属的地图节点，**但不建战斗**。

        用于「只知道楼层、不知道遭遇」的事件（例如存档源的 HP/金币快照）：
        这类事件应该落到正确的节点上，却不该凭空造出一场战斗。
        """
        floor = ev.floor or (ev.data.get("floor") if ev.data else 0)
        if ev.node_id or floor:
            self._on_map_node(Event(
                kind=K.MAP_NODE, run_id=ev.run_id, act=ev.act,
                floor=int(floor or 0), node_id=ev.node_id or "",
                ts=ev.ts, source=ev.source,
                data={"point_type": (ev.data or {}).get("point_type", ""),
                      "visited": True, "floor": int(floor or 0)}))
        return self._cur

    def _ensure_combat(self, ev: Event) -> Combat:
        if ev.data.get("floor"):
            self._stage_floor = int(ev.data["floor"])
        if self._cur is None:
            # 自动补一个地图点。注意不能和 _on_map_node 抢同一个 _order 计数器：
            # 若当前楼层已有节点，则往后顺延，避免多个战斗挤进同名节点相互覆盖。
            self._node_seq += 1
            floor = ev.floor or self._stage_floor or 0
            used = {int(n.order or 0) for n in self.run.floors}
            if not floor or floor in used:
                floor = (max(used) + 1) if used else (self._order + 1)
                if floor in used:
                    floor = max(used) + 1
            self._on_map_node(Event(kind=K.MAP_NODE, node_id=f"n{self._node_seq:03d}",
                                    floor=floor, act=ev.act,
                                    data={"point_type": ev.data.get("point_type", "monster")},
                                    ts=ev.ts))
        assert self._cur is not None
        key = self._pkey(ev)
        self._track_player(key, str(ev.data.get("char_id") or ""),
                           str(ev.data.get("player_name") or ""))
        enc = ev.encounter_id or self._cur.room_id or "UNKNOWN"
        c = self._combats.get(key)
        if c is None or c.encounter_id != enc:
            c = Combat(encounter_id=enc, name=self.g.encounter(enc),
                       node_id=self._cur.node_id, floor=self._cur.order,
                       act=self._cur.act, player_id=key,
                       character=str(ev.data.get("char_id") or ""),
                       source=ev.source or "live")
            self._combats[key] = c
            if c not in self._cur.combats:
                self._cur.combats.append(c)
            self._cur.room_id = self._cur.room_id or enc
            self._cur.room_label = self._cur.room_label or c.name
            if self._cur.point_type in ("unknown", "", None):
                self._cur.point_type = "monster"
                self._cur.type_label = self.g.name("monster")
        return c

    def _on_combat_start(self, ev: Event) -> None:
        key = self._pkey(ev)
        self._combats.pop(key, None)
        c = self._ensure_combat(ev)
        if ev.data.get("hp") is not None:
            c.player_hp_start = ev.data["hp"]
        if ev.data.get("enemy_hp") is not None:
            c.enemy_hp_start = ev.data["enemy_hp"]

    def _on_combat_end(self, ev: Event) -> None:
        key = self._pkey(ev)
        c = self._combats.get(key)
        if c is None:
            return
        c.outcome = ev.data.get("outcome", c.outcome or "")
        if ev.data.get("hp") is not None:
            c.player_hp_end = ev.data["hp"]
        if ev.data.get("enemy_hp") is not None:
            c.enemy_hp_end = ev.data["enemy_hp"]
        if ev.data.get("turns"):
            c.turns = max(c.turns, int(ev.data["turns"]))
        if self._cur is not None:
            self._cur.turns_taken = c.turns
        self._combats.pop(key, None)
        self._cur = None

    def _on_turn_start(self, ev: Event) -> None:
        c = self._ensure_combat(ev)
        if ev.turn:
            c.turns = max(c.turns, ev.turn)

    # ---- 数值变化 ----
    def _on_player_stat(self, ev: Event) -> None:
        """玩家数值快照（绝对值，幂等）。

        注意：**不能无条件 _ensure_combat**。存档源也会发 PLAYER_STAT（HP/金币），
        但它不带遭遇信息；无条件建战斗会凭空造出 encounter_id=UNKNOWN 的假战斗，
        每个非战斗楼层都会多出一条（实测踩过）。
        只有真正属于战斗的字段（伤害/出牌/格挡/能量）才需要战斗上下文。
        """
        d = ev.data
        combat_fields = (
            "player_damage_dealt", "player_damage_taken", "player_block",
            "player_blocked", "player_energy_spent", "player_energy_wasted",
            "cards_played", "cards_drawn", "cards_discarded", "cards_exhausted",
            "overkill", "max_hit", "damage_dealt", "turn_damage", "turn_taken",
            "turn_cards", "turn_energy", "turn_block", "dpt", "taken_by_turn",
        )
        has_combat_data = any(d.get(k) for k in combat_fields)
        node = self._node_for(ev)
        c = self._combats.get(self._pkey(ev))
        if c is None and (has_combat_data or ev.encounter_id):
            c = self._ensure_combat(ev)

        pid = self._pkey(ev)
        authoritative = bool(d.get("hp_authoritative"))
        if node is not None and (d.get("hp") is not None
                                 or d.get("max_hp") is not None
                                 or d.get("gold") is not None
                                 or d.get("choices")):
            ps = node.stat(pid)
            if ps is None:
                ps = PlayerStat(player_id=pid, order=node.order, act=node.act)
                node.player_stats.append(ps)
            if d.get("hp") is not None:
                ps.hp = int(d["hp"])
            if d.get("max_hp") is not None:
                ps.max_hp = int(d["max_hp"])
            if d.get("gold") is not None:
                ps.gold = int(d["gold"])
            if authoritative:
                ps.hp_source = "current_run"
            self._apply_choices(ps, d.get("choices"))

        # 逐层 HP 曲线（真实值）：直接采用，用于替换「按基线推算」
        curve = d.get("hp_curve")
        if isinstance(curve, list) and curve:
            run = self.run
            run.notes["hp_curve"] = [
                {"floor": int(h.get("floor") or 0), "hp": h.get("hp"),
                 "max_hp": h.get("max_hp"), "gold": h.get("gold"),
                 "damage_taken": int(h.get("damage_taken") or 0),
                 "hp_healed": int(h.get("hp_healed") or 0)}
                for h in curve if h.get("hp") is not None]
            run.notes["hp_source"] = "current_run"

        if c is None:
            return          # 纯 HP/金币快照，没有战斗可归属，结束

        # ---- 以下都是战斗级累计数值（绝对值，幂等）----
        mapping = {
            "damage_dealt": "player_damage_dealt",
            "damage_taken": "player_damage_taken",
            "block_gained": "player_block",
            "blocked": "player_blocked",
            "energy_spent": "player_energy_spent",
            "energy_wasted": "player_energy_wasted",
            "cards_played": "cards_played",
            "cards_drawn": "cards_drawn",
            "cards_discarded": "cards_discarded",
            "cards_exhausted": "cards_exhausted",
            "overkill": "overkill",
            "max_hit": "max_hit",
            "hp": "player_hp_end",
        }
        for src, dst in mapping.items():
            if src in d and d[src] is not None:
                if dst == "max_hit":
                    c.max_hit = max(c.max_hit, int(d[src]))
                    c.max_hit_card = d.get("max_hit_card", c.max_hit_card)
                else:
                    setattr(c, dst, int(d[src]))
        # 也接受已经映射好的目标字段名
        for dst in ("player_damage_dealt", "player_damage_taken", "player_block",
                    "player_blocked", "player_energy_spent", "player_energy_wasted",
                    "cards_played", "cards_drawn", "cards_discarded", "cards_exhausted",
                    "overkill"):
            if dst in d and d[dst] is not None:
                setattr(c, dst, int(d[dst]))
        if d.get("max_hit_card"):
            c.max_hit_card = str(d["max_hit_card"])

        # 逐回合输出：dpt 是「每个回合各自的伤害值」（不是增量），幂等写入
        deals = d.get("dpt")
        if isinstance(deals, dict) and deals:
            for turn_key, value in deals.items():
                try:
                    t_no = int(turn_key)
                except (TypeError, ValueError):
                    continue
                self._write_turn_stat(c, t_no, "damage_dealt", int(value or 0))
        # 逐回合承伤：数据源按事件明细汇总出来的真实值（甲方每次挨打多少）
        taken = d.get("taken_by_turn")
        if isinstance(taken, dict) and taken:
            for turn_key, value in taken.items():
                try:
                    t_no = int(turn_key)
                except (TypeError, ValueError):
                    continue
                self._write_turn_stat(c, t_no, "damage_taken", int(value or 0))
        turn_dealt = int(d.get("turn_damage") or 0)
        turn_taken = int(d.get("turn_taken") or 0)
        if turn_dealt or turn_taken or d.get("hp") is not None:
            self._add_turn_stat(
                ev, side="player", hp=d.get("hp"), max_hp=d.get("max_hp"),
                block=int(d.get("block") or 0), damage_dealt=turn_dealt,
                damage_taken=turn_taken, block_gained=int(d.get("turn_block") or 0),
                energy_spent=int(d.get("turn_energy") or 0),
                cards_played=int(d.get("turn_cards") or 0))

    def _write_turn_stat(self, c: Combat, turn: int, field: str, value: int) -> None:
        """按回合写入来自数据源的「累计值」（dpt 逐回合输出 / 逐回合承伤）。

        数据源给的是截至该回合的累计值，同一回合会随轮询反复出现，因此：
        * 值没变 → 什么都不做（幂等，反复读同一份状态不会重复计数）；
        * 值变了 → 只累加增量（同一回合里逐条事件仍会被正确合并）。
        去重基线存在 `TurnStat.applied` 里，按字段名区分。
        """
        for t in c.turn_stats:
            if t.turn == turn and t.side == "player":
                applied = t.applied.get(field)
                if applied == value:
                    return
                setattr(t, field, (getattr(t, field, 0) or 0) + (value - (applied or 0)))
                t.applied[field] = value
                return
        t = TurnStat(turn=turn, side="player", key="1", **{field: value})
        t.applied[field] = value
        c.turn_stats.append(t)

    def _on_enemy_stat(self, ev: Event) -> None:
        c = self._ensure_combat(ev)
        d = ev.data
        if d.get("damage_dealt") is not None:
            c.enemy_damage_dealt = int(d["damage_dealt"])
        if d.get("hp") is not None:
            c.enemy_hp_end = int(d["hp"])
            self._add_turn_stat(ev, side="enemy", hp=int(d["hp"]),
                                max_hp=d.get("max_hp"), block=int(d.get("block") or 0),
                                damage_dealt=int(d.get("turn_damage") or 0))
        elif d.get("block") is not None or d.get("turn_damage"):
            self._add_turn_stat(ev, side="enemy", hp=None, max_hp=d.get("max_hp"),
                                block=int(d.get("block") or 0),
                                damage_dealt=int(d.get("turn_damage") or 0))

    def _add_turn_stat(self, ev: Event, *, side: str, hp: int | None, max_hp: int | None,
                       block: int, damage_dealt: int = 0, damage_taken: int = 0,
                       block_gained: int = 0, energy_spent: int = 0,
                       cards_played: int = 0, note: str = "") -> None:
        c = self._ensure_combat(ev)
        turn = ev.turn or max(c.turns, 1)
        key = ev.key or ("1" if side == "player" else ev.name)
        pid = self._pkey(ev)
        same_turn = [t for t in c.turn_stats if t.turn == turn and t.side == side]
        # 同一回合同一方只应有一条记录：优先精确 key，其次无 key 的记录，最后才新建
        target = next((t for t in same_turn if t.key == key), None) \
            or next((t for t in same_turn if not t.key), None) \
            or next((t for t in same_turn if t.key in ("", "1")), None)
        if target is not None:
            target.hp = hp if hp is not None else target.hp
            target.max_hp = max_hp if max_hp is not None else target.max_hp
            target.block = block or target.block
            target.damage_dealt += damage_dealt
            target.damage_taken += damage_taken
            target.block_gained += block_gained
            target.energy_spent += energy_spent
            target.cards_played += cards_played
            target.note = note or target.note
            if not target.key:
                target.key = key
            if target.name == "" and ev.name:
                target.name = ev.name
            return
        c.turn_stats.append(TurnStat(
            turn=turn, side=side, key=key, name=ev.name, player_id=pid, hp=hp,
            max_hp=max_hp, block=block, damage_dealt=damage_dealt,
            damage_taken=damage_taken, block_gained=block_gained,
            energy_spent=energy_spent, cards_played=cards_played, note=note))

    # ---- 伤害 / 卡牌 ----
    def _on_damage(self, ev: Event) -> None:
        """一次伤害：**只**更新卡牌归属与事件流，不碰逐回合统计。

        逐回合曲线的权威来源是 PLAYER_STAT 里的 dpt（我方输出）与
        taken_by_turn（我方承伤）——它们本身就是「按回合汇总好的值」。
        如果这里再按事件累加一次，同一击会被算两遍（实测曲线值翻倍）。
        带 `record_only` 的是历史补发条目，连卡牌也不累加。
        """
        c = self._ensure_combat(ev)
        d = ev.data
        if not d.get("record_only") and ev.side != "enemy":
            cid = str(d.get("card_id") or d.get("actor") or "")
            canon = self.g.canonical(cid) if cid else ""
            value = int(ev.value or d.get("damage") or 0)
            if canon and self.g.is_known(canon) and value:
                self._card(c, canon).damage += value
        self._record(ev, kind=K.DAMAGE)

    def _on_card_play(self, ev: Event) -> None:
        c = self._ensure_combat(ev)
        # 历史补发条目（每次轮询重发全量）只进事件流，不累加次数：
        # 出牌次数由 _combo_diff 的新增事件负责
        if ev.data.get("record_only") or ev.data.get("plays") is None:
            self._record(ev, kind=K.CARD_PLAY)
            return
        cid = self.g.canonical(ev.key)
        if not cid or not self.g.is_known(cid):
            return          # 非卡牌条目（脏数据）直接忽略
        cs = self._card(c, cid)
        cs.plays += ev.value or 1
        # 记录已计入的 cpc 累计值，供后续 CARD_STAT 快照去重
        key = (c.encounter_id, cid)
        self._plays_applied[key] = max(self._plays_applied.get(key, 0),
                                       int(ev.data["plays"]))
        self._add_turn_stat(ev, side="player", hp=ev.data.get("hp"),
                            max_hp=ev.data.get("max_hp"),
                            block=int(ev.data.get("block") or 0),
                            cards_played=ev.value or 1)
        self._record(ev, kind=K.CARD_PLAY)

    def _card(self, c: Combat, card_id: str) -> CardStat:
        cid = card_id
        for cs in c.card_stats:
            if cs.card_id == cid:
                return cs
        cs = CardStat(card_id=cid, name=self.g.card(cid))
        c.card_stats.append(cs)
        return cs

    def _on_block(self, ev: Event) -> None:
        c = self._ensure_combat(ev)
        # 历史补发条目只进事件流：格挡累计由 PLAYER_STAT 快照负责
        if ev.data.get("record_only"):
            self._record(ev, kind=K.BLOCK)
            return
        if ev.side == "enemy":
            self._add_turn_stat(ev, side="enemy", hp=ev.data.get("hp"),
                                max_hp=ev.data.get("max_hp"), block=ev.value)
        else:
            c.player_block += ev.value
            self._add_turn_stat(ev, side="player", hp=ev.data.get("hp"),
                                max_hp=ev.data.get("max_hp"), block=ev.value,
                                block_gained=ev.value)
            # 只有明确的卡牌来源才计入卡牌格挡（key 是玩家 id 时不能当卡牌）
            if ev.key and ev.data.get("card_id"):
                cid = str(ev.data["card_id"])
                if self.g.is_known(cid):
                    self._card(c, cid).block += ev.value

    def _on_card_stat(self, ev: Event) -> None:
        """卡牌累计统计的绝对值快照（幂等）。

        `plays`（cpc）由 CARD_PLAY 增量与快照共同决定，这里取「已应用过的最大值」，
        避免「先发快照再发增量」时被重复计数。其他字段直接赋值。
        """
        c = self._ensure_combat(ev)
        cid = self.g.canonical(ev.key)
        if not cid:
            return
        cs = self._card(c, cid)
        for field in ("damage", "block", "energy", "drawn", "discarded", "exhausted"):
            if ev.data.get(field) is not None:
                setattr(cs, field, int(ev.data[field]))
        if ev.data.get("plays") is not None:
            total = int(ev.data["plays"])
            key = (c.encounter_id, cid)
            applied = self._plays_applied.get(key, 0)
            if total > applied:
                cs.plays += total - applied
                self._plays_applied[key] = total

    def _on_card_gain(self, ev: Event) -> None:
        if self._cur is not None and ev.key:
            self._cur.cards_gained.append(ev.key)
        if ev.key:
            self.run.deck.append(ev.key)

    def _on_card_remove(self, ev: Event) -> None:
        if self._cur is not None and ev.key:
            self._cur.cards_removed.append(ev.key)
        if ev.key in self.run.deck:
            self.run.deck.remove(ev.key)

    def _on_relic_gain(self, ev: Event) -> None:
        if ev.key and ev.key not in self.run.relics:
            self.run.relics.append(ev.key)
        if self._cur is not None and ev.key:
            self._cur.relics_gained.append(ev.key)

    def _on_potion(self, ev: Event) -> None:
        if ev.key:
            if ev.data.get("used"):
                if ev.key in self.run.potions:
                    self.run.potions.remove(ev.key)
            elif ev.key not in self.run.potions:
                self.run.potions.append(ev.key)
            if self._cur is not None:
                self._cur.potions.append(ev.key)

    def _on_log(self, ev: Event) -> None:
        # 「某张卡打出多少伤害」的记录（回放与实时源都用 LOG，避免与 dpt 双计），
        # 曲线不参与，但要计入卡牌贡献表。
        cid = ev.data.get("card_id")
        dmg = int(ev.data.get("damage") or 0)
        if cid and dmg:
            c = self._ensure_combat(ev)
            canon = self.g.canonical(str(cid))
            if canon and self.g.is_known(canon):
                self._card(c, canon).damage += dmg
        # 敌方造成的伤害：只进事件流，累计承伤以数据源的绝对值为准
        self._record(ev, kind=K.LOG)

    def _on_raw(self, ev: Event) -> None:
        pass

    # ---- 事件落库缓冲 ----
    def _record(self, ev: Event, kind: str) -> None:
        """实时事件去重后缓存到「该玩家当前那场战斗」，供流程变化展示。

        必须带上 node_id / encounter_id，否则前端无法判断这条事件属于哪场战斗，
        上一场的数据就会混进当前战斗的事件流（实测踩过）。
        找不到对应战斗时退回「当前节点正在进行的战斗」，而不是直接丢掉。
        """
        key = (ev.turn, kind, ev.label, ev.value, ev.key, ev.side)
        if key in self._seen_events:
            return
        self._seen_events.add(key)

        target = self._combats.get(self._pkey(ev))
        if target is None and self._cur is not None:
            # 玩家 key 对不上（多数据源各自编号）时，用当前节点上的战斗兜底
            target = self._cur.combat
        if target is None:
            return          # 真的没有战斗在进行（例如地图上选牌），不记入战斗事件流

        detail = {k: v for k, v in ev.data.items() if k != "raw"}
        detail.setdefault("player_id", self._pkey(ev))
        rec = EventRecord(turn=ev.turn, kind=kind, label=ev.label or ev.key or kind,
                          value=ev.value, side=ev.side or "player",
                          detail=detail, ts=ev.ts,
                          node_id=target.node_id or (ev.node_id or ""),
                          encounter_id=target.encounter_id or (ev.encounter_id or ""))
        target.events.append(rec)

    # ---- 访问器 ----
    @property
    def current_combat(self) -> Combat | None:
        """默认视角：本机玩家（或第一位玩家）当前那场战斗。"""
        if not self._combats:
            return None
        key = self.run.local_player or next(iter(self._combats))
        return self._combats.get(key) or next(iter(self._combats.values()))

    def combat_of(self, player_key: str) -> Combat | None:
        return self._combats.get(player_key)

    @property
    def all_combats(self) -> list[Combat]:
        return list(self._combats.values())

    @property
    def current_turn(self) -> int:
        return self._current_turn

    @property
    def current_node(self) -> MapNode | None:
        return self._cur

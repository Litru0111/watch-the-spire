"""实时状态收集与快照。

Pipeline:
    DamageMeter_combat_state.json
        ↓ (CombatStateAdapter 轮询 + diff)
    事件流 (EventBus)
        ↓ (Tracker 状态机)
    RunState
        ↓ (LiveState.swap 原子替换 + 快照)
    /api/live 前端 1 秒轮询
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .adapters.damage_meter import T_BLOCK, T_LOG
from .events import Event, EventBus, K
from . import effects
from .glossary import Glossary, default_glossary
from .model import RunState
from .summary import base_hp_for, hp_projection
from .tracker import Tracker


class LiveState:
    """线程安全地持有「当前实时对局状态」。"""

    def __init__(self, glossary: Glossary | None = None) -> None:
        self.g = glossary or default_glossary()
        self._lock = threading.RLock()
        self.tracker = Tracker("live", self.g)
        self.last_event: Event | None = None
        self.last_change = 0.0
        self.current_node_id = ""
        self.current_encounter = ""
        self.replay: dict[str, Any] = {}
        # 逐回合承伤的兜底时间线（数据源明细被滚动挤掉时使用）
        self._taken_by_turn: dict[int, int] = {}
        self._taken_seen = 0
        self._taken_encounter = ""
        # 我方数值预测用的轨迹
        self._max_hp_seen = 0
        self._floors_seen = 0
        self._floors_passed = 0
        self._at_node_start = True
        self.run_started_at: float = 0.0
        # 上一局收尾时回调（由 CLI 注入，用来落库）
        self._on_run_finished: Any = None

    def set_finish_hook(self, fn: Any) -> None:
        """注册「一局结束/被新对局取代」时的回调（用于把该局落库）。"""
        self._on_run_finished = fn

    def _notify_finished(self, run: RunState) -> None:
        if self._on_run_finished:
            try:
                self._on_run_finished(run)
            except Exception as exc:      # 回调失败不能影响监控
                print(f"[live] 收尾回调异常: {exc!r}")

    # ------------------------------------------------------------ 写
    def on_event(self, ev: Event) -> None:
        with self._lock:
            if ev.kind == K.RUN_START and ev.run_id and ev.run_id != self.tracker.run.run_id:
                # 换局：先把上一局收尾（否则它会永远显示「进行中」）
                prev = self.tracker.run
                if prev.run_id and prev.floors:
                    prev.live = False
                    prev.end_time = prev.end_time or ev.ts
                    if prev.win is None and not prev.killed_by:
                        prev.notes["ended_reason"] = "被新对局取代（未记录到结束事件）"
                    self._notify_finished(prev)
                self.tracker = Tracker(ev.run_id, self.g)
                self.current_encounter = ""
                self.current_node_id = ""
                self._taken_by_turn = {}
                self._taken_seen = 0
                self._taken_encounter = ""
                self._max_hp_seen = 0
                self._floors_seen = 0
                self.run_started_at = ev.ts
            if ev.encounter_id:
                self.current_encounter = ev.encounter_id
            if ev.node_id:
                self.current_node_id = ev.node_id
            # 逐回合承伤的时间线：数据源的明细会被事件缓冲滚动挤掉，
            # 这里按「累计承伤出现增长的那一刻」把它归到对应回合，作为兜底。
            total = ev.data.get("player_damage_taken")
            if total is not None and ev.turn:
                total = int(total)
                if total > self._taken_seen:
                    if self.current_encounter != self._taken_encounter:
                        self._taken_by_turn = {}       # 换场次：本场重新累计
                        self._taken_encounter = self.current_encounter
                        self._taken_seen = 0
                    delta = total - self._taken_seen
                    if delta > 0:
                        self._taken_by_turn[ev.turn] = \
                            self._taken_by_turn.get(ev.turn, 0) + delta
                        self._taken_seen = total
            self.tracker.apply(ev)
            self._note_floor_progress(ev)
            self.last_event = ev
            self.last_change = time.time()

    def reset(self) -> None:
        with self._lock:
            keep = self.tracker.run.run_id
            self.tracker = Tracker(keep, self.g)
            self.current_encounter = ""
            self.current_node_id = ""
            self._taken_by_turn = {}
            self._taken_seen = 0
            self._taken_encounter = ""

    def attach(self, bus: EventBus) -> None:
        bus.subscribe(self.on_event)

    def set_replay_info(self, info: dict[str, Any]) -> None:
        with self._lock:
            self.replay = info

    def flush_events_to(self, store: Store, run_id: str = "") -> int:
        """把当前累积的事件流落库，返回**新写入**的条数。

        必须幂等：每场战斗自己记水位线（Combat.persisted_events），只写新增部分。
        否则每次 flush 都会把同一批事件重写一遍，事件表条数翻几倍、
        界面上的伤害合计也跟着虚高（实测踩过这个坑）。
        每条事件自带 node_id / encounter_id，前端可据此只看当前这场战斗。
        """
        run = self.tracker.run
        rid = run_id or run.run_id
        if not rid or not run.floors:
            return 0
        store.save_run(run)
        written = 0
        for c in run.combats:
            total = len(c.events)
            if total <= c.persisted_events:
                continue
            new = c.events[c.persisted_events:]
            store.add_events(rid, new)
            c.persisted_events = total
            written += len(new)
        return written

    # ------------------------------------------------------------ 读
    def snapshot(self, fresh_window: float = 180.0, stale_after: float = 45.0) -> dict[str, Any]:
        """实时快照。

        stale_after：超过这么久没有新数据就标记为 stale（界面会提示「等待游戏数据」），
        避免把几分钟前的旧数字当成"当前状态"展示。
        """
        with self._lock:
            snap = self._snapshot(fresh_window)
            age = snap.get("stale_seconds")
            snap["stale"] = bool(age is not None and age > stale_after)
            snap["stale_after"] = stale_after
            snap["prediction"] = self._prediction_locked()
            snap["card_drafts"] = self._card_drafts_locked()
            snap["event_choices"] = self._event_choices_locked()
            return snap

    # ------------------------------------------------------------ 进度与预测
    def _note_floor_progress(self, ev: Event) -> None:
        """跟踪「到过哪些楼层 / 每层的承伤」，供数值预测使用。"""
        try:
            floor = int(ev.data.get("floor") or 0)
        except (TypeError, ValueError):
            floor = 0
        if not floor:
            return
        if ev.kind == K.MAP_NODE and floor > self._floors_seen:
            if self._floors_seen:
                self._floors_passed = self._floors_seen
            self._floors_seen = floor
        if ev.data.get("max_hp"):
            self._max_hp_seen = max(self._max_hp_seen, int(ev.data["max_hp"]))

    def _floor_damage(self) -> list[tuple[int, int]]:
        """(楼层, 本机玩家该层承伤) —— 优先用存档里的真实逐层数据。

        来源顺序：
          1. current_run.save 的 map_point_history（真实承伤，最准）
          2. 本程序自己按事件汇总的逐层承伤（DamageMeter）
        多人局只统计本机玩家。
        """
        me = self.run_local_key()
        curve = self.tracker.run.notes.get("hp_curve")
        if isinstance(curve, list) and curve:
            out = {int(h.get("floor") or 0): int(h.get("damage_taken") or 0)
                   for h in curve if h.get("floor")}
            if out:
                return sorted(out.items())

        out: dict[int, int] = {}
        for n in self.tracker.run.floors:
            s = n.stat(me)
            dmg = 0
            if s and s.damage_taken:
                dmg = int(s.damage_taken)
            c = n.combat_of(me)
            if c and c.player_damage_taken:
                dmg = max(dmg, int(c.player_damage_taken))
            if dmg:
                out[int(n.order or 0)] = dmg
        return sorted(out.items())

    def run_local_key(self) -> str:
        """本机玩家在对局里的 key（实时源里是 DamageMeter 的玩家 key）。"""
        run = self.tracker.run
        return run.local_player or (run.players[0].player_id if run.players else "1")

    def _prediction_locked(self) -> dict[str, Any]:
        """我方数值预测：按目前掉血速度外推，判断会不会因血量耗尽而结束。"""
        run = self.tracker.run
        c = self.tracker.current_combat
        node = self.tracker.current_node
        floor = int(run.current_floor or (node.order if node else 0) or 0)
        act = int(run.current_act or 1)

        per_floor = self._floor_damage()
        done = [d for f, d in per_floor if f and f < floor]
        if floor and c and c.player_damage_taken:
            done.append(c.player_damage_taken)      # 当前层进行中的承伤
        if not done:
            return {"available": False,
                    "reason": "还没有可用的承伤数据（打一场战斗后即可预测）"}

        taken_per_floor = sum(done) / len(done)
        # ---- HP：优先用「进行中存档」的实测值 ----
        # current_run.save 是唯一记录实时 HP 的数据源。
        # 千万不要用 max_hp − 累计承伤去推算：药水回血、休息处、事件治疗、
        # 生命上限变化都会让它偏得离谱（实测曾把 22/75 显示成 62/75）。
        node_me = node.stat(self.run_local_key()) if node else None
        real_hp = node_me.hp if node_me and node_me.hp_source == "current_run" else None
        real_max = node_me.max_hp if node_me and node_me.max_hp else None
        if real_hp is None and node_me and node_me.hp is not None:
            # 其它来源（回放/历史）的 HP 也算实测
            real_hp = node_me.hp
            real_max = real_max or node_me.max_hp

        max_hp = real_max or self._max_hp_seen or (node_me.max_hp if node_me else None) or 0
        assumed = real_hp is None
        if not max_hp:
            base, why = base_hp_for(run.character)
            max_hp, assumed = base, True
        elif real_hp is not None:
            why = "进行中存档实测"
        else:
            why = "本局记录的生命上限"
        if real_hp is not None:
            est_hp = int(real_hp)
        else:
            node_damage = (c.player_damage_taken if c
                           else ((node_me.damage_taken if node_me else 0) or 0))
            est_hp = max(int(max_hp) - int(node_damage), 0)

        proj = hp_projection(hp=est_hp, max_hp=int(max_hp),
                             damage_taken=int(sum(done)), floors_done=len(done),
                             act=act, floor=floor or 1)
        proj.update({
            "est_hp": est_hp,
            "max_hp": int(max_hp),
            "estimated_hp": assumed,
            "hp_authoritative": real_hp is not None,
            "hp_basis": why,
            "floors_done": len(done),
            "source_note": ("HP 来自进行中存档（current_run.save），是实测值"
                            if real_hp is not None else
                            "实时数据源没有 HP：这里按角色基线推算，"
                            "仅供参考，不要据此判断能否活下来"),
        })
        return proj

    # ------------------------------------------------------------ 抓取与事件
    def _card_drafts_locked(self) -> list[dict[str, Any]]:
        """本局（实时）抓到的卡牌与可选牌 —— 按本机玩家分流。

        `kind` 是**动作类型**，界面据此区分四种情况（别把「变化」当成「跳过」）：
            picked      三选一里拿走的
            skipped     三选一里没拿的
            gained      从其它途径进牌组的（含变化后的新牌）
            transformed 被变化掉的原牌
            removed     被移除的原牌
        """
        me = self.run_local_key()
        out: list[dict[str, Any]] = []
        for n in self.tracker.run.floors:
            s = n.stat(me)
            if s is None:
                continue
            for ch in (s.card_choices or []):
                cid = ch.get("card")
                if not cid:
                    continue
                kind = ch.get("kind") or ("picked" if ch.get("picked") else "skipped")
                name = self.g.label(cid)
                eff = effects.effect_with_values(cid)
                # 升级：卡牌升级**不换 ID**（还是 CARD.PREPARED），
                # 显示名要加 `+`、效果要用升级后的数值（打击 6→9 点）。
                # 基础与升级两份都带上，界面按 kind 取。
                if kind == "upgraded":
                    name = effects.upgraded_name(cid, name)
                    up = effects.effect_upgraded(cid)
                    if up:
                        eff = up
                out.append({
                    "floor": n.order, "card_id": cid, "name": name,
                    "picked": bool(ch.get("picked")), "kind": kind,
                    "into": ch.get("into") or "", "into_name": ch.get("into_name") or "",
                    "from": ch.get("from") or "", "from_name": ch.get("from_name") or "",
                    # 效果文案（含数值；升级牌给升级后的数值）
                    "effect": eff,
                    "effect_base": effects.effect_with_values(cid),
                    "effect_upgraded": effects.effect_upgraded(cid),
                    "upgraded": kind == "upgraded",
                    "into_effect": effects.effect_with_values(ch.get("into") or ""),
                    "from_effect": effects.effect_with_values(ch.get("from") or ""),
                    "room": n.room_label or n.type_label})
            have = {d["card_id"] for d in out}
            for cid in (s.cards_gained or []):
                if cid not in have:
                    out.append({"floor": n.order, "card_id": cid,
                                "name": self.g.label(cid), "picked": True,
                                "kind": "gained",
                                "effect": effects.effect_with_values(cid),
                                "room": n.room_label or n.type_label})
        out.sort(key=lambda d: d["floor"] or 0)
        return out

    def _event_choices_locked(self) -> list[dict[str, Any]]:
        """本局（实时）的房间 / 事件选择记录 —— 按本机玩家分流。"""
        g = self.g
        me = self.run_local_key()
        out: list[dict[str, Any]] = []
        for n in self.tracker.run.floors:
            if not n.visited:
                continue
            s = n.stat(me)
            potions = [g.name(p) for p in ((s.potions if s else []) or []) if p]
            relics = [g.name(r) for r in ((s.relics_gained if s else []) or []) if r]
            relic_ids = [r for r in ((s.relics_gained if s else []) or []) if r]
            relic_effects = [effects.effect_with_values(r) for r in relic_ids]
            # 开场事件（远古/涅奥）的完整候选项：DamageMeter 看不到，
            # 由存档解析后存在 run.notes 里，这里透出去给界面上色。
            ancient = self.tracker.run.notes.get("ancient_choice")
            if not isinstance(ancient, dict):
                ancient = None
            # 休息处的选择（升级/睡觉）与升级过的牌
            rest = [c.get("rest_site_choices") or [] for c in (s.card_choices if s else [])
                    if c.get("rest_site_choices")]
            rest_choice = rest[0] if rest else []
            upgraded = [g.label(c.get("card")) for c in ((s.card_choices if s else []) or [])
                        if c.get("kind") == "upgraded" and c.get("card")]
            if n.room_label or potions or relics or ancient or rest_choice or upgraded \
                    or n.point_type:
                out.append({
                    "floor": n.order, "type": n.point_type,
                    "type_label": n.type_label, "room": n.room_label or "",
                    "relics": relics, "potions": potions,
                    "relic_ids": relic_ids, "relic_effects": relic_effects,
                    "rest_choice": rest_choice, "upgraded": upgraded,
                    "ancient": ancient if n.order == 1 else None,
                    "hp_after": s.hp if s else None, "gold": s.gold if s else None,
                })
        return out

    def _snapshot(self, fresh_window: float) -> dict[str, Any]:
        run: RunState = self.tracker.run
        c = self.tracker.current_combat
        node = self.tracker.current_node
        # 当前视角玩家（本机优先）
        me_key = run.local_player or (run.players[0].player_id if run.players else "1")
        me = node.stat(me_key) if node else None
        age = time.time() - self.last_change if self.last_change else None
        available = bool(self.last_change and age is not None and age < fresh_window)

        node_damage = (me.damage_taken if me else 0) or 0
        dpt: dict[str, int] = {}
        taken_by_turn: dict[str, int] = {}
        cards: list[dict[str, Any]] = []
        turns = max(c.turns if c else 0, 1)
        cumulative_dealt = c.player_damage_dealt if c else 0
        # 承伤以战斗中记录的值为准（数据源给的绝对值）；
        # 节点的 damage_taken 只在对局存档里才有，用于回放/历史
        cumulative_taken = c.player_damage_taken if c else node_damage
        if c:
            for t in c.turn_stats:
                if t.turn:
                    dpt[str(t.turn)] = dpt.get(str(t.turn), 0) + (t.damage_dealt or 0)
                    if t.damage_taken:
                        taken_by_turn[str(t.turn)] = (
                            taken_by_turn.get(str(t.turn), 0) + t.damage_taken)
            # 逐回合承伤优先用「每击明细」汇总；明细被事件缓冲挤掉时，
            # 用 LiveState 自己按回合记录的增量时间线兜底。
            if not taken_by_turn and self._taken_by_turn:
                taken_by_turn = {str(k): v for k, v in self._taken_by_turn.items()} \
                    if self._taken_encounter == self.current_encounter else {}
            measured_taken = bool(taken_by_turn)
            if not measured_taken and cumulative_taken and dpt:
                # 连增量时间线都没有（例如中途开始监控）：按回合均摊并在界面标注
                per_turn = int(round(cumulative_taken / max(len(dpt), 1)))
                for k in dpt:
                    taken_by_turn.setdefault(k, per_turn)
            cards = [{"name": self.g.label(s.card_id) or s.name,
                      "card_id": s.card_id,
                      "plays": s.plays, "damage": s.damage, "block": s.block}
                     for s in sorted(c.card_stats, key=lambda x: -x.damage)]
        elif self.last_event and self.last_event.data.get("dpt"):
            dpt = {str(k): int(v) for k, v in self.last_event.data["dpt"].items()}

        return {
            "available": available,
            "stale_seconds": round(age, 1) if age is not None else None,
            "run_id": run.run_id,
            "seed": run.seed,
            "character": run.character,
            "character_name": run.character_name or self.g.char(run.character),
            "ascension": run.ascension,
            # 对局模式：standard / custom（自定义）。自定义还带修正项，
            # 界面据此打「自定义」标记并列出修正项（如「选牌」）。
            "game_mode": run.game_mode,
            "modifiers": list(run.modifiers or []),
            "modifier_names": [self.g.name(m) for m in (run.modifiers or [])],
            "floor": run.current_floor or (node.order if node else 0),
            "act": run.current_act,
            "point_type": node.point_type if node else "",
            "point_label": node.type_label if node else "",
            "encounter": self.current_encounter or (c.encounter_id if c else ""),
            "encounter_name": self.g.encounter(self.current_encounter or (c.encounter_id if c else "")),
            # 当前战斗所在节点：前端用它来只拉「这一场」的事件，
            # 否则上一场的回合会混进实时事件流
            "node_id": (c.node_id if c and c.node_id else
                        (node.node_id if node else "")),
            "turn": self.tracker.current_turn or (c.turns if c else 0),
            "hp": (me.hp if me and me.hp is not None else None),
            "max_hp": (me.max_hp if me and me.max_hp else None),
            "gold": (me.gold if me and me.gold is not None else None),
            "hp_authoritative": bool(me and me.hp_source == "current_run"),
            "hp_source": (me.hp_source if me else ""),
            "damage_dealt": cumulative_dealt,
            "damage_taken": cumulative_taken,
            "blocked": c.player_blocked if c else 0,
            "block_total": c.player_block if c else 0,
            "cards_played": c.cards_played if c else 0,
            "energy_spent": c.player_energy_spent if c else 0,
            "max_hit": c.max_hit if c else 0,
            "max_hit_card": self.g.card(c.max_hit_card) if c and c.max_hit_card else "",
            "dpt": round(c.player_dpt, 1) if c else 0,
            "dpt_by_turn": dpt,
            "taken_by_turn": taken_by_turn,
            "cards": cards,
            "enemy_hp": None,
            "enemy_max_hp": None,
            "server_time": time.time(),
            "updated_at": self.last_change or None,
            "replay": self.replay or None,
        }


# ------------------------------------------------------------------ 回放

def build_replay_writer(store, run_id: str, player_key: str = "1"):
    """从数据库里的一场真实对局，构造「DamageMeter 风格」的状态序列。

    返回 (total_turns, writer_fn)：
        writer_fn(state_path, upto_turn) -> dict
            写出「到第 upto 回合为止」的完整状态快照。
    所有累计字段都单调递增，因此适配器 diff 出来的事件天然正确；
    逐回合推进时（i, i+1, i+2…）就会自然产生 TURN_START 与逐回合曲线。
    """
    row = store.run_row(run_id)
    if not row:
        raise ValueError(f"找不到对局 {run_id}")
    g = default_glossary()
    nodes = store.nodes(run_id)
    combats = {c["node_id"]: c for c in store.combats(run_id)}

    turns: list[dict[str, Any]] = []
    cumulative_dealt = 0
    cumulative_block = 0
    cumulative_energy = 0
    running_hp: int | None = None
    for n in nodes:
        c = combats.get(n["node_id"])
        if not c:
            # 非战斗层也可能改变 HP（休息/事件），继续维护轨迹
            if n.get("hp_after") is not None:
                running_hp = int(n["hp_after"])
            continue
        # 战斗层的起始 HP = 本层开始时（即上一次记录的）HP
        hp_start = running_hp if running_hp is not None else n.get("hp_before")
        enc = c["encounter_id"] or f"FLOOR_{n['ord']}"
        n_turns = int(c["turns"] or 0) or 1
        dmg = int(c["dmg_dealt"] or 0)
        taken = int(c["dmg_taken"] or 0)
        energy = int(c["energy_spent"] or 0)
        cards_played = int(c["cards_played"] or 0)
        cards = store.card_stats(run_id, n["node_id"])
        for t in range(1, n_turns + 1):
            share = dmg // n_turns + (1 if t <= dmg % n_turns else 0)
            share_taken = taken // n_turns + (1 if t <= taken % n_turns else 0)
            cumulative_dealt += share
            # HP 轨迹：本层结束时的 HP 按回合摊回去，让血条随回放下降
            hp_here = hp_start
            if hp_start is not None and taken:
                hp_here = hp_start + int(round(taken * (n_turns - t) / n_turns))
            turn: dict[str, Any] = {
                "turn": 0, "encounter": enc, "floor": int(n["ord"] or 0),
                "room_label": c.get("name") or g.encounter(enc),
                "point_type": n.get("point_type") or "monster",
                "dpt": share, "cum_dealt": cumulative_dealt,
                "cum_taken": taken, "turn_taken": share_taken,
                "energy": energy / n_turns, "cards": cards_played / n_turns,
                "hp": hp_here, "max_hp": n.get("max_hp"),
                "gold": n.get("gold"), "block": 0, "events": [], "card_plays": {},
            }
            if cards:
                cs = cards[(t - 1) % len(cards)]
                cid = cs["card_id"]
                cname = cs["name"] or g.card(cid)
                turn["events"].append({"t": T_LOG, "card": cid,
                                       "label": f"{cname} → {share}", "v": share})
                turn["card_plays"][cid] = 1
                turn["block"] = max(share // 4, 1)
                turn["events"].append({"t": T_BLOCK, "label": f"+{turn['block']} (格挡)",
                                       "v": turn["block"]})
            cumulative_block += turn["block"]
            cumulative_energy += turn["energy"]
            turn["block_total"] = cumulative_block
            turn["energy_total"] = cumulative_energy
            turns.append(turn)
        # 本层结束后的 HP 作为下一层的起点
        if n.get("hp_after") is not None:
            running_hp = int(n["hp_after"])

    for i, t in enumerate(turns, start=1):
        t["turn"] = i

    # 每一层（楼层）对应的回合范围，用于在回放时逐个登记地图点
    stages: list[tuple[int, int, dict[str, Any]]] = []
    for t in turns:
        if not stages or stages[-1][2]["floor"] != t["floor"]:
            stages.append((t["turn"], t["turn"], t))
        else:
            stages[-1] = (stages[-1][0], t["turn"], stages[-1][2])

    def stage_for(turn_no: int) -> dict[str, Any] | None:
        for lo, hi, t in stages:
            if lo <= turn_no <= hi:
                return t
        return stages[-1][2] if stages else None

    def state_at(upto: int) -> dict[str, Any]:
        if not turns:
            t0: dict[str, Any] = {
                "turn": 1, "encounter": "UNKNOWN", "floor": 1, "room_label": "未知",
                "point_type": "monster", "dpt": 0, "cum_dealt": 0, "cum_taken": 0,
                "take_per_turn": 0, "energy": 0, "cards": 0, "hp": None, "max_hp": None,
                "gold": None, "block": 0, "events": [], "card_plays": {}}
        else:
            t0 = turns[min(max(upto, 1), len(turns)) - 1]
        done = [t for t in turns if t["turn"] <= t0["turn"]]
        dpt = {str(t["turn"]): t["dpt"] for t in done}
        cpc: dict[str, int] = {}
        for t in done:
            for cid, n in t["card_plays"].items():
                cpc[cid] = cpc.get(cid, 0) + n
        players = {player_key: {
            "name": "回放", "key": player_key,
            "char_id": row.get("character") or "",
            "color": [0.5, 1, 0, 1],
            "dmg_dealt": t0["cum_dealt"], "dmg_taken": t0["cum_taken"],
            "blocked": 0, "overkill": 0,
            "hits": sum(cpc.values()), "cards_played": sum(cpc.values()),
            "block_total": t0["block_total"],
            "energy_spent": int(round(t0["energy_total"])),            "energy_wasted": 0, "potions": 0, "drawn": 0, "discarded": 0, "exhausted": 0,
            "orbs": 0, "stars": 0, "summons": 0,
            "max_hit": max(dpt.values()) if dpt else 0, "max_hit_card": "",
            "assist_dmg": 0, "assist_block": 0, "dmg_prevented": 0, "dmg_boosted": 0,
            "hp": t0.get("hp"), "max_hp": t0.get("max_hp"), "gold": t0.get("gold"),
            "dpt": dpt, "dbc": {}, "cpc": cpc, "dbs": {}, "ctc": {}, "bbc": {}, "esbc": {},
            "debuffs": {}, "dpp": {}, "draw_ct": {}, "disc_ct": {}, "exh_ct": {},
        }}
        events: list[dict[str, Any]] = []
        for t in done[-4:]:
            for e in t["events"]:
                events.append({"t": e["t"], "turn": t["turn"], "p": player_key,
                               "l": e["label"], "v": e["v"], "card": e.get("card", "")})
        return {
            "schema": 1,
            "run_seed": _numeric_seed(row.get("seed") or run_id),
            "turn": t0["turn"],
            "encounter": t0["encounter"],
            "tracking": True,
            "players": players,
            "segments": [{
                "turn_count": t0["turn"], "encounter": t0["encounter"],
                "floor": t0["floor"], "room_label": t0["room_label"],
                "point_type": t0["point_type"],
                "players": {player_key: {"dmg_dealt": t0["cum_dealt"],
                                         "dmg_taken": int(round(t0["cum_taken"])),
                                         "blocked": 0, "dpt": dpt}},
            }],
            "events": events,
        }

    def writer(state_path, upto_turn: int) -> dict[str, Any]:
        st = state_at(upto_turn)
        state_path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        return st

    total = turns[-1]["turn"] if turns else 0
    return total, writer, stage_for


def _numeric_seed(seed: str) -> int:
    """DamageMeter 的 run_seed 是数字；把字符串种子稳定地映射成数字。"""
    if seed.isdigit():
        return int(seed)
    h = 0
    for ch in seed:
        h = (h * 131 + ord(ch)) % 10_000_000
    return h or 1


def start_replay(store, run_id: str, live: LiveState, sandbox_dir, speed: float = 4.0,
                 player_key: str = "1", realtime: bool = False) -> dict[str, Any]:
    """在后台线程里把一个真实对局重放成实时事件流（逐回合推进）。"""
    g = live.g
    total_turns, writer, _stage_for = build_replay_writer(store, run_id, player_key)
    from .adapters.damage_meter import CombatStateAdapter

    sandbox = _ReplaySandbox(sandbox_dir)
    state_path = sandbox.path
    adapter = CombatStateAdapter(state_path, g, player_key)
    bus = EventBus()
    live.attach(bus)

    row = store.run_row(run_id) or {}
    interval = 2.0 if realtime else max(2.0 / max(speed, 0.1), 0.02)
    stop = threading.Event()
    live.set_replay_info({"run_id": run_id, "seed": row.get("seed"),
                          "turns": total_turns, "interval": round(interval, 3),
                          "done": False, "eta": round(total_turns * interval, 1)})

    prev: dict[str, Any] | None = None
    try:
        for i in range(1, max(total_turns, 1) + 1):
            cur = writer(state_path, i)
            if prev is None:
                for ev in adapter.initial_events(cur):
                    bus.emit(ev)
            else:
                for ev in adapter.diff(prev, cur):
                    bus.emit(ev)
            prev = cur
            if stop.wait(interval):
                break
    finally:
        live.set_replay_info({"run_id": run_id, "seed": row.get("seed"),
                              "turns": total_turns, "interval": round(interval, 3),
                              "done": True, "eta": round(total_turns * interval, 1)})
    return {"run_id": run_id, "turns": total_turns,
            "eta": round(total_turns * interval, 1), "interval": round(interval, 3)}


class _ReplaySandbox:
    """把回放状态写到临时目录，绝不碰游戏真实文件。"""

    def __init__(self, root) -> None:
        from pathlib import Path
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "DamageMeter_combat_state.json"

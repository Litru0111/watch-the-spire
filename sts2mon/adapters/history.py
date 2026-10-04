"""历史对局适配器：解析 Slay the Spire 2 的 `.run` 存档（JSON）。

文件位置：
    %APPDATA%/SlayTheSpire2/steam/<steamid>/[modded/]profile*/saves/history/<ts>.run

包含：种子、角色、进阶、胜负、各 Act 的地图点序列（含房间 model_id、耗时回合）、
每层的 HP/金币/受伤/治疗/选牌/遗物/药水，以及顶层牌组与遗物。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from ..events import Event, K
from ..glossary import Glossary, default_glossary
from ..model import (ActState, CardStat, Combat, MapNode, PlayerStat, RunPlayer,
                     RunState, TurnStat)
from .. import paths

# 游戏写出的存档根目录（跨平台，由 paths 统一决定）
DEFAULT_ROOTS = [paths.sts2_appdata()]

# 顶层字段里这些属于「整场汇总」而非地图
_META_KEYS = {"acts", "ascension", "build_id", "game_mode", "killed_by_encounter",
              "killed_by_event", "map_point_history", "modifiers", "platform_type",
              "players", "run_time", "schema_version", "seed", "start_time",
              "was_abandoned", "win"}


def find_run_files(roots: Iterable[Path] | None = None) -> list[Path]:
    out: list[Path] = []
    for root in (roots or DEFAULT_ROOTS):
        if not Path(root).is_dir():
            continue
        out.extend(Path(root).rglob("history/*.run"))
    return sorted({p.resolve() for p in out if p.is_file()}, key=lambda p: p.stat().st_mtime)


# 存档路径形如 .../steam/<steamid>/[modded/]profile<N>/saves/history/<ts>.run，
# 目录名里的那串数字就是**本机账号的 Steam ID**，可直接与 players[].id 对齐。
_STEAM_ID_IN_PATH = re.compile(r"[\\/]steam[\\/](\d{15,20})[\\/]")


def local_steam_id(path: str | Path) -> str:
    """从存档路径里取本机 Steam ID（取不到返回空串）。

    这是判定「本机玩家」最可靠的依据：
    `.run` 里**没有** local_player_id 字段（实测恒为 None），
    只能靠账号 ID 与 `players[].id` 对齐。
    """
    m = _STEAM_ID_IN_PATH.search(str(path))
    return m.group(1) if m else ""


def _modifier_ids(raw: Any) -> list[str]:
    """自定义对局的修正项 ID 列表，剥掉 `MODIFIER.` 前缀。

    存档形态：`[{"id": "MODIFIER.DRAFT"}]`；也兼容纯字符串列表。
    """
    out: list[str] = []
    for item in (raw or []):
        mid = ""
        if isinstance(item, dict):
            mid = str(item.get("id") or item.get("name") or "")
        else:
            mid = str(item or "")
        if not mid:
            continue
        bare = mid.split(".")[-1] if "." in mid else mid
        if bare and bare not in out:
            out.append(bare)
    return out


def _id_of(obj: Any) -> str:
    """存档里的牌/遗物/事件可能是 str，也可能是 {'id': ...} / {'Key': ...} / {'choice': ...}。

    事件选项尤其嵌套得深，形如：
        {"Key": "THE_LEGENDS_WERE_TRUE.pages.INITIAL.options.NONE.title", "table": "events"}
    这里取 Key 并取最后一段（NONE / MORE_DRINK 这类选项名），便于展示。
    """
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for key in ("id", "choice", "key", "Key", "TextKey"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    # 事件选项形如 {"title": {"key": "....options.NONE.title", "table": "events"}}
    for nested in ("title", "card", "relic", "option"):
        inner = obj.get(nested)
        if isinstance(inner, (dict, str)):
            got = _id_of(inner)
            if got:
                return got
    return ""


def _short_key(raw: str) -> str:
    """'THE_LEGENDS_WERE_TRUE.pages.INITIAL.options.NONE.title' -> 'NONE'。"""
    if not raw or "." not in raw:
        return raw
    parts = [p for p in raw.split(".") if p and p not in ("title", "pages", "options")]
    # 形如 <事件>.pages.<页>.options.<选项>[.title]，取选项名
    if "options" in raw.split("."):
        idx = raw.split(".").index("options")
        seg = raw.split(".")[idx + 1:idx + 2]
        if seg:
            return seg[0]
    return parts[-1] if parts else raw


def _ids_of(seq: Any) -> list[str]:
    if not isinstance(seq, list):
        return []
    return [i for i in (_id_of(x) for x in seq) if i]


class HistoryAdapter:
    """把 .run JSON 转成 RunState（也可转成事件流）。"""

    def __init__(self, glossary: Glossary | None = None) -> None:
        self.g = glossary or default_glossary()

    # ---------------------------------------------------------- 入口
    def parse_file(self, path: str | Path) -> RunState:
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        # run_id 用种子：rdps_meter/<seed>.json 与 othe mod 数据都以种子为键，
        # 文件名只是写入时间戳（unix 秒），不能用来join。
        seed = str(data.get("seed") or "")
        # 路径要传下去：本机 Steam ID 只能从路径里取（见 local_steam_id）
        run = self.parse(data, run_id=seed or p.stem, path=p)
        run.notes["file"] = str(p)
        run.notes["file_name"] = p.name
        return run

    def parse(self, data: dict[str, Any], run_id: str = "",
              path: str | Path | None = None) -> RunState:
        seed = str(data.get("seed") or "")
        run = RunState(
            run_id=run_id or seed or f"run-{int(time.time())}",
            seed=seed,
            ascension=int(data.get("ascension") or 0),
            game_mode=str(data.get("game_mode") or "standard"),
            modifiers=_modifier_ids(data.get("modifiers")),
            build_id=str(data.get("build_id") or ""),
            start_time=float(data.get("start_time") or 0.0),
            run_time=int(data.get("run_time") or 0),
            win=bool(data.get("win")) if data.get("win") is not None else None,
            abandoned=bool(data.get("was_abandoned")),
            source="run_history",
            live=False,
        )
        run.end_time = run.start_time + run.run_time if run.start_time else 0.0

        killed = _id_of(data.get("killed_by_encounter"))
        if killed and killed.upper() not in ("NONE.NONE", "NONE"):
            run.killed_by = self.g.encounter(killed)
        else:
            kev = _id_of(data.get("killed_by_event"))
            if kev and kev.upper() not in ("NONE.NONE", "NONE"):
                run.killed_by = self.g.name(kev)

        players_raw = data.get("players") or []
        for slot, p in enumerate(players_raw):
            pid = str(p.get("id") or p.get("player_id") or p.get("net_id") or f"p{slot}")
            char = _id_of(p.get("character")) or str(p.get("character") or "")
            rp = RunPlayer(
                player_id=pid,
                name=str(p.get("name") or ""),
                character=char,
                character_name=self.g.char(char),
                deck=_ids_of(p.get("deck")),
                relics=_ids_of(p.get("relics")),
                potions=_ids_of(p.get("potions")),
                badges=p.get("badges") or [],
                is_local=False,
            )
            run.players.append(rp)
            if slot == 0:
                # 兼容字段：队长视角（单人局即唯一玩家）
                run.character = rp.character
                run.character_name = rp.character_name
                run.deck = rp.deck
                run.relics = rp.relics
                run.potions = rp.potions
                run.badges = rp.badges
                run.notes["player_id"] = pid
        # 本机玩家判定（按可靠性排序）：
        #   1) 单人局：只有一位玩家，就是本机（**必须先判**——
        #      这时存档里的 id 是整数 1 而不是 Steam ID，
        #      若先去匹配 Steam ID 会匹配不上，反而一个都不标本机）
        #   2) 多人局：用存档路径里的 Steam ID 与 players[].id 对齐，
        #      这是唯一可靠依据（`.run` 里没有 local_player_id，实测恒为 None）
        # 绝不能退化成「players[0]」：三人局里玩家顺序不固定，
        # 那样会把队友当成自己（实测踩过，8 场多人局全认错）。
        if len(run.players) == 1:
            local = str(run.players[0].player_id)
        else:
            local = str(data.get("local_player_id")
                        or (local_steam_id(path) if path else "") or "")
        run.local_player = local
        for rp in run.players:
            rp.is_local = (rp.player_id == run.local_player)

        act_ids = _ids_of(data.get("acts"))
        mph = data.get("map_point_history") or []

        order = 0
        for act_idx, points in enumerate(mph):
            act_no = act_idx + 1
            act = ActState(act=act_no,
                           act_id=act_ids[act_idx] if act_idx < len(act_ids) else "",
                           finished=True)
            run.acts.append(act)
            # 每位玩家各自的上一段 HP（用于算 hp_before）
            prev_hp: dict[str, int | None] = {}
            for pt in points or []:
                order += 1
                node = self._node(pt, act_no, order, prev_hp)
                act.nodes.append(node)
                for s in node.player_stats:
                    if s.hp is not None:
                        prev_hp[s.player_id] = s.hp
            if act.nodes:
                act.current_node = act.nodes[-1].node_id
        run.current_floor = order
        run.current_act = len(run.acts) or 1
        return run

    # ---------------------------------------------------------- 单个地图点
    def _node(self, pt: dict[str, Any], act_no: int, order: int,
              prev_hp: dict[str, int | None]) -> MapNode:
        ptype = str(pt.get("map_point_type") or "unknown")
        node = MapNode(
            node_id=f"a{act_no}f{order:03d}",
            order=order, act=act_no,
            point_type=ptype, type_label=self.g.name(ptype),
            visited=True,
        )
        rooms = pt.get("rooms") or []
        if rooms:
            r0 = rooms[0]
            node.room_id = _id_of(r0.get("model_id")) or str(r0.get("model_id") or "")
            node.room_label = self.g.encounter(node.room_id) if node.room_id else ""
            node.turns_taken = int(r0.get("turns_taken") or 0)

        # 每位玩家一条 PlayerStat（多人对局就在这里分流）
        for st in pt.get("player_stats") or []:
            pid = str(st.get("player_id") or st.get("id") or "")
            if not pid:
                continue
            node.player_stats.append(
                self._player_stat(pid, st, act_no, order, prev_hp.get(pid)))

        # 战斗记录：每位玩家各一条（多人局的正确形状）
        if ptype in ("monster", "elite", "boss") or node.turns_taken:
            for ps in node.player_stats:
                c = self._combat_for(node, ps)
                if c is not None:
                    node.combats.append(c)
            if not node.player_stats:
                c = self._combat_for(node, None)
                if c is not None:
                    node.combats.append(c)
        return node

    def _player_stat(self, pid: str, st: dict[str, Any], act_no: int, order: int,
                     hp_before: int | None) -> PlayerStat:
        ps = PlayerStat(
            player_id=pid, order=order, act=act_no,
            hp=_int_or_none(st.get("current_hp")),
            max_hp=_int_or_none(st.get("max_hp")),
            hp_before=hp_before,
            gold=_int_or_none(st.get("current_gold")),
            damage_taken=int(st.get("damage_taken") or 0),
            hp_healed=int(st.get("hp_healed") or 0),
            gold_gained=int(st.get("gold_gained") or 0),
            gold_spent=int(st.get("gold_spent") or 0),
            max_hp_gained=int(st.get("max_hp_gained") or 0),
            max_hp_lost=int(st.get("max_hp_lost") or 0),
            cards_gained=_ids_of(st.get("cards_gained")),
            cards_removed=_ids_of(st.get("cards_removed")),
            relics_gained=_ids_of(st.get("relic_choices")),
            potions=_ids_of(st.get("potion_choices")) or _ids_of(st.get("potions")),
        )
        for c in st.get("card_choices") or []:
            cid = _id_of(c.get("card"))
            if cid:
                ps.card_choices.append({"card": cid, "name": self.g.card(cid),
                                        "picked": bool(c.get("was_picked"))})
        for c in st.get("relic_choices") or []:
            rid = _id_of(c)
            if rid:
                ps.card_choices.append({"relic": rid, "name": self.g.name(rid)})
        for c in st.get("event_choices") or []:
            raw = _id_of(c)
            if raw:
                ps.card_choices.append({"event": _short_key(raw),
                                        "name": self.g.name(_short_key(raw)),
                                        "raw": raw})
        return ps

    def _combat_for(self, node: MapNode, ps: PlayerStat | None) -> Combat | None:
        """为某位玩家在某层生成一条战斗记录。

        存档只给到「本轮掉了多少血、打了多少回合」的粒度，
        输出/出牌等细粒度只有 rdps 与实时源才有。
        """
        if not node.turns_taken and not (ps and ps.damage_taken):
            return None
        c = Combat(
            encounter_id=node.room_id or node.point_type.upper(),
            name=node.room_label or node.type_label,
            node_id=node.node_id, floor=node.order, act=node.act,
            player_id=ps.player_id if ps else "",
            character=ps.character if ps else "",
            turns=node.turns_taken,
            player_hp_start=ps.hp_before if ps else None,
            player_hp_end=ps.hp if ps else None,
            player_damage_taken=ps.damage_taken if ps else 0,
            outcome="win" if (ps and ps.hp) else "",
            source="run_history",
        )
        if node.turns_taken > 0 and c.player_damage_taken:
            per = c.player_damage_taken / node.turns_taken
            for t in range(1, node.turns_taken + 1):
                c.turn_stats.append(TurnStat(
                    turn=t, side="player", key=ps.player_id if ps else "1",
                    name="", player_id=ps.player_id if ps else "",
                    damage_taken=round(per), note="按场均摊（存档粒度）"))
        return c

    # ---------------------------------------------------------- 事件流
    def to_events(self, run: RunState) -> list[Event]:
        """把 RunState 展开成事件流，喂给 Tracker 可复现同样状态。"""
        evs: list[Event] = [Event(
            kind=K.RUN_START, run_id=run.run_id, seed=run.seed, character=run.character,
            ascension=run.ascension, ts=run.start_time or time.time(),
            data={"game_mode": run.game_mode, "build_id": run.build_id,
                  "modifiers": run.modifiers})]
        for act in run.acts:
            evs.append(Event(kind=K.ACT_ENTER, act=act.act, run_id=run.run_id,
                             data={"act_id": act.act_id}))
            for n in act.nodes:
                evs.append(Event(
                    kind=K.MAP_NODE, run_id=run.run_id, act=n.act, floor=n.order,
                    node_id=n.node_id, data={
                        "point_type": n.point_type, "room_id": n.room_id,
                        "room_label": n.room_label, "visited": True}))
                if n.combat:
                    evs.append(Event(kind=K.COMBAT_START, run_id=run.run_id, act=n.act,
                                     floor=n.order, node_id=n.node_id,
                                     encounter_id=n.combat.encounter_id,
                                     ts=run.start_time))
                    evs.append(Event(
                        kind=K.PLAYER_STAT, run_id=run.run_id, act=n.act, floor=n.order,
                        node_id=n.node_id, encounter_id=n.combat.encounter_id,
                        data={"damage_taken": n.damage_taken, "hp": n.hp_after,
                              "max_hp": n.max_hp, "gold": n.gold}))
                    evs.append(Event(kind=K.COMBAT_END, run_id=run.run_id, act=n.act,
                                     floor=n.order, node_id=n.node_id,
                                     encounter_id=n.combat.encounter_id,
                                     data={"turns": n.turns_taken, "outcome": "win"}))
        evs.append(Event(kind=K.RUN_END, run_id=run.run_id, ts=run.end_time or time.time(),
                         data={"win": run.win, "run_time": run.run_time,
                               "abandoned": run.abandoned, "killed_by": run.killed_by,
                               "deck": run.deck}))
        return evs


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ rdps 明细

class RdpsAdapter:
    """rdps_meter/<seed>.json —— 更细的战斗明细（逐卡伤害/增益/Debuff）。"""

    def __init__(self, root: str | Path | None = None,
                 glossary: Glossary | None = None) -> None:
        self.root = Path(root) if root else (paths.sts2_appdata() / "rdps_meter")
        self.g = glossary or default_glossary()

    def available_runs(self) -> dict[str, Path]:
        if not self.root.is_dir():
            return {}
        return {p.stem: p for p in self.root.glob("*.json")}

    def load(self, run_id: str) -> dict[str, Any] | None:
        p = self.available_runs().get(run_id)
        if not p:
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8-sig"))
        except Exception:
            return None

    def enrich(self, run: RunState, prefer_player: str | None = None) -> int:
        """把 rdps 的战斗明细贴到 run 的战斗节点上，返回命中场次数。

        rdps 文件名 = 种子（与 run.run_id 一致）；每场战斗里的 Players[].NetId
        就是 Steam ID，可直接与存档的 players[].id 对齐——多人对局因此能精确分流。
        """
        data = self.load(run.run_id)
        if not data:
            return 0
        recs = data.get("Combats") or []
        if not recs:
            return 0
        # 把 rdps 的 Roster 也并进来（含玩家名，存档里没有）
        by_netid: dict[str, Any] = {}
        for r in data.get("Roster") or []:
            nid = str(r.get("NetId") or "")
            if nid:
                by_netid[nid] = r
        for rp in run.players:
            r = by_netid.get(rp.player_id)
            if r and r.get("Name") and not rp.name:
                rp.name = str(r["Name"])

        # 按「战斗出现的顺序」与 rdps 的 Combats 顺序对齐
        stages: list[tuple[MapNode, Combat]] = []
        for node in run.floors:
            for c in node.combats:
                stages.append((node, c))
        if not stages:
            return 0

        pid = str(run.local_player or run.notes.get("player_id") or prefer_player or "")
        hits = 0
        for (node, combat), rec in zip(stages, recs):
            label = rec.get("Label") or ""
            if label:
                combat.name = label
            players = rec.get("Players") or []
            if not players:
                continue
            # 找到这条战斗记录对应的那位玩家（按 NetId）
            me = None
            if combat.player_id:
                me = next((p for p in players
                           if str(p.get("NetId") or "") == str(combat.player_id)), None)
            if me is None and pid:
                me = next((p for p in players if str(p.get("NetId") or "") == pid), None)
            if me is None and len(players) == 1:
                me = players[0]
            if me is None:
                continue

            dealt_total = 0
            played = 0
            for dealt in me.get("Dealt") or []:
                raw_card = str(dealt.get("Card") or "")
                amount = int(dealt.get("Amount") or 0)
                if not raw_card:
                    continue
                # rdps 给的是本地化名（中文），归一化成规范 ID 以便与存档数据汇总
                cid = self.g.canonical(raw_card)
                cs = next((c for c in combat.card_stats if c.card_id == cid), None)
                if cs is None:
                    cs = CardStat(card_id=cid, name=self.g.card(cid))
                    combat.card_stats.append(cs)
                cs.damage += amount
                dealt_total += amount
                played += 1
            if dealt_total:
                combat.player_damage_dealt = dealt_total
            # rdps 只记录每次伤害的来源卡，没有「出牌次数」字段：
            # 用「产生过伤害的条目数」作为打出次数的下界，并标注来源。
            if played:
                for cs in combat.card_stats:
                    if cs.plays == 0 and cs.damage:
                        cs.plays = 1
                combat.notes_plays_estimated = True

            blocked_total = 0
            for blocked in me.get("Blocked") or []:
                raw_card = str(blocked.get("Card") or "")
                amount = int(blocked.get("Amount") or 0)
                if not raw_card:
                    continue
                cid = self.g.canonical(raw_card)
                cs = next((c for c in combat.card_stats if c.card_id == cid), None)
                if cs is None:
                    cs = CardStat(card_id=cid, name=self.g.card(cid))
                    combat.card_stats.append(cs)
                cs.block += amount
                blocked_total += amount
            if blocked_total:
                combat.player_block = blocked_total

            # 增益/Debuff 的给予与承受：作为敌方交互记录保留
            for given in me.get("Given") or []:
                effect = str(given.get("Effect") or "")
                if not effect:
                    continue
                combat.turn_stats.append(TurnStat(
                    turn=0, side="debuff", key=f"given:{effect}",
                    player_id=combat.player_id, name=self.g.name(effect),
                    note=f"施加量 {given.get('Amount')}"))
            for got in me.get("Received") or []:
                effect = str(got.get("Effect") or "")
                if not effect:
                    continue
                combat.turn_stats.append(TurnStat(
                    turn=0, side="debuff", key=f"received:{effect}",
                    player_id=combat.player_id, name=self.g.name(effect),
                    note=f"承受量 {got.get('Amount')}"))
            hits += 1
        run.notes["rdps_matched"] = hits
        return hits


def load_all_history(roots: Iterable[Path] | None = None,
                     glossary: Glossary | None = None,
                     limit: int | None = None) -> list[RunState]:
    ad = HistoryAdapter(glossary)
    files = find_run_files(roots)
    if limit:
        files = files[-limit:]
    out: list[RunState] = []
    for f in files:
        try:
            out.append(ad.parse_file(f))
        except Exception as exc:
            print(f"[history] 跳过 {f.name}: {exc!r}")
    return out

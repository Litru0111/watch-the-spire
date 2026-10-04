"""进行中存档（current_run.save）适配器 —— 真实 HP / 金币来源。

这是**唯一能拿到实时生命值**的数据源：
    %APPDATA%/SlayTheSpire2/steam/<steamid>/<modded|>/profile<N>/saves/current_run.save

实测结构（schema_version 6）：
    players[].current_hp / max_hp / gold / deck / relics / potions
    map_point_history[act][point].player_stats[] = 每层每人的数值（含 current_hp）
    visited_map_coords / current_act_index / start_time / save_time

为什么必须用它：
    DamageMeter 只统计伤害与出牌，**完全不记录我方 HP**（实测字段表里没有）；
    rdps / .run 也都没有。之前用「角色基线推算」代替，结果把 22/75 显示成 62/75，
    预测方向都是错的 —— 这个适配器就是来修这个问题的。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..events import Event, K
from ..glossary import Glossary, default_glossary, split_id
from .. import paths
from .history import local_steam_id

PROFILE_ROOTS = [paths.sts2_appdata() / "steam"]


def find_current_run_files() -> list[Path]:
    """找到所有存档位下正在进行的对局文件（可能同时存在多个存档位）。"""
    out: list[Path] = []
    for root in PROFILE_ROOTS:
        if not root.is_dir():
            continue
        for steam_dir in sorted(root.iterdir()):
            if not steam_dir.is_dir():
                continue
            for mode in ("modded", ""):
                base = steam_dir / mode if mode else steam_dir
                if not base.is_dir():
                    continue
                for prof in sorted(base.glob("profile*")):
                    f = prof / "saves" / "current_run.save"
                    if f.is_file():
                        out.append(f)
    return out


def newest_current_run() -> Path | None:
    files = find_current_run_files()
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


class CurrentRunAdapter:
    """读取进行中存档，产出真实 HP / 金币 / 卡组相关的实时事件。"""

    def __init__(self, path: str | Path | None = None,
                 glossary: Glossary | None = None) -> None:
        self.path = Path(path) if path else (newest_current_run()
                                             or Path("__missing_current_run__"))
        self.g = glossary or default_glossary()
        self._last_turn_key: tuple = ()

    # ---------- 读取 ----------
    def exists(self) -> bool:
        return self.path.is_file()

    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def read(self) -> dict[str, Any] | None:
        if not self.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None

    # ---------- 解析 ----------
    def me(self, st: dict[str, Any] | None) -> dict[str, Any]:
        """本机玩家。

        **不能直接取 `players[0]`**：多人对局里玩家顺序不固定，
        取第一个会拿到队友，HP/金币/牌组全看错人。
        存档里每名玩家都有 `id`（Steam ID），用它跟本机账号对齐；
        对齐不上（单人局、或取不到账号）才退回第一个。
        """
        if not st:
            return {}
        players = [p for p in (st.get("players") or []) if isinstance(p, dict)]
        if not players:
            return {}
        # 单人局：players[0].id 就是 1（不是 Steam ID），取唯一的那个即可
        if len(players) == 1:
            return players[0]
        # 多人局：必须按 Steam ID 认，不能取第一个（顺序不固定）
        local = local_steam_id(self.path) if getattr(self, "path", None) else ""
        if local:
            for p in players:
                if str(p.get("id") or "") == local:
                    return p
        return players[0]

    def snapshot(self, st: dict[str, Any] | None) -> dict[str, Any]:
        """整理出「实时状态」需要的一组真实数值。

        字段名以实测为准：角色是 `character_id`（不是 character）、
        本机玩家是 `net_id`。逐层曲线里当前楼层的 stats 可能还没写（会是 0/0），
        所以**实时 HP 一律取 players[0].current_hp**，那是权威值。
        """
        me = self.me(st)
        if not me:
            return {}
        hp = me.get("current_hp")
        max_hp = me.get("max_hp")
        return {
            "hp": int(hp) if hp is not None else None,
            "max_hp": int(max_hp) if max_hp is not None else None,
            "gold": int(me.get("gold") or 0),
            "deck_size": len(me.get("deck") or []),
            "relic_count": len(me.get("relics") or []),
            "potion_count": len(me.get("potions") or []),
            "floor": self.floor(st),
            "act": int(st.get("current_act_index") or 0) + 1,
            "seed": self.seed(st),
            "character": self._char_id(me),
            "player_id": str(me.get("id") or me.get("net_id") or ""),
            # 对局模式与自定义修正项。
            # 存档里 `game_mode` 会是 "custom"，`modifiers` 形如
            # [{"id": "MODIFIER.DRAFT"}]；早先这里写死 "standard"，
            # 自定义对局被当成标准局（实测踩过）。
            "game_mode": str(st.get("game_mode") or "standard"),
            "modifiers": self._modifiers(st),
            "relics": [split_id(str(r.get("id") or ""))[1]
                       for r in (me.get("relics") or []) if isinstance(r, dict)],
            "deck": [split_id(str(c.get("id") or ""))[1]
                     for c in (me.get("deck") or []) if isinstance(c, dict)],
            "save_time": st.get("save_time"),
        }

    def _modifiers(self, st: dict[str, Any]) -> list[str]:
        """自定义对局的修正项 ID 列表（标准局为空）。

        存档形态：`modifiers: [{"id": "MODIFIER.DRAFT"}]`，
        也可能是纯字符串列表，两种都兼容。剥掉 `MODIFIER.` 前缀。
        """
        out: list[str] = []
        for raw in (st.get("modifiers") or []):
            mid = ""
            if isinstance(raw, dict):
                mid = str(raw.get("id") or raw.get("name") or "")
            else:
                mid = str(raw)
            if not mid:
                continue
            bare = split_id(mid)[1]
            if bare and bare not in out:
                out.append(bare)
        return out

    def _char_id(self, me: dict[str, Any]) -> str:
        """角色 ID：`CHARACTER.LEX_NINJA2_CHARACTER_LEX_NINJA2` → `LEX_NINJA2`。

        `split_id` 对 `CHARACTER.X` 返回的是 `X`（即 `LEX_NINJA2_CHARACTER_LEX_NINJA2`），
        所以这里取 `CHARACTER.` 之后去掉 `<MOD>_CHARACTER_` 后缀段。
        """
        bare = split_id(str(me.get("character_id") or ""))[1]
        if "_CHARACTER_" in bare:
            return bare.split("_CHARACTER_")[0]
        return bare

    # ---------- 选择类数据（开场事件、三选一、遗物/药水）----------
    def choices(self, st: dict[str, Any] | None,
                floor: int | None = None) -> dict[str, Any]:
        """解析某一层的全部「选择」。

        重点是**开场事件（ancient）**：存档里叫 `ancient_choice`，
        结构是若干候选项 + `was_chosen`，例如涅奥的「新叶 / 卷轴箱 / 危险剪刀」。
        这是 DamageMeter 完全看不到的（开局没有战斗），只能从存档拿。

        floor=None 时取最后一层。**必须严格按照楼层定位**：
        早期版本遍历时写错了 break，导致每层都返回最后一层的数据，
        于是第 1 层的开场选择被写到了后面每一层上。

        输出结构与历史/实时界面共用：
            card_choices   [{card|relic|event, name, picked, kind, into, from}]
            relics_gained  [relic_id]
            potions        [potion_id]
            ancient        {name, picked, options:[{id,name,chosen}]}
        """
        if not st:
            return {}
        pt = self._point_at(st, floor)
        if not pt:
            return {}
        stats = pt.get("player_stats") or []
        if not stats:
            return {}
        return self._parse_choices(stats[0], str(pt.get("map_point_type") or ""))

    def _point_at(self, st: dict[str, Any], floor: int | None) -> dict[str, Any] | None:
        """取第 floor 个地图点（按 act→point 的线性顺序；floor=None 取最后一个）。"""
        points: list[dict[str, Any]] = []
        for act in st.get("map_point_history") or []:
            for pt in act or []:
                if isinstance(pt, dict):
                    points.append(pt)
        if not points:
            return None
        if floor is None:
            return points[-1]
        try:
            idx = int(floor) - 1
        except (TypeError, ValueError):
            return None
        if 0 <= idx < len(points):
            return points[idx]
        # 楼层号可能大于已写出的点数（当前层还没结算）。
        # 退回最后一个点，但只有在它确实是同一层时才用 —— 否则宁可不返回，
        # 免得把上一层的选择当成这一层的。
        return None

    @staticmethod
    def _card_id(raw: Any) -> str:
        """从「卡牌条目」里取出规范 ID。

        实测有两种形态，都要认：
            "CARD.PINPOINT"                                     （字符串）
            {"id": "CARD.PINPOINT", "floor_added_to_deck": 2}    （字典）
        早期只当字符串处理，结果整个 dict 被当成牌名显示出来。
        """
        if isinstance(raw, dict):
            raw = raw.get("id") or raw.get("card") or raw.get("card_id") or ""
        return split_id(str(raw or ""))[1]

    def _parse_choices(self, ps: dict[str, Any], point_type: str) -> dict[str, Any]:
        out: dict[str, Any] = {"point_type": point_type,
                               "card_choices": [], "relics_gained": [],
                               "potions": [], "ancient": None}

        # 开场事件：候选项 + 选中的那个
        options: list[dict[str, Any]] = []
        for c in ps.get("ancient_choice") or []:
            key = str(c.get("TextKey") or "")
            title = c.get("title") or {}
            if not key and isinstance(title, dict):
                key = str(title.get("key") or "").split(".")[0]
            if key:
                options.append({"id": key, "name": self.g.name(key),
                                "chosen": bool(c.get("was_chosen"))})
        if options:
            picked = next((o for o in options if o["chosen"]), None)
            out["ancient"] = {"name": self.g.name(point_type or "ancient"),
                              "picked": picked["id"] if picked else "",
                              "picked_name": picked["name"] if picked else "",
                              "options": options}

        # 遗物：relic_choices 带 was_picked；ancient 的候选项也算遗物
        for c in ps.get("relic_choices") or []:
            rid = ""
            picked = True
            if isinstance(c, dict):
                rid = split_id(str(c.get("choice") or c.get("relic") or ""))[1]
                picked = bool(c.get("was_picked", True))
            else:
                rid = split_id(str(c))[1]
            if rid:
                out["relics_gained"].append(rid)
                out["card_choices"].append({"relic": rid, "name": self.g.name(rid),
                                            "picked": picked})
        for o in (out["ancient"] or {}).get("options", []):
            if o["chosen"] and o["id"] not in out["relics_gained"]:
                out["relics_gained"].append(o["id"])
                out["card_choices"].append({"relic": o["id"], "name": o["name"],
                                            "picked": True})

        # 卡牌：三选一（card_choices 带 was_picked）
        for c in ps.get("card_choices") or []:
            cid = self._card_id((c or {}).get("card")) if isinstance(c, dict) else ""
            if cid:
                out["card_choices"].append({
                    "card": cid, "name": self.g.label(cid),
                    "picked": bool(c.get("was_picked")),
                    "kind": "picked" if c.get("was_picked") else "skipped"})
        # 卡牌变形（开场「新叶」「危险剪刀」那种）：原牌 → 新牌
        # 注意：原牌不能标成「跳过」——它既没被选中也没被跳过，
        # 而是被**变化**了。这里给显式的 kind，并指明变形成了哪张牌。
        for c in ps.get("cards_transformed") or []:
            old = self._card_id(((c or {}).get("original_card") or {}).get("id"))
            new = self._card_id(((c or {}).get("final_card") or {}).get("id"))
            if old:
                out["card_choices"].append({
                    "card": old, "name": self.g.label(old),
                    "picked": False, "kind": "transformed",
                    "into": new, "into_name": self.g.label(new) if new else ""})
            if new:
                out["card_choices"].append({
                    "card": new, "name": self.g.label(new),
                    "picked": True, "kind": "gained",
                    "from": old, "from_name": self.g.label(old) if old else ""})
        for raw in ps.get("cards_gained") or []:
            c = self._card_id(raw)
            if c:
                out["card_choices"].append({"card": c, "name": self.g.label(c),
                                            "picked": True, "kind": "gained"})
        for raw in ps.get("cards_removed") or []:
            c = self._card_id(raw)
            if c:
                out["card_choices"].append({"card": c, "name": self.g.label(c),
                                            "picked": False, "kind": "removed"})

        # 事件选择（普通事件）：{"title": {"key": "XXX.title", "table": "events"}}
        for c in ps.get("event_choices") or []:
            title = (c or {}).get("title") or {}
            key = str(title.get("key") or "").split(".")[0] if isinstance(title, dict) else ""
            if key:
                out["card_choices"].append({"event": key, "name": self.g.name(key)})

        # 药水
        for c in ps.get("potion_choices") or []:
            pid = split_id(str(c if not isinstance(c, dict)
                               else (c.get("potion") or c.get("choice") or "")))[1]
            if pid:
                out["potions"].append(pid)

        # 休息处：rest_site_choices 形如 ["SMITH"]（升级）或 ["REST"]（睡觉）
        rest = ps.get("rest_site_choices") or []
        if rest:
            out["rest_site_choices"] = [str(x) for x in rest]
        # 升级：upgraded_cards 是被升级的牌，单独记一类，界面显示「升级」
        for raw in ps.get("upgraded_cards") or []:
            c = self._card_id(raw)
            if c:
                out["card_choices"].append({"card": c, "name": self.g.label(c),
                                            "picked": True, "kind": "upgraded"})

        out["hp_healed"] = int(ps.get("hp_healed") or 0)
        return out

    def floor(self, st: dict[str, Any] | None) -> int:
        """已经走过的**总**楼层数（跨 Act 累计）。

        `map_point_history` 是权威来源，**包含当前 Act 的已结算部分**：
        真机实测 Act1 恒 17 点、Act2 满 16 点、Act3 进行中就有 6 点，
        三者相加 39 就是实际走过的层数。历史存档里也能看到
        `[17,16,7]` / `[17,5]` 这类「当前 Act 进行中」的形态。

        早先直接用 `len(visited_map_coords)` 当总层数 —— 那只是**当前 Act**
        的坐标数，多 Act 时严重少算（实测：39 层却报 7 层），
        于是回填只覆盖前 7 层，**后面的升级/选择全部丢失**。
        """
        if not st:
            return 0
        acts = st.get("map_point_history") or []
        total = sum(len(a or []) for a in acts)
        return total if total else len(st.get("visited_map_coords") or [])

    def seed(self, st: dict[str, Any] | None) -> str:
        """对局标识：存档里没有 seed 字段，用 start_time 当 run_id。

        这与 skada_run_token.json 的 StartTime 是同一个值（实测一致），
        也是「逐局令牌」用来识别新对局的键，两个数据源因此能对齐。
        """
        if not st:
            return ""
        if st.get("seed"):
            return str(st["seed"])
        start = st.get("start_time")
        return f"run-{start}" if start else ""

    def hp_history(self, st: dict[str, Any] | None) -> list[dict[str, Any]]:
        """逐层 HP 曲线（真实值，不是推算）。

        当前楼层往往还没写 player_stats（会是 0/0），这类条目直接跳过；
        实时 HP 由 snapshot() 取本机玩家（见 me()，多人局不能取 players[0]）。
        """
        if not st:
            return []
        # 本机玩家的 id：存档里字段名是 `id`（Steam ID），
        # 不是 net_id / player_id —— 用错字段会一直是空串（实测踩过）。
        me_id = str(self.me(st).get("id") or "")
        players = st.get("players") or []
        out: list[dict[str, Any]] = []
        floor = 0
        for act_idx, points in enumerate(st.get("map_point_history") or []):
            for pt in points or []:
                floor += 1
                stats = pt.get("player_stats") or []
                s = None
                if me_id:
                    s = next((x for x in stats
                              if str(x.get("player_id") or "") == me_id), None)
                s = s or (stats[0] if stats else None)
                if not s or not s.get("max_hp"):
                    continue        # 当前楼层还没结算，跳过（别把 0/0 画进曲线）
                out.append({
                    "floor": floor, "act": act_idx + 1,
                    "hp": s.get("current_hp"), "max_hp": s.get("max_hp"),
                    "gold": s.get("current_gold"),
                    "damage_taken": int(s.get("damage_taken") or 0),
                    "hp_healed": int(s.get("hp_healed") or 0),
                })
        return out

    def run_id(self, st: dict[str, Any] | None) -> str:
        """与「逐局令牌」一致的 run_id（start_time）。"""
        return self.seed(st) or "current-run"

    # ---------- 事件 ----------
    def initial_events(self, st: dict[str, Any] | None) -> list[Event]:
        """首读：把真实 HP/金币 以及逐层 HP 曲线一次性发出来。"""
        snap = self.snapshot(st)
        if not snap:
            return []
        run_id = self.run_id(st)
        floor = snap["floor"]
        ts = time.time()
        # 关键：node_id 必须与 DamageMeter 适配器**完全一致**（都用 f{floor:03d}），
        # 否则两个数据源会各建一个地图节点，HP 就挂不到战斗节点上。
        # 同时发一个 MAP_NODE：楼层只有这个数据源知道（DamageMeter 没有），
        # 由它来定节点，后续 DamageMeter 的事件才会挂到同一个节点上。
        events: list[Event] = []
        # 必须发 run_start：LiveState 靠它确定「这一局是谁」。
        # 少了它，tracker 会一直停在默认 run_id（'live'），
        # 等另一个数据源的 run_start 到来时就被判成「换局」而重建，
        # 于是这里发出的 HP 全丢（实测踩过）。
        events.append(Event(
            kind=K.RUN_START, run_id=run_id, seed=run_id.removeprefix("run-"),
            ts=ts, source="current_run",
            character=str(snap.get("character") or ""),
            data={"game_mode": snap.get("game_mode") or "standard",
                  "modifiers": snap.get("modifiers") or [],
                  "hp_authoritative": True}))

        # 补齐**所有已经走过的楼层**：监控可能是中途启动的，
        # 而开场（涅奥）选择与前面几层的三选一都发生在我们启动之前。
        # 只发当前层的话，这些历史选择会永远看不到（实测踩过）。
        hist = self.hp_history(st)
        hp_by_floor = {int(h["floor"]): h for h in hist}
        for f_no in range(1, int(floor) + 1):
            node_id = f"f{f_no:03d}"
            info = hp_by_floor.get(f_no) or {}
            events.append(Event(
                kind=K.MAP_NODE, run_id=run_id,
                act=int(info.get("act") or snap.get("act") or 1),
                floor=f_no, node_id=node_id, ts=ts, source="current_run",
                data={"point_type": self._point_type(st, f_no),
                      "room_id": self._room_info(st, f_no)[0],
                      "room_label": self._room_info(st, f_no)[1],
                      "visited": True, "floor": f_no,
                      # 标记为权威：允许它清空该层残留的旧房间信息
                      "_authoritative_room": True},
                key=None))
            # 每层的 HP / 金币 / 选择都带上：这样历史楼层不是空壳
            per_floor: dict[str, Any] = {"floor": f_no, "hp_authoritative": True}
            if info.get("hp") is not None:
                per_floor["hp"] = info["hp"]
            if info.get("max_hp"):
                per_floor["max_hp"] = info["max_hp"]
            if info.get("gold") is not None:
                per_floor["gold"] = info["gold"]
            if f_no == int(floor):
                per_floor.update(snap)
            per_floor["choices"] = self.choices(st, f_no)
            events.append(Event(
                kind=K.PLAYER_STAT, run_id=run_id, turn=0, ts=ts,
                source="current_run", node_id=node_id, data=per_floor))

        if hist:
            events.append(Event(
                kind=K.PLAYER_STAT, run_id=run_id, turn=0, ts=ts, source="current_run",
                node_id=f"f{floor:03d}" if floor else "",
                data={"hp_curve": hist,
                      "floor_hp": {str(h["floor"]): h["hp"] for h in hist},
                      "hp_authoritative": True, "floor": floor}))
        self._last_turn_key = (snap.get("save_time"), floor, snap.get("hp"))
        return events

    def diff(self, prev: dict[str, Any] | None, cur: dict[str, Any] | None) -> list[Event]:
        """存档没变就什么都不发；变了才发新的绝对快照（幂等）。

        **换楼层时必须发 map_node**：否则下游不会为这一层建节点，
        商店/休息处/事件/宝箱这些没有战斗的楼层会被整个丢掉
        （只有打过架的房间会因为自动建节点而留下）——实测踩过这个坑。
        """
        if cur is None:
            return []
        snap = self.snapshot(cur)
        if not snap:
            return []
        key = (snap.get("save_time"), snap["floor"], snap["hp"], snap["gold"])
        if key == self._last_turn_key:
            return []
        # 换了存档位/新一局：退化成首读
        if prev is None or self.seed(prev) != self.seed(cur):
            return self.initial_events(cur)
        self._last_turn_key = key

        floor = snap["floor"]
        act = int(snap.get("act") or 1)
        node_id = f"f{floor:03d}" if floor else ""
        events: list[Event] = []
        prev_floor = self.floor(prev) if prev else 0
        changed_floor = bool(floor and floor != prev_floor)
        if changed_floor:
            room_id, room_label = self._room_info(cur, floor)
            events.append(Event(
                kind=K.MAP_NODE, run_id=self.run_id(cur), act=act,
                floor=floor, node_id=node_id, ts=time.time(),
                source="current_run",
                data={"point_type": self._point_type(cur, floor),
                      "room_id": room_id, "room_label": room_label,
                      "visited": True, "floor": floor,
                      "_authoritative_room": True},
                key=None))

        # 选择数据不一定和「楼层号变化」同时到位：游戏是**离开一层时**才把该层的
        # player_stats 写进存档的。所以进入第 N 层那一刻读到的是空的，
        # 等玩家走到第 N+1 层，第 N 层的数据才出现——此时楼层号已经变了两次，
        # 只按「楼层变化」发选择的实现会把这一层永久漏掉（实测踩过）。
        # 因此：换层时发当前层的选择；另外只要**任何一层的选择变多了就补发**。
        choices_map: dict[int, Any] = {}
        for f_no in range(1, int(floor) + 1):
            ch = self.choices(cur, f_no)
            if ch.get("card_choices") or ch.get("ancient"):
                choices_map[f_no] = ch
                prev_ch = self.choices(prev, f_no) if prev else {}
                grew = (len(ch.get("card_choices") or [])
                        > len(prev_ch.get("card_choices") or []))
                if grew:
                    # 该层数据（新出现或变多）——单独补一个事件写进对应节点
                    events.append(Event(
                        kind=K.PLAYER_STAT, run_id=self.run_id(cur), turn=0,
                        ts=time.time(), source="current_run",
                        node_id=f"f{f_no:03d}",
                        data={"floor": f_no, "choices": ch,
                              "hp_authoritative": True}))

        events.append(Event(kind=K.PLAYER_STAT, run_id=self.run_id(cur), turn=0,
                            ts=time.time(), source="current_run",
                            node_id=node_id,
                            data={**snap, "hp_authoritative": True,
                                  "choices": choices_map.get(int(floor), {})}))
        return events

    def _point_type(self, st: dict[str, Any], floor: int) -> str:
        """当前楼层的房间类型。

        存档里 `map_point_type` 常常是 `unknown`（事件房就是这种），
        但同一个点的 `rooms[0].room_type` 会给出更具体的类型（event/shop/monster…）。
        用后者兜底，否则事件房会显示成「未知」。
        """
        pt = self._point_at(st, floor)
        if not pt:
            return ""
        pt_type = str(pt.get("map_point_type") or "")
        rooms = pt.get("rooms") or []
        room_type = ""
        if rooms and isinstance(rooms[0], dict):
            room_type = str(rooms[0].get("room_type") or "")
        # 更具体的优先：unknown / 空 都让 room_type 顶上
        if room_type and (not pt_type or pt_type == "unknown"):
            return room_type
        return pt_type or room_type

    def _room_info(self, st: dict[str, Any], floor: int) -> tuple[str, str]:
        """(room_id, room_label)：房间/事件的标识与显示名。

        事件房的 `rooms[0].model_id` 形如 `EVENT.THE_LEGENDS_WERE_TRUE`，
        把它解析出来才能显示「传说是真的」而不是「未知」。
        encounter 房则是 `ENCOUNTER.XXX`，交给 encounter() 译名。
        """
        pt = self._point_at(st, floor)
        if not pt:
            return "", ""
        rooms = pt.get("rooms") or []
        model = ""
        room_type = ""
        if rooms and isinstance(rooms[0], dict):
            model = str(rooms[0].get("model_id") or "")
            room_type = str(rooms[0].get("room_type") or "")
        if model and model != "None":
            prefix, bare = split_id(model)
            up = (prefix or "").upper()
            if up == "EVENT" or bare.startswith("EVENT_"):
                return bare, self.g.name(bare)
            if up in ("ENCOUNTER", "MONSTER") or bare:
                return bare, self.g.encounter(bare)
            return bare, self.g.name(bare)
        # 没有 model_id 的房间（商店/休息处/宝箱）：用类型名当标签，
        # 否则界面上这些楼层会是空白（实测踩过）
        label = self.g.name(room_type or str(pt.get("map_point_type") or ""))
        return "", label

"""杀戮尖塔2 数据监控 —— 核心数据模型与存储。

设计要点
--------
* 三路数据源（实时 mod 状态文件 / 历史 .run 存档 / rdps 战斗明细）都归一化成
  同一套 dataclass 模型，再写入同一张 SQLite 库。
* 每个楼层 = 一个 MapNode（地图点），战斗点额外带 Combat。
* Combat 记录我方与敌方的逐回合数据变化（TurnStat）与逐卡数据（CardStat）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 3

# ---------------------------------------------------------------- 模型


@dataclass
class CardStat:
    """单张卡在整场战斗中的贡献。"""
    card_id: str
    name: str = ""
    plays: int = 0
    damage: int = 0
    block: int = 0
    energy: int = 0
    drawn: int = 0
    discarded: int = 0
    exhausted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlayerStat:
    """某一个玩家、在某一层的数据（多人对局里每人一条）。

    单人局就是一条；多人局每层会有 N 条——这是「区分多人对局」的基础。
    """
    player_id: str
    order: int = 0
    act: int = 0
    name: str = ""
    character: str = ""
    character_name: str = ""
    hp: int | None = None
    max_hp: int | None = None
    hp_before: int | None = None
    gold: int | None = None
    damage_taken: int = 0
    hp_healed: int = 0
    gold_gained: int = 0
    gold_spent: int = 0
    max_hp_gained: int = 0
    max_hp_lost: int = 0
    cards_gained: list[str] = field(default_factory=list)
    cards_removed: list[str] = field(default_factory=list)
    card_choices: list[dict[str, Any]] = field(default_factory=list)
    relics_gained: list[str] = field(default_factory=list)
    potions: list[str] = field(default_factory=list)
    hp_source: str = ""           # 该 HP 值的来源（current_run = 存档实测）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TurnStat:
    """某一个回合里，某一方的数据快照（实时监控的核心）。"""
    turn: int
    side: str = "player"          # player / enemy
    key: str = ""                 # 玩家 id 或怪物名
    name: str = ""
    player_id: str = ""           # 归属玩家（多人对局里用来分流）
    hp: int | None = None
    max_hp: int | None = None
    block: int = 0
    damage_dealt: int = 0
    damage_taken: int = 0
    block_gained: int = 0
    energy_spent: int = 0
    cards_played: int = 0
    note: str = ""
    # 数据源给过的「累计值」基线（按字段名），用于幂等去重，见 Tracker._write_turn_stat
    applied: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventRecord:
    """实时事件流中的一条（用于「变化过程」回放）。

    node_id / encounter_id 必须带上：前端靠它把事件按「战斗」分组，
    否则上一场战斗的回合会混进当前战斗的事件流（实测踩过）。
    """
    turn: int
    kind: str
    label: str
    value: int = 0
    side: str = "player"
    detail: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    node_id: str = ""
    encounter_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["detail"] = json.dumps(self.detail, ensure_ascii=False)
        return d


@dataclass
class Combat:
    """一场战斗（多人对局里每位玩家各有一条）。"""
    encounter_id: str
    name: str = ""
    node_id: str = ""
    player_id: str = ""              # 归属玩家
    character: str = ""
    floor: int = 0
    act: int = 0
    turns: int = 0
    player_hp_start: int | None = None
    player_hp_end: int | None = None
    player_damage_dealt: int = 0
    player_damage_taken: int = 0
    player_block: int = 0
    player_blocked: int = 0          # 实际挡下的伤害
    player_energy_spent: int = 0
    player_energy_wasted: int = 0
    cards_played: int = 0
    cards_drawn: int = 0
    cards_discarded: int = 0
    cards_exhausted: int = 0
    overkill: int = 0
    max_hit: int = 0
    max_hit_card: str = ""
    enemy_damage_dealt: int = 0      # 敌方打出的总伤害
    enemy_hp_start: int = 0
    enemy_hp_end: int = 0
    outcome: str = ""                # win / loss / unknown
    source: str = ""                 # damage_meter / run_history / rdps
    notes_plays_estimated: bool = False   # 打出次数是否为下界估算（rdps 源无次数）
    # 已落库的事件条数（水位线）：落库必须幂等，
    # 否则每次 flush 都会把同一批事件重写一遍（实测条数会翻几倍）
    persisted_events: int = 0
    turn_stats: list[TurnStat] = field(default_factory=list)
    card_stats: list[CardStat] = field(default_factory=list)
    events: list[EventRecord] = field(default_factory=list)

    @property
    def player_dpt(self) -> float:
        return round(self.player_damage_dealt / self.turns, 2) if self.turns else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["player_dpt"] = self.player_dpt
        return d


@dataclass
class MapNode:
    """地图上的一个点（一层）。玩家相关数据在 player_stats 里（多人局每人一条）。"""
    node_id: str
    order: int = 0
    act: int = 0
    point_type: str = "unknown"
    type_label: str = ""
    col: int | None = None
    row: int | None = None
    children: list[str] = field(default_factory=list)
    parents: list[str] = field(default_factory=list)
    room_id: str = ""
    room_label: str = ""
    turns_taken: int = 0
    visited: bool = False
    current: bool = False
    is_start: bool = False        # 地图上的可选起点（analytics 提供）
    player_stats: list[PlayerStat] = field(default_factory=list)
    combats: list[Combat] = field(default_factory=list)   # 多人局：每位玩家一条
    turn_stats: list[TurnStat] = field(default_factory=list)

    @property
    def combat(self) -> Combat | None:
        """兼容单人局 / 默认视角：取第一顺位玩家的那条。"""
        return self.combats[0] if self.combats else None

    @combat.setter
    def combat(self, value: Combat | None) -> None:
        self.combats = [value] if value is not None else []

    def combat_of(self, player_id: str | None = None) -> Combat | None:
        if player_id:
            return next((c for c in self.combats if c.player_id == player_id), None)
        return self.combats[0] if self.combats else None

    # ---- 便捷访问（单人局/取队长用） ----
    def stat(self, player_id: str | None = None) -> PlayerStat | None:
        """取某个玩家的数据；不指定则取第一个（单人局即唯一一个）。"""
        if player_id:
            return next((s for s in self.player_stats if s.player_id == player_id), None)
        return self.player_stats[0] if self.player_stats else None

    @property
    def players(self) -> list[str]:
        return [s.player_id for s in self.player_stats]

    @property
    def multiplayer(self) -> bool:
        return len(self.player_stats) > 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("combats", None)
        return d


@dataclass
class ActState:
    """一层（Act）。"""
    act: int
    act_id: str = ""
    nodes: list[MapNode] = field(default_factory=list)
    current_node: str = ""
    finished: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"act": self.act, "act_id": self.act_id,
                "nodes": [n.to_dict() for n in self.nodes],
                "current_node": self.current_node, "finished": self.finished}


@dataclass
class RunPlayer:
    """对局中的一名玩家（对局级信息：牌组、遗物、徽章）。"""
    player_id: str
    name: str = ""
    character: str = ""
    character_name: str = ""
    deck: list[str] = field(default_factory=list)
    relics: list[str] = field(default_factory=list)
    potions: list[str] = field(default_factory=list)
    badges: list[dict[str, Any]] = field(default_factory=list)
    is_local: bool = False        # 是否本机玩家

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunState:
    """一整场对局（实时进行中或已结束）。"""
    run_id: str
    seed: str = ""
    character: str = ""           # 兼容字段：队长（单人局即唯一玩家）的角色
    character_name: str = ""
    ascension: int = 0
    game_mode: str = "standard"
    # 自定义对局的修正项（如 ["DRAFT"]）。标准局为空。
    # 存档里 game_mode == "custom" 时才有意义，用来区分「自定义」与「标准」对局。
    modifiers: list[str] = field(default_factory=list)
    build_id: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    run_time: int = 0
    win: bool | None = None
    abandoned: bool = False
    acts: list[ActState] = field(default_factory=list)
    relics: list[str] = field(default_factory=list)
    deck: list[str] = field(default_factory=list)
    potions: list[str] = field(default_factory=list)
    killed_by: str = ""
    badges: list[dict[str, Any]] = field(default_factory=list)
    players: list[RunPlayer] = field(default_factory=list)   # 多人对局：每人在此
    local_player: str = ""        # 本机玩家 id（rdps/实时数据以它为主视角）
    # 权威本机身份：存档路径里的 steam/<id>。与 local_player 的区别是
    # 它**在拿到存档前**就能确定，所以 tracker 靠它判定 is_local，
    # 而不是靠「第一个出现的玩家」猜（多人局会认错人）。
    local_player_id: str = ""
    # 已知的玩家人数（0 = 未知）。用于「确定单人」时才敢认定唯一玩家是本机。
    player_count: int = 0
    source: str = ""
    live: bool = False
    current_floor: int = 0
    current_act: int = 1
    updated_at: float = field(default_factory=time.time)
    notes: dict[str, Any] = field(default_factory=dict)

    # ---- 便捷访问 ----
    @property
    def multiplayer(self) -> bool:
        return len(self.players) > 1 or any(n.multiplayer for n in self.floors)

    def is_valid(self) -> bool:
        """这场对局是否值得出现在列表里。

        **排除两类**（实测都在用户的库中出现过）：

        1. **回放残留**（`source == "replay"`）
           `sts2mon demo` 会把一场历史对局重放成实时流，结束后把 tracker
           的状态存了下来。于是库里多出一条 run_id 形如 `live-<seed>`、
           `start_time` 与 `end_time` 只差**零点几秒**、却写着十几个楼层
           几十场战斗的记录 —— 它根本不是用户打的。

        2. **空对局**（没有战斗，也没有回合）
           开局就退（`abandoned=1`）、或回到主菜单时游戏落下的存档。
           实测有 1 层 0 战 0 回合、时长 11~80 秒的记录。

        3. **时长与起止不符**
           真实对局的 `end_time - start_time` **精确等于** `run_time`
           （113/113 场实测偏差全为 0.000），而回放残留偏差 40~49 秒
           —— 回放器压缩了时间轴。容差取 5 秒，两边都留足余量。

        4. **截断的实时采集**
           `run_time == 0` 且没有结局（`win is None`）也不是进行中。
           实时监控启动后被杀掉就会留下这种：实测有「1 层 3 战、
           结束时间只比开始晚 0.6 秒」的记录 —— 它排在列表最前面（按时间倒序），
           看起来像一场坏掉的对局。
           真实存档 113 场里没有一条 `run_time == 0` 或 `win is None`，
           所以这条规则不误伤。

        **不排除**「1 层但有战斗」的 —— 那是真的在第一层死了，是有效对局。
        """
        if str(self.source or "") in ("replay", "live-aborted"):
            return False
        if self.start_time and self.end_time:
            if abs((self.end_time - self.start_time) - self.run_time) > 5:
                return False
        # 截断的实时采集：没有时长、没有结局、也不是进行中
        if self.run_time == 0 and self.win is None and not self.live:
            return False
        combats = sum(len(n.combats) for n in self.floors) or 0
        if combats > 0:
            return True
        # 没有战斗：至少要有走过多层，才算真的玩过
        return len(self.floors) > 1

    @property
    def player_ids(self) -> list[str]:
        return [p.player_id for p in self.players]

    def player(self, player_id: str | None = None) -> RunPlayer | None:
        if player_id:
            return next((p for p in self.players if p.player_id == player_id), None)
        return self.players[0] if self.players else None

    @property
    def floors(self) -> list[MapNode]:
        out: list[MapNode] = []
        for a in self.acts:
            out.extend(a.nodes)
        return sorted(out, key=lambda n: n.order)

    @property
    def combats(self) -> list[Combat]:
        """全部战斗（多人局里每位玩家各一条）。"""
        out: list[Combat] = []
        for n in self.floors:
            out.extend(n.combats)
        return out

    def combats_of(self, player_id: str) -> list[Combat]:
        """某位玩家的所有战斗（多人局按玩家分流）。"""
        return [c for n in self.floors for c in n.combats
                if not c.player_id or c.player_id == player_id]

    def floor(self, order: int) -> MapNode | None:
        for n in self.floors:
            if n.order == order:
                return n
        return None

    def act(self, act: int) -> ActState | None:
        for a in self.acts:
            if a.act == act:
                return a
        return None

    def ensure_act(self, act: int, act_id: str = "") -> ActState:
        a = self.act(act)
        if a is None:
            a = ActState(act=act, act_id=act_id)
            self.acts.append(a)
            self.acts.sort(key=lambda x: x.act)
        elif act_id and not a.act_id:
            a.act_id = act_id
        return a

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "seed": self.seed,
            "character": self.character, "character_name": self.character_name,
            "ascension": self.ascension, "game_mode": self.game_mode,
            "modifiers": self.modifiers,
            "build_id": self.build_id, "start_time": self.start_time,
            "end_time": self.end_time, "run_time": self.run_time,
            "win": self.win, "abandoned": self.abandoned,
            "acts": [a.to_dict() for a in self.acts],
            "relics": self.relics, "deck": self.deck, "potions": self.potions,
            "killed_by": self.killed_by, "badges": self.badges,
            "players": [p.to_dict() for p in self.players],
            "local_player": self.local_player,
            "multiplayer": self.multiplayer,
            "source": self.source, "live": self.live,
            "current_floor": self.current_floor, "current_act": self.current_act,
            "updated_at": self.updated_at, "notes": self.notes,
        }

    def summary(self, player_id: str | None = None) -> dict[str, Any]:
        """整体或某位玩家的概要。

        player_id=None 时给出「全队合计 + 队长视角」，用于列表展示。
        """
        if player_id:
            cmb = self.combats_of(player_id)
            p = self.player(player_id)
            total_dealt = sum(c.player_damage_dealt for c in cmb)
            total_taken = sum(c.player_damage_taken for c in cmb)
            stats = [n.stat(player_id) for n in self.floors]
            stats = [s for s in stats if s]
            hp_end = next((s.hp for s in reversed(stats) if s.hp is not None), None)
            return {
                "run_id": self.run_id, "player_id": player_id,
                "character": (p.character_name if p else "") or self.character_name,
                "ascension": self.ascension, "win": self.win,
                "floors": len([s for s in stats if s]),
                "combats": len(cmb),
                "turns": sum(c.turns for c in cmb),
                "damage_dealt": total_dealt, "damage_taken": total_taken,
                "hp_end": hp_end,
                "deck_size": len(p.deck) if p else 0,
                "relic_count": len(p.relics) if p else 0,
                "base": self.summary(),
            }
        combats = self.combats
        total_dealt = sum(c.player_damage_dealt for c in combats)
        total_taken = sum(c.player_damage_taken for c in combats)
        return {
            "run_id": self.run_id, "seed": self.seed,
            "character": self.character_name or self.character,
            "ascension": self.ascension, "win": self.win,
            "abandoned": self.abandoned,
            "floors": len(self.floors), "combats": len(combats),
            "turns": sum(c.turns for c in combats),
            "damage_dealt": total_dealt, "damage_taken": total_taken,
            "run_time": self.run_time, "start_time": self.start_time,
            "killed_by": self.killed_by, "live": self.live,
            "current_floor": self.current_floor, "current_act": self.current_act,
            "deck_size": len(self.deck), "relic_count": len(self.relics),
            "player_count": max(len(self.players), 1),
            "multiplayer": self.multiplayer,
        }


# ---------------------------------------------------------------- 存储


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# 这些列在 SQLite 里可能以文本形式返回 "0"/"1"，需要还原成数字（否则 bool("0") 为真）
_BOOL_COLUMNS = {
    "win", "abandoned", "live", "multiplayer", "is_local", "visited", "current",
    "is_start", "tracking",
}


DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    seed TEXT, character TEXT, character_name TEXT,
    ascension INTEGER, game_mode TEXT, build_id TEXT,
    modifiers TEXT,
    start_time REAL, end_time REAL, run_time INTEGER,
    win INTEGER, abandoned INTEGER, killed_by TEXT,
    source TEXT, live INTEGER, current_floor INTEGER, current_act INTEGER,
    floors INTEGER, combats INTEGER, turns INTEGER,
    damage_dealt INTEGER, damage_taken INTEGER,
    deck_size INTEGER, relic_count INTEGER,
    deck TEXT, relics TEXT, badges TEXT, notes TEXT,
    player_count INTEGER DEFAULT 1,
    multiplayer INTEGER DEFAULT 0,
    local_player TEXT DEFAULT '',
    updated_at REAL
);

-- 对局中的玩家（多人对局每人一行）
CREATE TABLE IF NOT EXISTS players (
    run_id TEXT, player_id TEXT, slot INTEGER,
    name TEXT, character TEXT, character_name TEXT,
    deck TEXT, relics TEXT, potions TEXT, badges TEXT,
    is_local INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, player_id)
);

-- 地图点：只存「与玩家无关」的信息（房间、遭遇、坐标）
CREATE TABLE IF NOT EXISTS nodes (
    run_id TEXT, node_id TEXT, ord INTEGER, act INTEGER,
    point_type TEXT, type_label TEXT, col INTEGER, row INTEGER,
    room_id TEXT, room_label TEXT, turns_taken INTEGER,
    visited INTEGER, current INTEGER,
    children TEXT, parents TEXT, is_start INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, node_id)
);

-- 每个玩家在每一层的数据（多人对局每层 N 行，这是区分玩家的关键）
CREATE TABLE IF NOT EXISTS player_stats (
    run_id TEXT, node_id TEXT, player_id TEXT,
    ord INTEGER, act INTEGER, name TEXT, character TEXT,
    hp INTEGER, max_hp INTEGER, hp_before INTEGER, gold INTEGER,
    damage_taken INTEGER, hp_healed INTEGER,
    gold_gained INTEGER, gold_spent INTEGER,
    max_hp_gained INTEGER, max_hp_lost INTEGER,
    cards_gained TEXT, cards_removed TEXT, card_choices TEXT,
    relics_gained TEXT, potions TEXT,
    PRIMARY KEY (run_id, node_id, player_id)
);

CREATE TABLE IF NOT EXISTS combats (
    run_id TEXT, node_id TEXT, encounter_id TEXT, name TEXT,
    player_id TEXT, character TEXT,
    act INTEGER, floor INTEGER, turns INTEGER,
    hp_start INTEGER, hp_end INTEGER,
    dmg_dealt INTEGER, dmg_taken INTEGER, block INTEGER, blocked INTEGER,
    energy_spent INTEGER, energy_wasted INTEGER,
    cards_played INTEGER, cards_drawn INTEGER, cards_discarded INTEGER, cards_exhausted INTEGER,
    overkill INTEGER, max_hit INTEGER, max_hit_card TEXT,
    enemy_dmg_dealt INTEGER, enemy_hp_start INTEGER, enemy_hp_end INTEGER,
    outcome TEXT, source TEXT,
    PRIMARY KEY (run_id, node_id, encounter_id, player_id)
);

CREATE TABLE IF NOT EXISTS turn_stats (
    run_id TEXT, node_id TEXT, encounter_id TEXT, player_id TEXT,
    turn INTEGER, side TEXT, key TEXT, name TEXT,
    hp INTEGER, max_hp INTEGER, block INTEGER,
    dmg_dealt INTEGER, dmg_taken INTEGER, block_gained INTEGER,
    energy_spent INTEGER, cards_played INTEGER, note TEXT
);

CREATE TABLE IF NOT EXISTS card_stats (
    run_id TEXT, node_id TEXT, encounter_id TEXT, player_id TEXT,
    card_id TEXT, name TEXT, plays INTEGER, damage INTEGER, block INTEGER,
    energy INTEGER, drawn INTEGER, discarded INTEGER, exhausted INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, node_id TEXT, encounter_id TEXT, player_id TEXT,
    turn INTEGER, kind TEXT, label TEXT, value INTEGER, side TEXT,
    detail TEXT, ts REAL
);

CREATE INDEX IF NOT EXISTS idx_nodes_run ON nodes(run_id, ord);
CREATE INDEX IF NOT EXISTS idx_pstats_run ON player_stats(run_id, player_id, ord);
CREATE INDEX IF NOT EXISTS idx_combats_run ON combats(run_id, act, floor);
CREATE INDEX IF NOT EXISTS idx_turn_run ON turn_stats(run_id, node_id, turn);
CREATE INDEX IF NOT EXISTS idx_card_run ON card_stats(run_id, card_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
"""


class Store:
    """SQLite 持久化。

    线程安全策略
    ------------
    * 每个线程持有自己的连接（sqlite3 连接不可跨线程使用），用 threading.local 缓存；
    * 写操作统一加锁，避免 "database is locked"；
    * 因此 Web 服务的多线程 handler 与后台监控线程可以共用同一个 Store。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._init_schema()

    # ---------- 连接管理 ----------
    def _init_schema(self) -> None:
        """建表 + 结构自愈。

        每次打开都做一次「DDL 列清单 vs 实际列」比对（很便宜），
        这样即使某次迁移中断、或版本号被提前写脏，也能自动修回来。
        """
        existing = self._existing_tables()
        old_version = self._read_schema_version() if existing else SCHEMA_VERSION
        if existing:
            self._migrate(old_version, existing)
        self.execute_script(DDL)
        self.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                     (str(SCHEMA_VERSION),))

    def _existing_tables(self) -> set[str]:
        try:
            rows = self.query("SELECT name FROM sqlite_master WHERE type='table'")
            return {r["name"] for r in rows}
        except sqlite3.Error:
            return set()

    def _read_schema_version(self) -> int:
        try:
            rows = self.query("SELECT value FROM meta WHERE key='schema_version'")
            return int(json.loads(rows[0]["value"])) if rows else 0
        except (sqlite3.Error, ValueError, TypeError, IndexError):
            return 0

    def _migrate(self, old_version: int, tables: set[str]) -> None:
        """结构性变更处理。

        做法：把 DDL 里每张表的列清单与库里实际的列对比，
        * 只缺列（且能安全补）→ ALTER TABLE 补上；
        * 缺表 / 列差异较大 → 直接重建该表（数据由 ingest 幂等重建）。
        """
        wanted = self._ddl_columns()
        rebuilt: list[str] = []
        added: list[str] = []
        for table, cols in wanted.items():
            if table == "meta":
                continue
            if table not in tables:
                continue                       # 交给 DDL 新建
            have = {r["name"] for r in self.query(f"PRAGMA table_info({table})")}
            missing = [c for c in cols if c not in have]
            if not missing:
                continue
            # 只缺列 → 尝试 ALTER TABLE 补齐；需要主键/索引的列则整表重建
            pk_like = {"run_id", "node_id", "player_id", "card_id", "encounter_id",
                       "key", "turn", "side"}
            if not (pk_like & set(missing)):
                try:
                    for c in missing:
                        self.execute(f"ALTER TABLE {table} ADD COLUMN {c} TEXT")
                        added.append(f"{table}.{c}")
                    continue
                except sqlite3.Error:
                    pass
            self.execute(f"DROP TABLE IF EXISTS {table}")
            rebuilt.append(table)
        if added or rebuilt:
            note = []
            if added:
                note.append(f"补列 {len(added)} 个")
            if rebuilt:
                note.append(f"重建表 {', '.join(rebuilt)}")
            print(f"[store] schema {old_version} -> {SCHEMA_VERSION}：{'；'.join(note)}"
                  f"（重新 ingest 即可恢复数据）")

    def _ddl_columns(self) -> dict[str, list[str]]:
        """从 DDL 文本解析每张表的列名（用于迁移比对）。

        注意：DDL 里一行可能写多列，所以必须按「顶层逗号」切分，再取每段的第一个词。
        """
        out: dict[str, list[str]] = {}
        marker = "CREATE TABLE IF NOT EXISTS "
        pos = 0
        while True:
            start = DDL.find(marker, pos)
            if start < 0:
                break
            name_start = start + len(marker)
            paren = DDL.find("(", name_start)
            if paren < 0:
                break
            table = DDL[name_start:paren].strip()
            depth, i = 0, paren
            while i < len(DDL):
                ch = DDL[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            body = DDL[paren + 1:i]
            pos = i + 1

            cols: list[str] = []
            depth, current = 0, ""
            parts: list[str] = []
            for ch in body:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                if ch == "," and depth == 0:
                    parts.append(current)
                    current = ""
                else:
                    current += ch
            parts.append(current)
            for seg in parts:
                seg = seg.strip()
                if not seg:
                    continue
                head = seg.split()[0]
                if head.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                    continue
                cols.append(head)
            out[table] = cols
        return out

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=20, check_same_thread=False)
            c.row_factory = sqlite3.Row
            try:
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA busy_timeout=10000")
                c.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            self._local.conn = c
            with self._conns_lock:
                self._conns.append(c)
        return c

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._write_lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def execute_script(self, script: str) -> None:
        with self._write_lock:
            self.conn.executescript(script)
            self.conn.commit()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        with self._write_lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def close(self) -> None:
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass

    # ---------- 写 ----------
    def save_run(self, run: RunState, *, replace_children: bool = True) -> None:
        s = run.summary()
        self.execute(
            """INSERT OR REPLACE INTO runs (
                run_id, seed, character, character_name, ascension, game_mode, build_id,
                modifiers,
                start_time, end_time, run_time, win, abandoned, killed_by,
                source, live, current_floor, current_act,
                floors, combats, turns, damage_dealt, damage_taken,
                deck_size, relic_count, deck, relics, badges, notes,
                player_count, multiplayer, local_player, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run.run_id, run.seed, run.character, run.character_name, run.ascension,
             run.game_mode, run.build_id, _json(run.modifiers or []),
             run.start_time, run.end_time, run.run_time,
             None if run.win is None else int(run.win), int(run.abandoned), run.killed_by,
             run.source, int(run.live), run.current_floor, run.current_act,
             s["floors"], s["combats"], s["turns"], s["damage_dealt"], s["damage_taken"],
             len(run.deck), len(run.relics), _json(run.deck), _json(run.relics),
             _json(run.badges), _json(run.notes),
             max(len(run.players), 1), int(run.multiplayer), run.local_player,
             run.updated_at))

        with self._write_lock:
            if replace_children:
                for table in ("nodes", "player_stats", "players", "combats",
                              "turn_stats", "card_stats"):
                    self.conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run.run_id,))
            for i, rp in enumerate(run.players):
                self._save_player(run.run_id, i, rp)
            for node in run.floors:
                self._save_node(run.run_id, node)
                for ps in node.player_stats:
                    self._save_player_stat(run.run_id, node.node_id, ps)
                if node.combats:
                    for c in node.combats:
                        self._save_combat(run.run_id, node, c)
                elif node.turn_stats:
                    # 实时监控里战斗可能还没收尾（无 Combat 记录），
                    # 逐回合数据仍然要入库，否则仪表盘点开没有曲线。
                    self._save_turn_card_stats(run.run_id, node.node_id,
                                               node.room_id or "LIVE", "", "",
                                               node.turn_stats, [])
            self.conn.commit()

    def _save_player(self, run_id: str, slot: int, p: RunPlayer) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO players (
                run_id, player_id, slot, name, character, character_name,
                deck, relics, potions, badges, is_local
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, p.player_id or f"p{slot}", slot, p.name, p.character,
             p.character_name, _json(p.deck), _json(p.relics), _json(p.potions),
             _json(p.badges), int(p.is_local)))

    def _save_node(self, run_id: str, n: MapNode) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO nodes (
                run_id, node_id, ord, act, point_type, type_label, col, row,
                room_id, room_label, turns_taken, visited, current,
                children, parents, is_start
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, n.node_id, n.order, n.act, n.point_type, n.type_label, n.col, n.row,
             n.room_id, n.room_label, n.turns_taken, int(n.visited), int(n.current),
             _json(n.children), _json(n.parents), int(n.is_start)))

    def _save_player_stat(self, run_id: str, node_id: str, s: PlayerStat) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO player_stats (
                run_id, node_id, player_id, ord, act, name, character,
                hp, max_hp, hp_before, gold,
                damage_taken, hp_healed, gold_gained, gold_spent,
                max_hp_gained, max_hp_lost,
                cards_gained, cards_removed, card_choices, relics_gained, potions
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, node_id, s.player_id, s.order, s.act, s.name, s.character,
             s.hp, s.max_hp, s.hp_before, s.gold,
             s.damage_taken, s.hp_healed, s.gold_gained, s.gold_spent,
             s.max_hp_gained, s.max_hp_lost,
             _json(s.cards_gained), _json(s.cards_removed), _json(s.card_choices),
             _json(s.relics_gained), _json(s.potions)))

    def _save_combat(self, run_id: str, node: MapNode, c: Combat) -> None:
        nid = c.node_id or node.node_id
        self.conn.execute(
            """INSERT OR REPLACE INTO combats (
                run_id, node_id, encounter_id, name, player_id, character,
                act, floor, turns,
                hp_start, hp_end, dmg_dealt, dmg_taken, block, blocked,
                energy_spent, energy_wasted, cards_played, cards_drawn, cards_discarded,
                cards_exhausted, overkill, max_hit, max_hit_card,
                enemy_dmg_dealt, enemy_hp_start, enemy_hp_end, outcome, source
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, nid, c.encounter_id, c.name, c.player_id, c.character,
             c.act, c.floor, c.turns, c.player_hp_start, c.player_hp_end,
             c.player_damage_dealt, c.player_damage_taken, c.player_block, c.player_blocked,
             c.player_energy_spent, c.player_energy_wasted, c.cards_played, c.cards_drawn,
             c.cards_discarded, c.cards_exhausted, c.overkill, c.max_hit, c.max_hit_card,
             c.enemy_damage_dealt, c.enemy_hp_start, c.enemy_hp_end, c.outcome, c.source))
        self._save_turn_card_stats(run_id, nid, c.encounter_id, c.player_id,
                                   c.character, c.turn_stats, c.card_stats)

    def _save_turn_card_stats(self, run_id: str, nid: str, encounter_id: str,
                              player_id: str, character: str,
                              turn_stats: list[TurnStat], card_stats: list[CardStat]) -> None:
        self.conn.execute(
            """DELETE FROM turn_stats
               WHERE run_id=? AND node_id=? AND encounter_id=? AND COALESCE(player_id,'')=?""",
            (run_id, nid, encounter_id, player_id or ""))
        self.conn.execute(
            """DELETE FROM card_stats
               WHERE run_id=? AND node_id=? AND encounter_id=? AND COALESCE(player_id,'')=?""",
            (run_id, nid, encounter_id, player_id or ""))
        for t in turn_stats:
            self.conn.execute(
                """INSERT INTO turn_stats (
                    run_id, node_id, encounter_id, player_id, turn, side, key, name,
                    hp, max_hp, block, dmg_dealt, dmg_taken, block_gained,
                    energy_spent, cards_played, note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, nid, encounter_id, t.player_id or player_id, t.turn, t.side,
                 t.key, t.name, t.hp, t.max_hp, t.block, t.damage_dealt, t.damage_taken,
                 t.block_gained, t.energy_spent, t.cards_played, t.note))
        for cs in card_stats:
            self.conn.execute(
                """INSERT INTO card_stats (
                    run_id, node_id, encounter_id, player_id, card_id, name, plays,
                    damage, block, energy, drawn, discarded, exhausted
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, nid, encounter_id, player_id, cs.card_id, cs.name, cs.plays,
                 cs.damage, cs.block, cs.energy, cs.drawn, cs.discarded, cs.exhausted))

    def add_events(self, run_id: str, events: Iterable[EventRecord],
                   node_id: str = "", encounter_id: str = "") -> None:
        """写入事件流。每条事件自带 node_id / encounter_id 时优先用它，
        这样前端才能按「节点」过滤出当前战斗，不会把上一场的混进来。"""
        rows = [(run_id, e.node_id or node_id, e.encounter_id or encounter_id,
                 e.turn, e.kind, e.label, e.value, e.side,
                 _json(e.detail), e.ts) for e in events]
        if rows:
            self.executemany(
                """INSERT INTO events (run_id,node_id,encounter_id,turn,kind,label,value,side,detail,ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
            self.conn.commit()

    def set_meta(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, _json(value)))

    def get_meta(self, key: str, default: Any = None) -> Any:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except Exception:
            return rows[0]["value"]

    # ---------- 读 ----------
    def run_ids(self) -> list[str]:
        rows = self.query("SELECT run_id FROM runs ORDER BY COALESCE(start_time,0) DESC")
        return [r["run_id"] for r in rows]

    def run_row(self, run_id: str) -> dict[str, Any] | None:
        rows = self.query("SELECT * FROM runs WHERE run_id=?", (run_id,))
        return self._expand(rows[0]) if rows else None

    def players(self, run_id: str) -> list[dict[str, Any]]:
        """对局中的玩家列表（单人局只有一条）。"""
        rows = self.query(
            "SELECT * FROM players WHERE run_id=? ORDER BY slot", (run_id,))
        return [self._expand(r) for r in rows]

    def nodes(self, run_id: str, player_id: str | None = None) -> list[dict[str, Any]]:
        """地图节点。

        player_id=None 时，每层附上「队长（第一顺位玩家）」的数值，
        保持单人局的调用方（前端旧字段）不用改；
        指定 player_id 时附该玩家的数值。
        """
        rows = self.query(
            """SELECT * FROM nodes WHERE run_id=?
               ORDER BY act, visited DESC, ord, row, col""", (run_id,))
        out = [self._expand(r) for r in rows]

        stats = self.player_stats(run_id, player_id)
        first_pid = (self.players(run_id) or [{}])[0].get("player_id")
        for n in out:
            mine = [s for s in stats if s["node_id"] == n["node_id"]]
            n["player_stats"] = mine
            # 兼容字段：默认展示第一顺位玩家的数据（单人局即唯一玩家）
            pick = next((s for s in mine if s["player_id"] == (player_id or first_pid)),
                        mine[0] if mine else None)
            if pick:
                n.update({
                    "hp_after": pick["hp"], "max_hp": pick["max_hp"],
                    "hp_before": pick["hp_before"], "gold": pick["gold"],
                    "damage_taken": pick["damage_taken"] or 0,
                    "hp_healed": pick["hp_healed"] or 0,
                    "gold_gained": pick["gold_gained"] or 0,
                    "gold_spent": pick["gold_spent"] or 0,
                    "cards_gained": pick["cards_gained"] or [],
                    "cards_removed": pick["cards_removed"] or [],
                    "card_choices": pick["card_choices"] or [],
                    "relics_gained": pick["relics_gained"] or [],
                    "potions": pick["potions"] or [],
                })
            else:
                n.update({"hp_after": None, "max_hp": None, "hp_before": None,
                          "gold": None, "damage_taken": 0, "hp_healed": 0,
                          "gold_gained": 0, "gold_spent": 0, "cards_gained": [],
                          "cards_removed": [], "card_choices": [], "relics_gained": [],
                          "potions": []})
        return out

    def player_stats(self, run_id: str,
                     player_id: str | None = None) -> list[dict[str, Any]]:
        if player_id:
            rows = self.query(
                "SELECT * FROM player_stats WHERE run_id=? AND player_id=? ORDER BY ord",
                (run_id, player_id))
        else:
            rows = self.query(
                "SELECT * FROM player_stats WHERE run_id=? ORDER BY ord, player_id",
                (run_id,))
        return [self._expand(r) for r in rows]

    def combats(self, run_id: str, player_id: str | None = None) -> list[dict[str, Any]]:
        if player_id:
            rows = self.query(
                """SELECT * FROM combats WHERE run_id=? AND player_id=?
                   ORDER BY act, floor""", (run_id, player_id))
        else:
            rows = self.query(
                "SELECT * FROM combats WHERE run_id=? ORDER BY act, floor, player_id",
                (run_id,))
        return [dict(r) for r in rows]

    def turn_stats(self, run_id: str, node_id: str | None = None,
                   player_id: str | None = None) -> list[dict[str, Any]]:
        cond, params = "", [run_id]
        if node_id:
            cond += " AND t.node_id=?"
            params.append(node_id)
        if player_id:
            cond += " AND COALESCE(t.player_id,'')=?"
            params.append(player_id)
        rows = self.query(
            f"""SELECT t.* FROM turn_stats t
                LEFT JOIN nodes n ON n.run_id = t.run_id AND n.node_id = t.node_id
                WHERE t.run_id=?{cond}
                ORDER BY COALESCE(n.ord, 0), t.turn, t.side""", tuple(params))
        return [dict(r) for r in rows]

    def card_stats(self, run_id: str, node_id: str | None = None,
                   player_id: str | None = None) -> list[dict[str, Any]]:
        cond, params = "", [run_id]
        if node_id:
            cond += " AND node_id=?"
            params.append(node_id)
        if player_id:
            cond += " AND COALESCE(player_id,'')=?"
            params.append(player_id)
        if node_id:
            rows = self.query(
                f"""SELECT * FROM card_stats WHERE run_id=?{cond}
                    ORDER BY damage DESC""", tuple(params))
        else:
            rows = self.query(
                f"""SELECT card_id, MAX(name) name, SUM(plays) plays, SUM(damage) damage,
                           SUM(block) block, SUM(energy) energy, SUM(drawn) drawn,
                           SUM(discarded) discarded, SUM(exhausted) exhausted
                    FROM card_stats WHERE run_id=?{cond}
                    GROUP BY card_id ORDER BY damage DESC""", tuple(params))
        return [dict(r) for r in rows]

    def events(self, run_id: str, limit: int = 500, after_id: int = 0,
               node_id: str | None = None,
               encounter_id: str | None = None) -> list[dict[str, Any]]:
        """读事件流（倒序）。

        node_id / encounter_id 用于过滤出「当前这场战斗」，
        否则上一场的回合事件会混进实时面板。
        """
        cond, params = "", [run_id, after_id]
        if node_id:
            cond += " AND node_id=?"
            params.append(node_id)
        if encounter_id:
            cond += " AND encounter_id=?"
            params.append(encounter_id)
        params.append(limit)
        rows = self.query(
            f"""SELECT * FROM events WHERE run_id=? AND id>?{cond}
                ORDER BY id DESC LIMIT ?""", tuple(params))
        return [self._expand(r) for r in rows]

    def prune_events(self, run_id: str, keep: int = 4000) -> int:
        """只保留最近 keep 条事件，返回删除条数。

        实时监控会持续写入新事件，长对局下不裁剪会一直膨胀。
        """
        rows = self.query(
            "SELECT id FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1 OFFSET ?",
            (run_id, keep))
        if not rows:
            return 0
        cutoff = int(rows[0]["id"])
        with self._write_lock:
            cur = self.conn.execute("DELETE FROM events WHERE run_id=? AND id<=?",
                                    (run_id, cutoff))
            self.conn.commit()
            return cur.rowcount or 0

    def migrate_run(self, src_id: str, dst_id: str) -> bool:
        """把一条对局的记录并到另一个 run_id 下，并删掉源记录。

        用途：实时对局只能用 `start_time` 当 id（进行中存档里没有 seed），
        打完之后 `.run` 才给出真正的 seed。不迁移的话同一局会留下两条
        （实时那条还停在 1 层，看起来像两局）。
        返回值：目标不存在或无源记录时返回 False。
        """
        if not src_id or not dst_id or src_id == dst_id:
            return False
        row = self.run_row(src_id)
        if not row:
            return False
        if self.run_row(dst_id):
            # 目标已存在：只删源，保留权威记录
            for table in ("runs", "nodes", "player_stats", "players", "combats",
                          "turn_stats", "card_stats", "events"):
                self.execute(f"DELETE FROM {table} WHERE run_id=?", (src_id,))
            return True
        # 目标不存在：整条改键
        for table in ("runs", "nodes", "player_stats", "players", "combats",
                      "turn_stats", "card_stats", "events"):
            self.execute(f"UPDATE {table} SET run_id=? WHERE run_id=?",
                         (dst_id, src_id))
        return True

    def collapse_duplicate_runs(self, prefer=None) -> list[tuple[str, str]]:
        """把「同一局」的多条记录合并掉，返回 [(被删, 保留)]。

        成因：同一局在不同阶段有**三套 run_id 约定** ——
          * 实时存档：`run-<start_time>`（current_run）
          * 战斗数据：`live-<start_time>`（DamageMeter）
          * 历史存档：`<seed>`（裸种子，.run 文件名）

        **join key 的优先级：run_id 后缀 > start_time**。
        实时行的 id 是 `run-<start_time>` / `live-<start_time>`，那个数字
        正好等于**该局历史记录的 `start_time`**；而实时行自己存的 start_time
        是「采集时刻」、seed 又是拿 start_time 顶替的，两者都不可靠。
        实测对照：
            run-1791118165  后缀 1791118165  ==  THPEWW4R4Q.start_time  ✔同一局
            run-1791094423  后缀 1791094423  ==  JV54MV4G75.start_time   ✔同一局

        保留优先级：`prefer` 指定的 > run_history > 有角色名 > 数据多 > 已结束。
        """
        def key_of(r: dict[str, Any]) -> str:
            rid = str(r.get("run_id") or "")
            # `run-1791118165` / `live-1791118165` → t1791118165
            tail = rid.rsplit("-", 1)[-1] if "-" in rid else ""
            if tail.isdigit() and len(tail) >= 6:
                return f"t{tail}"
            st = r.get("start_time")
            if st:
                return f"t{int(round(float(st)))}"
            return f"s{str(r.get('seed') or '').strip()}"

        groups: dict[str, list[dict[str, Any]]] = {}
        for r in self.all_run_rows():
            k = key_of(r)
            if k in ("t0", "s"):
                continue
            groups.setdefault(k, []).append(r)

        def rank(r: dict[str, Any]) -> tuple:
            hist = 1 if str(r.get("source") or "") == "run_history" else 0
            named = 1 if (r.get("character") or "") else 0
            counts = [
                int(self.query(f"SELECT COUNT(*) c FROM {t} WHERE run_id=?",
                               (r["run_id"],))[0]["c"])
                for t in ("nodes", "player_stats", "events", "combats")]
            ended = 1 if not r.get("live") else 0
            return (hist, named, sum(counts), ended)

        moved: list[tuple[str, str]] = []
        for _k, rows in groups.items():
            if len(rows) < 2:
                continue
            want = str(prefer(rows) or "") if prefer else ""
            keep = next((r for r in rows if r["run_id"] == want), None)
            if keep is None:
                keep = max(rows, key=rank)
            for r in rows:
                if r["run_id"] != keep["run_id"]:
                    if self.migrate_run(r["run_id"], keep["run_id"]):
                        moved.append((r["run_id"], keep["run_id"]))
        return moved

    def backfill_character_names(self) -> int:
        """补齐缺失的角色名，并把占位符 `?` 清掉。

        成因：实时采集初期还拿不到角色 ID，而词典对空值的兜底是 `'?'`，
        于是 `character_name` 被写成了问号。界面上就显示成 `?`。
        这里把 `?`（以及空值）当作「未知」清空，让界面回退到别的字段；
        能从同类记录或历史存档补到真名的就补上。
        """
        fixed = 0
        # ① `?` 一律清空 —— 它是占位符，不是真名字
        for table in ("runs", "players"):
            cur = self.execute(
                f"UPDATE {table} SET character_name='' "
                f"WHERE character_name IS NOT NULL AND character_name NOT IN ('') "
                f"AND (character_name='?' OR character_name='？' OR character_name='未知')")
            fixed += cur.rowcount if cur else 0
        # ② 同一 seed 的其它记录里有真名/真角色，就借用过来
        rows = [r for r in self.all_run_rows() if not (r.get("character") or "")]
        for r in rows:
            seed = str(r.get("seed") or "").strip()
            donor = next((x for x in self.all_run_rows()
                          if str(x.get("seed") or "") == seed
                          and (x.get("character") or "")
                          and x["run_id"] != r["run_id"]), None)
            if not donor:
                continue
            self.execute(
                "UPDATE runs SET character=?, character_name=? WHERE run_id=?",
                (donor.get("character") or "", donor.get("character_name") or "",
                 r["run_id"]))
            self.execute(
                "UPDATE players SET character=?, character_name=? WHERE run_id=?",
                (donor.get("character") or "", donor.get("character_name") or "",
                 r["run_id"]))
            fixed += 1
        return fixed

    def prune_orphan_live_runs(self, known_seeds: set[str] | None = None) -> list[str]:
        """删掉「采集开始的残片」——没有权威数据的实时空壳。

        特征：source='live'、没有角色、没有历史存档对应、而且
        `player_stats` 为空（**一点 HP 都没采到**）。这类行只有几条战斗/出牌
        碎片，界面上就是「? · 1 层」，纯噪声。实测残留：
            live              （连 seed 都没有）
            live-310330818    （1 层、无 HP、无对应历史存档）

        有 HP 或无历史的实时行一律保留 —— 那可能是真在进行的对局。
        `known_seeds` 是历史存档的 seed 集合；命中说明这局有权威记录，
        交给 collapse_duplicate_runs 合并，别在这里删。

        判据（全部满足才删）：
          * source='live'（实时采集留下的）
          * 没有角色
          * seed 不在历史存档里（说明这局没有权威记录可合并）
          * **一条带 HP/金币的 player_stats 都没有**（纯碎片）
        `run_id == 'live'` 这种占位 id 即使有一条空 player_stats 也照删 ——
        它连「哪一局」都无法标识。
        """
        known = {str(x) for x in (known_seeds or set())}
        gone: list[str] = []
        for r in self.all_run_rows():
            rid = str(r["run_id"])
            if str(r.get("source") or "") != "live":
                continue
            if (r.get("character") or "").strip():
                continue
            seed = str(r.get("seed") or "").strip()
            if seed and seed in known:
                continue
            # 占位 id（`live`）：没有 start_time、没有 seed，无法归属到任何一局。
            # 它是采集器早期版本的遗留，即使侥幸存到一条 HP 也没有意义
            # （界面上就是孤零零一行「? · 1 层」）。
            if not seed and not r.get("start_time"):
                for table in ("runs", "nodes", "player_stats", "players", "combats",
                              "turn_stats", "card_stats", "events"):
                    self.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))
                gone.append(rid)
                continue
            n_ps_nonempty = 0
            for row in self.query("SELECT hp, max_hp, gold FROM player_stats WHERE run_id=?",
                                  (rid,)):
                if row["hp"] is not None or row["max_hp"] is not None or row["gold"]:
                    n_ps_nonempty += 1
            # 有 HP 说明确实在采这一局，留着
            if n_ps_nonempty:
                continue
            # 无 HP 且无历史归属 → 纯碎片，删
            for table in ("runs", "nodes", "player_stats", "players", "combats",
                          "turn_stats", "card_stats", "events"):
                self.execute(f"DELETE FROM {table} WHERE run_id=?", (rid,))
            gone.append(rid)
        return gone

    def mark_replay_artifacts(self) -> list[str]:
        """把「回放残留」标成 `source='replay'`，返回被标的 run_id。

        **为什么不是删掉**：这些行里有完整的战斗/事件数据（只是归属错了），
        删了不可逆；标上来源既可隐藏、也保留了「它从哪来」的事实。

        判据（在 113 场真实存档上验证过，0 误伤）：
        `start_time` 与 `end_time` 的差**不等于** `run_time`。
        真实对局这三者精确相等（`sts2mon demo` 压缩了时间轴，所以不等）。
        另外 `run_id` 形如 `live-<seed>` 也是佐证 —— 回放器会新建一个 tracker。
        """
        fixed: list[str] = []
        for r in self.all_run_rows():
            rid = str(r["run_id"])
            if str(r.get("source") or "") in ("replay", "live-aborted"):
                continue
            st_t = float(r.get("start_time") or 0)
            en_t = float(r.get("end_time") or 0)
            rt = int(r.get("run_time") or 0)
            # 截断的实时采集：没有时长、没有结局、也不是进行中
            if rt == 0 and r.get("win") is None and not r.get("live"):
                self.execute("UPDATE runs SET source='live-aborted' WHERE run_id=?",
                             (rid,))
                fixed.append(rid)
                continue
            if not (st_t and en_t):
                continue
            if abs((en_t - st_t) - rt) <= 5:
                continue
            if not rid.startswith(("live-", "run-")):
                # 真实存档的 run_id 是裸种子，不会是 live-/run- 前缀；
                # 前缀 + 时长不符同时成立才判定，避免误伤。
                continue
            self.execute("UPDATE runs SET source='replay' WHERE run_id=?", (rid,))
            fixed.append(rid)
        return fixed

    def all_run_rows(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM runs ORDER BY COALESCE(start_time,0) DESC")
        return [self._expand(r) for r in rows]

    def close_stale_live_runs(self, keep: str = "", max_idle_seconds: float = 900.0,
                             now: float | None = None) -> list[str]:
        """把「卡在进行中」的实时对局收尾，返回被收尾的 run_id 列表。

        实时监控是靠进程内存里的状态标记 live 的；进程被杀、游戏被关、
        或换了存档之后，旧对局会永远停在 live=1。
        这里按「最后更新时间」判定：太久没更新的实时对局一律标记为已结束。
        """
        import time as _t
        now = now if now is not None else _t.time()
        closed: list[str] = []
        for r in self.all_run_rows():
            rid = r["run_id"]
            if not r.get("live") or rid == keep:
                continue
            last = float(r.get("updated_at") or 0)
            if last and (now - last) < max_idle_seconds:
                continue
            notes = r.get("notes") or {}
            if not isinstance(notes, dict):
                notes = {}
            notes["ended_reason"] = notes.get("ended_reason") or "监控中断（进程停止/换存档）"
            self.execute(
                """UPDATE runs SET live=0, notes=?, end_time=COALESCE(NULLIF(end_time,0), ?)
                   WHERE run_id=?""",
                (_json(notes), last or now, rid))
            closed.append(rid)
        return closed

    @staticmethod
    def _expand(row: sqlite3.Row) -> dict[str, Any]:
        """把一行记录里的 JSON 文本与「SQLite 存成文本的数字」还原成合适的类型。

        注意：SQLite 有时会把 0/1 当文本返回，而 bool("0") 是 True —— 必须显式转换，
        否则 multiplayer=0 会被误判成多人对局。
        """
        d = dict(row)
        for k, v in list(d.items()):
            if not isinstance(v, str):
                continue
            if v[:1] in "[{":
                try:
                    d[k] = json.loads(v)
                    continue
                except Exception:
                    pass
            if v in ("0", "1") and k in _BOOL_COLUMNS:
                d[k] = int(v)
        return d

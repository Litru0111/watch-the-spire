"""归一化事件流。

三种数据源（实时 mod 文件 / 历史存档 / rdps 明细）都翻译成同一套 Event，
再由 Tracker 增量重建 RunState。这样「实时」和「事后」共用一条管线。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any


class K:
    """事件类型常量。"""
    RUN_START = "run_start"
    RUN_END = "run_end"
    ACT_ENTER = "act_enter"
    MAP_NODE = "map_node"                  # 进入/记录一个地图点
    NODE_ENTER = "node_enter"
    NODE_LEAVE = "node_leave"
    COMBAT_START = "combat_start"
    COMBAT_END = "combat_end"
    TURN_START = "turn_start"
    PLAYER_STAT = "player_stat"            # 我方数值变化（HP/格挡/能量…）
    ENEMY_STAT = "enemy_stat"              # 敌方数值变化
    DAMAGE = "damage"                      # 伤害事件（含每卡归属）
    BLOCK = "block"
    CARD_PLAY = "card_play"
    CARD_STAT = "card_stat"                # 卡牌累计统计的「绝对值」快照
    CARD_GAIN = "card_gain"
    CARD_REMOVE = "card_remove"
    RELIC_GAIN = "relic_gain"
    POTION = "potion"
    ROOM = "room"                          # 房间/遭遇标识
    LOG = "log"
    RAW = "raw"


@dataclass
class Event:
    """一条归一化事件。"""
    kind: str
    seq: int = 0
    ts: float = field(default_factory=time.time)
    run_id: str = ""
    seed: str = ""
    character: str = ""
    ascension: int = 0
    act: int = 0
    floor: int = 0
    node_id: str = ""
    encounter_id: str = ""
    turn: int = 0
    side: str = ""                # player / enemy
    key: str = ""                 # 玩家 id 或怪物标识
    name: str = ""
    value: int = 0
    label: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "Event":
        obj = json.loads(line)
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in obj.items() if k in known})


class EventBus:
    """极简事件总线：内存订阅 + 可选 JSONL 落盘（既做日志也做回放源）。"""

    def __init__(self, jsonl_path: str | None = None) -> None:
        self._subs: list[Any] = []
        self._seq = 0
        self._fh = open(jsonl_path, "a", encoding="utf-8") if jsonl_path else None

    def subscribe(self, fn) -> None:
        self._subs.append(fn)

    def emit(self, ev: Event) -> Event:
        if not ev.seq:
            self._seq += 1
            ev.seq = self._seq
        else:
            self._seq = max(self._seq, ev.seq)
        for fn in self._subs:
            try:
                fn(ev)
            except Exception as exc:  # 单个订阅者出错不影响整体
                print(f"[bus] 订阅者异常: {exc!r}")
        if self._fh:
            self._fh.write(ev.to_json() + "\n")
            self._fh.flush()
        return ev

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

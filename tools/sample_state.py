"""测试用的样本状态生成器。

真机状态文件（DamageMeter_combat_state.json）只在游戏进行战斗时才存在，
所以测试不能依赖它。这里生成结构完全一致的样本状态，
字段名与语义对齐真机观测结果；若真机文件存在则优先用真的。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REAL = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "DamageMeter_combat_state.json"


def real_state() -> dict[str, Any] | None:
    """真机状态文件（存在且可解析时）。"""
    if not REAL.is_file():
        return None
    try:
        return json.loads(REAL.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def make_state(seed: int = 1234567, turn: int = 1, enc: str = "SEAPUNK_WEAK",
               dealt: int = 0, taken: int = 0, cards_played: int = 0,
               chars: list[str] | None = None, *, tracking: bool = True,
               events: list[dict[str, Any]] | None = None,
               cards: dict[str, int] | None = None,
               floor: int = 1, room: str = "测试房间") -> dict[str, Any]:
    """生成一份 DamageMeter 风格的状态（多人时 chars 给多个角色 id）。"""
    chars = chars or ["SILENT"]
    # 逐回合伤害：把累计值按回合摊开，并把余数补到最后一回合，
    # 保证「曲线之和 == 累计输出」（测试里有这条断言）
    turn = max(int(turn or 1), 1)
    per_turn = dealt // turn
    dpt_map = {str(t): per_turn for t in range(1, turn + 1)}
    dpt_map[str(turn)] = dealt - per_turn * (turn - 1)
    players: dict[str, Any] = {}
    for i, char in enumerate(chars, start=1):
        key = str(i)
        cpc = dict(cards or {})
        players[key] = {
            "name": f"P{i}", "key": key, "char_id": char, "color": [1, 1, 1, 1],
            "dmg_dealt": dealt, "dmg_taken": taken, "blocked": 0, "overkill": 0,
            "hits": cards_played, "cards_played": cards_played, "block_total": 0,
            "energy_spent": 0, "energy_wasted": 0, "potions": 0, "drawn": 0,
            "discarded": 0, "exhausted": 0, "orbs": 0, "stars": 0, "summons": 0,
            "max_hit": max(dpt_map.values()) if dpt_map else 0, "max_hit_card": "",
            "assist_dmg": 0, "assist_block": 0,
            "dmg_prevented": 0, "dmg_boosted": 0,
            "dpt": dict(dpt_map),
            "dbc": {}, "dbs": {}, "cpc": cpc, "ctc": {}, "bbc": {}, "esbc": {},
            "debuffs": {}, "dpp": {}, "draw_ct": {}, "disc_ct": {}, "exh_ct": {},
        }
    return {
        "schema": 1, "run_seed": seed, "turn": turn, "encounter": enc,
        "tracking": tracking, "players": players,
        "segments": [{"turn_count": turn, "encounter": enc,
                      "room_label": room, "point_type": "monster", "floor": floor,
                      "players": {k: {"dmg_dealt": dealt, "dmg_taken": taken,
                                      "blocked": 0, "dpt": {}} for k in players}}],
        "events": events if events is not None else [],
    }


def write_state(path: Path, **kw) -> dict[str, Any]:
    st = make_state(**kw)
    path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st

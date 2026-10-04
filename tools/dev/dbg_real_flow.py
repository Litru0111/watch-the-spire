"""复刻真实监控序列：存档源 + 战斗源交替，逐层推进到第 5 层。

手工只喂存档源时一切正常，本脚本加上战斗源，复现「选择数据丢失」。
"""
import io
import contextlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon import paths

G = default_glossary()
src = CurrentRunAdapter()
full = src.read()
total = src.snapshot(full)["floor"]
print(f"存档总层数 = {total}")

tmpdir = Path(tempfile.mkdtemp())
save_p = tmpdir / "cur.save"
dm_p = tmpdir / "dm.json"
save = CurrentRunAdapter(save_p, G)
dm = CombatStateAdapter(dm_p, G)
dm.set_run_id_hint(src.run_id(full))

live = LiveState(G)
bus = EventBus()
live.attach(bus)

pts_all = [pt for a in (full.get("map_point_history") or []) for pt in (a or [])]


def trim(upto: int) -> dict:
    out = json.loads(json.dumps(full))
    out["map_point_history"] = [pts_all[:upto]]
    out["visited_map_coords"] = [[i % 7, i // 7] for i in range(upto)]
    return out


def dm_state_for(floor: int, turn: int) -> dict:
    """给某一层编一个最小的战斗状态（只为触发战斗源的事件）。"""
    enc = f"ENCOUNTER_F{floor}"
    return {
        "schema": 1, "run_seed": src.seed(full), "turn": turn, "encounter": enc,
        "tracking": True,
        "players": {"1": {
            "name": "P", "key": "1", "char_id": "SILENT", "color": [1, 1, 1, 1],
            "dmg_dealt": turn * 6, "dmg_taken": 0, "blocked": 0, "overkill": 0,
            "hits": turn, "cards_played": turn, "block_total": 0, "energy_spent": 0,
            "energy_wasted": 0, "potions": 0, "drawn": 0, "discarded": 0,
            "exhausted": 0, "orbs": 0, "stars": 0, "summons": 0, "max_hit": 6,
            "max_hit_card": "", "assist_dmg": 0, "assist_block": 0,
            "dmg_prevented": 0, "dmg_boosted": 0,
            "dpt": {str(t): 6 for t in range(1, turn + 1)},
            "dbc": {}, "cpc": {}, "ctc": {}, "bbc": {}, "esbc": {}, "debuffs": {},
            "dpp": {}, "draw_ct": {}, "disc_ct": {}, "exh_ct": {},
        }},
        "segments": [{"turn_count": turn, "encounter": enc,
                      "room_label": "", "point_type": "monster"}],
        "events": [],
    }


prev_save = None
prev_dm = None
lines: list[str] = []
for f in range(1, total + 1):
    cur = trim(f)
    save_p.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    dm.set_floor_hint(f)
    dm.set_act_hint(1)
    sev = save.initial_events(cur) if prev_save is None else save.diff(prev_save, cur)
    with contextlib.redirect_stdout(io.StringIO()):
        for e in sev:
            bus.emit(e)
    prev_save = cur
    n_ch = sum(len(((e.data or {}).get("choices") or {}).get("card_choices") or [])
               for e in sev)

    # 战斗源：每层打 2 回合
    prev_dm = None
    for t in (1, 2):
        st = dm_state_for(f, t)
        dm_p.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        dev = dm.initial_events(st) if prev_dm is None else dm.diff(prev_dm, st)
        with contextlib.redirect_stdout(io.StringIO()):
            for e in dev:
                bus.emit(e)
        prev_dm = st

    node = live.tracker._cur
    s = node.stat(live.run_local_key()) if (node and node.player_stats) else None
    lines.append(f"第 {f} 层：存档事件={[e.kind for e in sev]} 带选择={n_ch} 条 "
                 f"| 当前节点={node.node_id if node else None} "
                 f"type={node.point_type if node else None!r} "
                 f"选择={len((s.card_choices if s else []) or [])}")

lines.append("")
lines.append("=== 各节点最终状态 ===")
for n in live.tracker.run.floors:
    s = n.stat(live.run_local_key()) if n.player_stats else None
    cc = (s.card_choices if s else []) or []
    lines.append(f"    {n.node_id} ord={n.order} type={n.point_type!r} 选择={len(cc)}")

out = paths.data_dir() / "dbg_real.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"写到 {out}")

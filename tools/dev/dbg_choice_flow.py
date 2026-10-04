"""逐事件追踪：存档源的选择数据被写到了哪个节点上。"""
import io
import contextlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.events import EventBus, K  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon import paths

G = default_glossary()
ad = CurrentRunAdapter()
st = ad.read()

# 用一份「逐层推进」的存档副本，模拟监控循环（每层都重新读一次存档）
live = LiveState(G)
bus = EventBus()
live.attach(bus)

tmp = Path(tempfile.mkdtemp()) / "cur.save"
snap = ad.snapshot(st)
n_floors = snap["floor"]
lines = [f"存档：{n_floors} 层 · HP {snap['hp']}/{snap['max_hp']}", ""]

prev = None
for floor in range(1, n_floors + 1):
    # 构造「只走到第 floor 层」的存档
    trimmed = json.loads(json.dumps(st))
    hist = trimmed.get("map_point_history") or []
    kept = []
    count = 0
    for act in hist:
        new_act = []
        for pt in act:
            count += 1
            if count <= floor:
                new_act.append(pt)
        if new_act:
            kept.append(new_act)
    trimmed["map_point_history"] = kept
    trimmed["visited_map_coords"] = [[i % 7, i // 7] for i in range(floor)]
    tmp.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
    ad.path = tmp

    evs = ad.diff(prev, trimmed)
    with contextlib.redirect_stdout(io.StringIO()):
        for e in evs:
            bus.emit(e)
    prev = trimmed

    kinds = [e.kind for e in evs]
    ch = ad.choices(trimmed, floor)
    lines.append(f"第 {floor} 层：事件={kinds}")
    lines.append(f"    解析出的选择：cards={len(ch.get('card_choices') or [])} "
                 f"relics={ch.get('relics_gained')} "
                 f"ancient={bool(ch.get('ancient'))}")
    for n in live.tracker.run.floors:
        s = n.stat(live.run_local_key())
        if s:
            cc = s.card_choices or []
            names = [c.get("name") for c in cc][:5]
            lines.append(f"    节点 {n.node_id}(ord={n.order}) hp={s.hp} "
                         f"选择数={len(cc)} {names} relics={s.relics_gained}")

out = paths.data_dir() / "dbg_choice_flow.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"写到 {out}")

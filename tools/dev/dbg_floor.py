"""调试：MAP_NODE 事件的楼层比较为何不生效。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.events import K  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.tracker import Tracker  # noqa: E402

G = default_glossary()
ad = CurrentRunAdapter()
st = ad.read()
tracker = Tracker("t", G)
lines: list[str] = []
for e in ad.initial_events(st):
    tracker.apply(e)
    if e.kind != K.MAP_NODE:
        continue
    node = next((x for x in tracker.run.floors if x.node_id == e.node_id), None)
    ev_floor = int(e.floor or (e.data.get("floor") or 0) or 0)
    node_ord = int(node.order or 0) if node else 0
    lines.append(
        f"[纯Tracker] node={e.node_id} ev_floor={ev_floor} 节点ord={node_ord} "
        f"room={node.room_label if node else None!r}")

# ---- 完整 LiveState 路径（含 DamageMeter 源与 CLI 的过时过滤）----
from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon import paths

dm = CombatStateAdapter(glossary=G)
snap = ad.snapshot(st)
dm.set_floor_hint(snap.get("floor"))
dm.set_act_hint(snap.get("act"))
dm.set_run_id_hint(ad.run_id(st))
dm_st = dm.read()
lines.append("")
lines.append(f"DamageMeter 自己的楼层 = "
             f"{dm._floor_of(dm._segment(dm_st)) if dm_st else None}"
             f" / 存档楼层 = {snap.get('floor')}")

live = LiveState(G)
bus = EventBus()
live.attach(bus)
for e in ad.initial_events(st):
    bus.emit(e)
if dm_st is not None:
    dm_floor = dm._floor_of(dm._segment(dm_st))
    if dm_floor and dm_floor < int(snap.get("floor") or 0):
        lines.append(f"[CLI 守卫] 战斗源楼层 {dm_floor} < 存档 {snap.get('floor')} → 丢弃")
    else:
        for e in dm.initial_events(dm_st):
            bus.emit(e)
lines.append("")
for n in live.tracker.run.floors:
    lines.append(f"[LiveState] {n.node_id} ord={n.order} "
                 f"type={n.point_type!r} room={n.room_label!r}")

out = paths.data_dir() / "dbg_floor.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"写到 {out}")

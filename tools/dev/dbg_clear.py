"""逐步追踪：房间名在第几个事件之后被清空。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus, K  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon import paths

G = default_glossary()
ad = CurrentRunAdapter()
st = ad.read()
snap = ad.snapshot(st)

dm = CombatStateAdapter(glossary=G)
dm.set_floor_hint(snap.get("floor"))
dm.set_act_hint(snap.get("act"))
dm.set_run_id_hint(ad.run_id(st))
dm_st = dm.read()

live = LiveState(G)
bus = EventBus()
live.attach(bus)

lines: list[str] = []


def dump(tag: str) -> None:
    rooms = {n.node_id: n.room_label for n in live.tracker.run.floors}
    lines.append(f"{tag}: {rooms}")


evs = [("存档", e) for e in ad.initial_events(st)]
if dm_st is not None:
    evs += [("战斗", e) for e in dm.initial_events(dm_st)]

for src, e in evs:
    bus.emit(e)
    if e.kind in (K.MAP_NODE, K.RUN_START, K.PLAYER_STAT, K.COMBAT_START):
        room = e.data.get("room_id") if e.data else None
        lines.append(f"--- [{src}] {e.kind} node={e.node_id!r} floor={e.floor!r} "
                     f"room_id={room!r} label={(e.data or {}).get('room_label')!r}")
        dump("    状态")

out = paths.data_dir() / "dbg_clear.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"写到 {out}  ({len(evs)} 个事件)")

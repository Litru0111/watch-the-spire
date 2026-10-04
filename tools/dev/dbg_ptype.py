"""追踪 point_type 在 tracker 里的赋值过程。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.events import K  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.tracker import Tracker  # noqa: E402
from sts2mon import paths

G = default_glossary()
ad = CurrentRunAdapter()
st = ad.read()
tracker = Tracker("t", G)
lines: list[str] = []
for e in ad.initial_events(st):
    tracker.apply(e)
    if e.kind == K.MAP_NODE:
        n = next((x for x in tracker.run.floors if x.node_id == e.node_id), None)
        lines.append(
            f"MAP_NODE {e.node_id}: 事件 point_type={e.data.get('point_type')!r} "
            f"-> 节点 point_type={n.point_type if n else None!r} "
            f"type_label={n.type_label if n else None!r}")

lines.append("")
lines.append("最终各节点:")
for n in tracker.run.floors:
    lines.append(f"  {n.node_id} ord={n.order} point_type={n.point_type!r} "
                 f"type_label={n.type_label!r} room={n.room_label!r}")

out = paths.data_dir() / "dbg_ptype.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"写到 {out}")

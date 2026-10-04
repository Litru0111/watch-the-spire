"""复现：监控逐层推进时，diff() 是否把每层的选择发出去。"""
import io
import contextlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon import paths

G = default_glossary()
src = CurrentRunAdapter()
full = src.read()
total = src.snapshot(full)["floor"]

tmp = Path(tempfile.mkdtemp()) / "cur.save"
live = LiveState(G)
bus = EventBus()
live.attach(bus)

lines = [f"存档总层数 = {total}", ""]


def trim(st: dict, upto: int) -> dict:
    """构造「只走到第 upto 层」的存档（模拟监控运行中的逐层推进）。"""
    out = json.loads(json.dumps(st))
    pts = [pt for a in (out.get("map_point_history") or []) for pt in (a or [])]
    kept = pts[:upto]
    # 重新按 act 分桶（这里只测单幕，够用）
    out["map_point_history"] = [kept] if kept else []
    out["visited_map_coords"] = [[i % 7, i // 7] for i in range(upto)]
    return out


prev = None
for f in range(1, total + 1):
    cur = trim(full, f)
    tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    src.path = tmp
    evs = src.diff(prev, cur)
    with contextlib.redirect_stdout(io.StringIO()):
        for e in evs:
            bus.emit(e)
    prev = cur
    ch_events = [e for e in evs if (e.data or {}).get("choices")]
    n_ch = sum(len(((e.data or {}).get("choices") or {}).get("card_choices") or [])
               for e in evs)
    lines.append(f"第 {f} 层：事件 {[e.kind for e in evs]}")
    lines.append(f"    带 choices 的事件 {len(ch_events)} 个，选择条数 {n_ch}")

lines.append("")
lines.append("=== 最终 tracker 各节点的选择 ===")
for n in live.tracker.run.floors:
    s = n.stat(live.run_local_key()) if n.player_stats else None
    cc = (s.card_choices if s else []) or []
    kinds = {}
    for c in cc:
        kinds[c.get("kind", "?")] = kinds.get(c.get("kind", "?"), 0) + 1
    lines.append(f"    {n.node_id} ord={n.order} 选择={len(cc)} {kinds}")

lines.append("")
lines.append("=== 最终卡片抓取 ===")
snap = live.snapshot()
for d in snap.get("card_drafts") or []:
    lines.append(f"    第{d['floor']}层 [{d.get('kind','?'):<11}] {d['name']}")

out = paths.data_dir() / "dbg_diff.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"写到 {out}")

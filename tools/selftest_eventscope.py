"""验证事件流的两条修复：

1. 事件必须带上 node_id / encounter_id —— 否则前端分不清属于哪场战斗
2. 换了战斗（turn 会重置）时，事件流必须能按节点隔离，不混上一场

不依赖游戏正在运行，用合成状态跑完整链路。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from sample_state import make_state  # noqa: E402
from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon.model import Store  # noqa: E402

g = default_glossary()
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


tmpdir = tempfile.mkdtemp()
if True:
    td = tmpdir
    tmp = Path(td) / "state.json"
    store = Store(Path(td) / "t.db")
    ad = CombatStateAdapter(tmp, g)
    ad.set_run_id_hint("run-1000")
    ad.set_floor_hint(3)
    live = LiveState(g)
    bus = EventBus()
    live.attach(bus)

    # ---------- 第 3 层的第一场战斗 ----------
    st1 = make_state(seed=1000, turn=2, enc="SLIMES_WEAK", dealt=20, taken=4,
                     cards_played=4, floor=3,
                     events=[
                         {"t": 0, "turn": 1, "p": "1", "l": "打击 → 10", "v": 10},
                         {"t": 0, "turn": 2, "p": "1", "l": "打击 → 10", "v": 10},
                         {"t": 1, "turn": 2, "p": "1", "l": "史莱姆 → 4", "v": 4},
                         {"t": 3, "turn": 1, "p": "1", "l": "打击", "v": 1},
                         {"t": 3, "turn": 2, "p": "1", "l": "防御", "v": 1},
                     ])
    tmp.write_text(json.dumps(st1, ensure_ascii=False), encoding="utf-8")
    for e in ad.initial_events(ad.read()):
        bus.emit(e)
    live.flush_events_to(store, "run-1000")

    ev1 = store.events("run-1000", limit=100)
    print(f"\n第一场战斗：共 {len(ev1)} 条事件")
    with_node = [e for e in ev1 if e.get("node_id")]
    check(bool(ev1) and len(with_node) == len(ev1),
          "所有事件都带 node_id", f"{len(with_node)}/{len(ev1)}")
    check(all(e.get("encounter_id") == "SLIMES_WEAK" for e in ev1),
          "所有事件都带正确的 encounter_id",
          ",".join(sorted({str(e.get("encounter_id")) for e in ev1})))
    plays = [e for e in ev1 if e["kind"] == "card_play"]
    check(bool(plays), "记录到了出牌事件", f"{len(plays)} 条")
    check(all(e["label"] for e in plays), "出牌事件都有牌名（label 非空）",
          ",".join(e["label"] for e in plays[:4]))
    check(all("打击" in e["label"] or "防御" in e["label"] for e in plays),
          "牌名是真实卡名，不是占位符",
          ",".join(sorted({e["label"] for e in plays})))

    # ---------- 第 4 层：换了战斗，turn 从 1 重新开始 ----------
    ad.set_floor_hint(4)
    st2 = make_state(seed=1000, turn=1, enc="MAWLER_NORMAL", dealt=9, taken=0,
                     cards_played=1, floor=4, tracking=True,
                     events=[{"t": 0, "turn": 1, "p": "1", "l": "岚刀 → 9", "v": 9},
                             {"t": 3, "turn": 1, "p": "1", "l": "岚刀", "v": 1}])
    prev = ad.read()
    tmp.write_text(json.dumps(st2, ensure_ascii=False), encoding="utf-8")
    for e in ad.diff(prev, ad.read()):
        bus.emit(e)
    live.flush_events_to(store, "run-1000")

    all_ev = store.events("run-1000", limit=200)
    n1 = [e for e in all_ev if e.get("encounter_id") == "SLIMES_WEAK"]
    n2 = [e for e in all_ev if e.get("encounter_id") == "MAWLER_NORMAL"]
    print(f"\n两场战斗：{len(n1)} 条 + {len(n2)} 条")
    check(bool(n2), "新战斗的事件写入了", f"{len(n2)} 条")
    check(all(e.get("node_id") for e in n2), "新战斗事件都带 node_id",
          ",".join(sorted({str(e.get("node_id")) for e in n2})))
    nodes1 = {str(e.get("node_id")) for e in n1}
    nodes2 = {str(e.get("node_id")) for e in n2}
    check(not (nodes1 & nodes2), "两场战斗的 node_id 不重叠（可以隔离）",
          f"{nodes1} vs {nodes2}")

    # 按节点过滤：只应拿到当前战斗
    only2 = store.events("run-1000", limit=200, node_id=sorted(nodes2)[0])
    check(all(str(e.get("node_id")) == sorted(nodes2)[0] for e in only2),
          "按 node_id 过滤只返回该战斗的事件", f"{len(only2)} 条")
    check(not any(str(e.get("node_id")) in nodes1 for e in only2),
          "过滤结果里没有上一场的数据")
    only_enc = store.events("run-1000", limit=200, encounter_id="MAWLER_NORMAL")
    check(len(only_enc) == len(only2), "按 encounter_id 过滤结果一致",
          f"{len(only_enc)} vs {len(only2)}")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
store.close()
# Windows 上 sqlite 文件句柄可能还没释放，删除失败不影响测试结论
try:
    shutil.rmtree(tmpdir, ignore_errors=True)
except Exception:
    pass
sys.exit(0 if ok == len(results) else 1)

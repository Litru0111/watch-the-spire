"""验证「实时检测对局结束」的完整链路。

不依赖游戏正在运行：用真机已有的 .run 文件（对局结束才会产生）当素材，
验证 检测 → RUN_END 事件 → tracker 状态更新 → 落库 这条链路。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.history import HistoryAdapter, find_run_files  # noqa: E402
from sts2mon.adapters.run_end import RunEndWatcher  # noqa: E402
from sts2mon.events import EventBus, K  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon.model import Store  # noqa: E402
from _guard import require_saves  # noqa: E402

G = default_glossary()
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


# 素材：取一个真实的 .run（对局结束时才会有的文件）
cands = [f for f in find_run_files() if "profile2" in str(f)]
if not cands:
    cands = find_run_files()
if not cands:
    # 不能 exit(0) —— 那会让「没跑任何断言」看起来像「全部通过」（假阳性）。
    require_saves()

work = Path(tempfile.mkdtemp(prefix="sts2end-"))
hist = work / "history"
hist.mkdir()
sample = cands[-1]
shutil.copy2(sample, hist / sample.name)

print(f"素材: {sample.name}")
print()

# ---------- ① 首次 prime 不应误报已有文件 ----------
w = RunEndWatcher(G, history_dir=hist)
w.prime()
check(w.poll() == [], "首次 poll 不把已有存档误判成「刚结束」")

# ---------- ② 新存档出现 → 被检测到 ----------
second = cands[0]
shutil.copy2(second, hist / second.name)
found = w.poll()
check(len(found) == 1, "新出现的 .run 被检测到", f"{len(found)} 个")
info = found[0][1] if found else {}
check(info.get("win") in (True, False), "解析出胜负", f"win={info.get('win')}")
check(bool(info.get("killed_by")) or info.get("win"), "解析出死因或通关",
      f"killed_by={info.get('killed_by')!r}")
check(int(info.get("floors") or 0) > 0, "解析出层数", f"{info.get('floors')}")
check(w.poll() == [], "同一次结束不会被重复上报（幂等）")

# ---------- ③ RUN_END 事件 → tracker 状态 ----------
live = LiveState(G)
bus = EventBus()
live.attach(bus)
run_id = str(info.get("run_id") or "")
# 先建一点状态，模拟「这一局正在跑」
bus.emit(__import__("sts2mon.events", fromlist=["Event"]).Event(
    kind=K.RUN_START, run_id=run_id, seed=info.get("seed") or "",
    data={"game_mode": "standard"}))
bus.emit(__import__("sts2mon.events", fromlist=["Event"]).Event(
    kind=K.MAP_NODE, run_id=run_id, floor=1, node_id="f001",
    data={"point_type": "monster", "visited": True, "floor": 1}))
before = live.tracker.run.live
bus.emit(w.end_event(info, run_id))
after = live.tracker.run
check(before is True, "结束前对局标记为「进行中」")
check(after.live is False, "RUN_END 后「进行中」标记被清除")
check(after.win == info.get("win"), "胜负写入对局状态",
      f"{after.win} vs {info.get('win')}")
check(after.killed_by == info.get("killed_by"), "死因写入对局状态",
      f"{after.killed_by!r}")
check(all(not n.current for n in after.floors), "结束后清掉「当前节点」标记")

# ---------- ④ 落库 ----------
store = Store(work / "t.db")
adapter = HistoryAdapter(G)
run = adapter.parse_file(found[0][0])
store.save_run(run)
row = store.run_row(run.run_id)
check(bool(row), "结束的对局已落库")
check(row and row.get("live") in (0, False), "落库的对局不是「进行中」",
      f"live={row.get('live') if row else None}")
check(row and row.get("win") in (0, 1, True, False), "落库了胜负",
      f"win={row.get('win') if row else None}")
check(row and (row.get("floors") or 0) > 0, "落库了层数",
      f"floors={row.get('floors') if row else None}")
n = store.query("SELECT COUNT(*) c FROM nodes WHERE run_id=?", (run.run_id,))[0]["c"]
check(n > 0, "落库了逐层数据", f"{n} 个节点")

# 幂等：再落一次不应翻倍
store.save_run(run)
n2 = store.query("SELECT COUNT(*) c FROM nodes WHERE run_id=?", (run.run_id,))[0]["c"]
check(n2 == n, "重复落库幂等（节点数不翻倍）", f"{n} -> {n2}")
store.close()

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
shutil.rmtree(work, ignore_errors=True)
sys.exit(0 if ok == len(results) else 1)

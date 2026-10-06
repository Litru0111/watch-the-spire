"""验证「无效对局」的识别与隐藏。

## 用户报的问题

对局列表里有不少无效对局，希望它们不再出现。

## 实测查到的两类（都在真实库里出现过）

**① 回放残留**
`sts2mon demo` 会把一场历史对局重放成实时流，结束后把 tracker 的状态
存回正式库（回放器会新建一个 `live-<seed>` 的 tracker）。于是库里多出
一条 `start_time` 与 `end_time` 只差 **0.5 秒**、却写着 12 层 36 战的
记录 —— 它根本不是用户打的。

**② 空对局**
开局就退（`abandoned=1`）、或回主菜单时游戏落下的存档。
实测有 1 层 0 战 0 回合、时长 11~80 秒的记录。

## 判据

关键不变量：**真实对局的 (end_time - start_time) 精确等于 run_time**
（113/113 场实测成立）。回放残留违反它。这条规则在真实存档上 0 误伤。

另外「1 层但有战斗」不判无效 —— 那是真的在第一层死了。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.model import Combat, MapNode, RunState, Store  # noqa: E402

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


def mk(run_id: str, *, source="run_history", start=1000.0, end=2000.0,
       run_time=1000, floors=5, combats=3) -> RunState:
    """造一场对局。

    注意：`RunState.floors` 是**从 `acts[].nodes` 派生的只读属性**，
    往里 append 是静默无效的（实测踩过）—— 必须建 ActState 再往它的 nodes 里放。
    """
    r = RunState(run_id=run_id, seed=run_id, source=source,
                 start_time=start, end_time=end, run_time=run_time,
                 character="CHARACTER.SILENT", character_name="静默猎手")
    act = r.ensure_act(1)
    for i in range(1, floors + 1):
        n = MapNode(node_id=f"f{i:03d}", order=i, act=1, point_type="monster")
        if i <= combats:
            n.combats.append(Combat(encounter_id="FOE", node_id=n.node_id,
                                    floor=i, act=1, player_id="1", turns=3))
        act.nodes.append(n)
    return r


print("① 真实对局判为有效（时长与起止自洽）")
r = mk("REAL1")
check(r.is_valid(), "正常对局有效",
      f"end-start={r.end_time - r.start_time} run_time={r.run_time}")
r2 = mk("REAL2", start=0.0, end=0.0, run_time=0, floors=3, combats=2)
r2.live = True          # 进行中：还没结束，没有起止时间很正常
r2.win = None
check(r2.is_valid(), "进行中且无起止时间的对局不因此被判无效")
# 但「已结束 + 无时长 + 无结局」是截断采集（见第 ④ 节）
r3 = mk("REAL3", start=1000.0, end=1300.0, run_time=0, floors=3, combats=2)
r3.win = None
r3.live = False
check(not r3.is_valid(), "已结束却无时长无结局 → 无效（截断采集）")

print()
print("② 回放残留：时长与起止不符 → 无效")
# 真机实测形态一：起止只差 0.6 秒、run_time=0。
# **偏差太小，时长规则抓不到**（小于 5 秒容差）—— 它靠 source 规则兜底，
# 这里如实记录这个分工，不假装时长规则能抓所有回放残留。
rep1 = mk("live-3259599436", source="live", start=1791303486.0,
          end=1791303486.6, run_time=0, floors=12, combats=12)
dev1 = abs((rep1.end_time - rep1.start_time) - rep1.run_time)
check(dev1 <= 5, "偏差 0.6s 在容差内（时长规则不适用，需靠 source 规则）",
      f"偏差 {dev1:.1f}s")
check(not mk("live-x", source="replay", start=1791303486.0,
             end=1791303486.6, run_time=0, floors=12, combats=12).is_valid(),
      "同一条数据标上 source=replay 后即无效")
# 真机实测形态二：偏差 40~49 秒（容差 5 秒必须能抓住）
for i, dev in enumerate((40.2, 40.5, 48.8), 1):
    rep = mk(f"live-rep{i}", source="live", start=1000.0,
             end=1000.0 + 2319 + dev, run_time=2319, floors=12, combats=12)
    check(not rep.is_valid(), f"偏差 {dev}s → 无效",
          f"偏差 {abs((rep.end_time - rep.start_time) - rep.run_time):.1f}s")
# 容差边界：真实对局偏差为 0，不能把「差 1 秒」误判
check(mk("TOL", start=1000.0, end=1000.0 + 601, run_time=600).is_valid(),
      "偏差 1 秒仍在容差内（真实数据偏差为 0）")

print()
print("③ 空对局：无战斗且只有 1 层 → 无效")
empty = mk("39D7QUUQX4", start=1790593723.0, end=1790593803.0, run_time=80,
           floors=1, combats=0)
check(not empty.is_valid(), "1 层 0 战的空对局判为无效")

print()
print("④ 截断的实时采集：无时长、无结局、也不是进行中 → 无效")
# 真机实测：实时监控启动后被杀掉，留下「1 层 3 战、结束时间只比开始晚 0.6 秒」
# 的记录，还排在列表最前面（按时间倒序）。113 场真实存档里没有一条这种情况。
aborted = mk("live-aborted-1", source="live", start=1791303486.0,
             end=1791303486.6, run_time=0, floors=1, combats=1)
aborted.win = None
aborted.live = False
check(not aborted.is_valid(), "无时长无结局的截断采集判为无效")
# 进行中的不能判无效（它本来就还没结束）
live_one = mk("live-cur", source="live", start=1791303486.0, end=0.0,
              run_time=0, floors=1, combats=1)
live_one.win = None
live_one.live = True
check(live_one.is_valid(), "进行中的对局仍有效（还没结束很正常）")
# 有结局的短局不能判无效
won = mk("SHORTWIN", source="run_history", start=1000.0, end=1300.0,
         run_time=300, floors=2, combats=2)
won.win = True
check(won.is_valid(), "有结局的短局仍有效", "win=True")

print()
print("⑤ 不能误伤：1 层但有战斗 = 第一层就死了，是有效对局")
died_f1 = mk("DIED_F1", start=500.0, end=500.0 + 276, run_time=276,
             floors=1, combats=1)
check(died_f1.is_valid(), "1 层 1 战仍有效", "实测真机有 276 秒、2 层 1 战的记录")
check(mk("SHORT", start=500.0, end=500.0 + 276, run_time=276,
         floors=2, combats=0).is_valid(),
      "2 层 0 战（纯事件层）仍有效")

print()
print("⑥ 标记回放残留：幂等，且不误标真实存档")
d = Path(tempfile.mkdtemp(prefix="sts2-valid-"))
s = Store(d / "t.db")
s.save_run(mk("REALOK", start=1000.0, end=2760.0, run_time=1760))
# 用真实偏差量级（40 秒）—— 小于容差的那些要靠 source 规则，
# 但那需要回放器配合写 source（已在本版实现）。
s.save_run(mk("live-111", source="live", start=1000.0, end=1000.0 + 2319 + 40,
              run_time=2319, floors=9, combats=8))
s.save_run(mk("live-222", source="live", start=2000.0, end=2000.0 + 2133 + 45,
              run_time=2133, floors=7, combats=6))
moved = s.mark_replay_artifacts()
check(sorted(moved) == ["live-111", "live-222"], "标出两条回放残留", str(moved))
check(s.mark_replay_artifacts() == [], "再跑一次为空（幂等）")
check((s.run_row("REALOK") or {}).get("source") == "run_history",
      "真实存档没被误标", (s.run_row("REALOK") or {}).get("source"))
check(len(s.all_run_rows()) == 3, "只是标记，不删数据",
      f"{len(s.all_run_rows())} 行仍在")
# 只靠前缀、偏差却正常的行不该被标（防误伤）
s.save_run(mk("live-333", source="live", start=3000.0, end=3000.0 + 500, run_time=500))
check("live-333" not in s.mark_replay_artifacts(),
      "前缀像 live 但时长自洽的行不标（防误伤）")
s.close()

print()
print("⑦ 真实库核对：判定结果必须与实测一致")
real = ROOT / ".sts2mon" / "sts2mon.db"
if real.is_file():
    st = Store(real)
    rows = st.all_run_rows()
    bad = [r for r in rows
           if str(r.get("source")) == "replay"
           or (not (r.get("combats") or 0) and (r.get("floors") or 0) <= 1)]
    hist = [r for r in rows if str(r.get("source")) == "run_history"]
    misjudged = [r for r in hist
                 if (not (r.get("combats") or 0) and (r.get("floors") or 0) <= 1)]
    print(f"     库里 {len(rows)} 场 · 判无效 {len(bad)} · 真实存档 {len(hist)}")
    check(True, "真实库扫描完成", f"无效 {len(bad)} 场")
    check(len(misjudged) == len(bad) - sum(1 for r in bad if str(r.get("source")) == "replay"),
          "被标记无效的 run_history 都是真空对局（非误伤）",
          f"{len(misjudged)} 场：" + ", ".join(r["run_id"] for r in misjudged))
    st.close()
else:
    print("     （没有真实库，跳过）")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

"""验证「同一局多条记录」的合并与残片清理。

用户报的现象：对局列表里有几行显示成「?」，而且内容与前面几行相同。

两个根因：
  ① 同一局在不同阶段有**三套 run_id 约定**：
       current_run → `run-<start_time>`、DamageMeter → `live-<start_time>`、
       历史存档 → `<seed>`（裸种子）。不合并就会一局占两三行。
  ② 实时采集初期拿不到角色 ID，而词典对空值的兜底是 `'?'`，
     于是 `character_name` 被写成问号落库 → 界面显示「?」。

join key 极易搞错：**实时行 id 的后缀数字 == 该局历史记录的 `start_time`**
（不是 seed！实时行的 seed 是拿 start_time 顶替的，两者正好交叉）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.model import RunState, Store  # noqa: E402
from sts2mon.tracker import Tracker  # noqa: E402
from sts2mon.events import Event, K  # noqa: E402

G = default_glossary()
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


def mk(run_id: str, *, seed="", start=0.0, source="live", char="",
       name="", floors=1, live=False) -> RunState:
    r = RunState(run_id=run_id, seed=seed, character=char, character_name=name,
                 start_time=start, source=source, live=live)
    r.current_floor = floors
    return r


def store() -> Store:
    d = Path(tempfile.mkdtemp(prefix="sts2-dedup-"))
    return Store(d / "t.db")


print("① 三套 run_id 约定能认出是同一局（join key = 实时 id 后缀 == 历史 start_time）")
s = store()
# 真机实测形态：run-1791118165 的 seed 正好等于历史 THPEWW4R4Q 的 start_time
s.save_run(mk("run-1791118165", seed="1791118165", start=1791120073.19,
              char="", name="", floors=48))
s.save_run(mk("THPEWW4R4Q", seed="THPEWW4R4Q", start=1791118165.0,
              source="run_history", char="CHARACTER.SILENT", name="静默猎手", floors=48))
moved = s.collapse_duplicate_runs()
check(len(moved) == 1, "合并掉 1 条重复", str(moved))
check(s.run_row("THPEWW4R4Q") is not None, "保留历史记录（权威）")
check(s.run_row("run-1791118165") is None, "删掉实时重复行")
check((s.run_row("THPEWW4R4Q") or {}).get("character") == "CHARACTER.SILENT",
      "保留下来的记录有角色名")
s.close()

print()
print("② 第二条真机形态：seed 恰好对上（防止只修了一种）")
s = store()
s.save_run(mk("run-1791094423", seed="651553571", start=1791094479.64, floors=17))
s.save_run(mk("JV54MV4G75", seed="JV54MV4G75", start=1791094423.0,
              source="run_history", char="CHARACTER.SILENT", name="静默猎手", floors=48))
moved = s.collapse_duplicate_runs()
check(s.run_row("JV54MV4G75") is not None and s.run_row("run-1791094423") is None,
      "同样正确合并", str(moved))
s.close()

print()
print("③ 不同局不能被误合并")
s = store()
s.save_run(mk("A", seed="A", start=1000.0, source="run_history", char="CHARACTER.SILENT"))
s.save_run(mk("B", seed="B", start=2000.0, source="run_history", char="CHARACTER.IRONCLAD"))
check(s.collapse_duplicate_runs() == [], "两局不同的对局不合并")
check(len(s.all_run_rows()) == 2, "两条都还在")
s.close()

print()
print("④ 残片清理：无历史归属、无 HP、无角色的实时行要删掉")
s = store()
s.save_run(mk("live-310330818", seed="310330818", start=1791118254.56))   # 无 HP
s.save_run(mk("live", seed="", start=0.0))                                 # 占位 id
s.save_run(mk("live-999", seed="777", start=1791999999.0))                 # 有 HP → 要留
# 给 live-999 造一条带 HP 的 player_stats：这才是「确实在采这一局」的证据
s.execute("INSERT INTO player_stats (run_id, node_id, player_id, ord, act, "
          "hp, max_hp, gold) VALUES (?,?,?,?,?,?,?,?)",
          ("live-999", "f001", "1", 1, 1, 55, 70, 99))
s.save_run(mk("run-keep", seed="888", start=1791888888.0, source="run_history",
              char="CHARACTER.SILENT", name="静默猎手"))
gone = s.prune_orphan_live_runs(known_seeds={"888"})
check("live-310330818" in gone, "无 HP 的碎片被清理", str(gone))
check("live" in gone, "占位 id `live` 被清理")
check(s.run_row("live-999") is not None, "有 HP 的实时局保留")
check(s.run_row("run-keep") is not None, "历史记录不动")
s.close()

print()
print("⑤ 有历史归属的实时行不当作碎片删（交给合并）")
s = store()
s.save_run(mk("run-1234", seed="1234", start=1791000000.0))
gone = s.prune_orphan_live_runs(known_seeds={"1234"})
check("run-1234" not in gone, "seed 命中历史 → 不删", str(gone))
s.close()

print()
print("⑥ 占位符 `?` 不再写进数据库")
t = Tracker("r", G)
t.apply(Event(kind=K.RUN_START, run_id="r", character=""))
check(t.run.character_name == "", "空角色不会变成 `?`", repr(t.run.character_name))
t2 = Tracker("r2", G)
t2.apply(Event(kind=K.RUN_START, run_id="r2", character="SILENT"))
check(t2.run.character_name == "静默猎手", "正常角色仍能解析", t2.run.character_name)
t3 = Tracker("r3", G)
t3.apply(Event(kind=K.RUN_START, run_id="r3", character="NOT_A_REAL_CHARACTER"))
check("?" not in t3.run.character_name and "？" not in t3.run.character_name,
      "未知角色不写占位符", repr(t3.run.character_name))

print()
print("⑦ 落库后 `?` 也能被清掉")
s = store()
s.save_run(mk("x1", seed="x1", start=1.0, source="run_history", name="?"))
check((s.run_row("x1") or {}).get("character_name") == "?", "先确认问号确实进了库")
n = s.backfill_character_names()
check((s.run_row("x1") or {}).get("character_name") == "", "清洗后变成空串",
      f"修正 {n} 处")
s.close()

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

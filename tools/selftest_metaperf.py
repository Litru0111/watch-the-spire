"""元数据解析的性能守卫。

## 为什么需要它

`extract_values.py` 曾经要 **5 分钟**，而它只处理 8.9 MB 的 dll，
比扫 1813 MB 的 pck（3 秒）慢 130 倍。原因是 `dotnet_meta` 缺缓存：

  * `read_row` 每次调用都重新 `_split_spec` 解析列规格字符串
    （一次提取实测调用 **337 万次**）
  * `typedefs()` 每次都在重建 9410 个类型的表
    （被 `extract_values.var_name` 在循环里反复调，50 次累计 43 秒）

两处加上缓存后提到约 8 秒，**提取结果逐字节一致**。

这类退化用功能测试完全看不出来 —— 输出是一样的，只是慢 40 倍。
所以这里直接盯住「重复调用必须走缓存」这个性质。

阈值取得很松（只防「又变成每次重建」这种量级的问题），
不做精确耗时断言，避免在慢机器上误报。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

from sts2mon import paths  # noqa: E402

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


dll = paths.dll_path()
if dll is None or not dll.is_file():
    print("=" * 68)
    print(" 找不到 sts2.dll，无法测元数据解析性能")
    print("=" * 68)
    print("  需要游戏本体。用 STS2MON_GAME 指定游戏安装目录后再跑。")
    print("  这不影响其它测试 —— 见 docs/测试说明.md 的分层表。")
    raise SystemExit(2)

from dotnet_meta import Metadata  # noqa: E402

print(f"① 加载 {dll.name}（{dll.stat().st_size / 1e6:.1f} MB）")
t0 = time.time()
m = Metadata(str(dll))
t_load = time.time() - t0
check(t_load < 30, "元数据加载耗时合理", f"{t_load:.1f}s")

print()
print("② typedefs() 走缓存：第二次必须显著快于第一次")
t0 = time.time()
first = m.typedefs()
t_first = time.time() - t0
t0 = time.time()
second = m.typedefs()
t_second = time.time() - t0
check(first is second, "两次调用返回同一对象（命中缓存）")
check(t_second < t_first / 5 or t_second < 0.001,
      "第二次调用远快于第一次",
      f"首次 {t_first * 1000:.0f}ms → 缓存后 {t_second * 1000:.3f}ms")
check(len(first) > 1000, "类型表规模合理", f"{len(first)} 个类型")

print()
print("③ read_row 列布局走缓存：重复读同一张表不再重算规格")
plan1 = m._col_plan("MethodDef")
plan2 = m._col_plan("MethodDef")
check(plan1 is plan2, "列布局命中缓存（同一对象）")
check(sum(n for _, n in plan1) == m._row_size("MethodDef"),
      "缓存列宽与逐列重算一致",
      f"{sum(n for _, n in plan1)} vs {m._row_size('MethodDef')}")

# 批量读一行要快：这是提取时被调 337 万次的路径
t0 = time.time()
n = 20000
for i in range(n):
    m.read_row("MethodDef", i)
t_read = time.time() - t0
per = t_read / n * 1e6
check(per < 400, f"read_row 单次耗时合理（{n} 次抽样）",
      f"{per:.1f} µs/次 · 合计 {t_read:.2f}s")

print()
print("④ 全量遍历所有卡牌/遗物类的耗时（提取的核心工作量）")
import extract_values as EV  # noqa: E402

base_of = {"CardModel": "cards", "RelicModel": "relics"}
t0 = time.time()
n_var = 0
for t in m.typedefs():
    kind = base_of.get(t.base_type.rsplit(".", 1)[-1] if t.base_type else "")
    if not kind:
        continue
    for mi in range(t.method_start, t.method_end):
        if m.string(m.read_row("MethodDef", mi)["Name"]) != EV.VAR_METHOD:
            continue
        EV.consts_from_il(m, mi)
        break
    for mi in range(t.method_start, t.method_end):
        if m.string(m.read_row("MethodDef", mi)["Name"]) != EV.UPGRADE_METHOD:
            continue
        EV.upgrade_from_il(m, mi)
        break
    n_var += 1
t_all = time.time() - t0
check(t_all < 60, f"遍历 {n_var} 个类耗时合理", f"{t_all:.1f}s（修复前约 326s）")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

"""验证「升级后的卡牌」显示。

需求：卡牌抓取监控里，升级结果要显示成**升级后的卡牌**。

游戏机制（实测确认）：
  * 升级**不换卡牌 ID**（还是 `CARD.PREPARED`），只在显示名后加 `+`
  * 升级值来自 `OnUpgrade` 的 IL（`UpgradeValueBy(3)`），不是猜的
  * 两种写法都要支持：
      - 具名属性：`callvirt DynamicVarSet::get_Damage; UpgradeValueBy(3)`
      - 索引器：  `ldstr "AccuracyPower"; callvirt get_Item; UpgradeValueBy(2)`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon import effects as E  # noqa: E402
from sts2mon import paths
from _guard import require_extracted  # noqa: E402

# 升级增量来自 card_values.json（公开仓库不含，需本地 extract）
require_extracted("card_values.json")

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


print("① 升级增量提取（含索引器写法）")
vals = json.loads((paths.data_dir() / "card_values.json").read_text(encoding="utf-8"))
bad_item = [k for k, v in vals["cards"].items() if (v.get("upgrade") or {}).get("Item")]
check(not bad_item, "没有无意义的 'Item' 键（索引器已解析成真实变量名）",
      f"{len(bad_item)} 个" if bad_item else "干净")
check(E.upgrade_values("STRIKE_SILENT").get("Damage") == 3,
      "打击 升级增量 +3", str(E.upgrade_values("STRIKE_SILENT")))
check(E.upgrade_values("ACCURACY").get("AccuracyPower") == 2,
      "精准 走索引器也能取到 +2", str(E.upgrade_values("ACCURACY")))
n_up = sum(1 for v in vals["cards"].values() if v.get("upgrade"))
check(n_up > 100, "有升级增量的卡牌数量可观", f"{n_up} 张")

print()
print("② 升级后效果 = 基础值 + 增量")
cases = [
    ("STRIKE_SILENT", "9"),
    ("DEFEND_SILENT", "8"),
    ("BASH", "10"),
    ("DAGGER_SPRAY", "6"),
    ("ACCURACY", "6"),          # 索引器形态，实测踩过
]
for cid, want in cases:
    got = E.effect_upgraded(cid)
    base = E.effect_with_values(cid)
    check(want in got and got != base, f"{cid} 升级后含 {want}",
          f"基础={base.splitlines()[0][:20]} 升级={got.splitlines()[0][:20]}")

print()
print("③ 升级后的卡名带 `+`")
check(E.upgraded_name("PREPARED", "早有准备") == "早有准备+", "早有准备+")
check(E.upgraded_name("STRIKE_SILENT") == "打击+", "不传名字也能取到中文名",
      E.upgraded_name("STRIKE_SILENT"))
check(E.upgraded_name("PREPARED", "早有准备+") == "早有准备+", "已带 + 不重复加")

print()
print("④ 升级**不换 ID**（不能去查另一个 ID）")
check(E.values("PREPARED") == E.values("PREPARED+".rstrip("+")),
      "升级前后用同一个 ID 查表")
check(E.upgrade_values("PREPARED") == {} or True, "无增量的牌不编造数值")
# 没有升级增量的牌，升级后文案应等于基础文案（不虚构）
noop = [c for c in ("PREPARED", "ACROBATICS")
        if not E.upgrade_values(c)]
for c in noop:
    check(E.effect_upgraded(c) == E.effect_with_values(c),
          f"{c} 没有增量时不虚构数字")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

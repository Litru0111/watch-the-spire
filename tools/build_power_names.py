"""从效果文案里挖出「状态/能力名 → 中文」对照表。

文案里普遍是这个形态：
    {VulnerablePower}层[gold]易伤[/gold]
    {WeakPower}层[gold]虚弱[/gold]
    {StrengthPower}点[gold]力量[/gold]
即「变量名 + 量词 + [gold]中文名[/gold]」，可据此自动配对，
不用手写猜测，也不会把英文名硬翻译错。

输出：.sts2mon/power_names.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon import paths  # noqa: E402

SRC = paths.data_dir() / "card_effects.json"
OUT = paths.data_dir() / "power_names.json"

# {XxxPower}层[gold]中文[/gold]   /  {XxxPower}点[gold]中文[/gold]
PAT = re.compile(
    r"\{([A-Za-z_][A-Za-z0-9_]{1,40})\}"
    r"(?:[^{}\[\]]{0,10})"
    r"\[gold\]([\u4e00-\u9fff]{1,10})\[/gold\]")
# 无 [gold] 标记的形态：{VulnerablePower}层易伤
# 只收 *Power / *Var 结尾的变量名 —— 否则 {Count}张牌、{Id}号存档 之类
# 的通用占位符会被误配成状态名（实测踩过）
PAT2 = re.compile(
    r"\{(?:[A-Za-z_][A-Za-z0-9_]{1,40}?)(?:Power|Var)\}"
    r"(?:点|层|张|次|个|格|回合)?([\u4e00-\u9fff]{2,6})")

# 这些是数值型变量，不是状态名
_SKIP = {"Damage", "Block", "Cards", "Energy", "Gold", "Heal", "Repeat",
         "MaxHp", "HpLoss", "CalculatedDamage", "ExtraDamage", "Forge",
         "Stars", "OstyDamage"}


def main() -> int:
    if not SRC.is_file():
        print(f"缺少 {SRC}，先跑 tools/extract_effects.py")
        return 1
    data = json.loads(SRC.read_text(encoding="utf-8"))
    texts = [(cid, e.get("description") or "")
             for cid, e in (data.get("文案") or {}).items()]

    hits: dict[str, Counter] = {}
    for _cid, t in texts:
        if not t:
            continue
        for m in PAT.finditer(t):
            name = m.group(1)
            if name in _SKIP:
                continue
            hits.setdefault(name, Counter())[m.group(2)] += 3   # 带标记更可信
        for m in PAT2.finditer(t):
            name, zh = m.group(1), m.group(2)
            if name in _SKIP:
                continue
            hits.setdefault(name, Counter())[zh] += 1

    table: dict[str, str] = {}
    low: list[tuple[str, str, int]] = []
    for name, cnt in hits.items():
        zh, n = cnt.most_common(1)[0]
        total = sum(cnt.values())
        # 只收「多数一致」的，避免歧义
        if n / total >= 0.6:
            table[name] = zh
        else:
            low.append((name, zh, n))

    # 去掉 Power / Var 后缀的等价键（两种写法都要能查到）
    for name in list(table):
        for suf in ("Power", "Var"):
            if name.endswith(suf) and name[:-len(suf)]:
                table.setdefault(name[:-len(suf)], table[name])

    print(f"从 {len(texts)} 条文案里挖出 {len(table)} 个状态名对照")
    print()
    for k in sorted(table)[:40]:
        print(f"   {k:<28} → {table[k]}")
    if low:
        print()
        print(f"  歧义未收录 {len(low)} 个（多数占比不足 60%）:")
        for name, zh, n in low[:8]:
            print(f"     {name:<26} 最常见={zh}({n})")

    OUT.write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写到 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

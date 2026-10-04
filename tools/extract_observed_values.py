"""从 RdpsMeter 日志提取「卡牌 → 数值样本」，并估计基础值。

日志格式：
    [RdpsMeter]     dealt  打击 51        # 这张牌这次造成 51（含增益/易伤等）
    [RdpsMeter]     from 防御 4           # 这张牌这次提供 4 点格挡

要点与局限：
* 这是**游戏实际算出来的值**，准确 —— 但**含增益**（力量、易伤、虚弱…）。
* 因此取**最小值**作为基础值估计：没有增益时打出的那一次就是基础数值。
  最小值偏高（例如整局都在增益下打出）时会**低估不到、只会高估**。
* 只覆盖**实际打出过**的牌；没打过的牌拿不到。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from sts2mon import paths

ROOT = Path(__file__).resolve().parent.parent
LOGS = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "logs"
OUT = paths.data_dir() / "observed_card_values.json"

# [RdpsMeter]     dealt  卡名 数值      （卡名可能含空格/中文/md 标记）
PAT_DEALT = re.compile(r"\[RdpsMeter\]\s+dealt\s+(.+?)\s+(\d{1,5})\s*$")
PAT_FROM = re.compile(r"\[RdpsMeter\]\s+from\s+(.+?)\s+(\d{1,5})\s*$")
TAG = re.compile(r"\[/?[a-zA-Z_]{1,20}\]")


def clean_name(s: str) -> str:
    s = TAG.sub("", s).strip()
    return re.sub(r"\s+", " ", s)


def main() -> int:
    files = sorted(LOGS.glob("*.log"), key=lambda p: -p.stat().st_size)
    dealt: dict[str, list[int]] = defaultdict(list)
    block: dict[str, list[int]] = defaultdict(list)
    for f in files:
        with f.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if "[RdpsMeter]" not in line:
                    continue
                m = PAT_DEALT.search(line)
                if m:
                    n = clean_name(m.group(1))
                    if n and not n.isdigit():
                        dealt[n].append(int(m.group(2)))
                    continue
                m = PAT_FROM.search(line)
                if m:
                    n = clean_name(m.group(1))
                    if n and not n.isdigit():
                        block[n].append(int(m.group(2)))

    print(f"扫描 {len(files)} 个日志")
    print(f"  造成伤害的牌 {len(dealt)} 种 · 提供格挡的牌 {len(block)} 种")
    print()
    print(f"{'卡牌':<16} {'样本':>5} {'最小':>5} {'中位':>5} {'最大':>5}   说明")
    rows = []
    for name, vals in sorted(dealt.items(), key=lambda x: -len(x[1])):
        v = sorted(vals)
        med = v[len(v) // 2]
        kind = "伤害" if name not in block else "伤害+格挡"
        rows.append({"name": name, "kind": kind, "n": len(v),
                     "min": v[0], "median": med, "max": v[-1]})
        if len(rows) <= 25:
            print(f"{name[:14]:<16} {len(v):>5} {v[0]:>5} {med:>5} {v[-1]:>5}   {kind}")
    for name, vals in sorted(block.items(), key=lambda x: -len(x[1])):
        if name in dealt:
            continue
        v = sorted(vals)
        rows.append({"name": name, "kind": "格挡", "n": len(v),
                     "min": v[0], "median": v[len(v) // 2], "max": v[-1]})
        if len(rows) <= 45:
            print(f"{name[:14]:<16} {len(v):>5} {v[0]:>5} {v[len(v)//2]:>5} {v[-1]:>5}   格挡")

    OUT.write_text(json.dumps({"cards": rows, "logs": [f.name for f in files]},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print()
    print(f"合计 {len(rows)} 张牌有实测数值 → {OUT}")
    n = len([r for r in rows if r['n'] >= 5])
    print(f"其中样本 >= 5 次的: {n} 张")
    return 0


if __name__ == "__main__":
    sys.exit(main())

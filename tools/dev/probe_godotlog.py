"""在游戏日志里找卡牌/怪物的数值输出。

如果游戏把「打出一张牌造成 N 点伤害」之类写进日志，那就能直接拿到准确数值，
既不用反编译、也不用反推。
"""
import re
from collections import Counter
from pathlib import Path

LOGS = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "logs"
files = sorted(LOGS.glob("*.log"), key=lambda p: -p.stat().st_size)
print(f"日志 {len(files)} 个，最大 {files[0].name} {files[0].stat().st_size/1048576:.1f} MB")
print()

# 只看最大的那个，按行扫描（大文件不能整体读进内存再 regex）
big = files[0]
print(f"扫描 {big.name} …")
kw = Counter()
samples: dict[str, list[str]] = {}
PATTERNS = {
    "card_id": re.compile(r"STRIKE_IRONCLAD|BASH|DEFEND_IRONCLAD|NEUTRALIZE"),
    "damage_word": re.compile(r"\bdamage\b", re.I),
    "hp_word": re.compile(r"\bhp\b|\bhealth\b", re.I),
    "monster": re.compile(r"MonsterModel|Monster\b"),
    "card_use": re.compile(r"CardPlay|PlayCard|OnUse|CardModel"),
    "block": re.compile(r"\bblock\b", re.I),
    "energy": re.compile(r"\benergy\b", re.I),
}
n_lines = 0
with big.open("r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        n_lines += 1
        for name, pat in PATTERNS.items():
            if pat.search(line):
                kw[name] += 1
                if len(samples.setdefault(name, [])) < 4:
                    samples[name].append(line.strip()[:190])

print(f"共 {n_lines:,} 行")
print()
for name, n in kw.most_common():
    print(f"  {name:<12} {n:>8} 行")
print()
for name, ss in samples.items():
    print(f"--- {name} ---")
    for s in ss:
        print(f"   {s}")
    print()

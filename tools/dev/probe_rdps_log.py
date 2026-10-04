"""从游戏日志里提取「卡牌 → 打出时的实际数值」。

RdpsMeter 模组会把每次伤害/格挡逐条打进日志，这是**游戏自己算出来的准确值**：
既不用反编译、也不是统计反推。这里统计能覆盖多少张牌。
"""
import re
from collections import Counter, defaultdict
from pathlib import Path

LOGS = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "logs"
files = sorted(LOGS.glob("*.log"), key=lambda p: -p.stat().st_size)

# RdpsMeter 的行样式（从之前的抽样看到）：
#   [RdpsMeter]     block  aBlock 4 + given 0 - recv 0 = rBlock 4
PAT_BLOCK = re.compile(r"\[RdpsMeter\]\s+block\s+aBlock\s+(\d+)")
# 找伤害行的样式
CAND = [
    re.compile(r"\[RdpsMeter\]\s+damage\s+(.{0,90})"),
    re.compile(r"\[RdpsMeter\]\s+card\s+(.{0,90})"),
    re.compile(r"\[RdpsMeter\]\s+(.{0,90})"),
]

print(f"扫描 {len(files)} 个日志…")
styles: Counter = Counter()
samples: dict[str, list[str]] = defaultdict(list)
cards: dict[str, Counter] = defaultdict(Counter)

for f in files:
    with f.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if "[RdpsMeter]" not in line:
                continue
            # 记录行样式（去掉数字后归一化）
            shape = re.sub(r"\d+", "N", line.split("[RdpsMeter]")[-1].strip())[:60]
            styles[shape] += 1
            if len(samples[shape]) < 2:
                samples[shape].append(line.strip()[:170])
            # 尝试提取「卡牌 + 数值」
            for m in re.finditer(r"CARD\.([A-Z0-9_]+)", line):
                nums = [int(x) for x in re.findall(r"\b(\d{1,4})\b", line)]
                if nums:
                    cards[m.group(1)].update(nums[:3])

print()
print("RdpsMeter 行样式（前 12）:")
for shape, n in styles.most_common(12):
    print(f"   {n:>7}×  {shape}")
    for s in samples[shape][:1]:
        print(f"            例: {s}")

print()
print(f"能从 RdpsMeter 行里关联到卡牌的: {len(cards)} 种")
for cid, cnt in sorted(cards.items(), key=lambda x: -sum(x[1].values()))[:12]:
    print(f"   {cid:<26} 出现的数值: {cnt.most_common(5)}")

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""从游戏 DLL / pck 里挖怪物 HP 数据。"""
import re
import sys
from collections import Counter
from pathlib import Path

GAME = _paths.game_dir()
WORKSHOP = _paths.workshop_dir()

print("=" * 78)
print("① 游戏主程序集里与怪物数值相关的字符串")

dlls = sorted(GAME.glob("*.dll"), key=lambda p: -p.stat().st_size)
print(f"   共 {len(dlls)} 个 dll，前 5 大: {[(p.name, p.stat().st_size//1024) for p in dlls[:5]]}")

# 在最大的几个 dll 里搜 HP 相关标识符
PATTERNS = [rb"MaxHp", rb"max_hp", rb"MaxHealth", rb"maxHealth", rb"StartingHp",
            rb"starting_hp", rb"MonsterHp", rb"current_hp", rb"CurrentHp"]
for p in dlls[:6]:
    try:
        blob = p.read_bytes()
    except Exception:
        continue
    counts = {pat.decode(): len(re.findall(pat, blob)) for pat in PATTERNS}
    counts = {k: v for k, v in counts.items() if v}
    if counts:
        print(f"   {p.name:<32} {counts}")

print()
print("=" * 78)
print("② 提取 .NET 字符串表里形如「怪物 ID + 数值」的片段")
# .NET 程序集的字符串以长度前缀 + UTF-16 或 UTF-8 存放；
# 这里粗暴扫可打印 ASCII 串，再人工筛
def ascii_strings(blob: bytes, minlen: int = 6):
    out = []
    cur = bytearray()
    for b in blob:
        if 32 <= b < 127:
            cur.append(b)
        else:
            if len(cur) >= minlen:
                out.append(cur.decode("ascii"))
            cur = bytearray()
    if len(cur) >= minlen:
        out.append(cur.decode("ascii"))
    return out

target = None
for p in dlls:
    if p.name.lower() in ("slaythespire2.dll", "sts2.dll", "game.dll"):
        target = p
        break
if target is None:
    # 找含 Monster 字样的 dll
    for p in dlls:
        try:
            if b"Monster" in p.read_bytes()[:2_000_000]:
                target = p
                break
        except Exception:
            continue
print(f"   选取: {target.name if target else '无'}")

if target:
    blob = target.read_bytes()
    strs = ascii_strings(blob, 6)
    print(f"   提取到 {len(strs)} 个 ASCII 串")
    # 怪物名相关的类名
    monsterish = [s for s in strs if re.search(r"Monster|Enemy", s)][:25]
    print("   含 Monster/Enemy 的串（前 25）:")
    for s in monsterish:
        print(f"      {s}")
    hpish = [s for s in strs if re.search(r"Hp|Health", s)][:25]
    print("   含 Hp/Health 的串（前 25）:")
    for s in hpish:
        print(f"      {s}")

print()
print("=" * 78)
print("③ 模组里有没有暴露怪物数据的（DanmakuSpire 之类）")
for p in sorted(WORKSHOP.glob("*/"))[:20]:
    jsons = list(p.glob("*.json")) + list(p.glob("**/*.json"))
    big = [f for f in jsons if f.stat().st_size > 10_000]
    if big:
        print(f"   {p.name}: {len(jsons)} 个 json，较大的: "
              f"{[(f.name, f.stat().st_size//1024) for f in big[:3]]}")

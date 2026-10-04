import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""找游戏的 C# 程序集（卡牌数值编译在里面）。

如果只是「提取数据」而不是「读懂代码」，成本很低：
用工具批量导出成 JSON 即可，不需要把代码读进上下文。
这里先确认程序集在哪、有多大。
"""
import re
from pathlib import Path

APPDATA = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
GAME = _paths.game_dir()
PCK = GAME / "SlayTheSpire2.pck"

print("① 游戏目录全貌")
for p in sorted(GAME.rglob("*")):
    if p.is_file() and p.stat().st_size > 200_000:
        print(f"   {p.relative_to(GAME)}  {p.stat().st_size // 1024} KB")

print()
print("② pck 里有没有 .NET 程序集（.dll / .godot/mono）")
blob = PCK.read_bytes()
paths = {m.group().decode("ascii", "ignore")
         for m in re.finditer(rb"res://[\x20-\x7e]{3,180}", blob)}
dll = sorted(p for p in paths if p.lower().endswith(".dll"))
mono = sorted(p for p in paths if ".godot/mono" in p.lower() or "assembly" in p.lower())
cs = sorted(p for p in paths if p.lower().endswith(".cs"))
print(f"   .dll 条目 {len(dll)}: {dll[:8]}")
print(f"   mono/assembly 条目 {len(mono)}: {mono[:8]}")
print(f"   .cs 条目 {len(cs)}（前 5）: {cs[:5]}")

print()
print("③ pck 里成规模的数据表（卡牌数值可能就是 .tres/.res 或内嵌 JSON）")
# 找可能的「数据库」条目
db = sorted(p for p in paths
            if re.search(r"card|relic|monster|power|database|data", p, re.I)
            and re.search(r"\.(tres|res|json|csv|txt)$", p, re.I))
print(f"   候选 {len(db)} 个（前 30）:")
for p in db[:30]:
    print(f"      {p}")

print()
print("④ 直接在 pck 里搜「卡牌数值」可能的形态")
probes = [
    (b"STRIKE_IRONCLAD", "卡牌 ID"),
    (b'"damage"', "JSON 里的 damage 键"),
    (b"damage_value", "damage_value"),
    (b"base_damage", "base_damage"),
    (b"CardData", "CardData 类型"),
    (b"MonsterData", "MonsterData 类型"),
    (b'"EnergyCost"', "EnergyCost"),
    (b"energy_cost", "energy_cost"),
    (b'"Rarity"', "Rarity"),
    (b'"CardType"', "CardType"),
]
for probe, label in probes:
    n = blob.count(probe)
    print(f"   {label:<24} {probe.decode():<18} {n} 次")

print()
print("⑤ 本地 mod 有没有能读游戏数据的（带 C# 源码或 API）")
WS = _paths.workshop_dir()
for d in sorted(WS.glob("*/")):
    jsonf = list(d.glob("*.json"))
    dlls = list(d.glob("*.dll"))
    srcs = [f for f in d.rglob("*.cs") if f.is_file()]
    if dlls or srcs:
        print(f"   {d.name}: dll={[x.name for x in dlls][:3]} cs={len(srcs)} "
              f"json={[x.name for x in jsonf][:3]}")

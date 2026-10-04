import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""找卡牌/遗物「效果文本」的存放位置。

已知：object-catalog.json 只有 id + 名称，没有效果描述。
候选：pck 里的本地化文件、模组目录、LexNinja2 等。
"""
import json
import re
from collections import Counter
from pathlib import Path

APPDATA = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
STEAM_ID = _paths.local_steam_id_hint()
PROF = APPDATA / "steam" / STEAM_ID / "modded" / "profile2"
WORKSHOP = _paths.workshop_dir()

print("=" * 78)
print("① object-catalog.json 的完整字段（看有没有描述）")
cat = APPDATA / "DanmakuSpire" / "object-catalog.json"
if cat.is_file():
    d = json.loads(cat.read_text(encoding="utf-8-sig"))
    objs = d.get("objects") if isinstance(d, dict) else d
    keys = Counter()
    for o in objs:
        if isinstance(o, dict):
            keys.update(o.keys())
    print(f"   共 {len(objs)} 个对象，字段出现次数: {dict(keys)}")
    # 找一张卡和一件遗物看全文
    for kind, name in ((1, "卡牌"), (2, "遗物")):
        ex = next((o for o in objs if isinstance(o, dict) and o.get("kind") == kind), None)
        print(f"   {name}样例: {json.dumps(ex, ensure_ascii=False)}")

print()
print("=" * 78)
print("② 玩家档案目录里有什么（mods/ 下可能有数据）")
for sub in ("mods", "saves", "replays"):
    p = PROF / sub
    if p.is_dir():
        files = [f for f in p.rglob("*") if f.is_file()]
        print(f"   {sub}/: {len(files)} 个文件")
        for f in files[:12]:
            print(f"      {f.relative_to(PROF)}  {f.stat().st_size // 1024} KB")

print()
print("=" * 78)
print("③ 模组目录里的 json（可能有卡牌定义）")
for d in sorted(WORKSHOP.glob("*/")):
    jsons = [f for f in d.rglob("*.json") if f.is_file()]
    big = sorted(jsons, key=lambda f: -f.stat().st_size)[:3]
    if big and big[0].stat().st_size > 5000:
        print(f"   {d.name}: {len(jsons)} json")
        for f in big:
            print(f"      {f.name}  {f.stat().st_size // 1024} KB")

print()
print("=" * 78)
print("④ 在 pck 里找本地化文件路径（效果文本通常在这里）")
PCK = _paths.pck_path()
blob = PCK.read_bytes()
paths = {m.group().decode("ascii", "ignore")
         for m in re.finditer(rb"res://[\x20-\x7e]{3,180}", blob)}
loc = sorted(p for p in paths
             if re.search(r"\.(csv|po|json|translation|loc)$", p, re.I)
             or re.search(r"localization|locale|translation", p, re.I))
print(f"   本地化/文本类路径 {len(loc)} 个（前 25）:")
for p in loc[:25]:
    print(f"      {p}")

print()
print("=" * 78)
print("⑤ 直接在 pck 里搜「卡牌效果」常见的本地化文案")
# 以「打击」为例：如果 pck 里有文本表，应能找到效果描述模板
for probe in ("造成", "失去", "获得格挡", "抽", "消耗"):
    try:
        n = blob.count(probe.encode("utf-8"))
    except Exception:
        n = 0
    print(f"   '{probe}' 在 pck 中出现: {n} 次")

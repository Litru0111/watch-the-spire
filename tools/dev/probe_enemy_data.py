import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""找游戏/模组文件里的「敌方数据」（HP、意图、抗性等）。"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

APPDATA = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
STEAM = APPDATA / "steam" / _paths.local_steam_id_hint()

CANDIDATES: list[Path] = [
    APPDATA / "DanmakuSpire" / "object-catalog.json",
    STEAM / "modded" / "profile2" / "mods" / "DanmakuSpire",
    APPDATA / "DanmakuSpire",
]

print("=" * 78)
print("① object-catalog.json 里的对象类型分布")
cat = APPDATA / "DanmakuSpire" / "object-catalog.json"
if cat.is_file():
    data = json.loads(cat.read_text(encoding="utf-8-sig"))
    objs = data.get("objects") if isinstance(data, dict) else data
    kinds = Counter(o.get("kind") for o in objs if isinstance(o, dict))
    print(f"   共 {len(objs)} 个对象 · kind 分布: {dict(kinds)}")
    # 看看一个怪物条目长什么样
    monsters = [o for o in objs if isinstance(o, dict) and o.get("kind") == 3]
    print(f"   怪物条目 {len(monsters)} 个，样例:")
    for m in monsters[:3]:
        print("     ", json.dumps(m, ensure_ascii=False)[:300])
    # 有没有 hp 字段
    with_hp = [o for o in monsters if any(k for k in o if re.search(r"hp|health", str(k), re.I))]
    print(f"   带 hp 字段的怪物条目: {len(with_hp)}")
else:
    print("   不存在")

print()
print("=" * 78)
print("② 游戏目录下的数据文件")
for base in (_paths.game_dir(),
             _paths.workshop_dir()):
    if not base.is_dir():
        print(f"   {base} 不存在")
        continue
    print(f"   --- {base} ---")
    files = [p for p in base.rglob("*") if p.is_file()]
    by_ext = Counter(p.suffix.lower() for p in files)
    print(f"      共 {len(files)} 个文件 · 扩展名: {dict(by_ext.most_common(10))}")
    # 找 pck（Godot 资源包）
    pcks = [p for p in files if p.suffix.lower() == ".pck"]
    print(f"      .pck 资源包 {len(pcks)} 个（前 5）:")
    for p in pcks[:5]:
        print(f"         {p.relative_to(base)}  {p.stat().st_size // 1024} KB")

print()
print("=" * 78)
print("③ 在文本类文件里搜「怪物名 + HP」的结构")
patterns = re.compile(r'"(max_?hp|hp|health)"\s*:', re.I)
hits: list[tuple[Path, int]] = []
for base in (STEAM / "modded" / "profile2" / "mods", APPDATA / "DanmakuSpire"):
    if not base.is_dir():
        continue
    for p in base.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".json", ".tres", ".txt", ".csv"):
            continue
        if p.stat().st_size > 4 * 1024 * 1024:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        n = len(patterns.findall(text))
        if n:
            hits.append((p, n))
print(f"   命中文件 {len(hits)} 个:")
for p, n in sorted(hits, key=lambda x: -x[1])[:12]:
    print(f"      {n:>5} 处  {p}")

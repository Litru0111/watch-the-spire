import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""定向找 pck 里的怪物定义资源，并确认本地化文本里有没有数值。"""
import re
from collections import Counter
from pathlib import Path

PCK = _paths.pck_path()
blob = PCK.read_bytes()
paths = {m.group().decode("ascii", "ignore")
         for m in re.finditer(rb"res://[\x20-\x7e]{3,180}", blob)}

print("① 目录分布（前 25）")
dirs = Counter(p.split("/")[2] for p in paths if p.count("/") >= 2)
for d, n in dirs.most_common(25):
    print(f"   {d:<28} {n}")

print()
print("② 找「怪物定义」类资源（非贴图/动画/音频）")
defs = sorted(p for p in paths
              if re.search(r"monster|enemy|encounter", p, re.I)
              and not re.search(r"\.(png|ctex|mp3|ogg|wav|atlas|skel|import)$", p, re.I)
              and "/imported/" not in p and "/animations/" not in p)
print(f"   共 {len(defs)} 个（前 30）:")
for p in defs[:30]:
    print(f"      {p}")

print()
print("③ 本地化文件里有没有 HP 数值")
loc = sorted(p for p in paths if re.search(r"localization|locale|i18n|strings", p, re.I))
print(f"   本地化相关 {len(loc)} 个（前 10）:")
for p in loc[:10]:
    print(f"      {p}")

# 在 pck 文本里搜「HP」+ 数字的结构
print()
print("④ 搜形如 'MaxHp': 8 / hp = 8 的结构")
for pat, label in ((rb"[Mm]ax_?[Hh][Pp][\"']?\s*[:=]\s*\d+", "MaxHp: N"),
                   (rb"[Ss]tarting_?[Hh][Pp][\"']?\s*[:=]\s*\d+", "StartingHp: N"),
                   (rb"[Mm]onster_?[Hh][Pp][\"']?\s*[:=]\s*\d+", "MonsterHp: N")):
    hits = re.findall(pat, blob)
    print(f"   {label:<16} 命中 {len(hits)} 处  样例: {[h.decode('ascii','ignore') for h in hits[:5]]}")

# 怪物数值可能在 .NET 里的 sts2 数据文件（Godot 导出会把 C# 编到 pck 的 .godot/mono 下）
mono = sorted(p for p in paths if ".godot/mono" in p or p.endswith(".dll"))
print()
print(f"⑤ pck 里的 mono/dll 条目 {len(mono)} 个（前 10）:")
for p in mono[:10]:
    print(f"      {p}")

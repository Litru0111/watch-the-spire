import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""从 Godot 资源包（.pck）里挖怪物数值。

.pck 是二进制容器；先扫内部路径表，找怪物/遭遇相关资源，
再在怪物 ID 附近找 HP 相关的结构。
"""
import re
import struct
from pathlib import Path

PCK = _paths.pck_path()
print(f"pck 大小: {PCK.stat().st_size / 1024 / 1024:.0f} MB")

blob = PCK.read_bytes()
magic = blob[:4]
print(f"magic = {magic!r}")
if magic == b"GDPC":
    ver, major, minor = struct.unpack_from("<III", blob, 4)
    print(f"格式版本 = {ver} · Godot {major}.{minor}")
    n_files = struct.unpack_from("<I", blob, 16)[0]
    print(f"文件条目数 = {n_files}")

paths: set[str] = set()
for m in re.finditer(rb"res://[\x20-\x7e]{3,180}", blob):
    paths.add(m.group().decode("ascii", "ignore"))
print(f"扫到 {len(paths)} 个 res:// 路径")

for label, pat in (("怪物/遭遇", r"monster|enemy|encounter"), ("数值/属性", r"hp|health|stat")):
    sel = sorted(p for p in paths if re.search(pat, p, re.I))
    print(f"\n与「{label}」相关 {len(sel)} 个（前 20）:")
    for p in sel[:20]:
        print(f"   {p}")

print("\n在二进制里搜怪物 ID 附近的 HP 字样")
for mid in (b"CEREMONIAL_BEAST", b"BYGONE_EFFIGY", b"NIBBIT", b"MAWLER"):
    hits = [m.start() for m in re.finditer(re.escape(mid), blob)]
    print(f"   {mid.decode()}: 出现 {len(hits)} 次")
    for i in hits[:1]:
        chunk = blob[max(0, i - 100): i + 260]
        printable = "".join(chr(b) if 32 <= b < 127 else "·" for b in chunk)
        print(f"      …{printable[:340]}…")

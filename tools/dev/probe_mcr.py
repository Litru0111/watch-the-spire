"""尝试解析 latest.mcr（进行中对局的回放流），看能否拿到实时 HP。

格式线索（从文件头观察）：
    uint32 版本长度 + "v0.107.1"
    uint32 commit 长度 + "59260271"
    uint64 ? / uint32 段数 / 逐段长度 ...
推测是自描述的二进制序列（MessagePack / 打包结构）。
先做结构化扫描：打印头部整数序列、字符串表，找 "hp"/"current_hp"/"max_hp" 等键名。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

R = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
CANDS = sorted(R.rglob("*.mcr"), key=lambda p: p.stat().st_mtime, reverse=True)
if not CANDS:
    print("没找到 .mcr")
    sys.exit(0)
p = CANDS[0]
raw = p.read_bytes()
print(f"文件: {p}")
print(f"大小: {len(raw):,} 字节  修改: {p.stat().st_mtime}")

# ---------- 1) 找可打印字符串 ----------
import re
strings = re.findall(rb"[\x20-\x7e]{4,}", raw)
print(f"\n可打印字符串 {len(strings)} 段，前 30 段：")
for s in strings[:30]:
    print("   ", s.decode("ascii", "replace"))

# 关键：找 hp 相关键名
for kw in (b"hp", b"current_hp", b"max_hp", b"health", b"player", b"relic", b"deck", b"card"):
    hits = [m.start() for m in re.finditer(re.escape(kw), raw)]
    print(f"\n含 {kw!r} 的位置 {len(hits)} 处" + (f": {hits[:8]}" if hits else ""))

# ---------- 2) 头部结构 ----------
def read_len_prefixed(buf: bytes, off: int):
    if off + 4 > len(buf):
        return None, off
    n = struct.unpack_from("<I", buf, off)[0]
    if n > 512:
        return None, off
    s = buf[off+4:off+4+n]
    return s, off + 4 + n

off = 0
print("\n头部解析：")
for label in ("version", "commit"):
    s, off = read_len_prefixed(raw, off)
    print(f"   {label} = {s}")

print("   之后的 32 字节 hex:", raw[off:off+32].hex(" "))
print("   之后的整数序列:",
      [struct.unpack_from("<I", raw, off + i*4)[0] for i in range(8)])

# ---------- 3) 扫描可能的「整数对」区域，找像 {hp, max_hp} 的相邻值 ----------
# 我们先看整体可打印内容之外的数值分布
import collections
counts = collections.Counter()
for i in range(0, min(len(raw) - 4, 20000), 4):
    v = struct.unpack_from("<I", raw, i)[0]
    counts[v] += 1
print("\n前 20000 字节里出现最多的小整数 (<= 200)：")
for v, c in counts.most_common(40):
    if v <= 200:
        print(f"   {v} 出现 {c} 次")

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""探查 sts2.dll 的 .NET 元数据布局（为后续解析做准备）。

.NET 程序集结构（ECMA-335）：
    DOS 头 → PE 头 → 可选头（含 Data Directory）
    第 15 项 Data Directory = CLI 头（RVA）
    CLI 头 +0x08 = Metadata RVA → 元数据根
    元数据根：签名 BSJB + 版本串 + 流数量 + 各流的 (偏移, 大小, 名字)
    流：#~（表）/ #Strings / #US / #GUID / #Blob

这里只做布局侦察：确认各流位置、有哪些表、每张表多少行。
"""
from __future__ import annotations

import struct
from pathlib import Path

DLL = _paths.dll_path()


def u16(b: bytes, o: int) -> int:
    return struct.unpack_from("<H", b, o)[0]


def u32(b: bytes, o: int) -> int:
    return struct.unpack_from("<I", b, o)[0]


# .NET 表编号 → 名称（ECMA-335 §II.22）
TABLE_NAMES = {
    0x00: "Module", 0x01: "TypeRef", 0x02: "TypeDef", 0x03: "FieldPtr",
    0x04: "Field", 0x05: "MethodPtr", 0x06: "MethodDef", 0x07: "ParamPtr",
    0x08: "Param", 0x09: "InterfaceImpl", 0x0A: "MemberRef", 0x0B: "Constant",
    0x0C: "CustomAttribute", 0x0D: "FieldMarshal", 0x0E: "DeclSecurity",
    0x0F: "ClassLayout", 0x10: "FieldLayout", 0x11: "StandAloneSig",
    0x12: "EventMap", 0x13: "EventPtr", 0x14: "Event", 0x15: "PropertyMap",
    0x16: "PropertyPtr", 0x17: "Property", 0x18: "MethodSemantics",
    0x19: "MethodImpl", 0x1A: "ModuleRef", 0x1B: "TypeSpec",
    0x1C: "ImplMap", 0x1D: "FieldRVA", 0x1E: "ENCLog", 0x1F: "ENCMap",
    0x20: "Assembly", 0x21: "AssemblyProcessor", 0x22: "AssemblyOS",
    0x23: "AssemblyRef", 0x24: "AssemblyRefProcessor", 0x25: "AssemblyRefOS",
    0x26: "File", 0x27: "ExportedType", 0x28: "ManifestResource",
    0x29: "NestedClass", 0x2A: "GenericParam", 0x2B: "MethodSpec",
    0x2C: "GenericParamConstraint",
}


def main() -> int:
    b = DLL.read_bytes()
    print(f"{DLL.name}  {len(b) / 1048576:.1f} MB")

    # ---- DOS 头 ----
    e_lfanew = u32(b, 0x3C)
    assert b[e_lfanew:e_lfanew + 4] == b"PE\0\0", "不是 PE 文件"
    print(f"PE 头偏移 0x{e_lfanew:X}")

    coff = e_lfanew + 4
    n_sections = u16(b, coff + 2)
    opt_size = u16(b, coff + 16)
    print(f"节区数 {n_sections} · 可选头大小 {opt_size}")

    opt = coff + 20
    magic = u16(b, opt)
    is_pe32p = magic == 0x20B
    print(f"可选头 magic 0x{magic:X} ({'PE32+' if is_pe32p else 'PE32'})")

    # Data Directory 起点：PE32+ 是 +112，PE32 是 +96
    dd = opt + (112 if is_pe32p else 96)
    cli_rva, cli_size = u32(b, dd + 14 * 8), u32(b, dd + 14 * 8 + 4)
    print(f"CLI 头 RVA 0x{cli_rva:X} 大小 {cli_size}")

    # ---- 节区表（RVA → 文件偏移）----
    sec = opt + opt_size
    sections = []
    for i in range(n_sections):
        o = sec + i * 40
        name = b[o:o + 8].rstrip(b"\0").decode("ascii", "ignore")
        vsize, vaddr = u32(b, o + 8), u32(b, o + 12)
        rsize, raddr = u32(b, o + 16), u32(b, o + 20)
        sections.append((name, vaddr, vsize, raddr, rsize))
        print(f"  节 {name:<10} RVA 0x{vaddr:08X} 虚拟 {vsize:>10} 文件 0x{raddr:08X} {rsize:>10}")

    def rva2off(rva: int) -> int:
        for _n, va, vs, ra, rs in sections:
            if va <= rva < va + max(vs, rs):
                return ra + (rva - va)
        return -1

    cli = rva2off(cli_rva)
    md_rva = u32(b, cli + 8)
    md = rva2off(md_rva)
    print(f"元数据根 文件偏移 0x{md:X}")
    assert b[md:md + 4] == b"BSJB", f"签名不符: {b[md:md+4]!r}"
    print("签名 BSJB ✔")

    major, minor = u16(b, md + 4), u16(b, md + 6)
    _res, vlen = u32(b, md + 8), u32(b, md + 12)
    ver = b[md + 16:md + 16 + vlen].rstrip(b"\0").decode("ascii", "ignore")
    print(f"元数据版本 {major}.{minor} '{ver}'")

    p = md + 16 + vlen
    p += 2                                  # flags
    n_streams = u16(b, p)
    p += 2
    print(f"流数量 {n_streams}")

    streams: dict[str, tuple[int, int]] = {}
    for _ in range(n_streams):
        off, size = u32(b, p), u32(b, p + 4)
        p += 8
        end = b.index(b"\0", p)
        # 名字按 4 字节对齐
        name = b[p:end].decode("ascii", "ignore")
        p = end + 1
        p += (-p) % 4
        streams[name] = (md + off, size)
        print(f"  流 {name:<10} 偏移 0x{md+off:X} 大小 {size:>10}")

    # ---- #~ 表流概览 ----
    key = "#~" if "#~" in streams else "#-"
    toff, tsize = streams[key]
    print(f"\n表流 {key} 大小 {tsize}")
    reserved, tmajor, tminor, heap_sizes = (
        u32(b, toff), b[toff + 4], b[toff + 5], b[toff + 6])
    valid = struct.unpack_from("<Q", b, toff + 8)[0]
    sorted_ = struct.unpack_from("<Q", b, toff + 16)[0]
    print(f"  堆大小标志 0x{heap_sizes:02X} "
          f"(string={'4' if heap_sizes & 1 else '2'} "
          f"guid={'4' if heap_sizes & 2 else '2'} "
          f"blob={'4' if heap_sizes & 4 else '2'})")

    present = [i for i in range(64) if valid >> i & 1]
    print(f"  存在的表 {len(present)} 张")

    q = toff + 24
    rows: dict[int, int] = {}
    for t in present:
        rows[t] = u32(b, q)
        q += 4
    for t in present:
        nm = TABLE_NAMES.get(t, f"T{t}")
        if rows[t] and nm in ("TypeDef", "Field", "MethodDef", "Constant",
                              "Property", "Assembly", "Module", "NestedClass"):
            print(f"    {t:>3} {nm:<16} {rows[t]:>6} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

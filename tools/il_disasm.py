"""极简 IL 反汇编器：把方法体转成人类可读的指令列表。

用途：卡牌的数值写在 `get_CanonicalVars` 之类的构造函数里（形如
`new DamageVar(8)`），只有看指令流才能把它们对应起来。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dotnet_meta import Metadata  # noqa: E402

# 常用操作码（够看卡牌构造函数）
OPS: dict[int, str] = {
    0x00: "nop", 0x01: "break", 0x02: "ldarg.0", 0x03: "ldarg.1",
    0x04: "ldarg.2", 0x05: "ldarg.3", 0x06: "ldloc.0", 0x07: "ldloc.1",
    0x08: "ldloc.2", 0x09: "ldloc.3", 0x0A: "stloc.0", 0x0B: "stloc.1",
    0x0C: "stloc.2", 0x0D: "stloc.3", 0x0E: "ldarg.s", 0x0F: "ldarga.s",
    0x10: "starg.s", 0x11: "ldloc.s", 0x12: "ldloca.s", 0x13: "stloc.s",
    0x14: "ldnull", 0x15: "ldc.i4.m1", 0x16: "ldc.i4.0", 0x17: "ldc.i4.1",
    0x18: "ldc.i4.2", 0x19: "ldc.i4.3", 0x1A: "ldc.i4.4", 0x1B: "ldc.i4.5",
    0x1C: "ldc.i4.6", 0x1D: "ldc.i4.7", 0x1E: "ldc.i4.8", 0x1F: "ldc.i4.s",
    0x20: "ldc.i4", 0x21: "ldc.i8", 0x22: "ldc.r4", 0x23: "ldc.r8",
    0x25: "dup", 0x26: "pop", 0x27: "jmp", 0x28: "call", 0x29: "calli",
    0x2A: "ret", 0x2B: "br.s", 0x2C: "brfalse.s", 0x2D: "brtrue.s",
    0x2E: "beq.s", 0x2F: "bge.s", 0x30: "bgt.s", 0x31: "ble.s",
    0x32: "blt.s", 0x33: "bne.un.s", 0x37: "bne.un.s", 0x38: "br",
    0x39: "brfalse", 0x3A: "brtrue", 0x3B: "beq", 0x3C: "bge",
    0x3D: "bgt", 0x3E: "ble", 0x3F: "blt", 0x40: "bne.un",
    0x45: "switch", 0x46: "ldind.i1", 0x47: "ldind.u1", 0x48: "ldind.i2",
    0x49: "ldind.u2", 0x4A: "ldind.i4", 0x58: "add", 0x59: "sub",
    0x5A: "mul", 0x5B: "div", 0x5C: "div.un", 0x5D: "rem", 0x62: "and",
    0x63: "or", 0x64: "xor", 0x66: "not", 0x67: "shl", 0x68: "shr",
    0x6F: "callvirt", 0x70: "cpobj", 0x72: "ldstr", 0x73: "newobj",
    0x74: "castclass", 0x75: "isinst", 0x7B: "ldfld", 0x7C: "ldflda",
    0x7D: "stfld", 0x7E: "ldsfld", 0x7F: "ldsflda", 0x80: "stsfld",
    0x8C: "box", 0x8D: "newarr", 0x8E: "ldlen", 0x8F: "ldelema",
    0x90: "ldelem.i1", 0x91: "ldelem.u1", 0x94: "ldelem.i4",
    0x9C: "stelem.i4", 0xA2: "stelem.ref", 0xA3: "ldelem",
    0xA4: "stelem", 0xA5: "unbox.any", 0xD0: "ldtoken",
    0xDD: "leave", 0xDE: "leave.s", 0xFE: "prefix",
}
PREFIX_OPS = {0x01: "ceq", 0x02: "cgt", 0x04: "clt", 0x06: "ldftn",
              0x09: "ldarg", 0x0A: "ldarga", 0x0B: "starg", 0x0C: "ldloc",
              0x0D: "ldloca", 0x0E: "stloc", 0x15: "initobj", 0x16: "constrained"}


def disasm(m: Metadata, mi: int, *, max_ins: int = 200) -> list[str]:
    """把方法体反汇编成文本行。"""
    il = m.method_body(mi)
    out: list[str] = []
    p = 0
    k = 0
    while p < len(il) and k < max_ins:
        k += 1
        op = il[p]
        ln = m._il_operand_size(il, p)
        arg = il[p + 1:p + ln]
        txt = ""
        if op == 0xFE and arg:
            txt = PREFIX_OPS.get(arg[0], f"prefix_0x{arg[0]:02X}")
            if arg[0] in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E) and len(arg) >= 3:
                txt += f" {int.from_bytes(arg[1:3], 'little')}"
        elif op == 0x20 and len(arg) >= 4:
            txt = str(struct.unpack_from("<i", arg, 0)[0])
        elif op == 0x21 and len(arg) >= 8:
            txt = str(struct.unpack_from("<q", arg, 0)[0])
        elif op in (0x1F, 0x0E, 0x10, 0x11, 0x12, 0x13) and arg:
            txt = str(struct.unpack_from("<b", arg, 0)[0])
        elif op in (0x72, 0x28, 0x6F, 0x73, 0x7B, 0x7D, 0x7E, 0x80, 0x8C,
                    0x74, 0x75, 0xA5, 0xD0) and len(arg) >= 4:
            tok = struct.unpack_from("<I", arg, 0)[0]
            tbl = {0x01: "TypeRef", 0x02: "TypeDef", 0x04: "Field",
                   0x06: "MethodDef", 0x0A: "MemberRef", 0x1B: "TypeSpec",
                   0x70: "UserString", 0x11: "StandAloneSig"}.get(tok >> 24,
                                                                  f"0x{tok>>24:02X}")
            idx = tok & 0xFFFFFF
            txt = f"{tbl}[{idx}]"
            nm = resolve_token(m, tok)
            if nm:
                txt += f" {nm}"
        elif op in (0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33,
                    0x37, 0xDE) and arg:
            txt = f"IL_{p + ln + struct.unpack_from('<b', arg, 0)[0]:04X}"
        elif op in (0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0xDD) \
                and len(arg) >= 4:
            txt = f"IL_{p + ln + struct.unpack_from('<i', arg, 0)[0]:04X}"
        out.append(f"{p:>4}: {OPS.get(op, f'op_0x{op:02X}'):<12} {txt}".rstrip())
        p += ln
    return out


def resolve_token(m: Metadata, tok: int) -> str:
    """把一个元数据 token 解析成可读名字。"""
    tag, idx = tok >> 24, (tok & 0xFFFFFF) - 1
    try:
        if tag == 0x70:                                   # 用户字符串
            off, _ = m.streams["#US"]
            p = off + (tok & 0xFFFFFF)
            b0 = m.b[p]
            if b0 & 0x80 == 0:
                n, p2 = b0, p + 1
            elif b0 & 0xC0 == 0x80:
                n, p2 = ((b0 & 0x3F) << 8) | m.b[p + 1], p + 2
            else:
                n = ((b0 & 0x1F) << 24) | (m.b[p + 1] << 16) | \
                    (m.b[p + 2] << 8) | m.b[p + 3]
                p2 = p + 4
            return '"' + m.b[p2:p2 + n].decode("utf-16-le", "ignore") + '"'
        if tag == 0x0A:                                   # MemberRef
            r = m.read_row("MemberRef", idx)
            cls_t, cls_i = m.coded("MemberRefParent", r["Class"])
            cls = ""
            if cls_t == "TypeRef":
                rr = m.read_row("TypeRef", cls_i)
                ns = m.string(rr["Namespace"])
                cls = f"{ns}.{m.string(rr['Name'])}" if ns else m.string(rr["Name"])
            elif cls_t in ("TypeDef", "TypeSpec"):
                cls = m._type_fullname(cls_i) if cls_t == "TypeDef" else "TypeSpec"
            nmm = m.string(r["Name"])
            return f"{cls}::{nmm}" if cls else nmm
        if tag == 0x06:                                   # MethodDef
            r = m.read_row("MethodDef", idx)
            # 方法名不够用（.ctor 到处都有），补上所属类型
            owner = ""
            for t in m.typedefs():
                if t.method_start <= idx < t.method_end:
                    owner = t.full_name
                    break
            nmm = m.string(r["Name"])
            return f"{owner}::{nmm}" if owner else nmm
        if tag == 0x04:                                   # Field
            r = m.read_row("Field", idx)
            return m.string(r["Name"])
        if tag == 0x01:                                   # TypeRef
            r = m.read_row("TypeRef", idx)
            ns = m.string(r["Namespace"])
            return f"{ns}.{m.string(r['Name'])}" if ns else m.string(r["Name"])
        if tag == 0x02:                                   # TypeDef
            r = m.read_row("TypeDef", idx)
            return m.string(r["Name"])
    except Exception:
        return ""
    return ""


if __name__ == "__main__":
    import os
    from pathlib import Path as _P

    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from sts2mon import paths  # noqa: E402

    _dll = os.environ.get("STS2_DLL") or paths.dll_path()
    if not _dll or not _P(_dll).is_file():
        print("找不到 sts2.dll。用 STS2_DLL 指定，或设 STS2MON_GAME 指向游戏目录。")
        raise SystemExit(2)
    m = Metadata(str(_dll))
    want = sys.argv[1] if len(sys.argv) > 1 else "Bash"
    meth = sys.argv[2] if len(sys.argv) > 2 else "get_CanonicalVars"
    t = next((x for x in m.typedefs() if x.name == want), None)
    if not t:
        print(f"没找到类型 {want}")
        raise SystemExit(1)
    for mi in range(t.method_start, t.method_end):
        nm = m.string(m.read_row("MethodDef", mi)["Name"])
        if nm != meth:
            continue
        print(f"=== {t.full_name}::{nm} ===")
        for line in disasm(m, mi):
            print("  " + line)

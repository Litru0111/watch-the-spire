"""纯 Python 的 .NET 程序集元数据读取器（ECMA-335）。

为什么不用现成工具：本机没有 .NET SDK、没有 PowerShell 7、没有 NuGet 缓存，
PowerShell 5.1 宿主是 .NET Framework 4.8、装不下 .NET 9 的程序集。
所以直接按 ECMA-335 解析 PE → 元数据 → 表/堆，零依赖。

能力：
* 读类型、字段、方法、属性、常量
* 读静态字段的**编译期常量初值**（Constant 表 / 字段 RVA）
* 读方法体的 **IL 字节码**，提取其中的数值常量（卡牌数值常写成 `ldc.i4 N`）
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- 表定义
T = {
    "Module": 0x00, "TypeRef": 0x01, "TypeDef": 0x02, "FieldPtr": 0x03,
    "Field": 0x04, "MethodPtr": 0x05, "MethodDef": 0x06, "ParamPtr": 0x07,
    "Param": 0x08, "InterfaceImpl": 0x09, "MemberRef": 0x0A, "Constant": 0x0B,
    "CustomAttribute": 0x0C, "FieldMarshal": 0x0D, "DeclSecurity": 0x0E,
    "ClassLayout": 0x0F, "FieldLayout": 0x10, "StandAloneSig": 0x11,
    "EventMap": 0x12, "EventPtr": 0x13, "Event": 0x14, "PropertyMap": 0x15,
    "PropertyPtr": 0x16, "Property": 0x17, "MethodSemantics": 0x18,
    "MethodImpl": 0x19, "ModuleRef": 0x1A, "TypeSpec": 0x1B, "ImplMap": 0x1C,
    "FieldRVA": 0x1D, "ENCLog": 0x1E, "ENCMap": 0x1F, "Assembly": 0x20,
    "AssemblyProcessor": 0x21, "AssemblyOS": 0x22, "AssemblyRef": 0x23,
    "AssemblyRefProcessor": 0x24, "AssemblyRefOS": 0x25, "File": 0x26,
    "ExportedType": 0x27, "ManifestResource": 0x28, "NestedClass": 0x29,
    "GenericParam": 0x2A, "MethodSpec": 0x2B, "GenericParamConstraint": 0x2C,
}
TNAME = {v: k for k, v in T.items()}

# 每张表的列结构。列名必须保留 —— TypeDef 有 Name/Namespace 两个字符串列，
# 不带名字就分不清哪个是哪个。
#   c<n>  定长 n 字节
#   s/g/b 字符串/guid/blob 堆索引（宽度由堆大小标志决定）
#   t:X   指向表 X 的索引
#   cs:X  指向表 X 的索引，其宽度决定「紧随其后同表索引」的宽度
#   cd:X  编码索引（编码名 X）
SCHEMA: dict[str, list[tuple[str, str]]] = {
    "Module": [("Generation", "c2"), ("Name", "s"), ("Mvid", "g"),
               ("EncId", "g"), ("EncBaseId", "g")],
    "TypeRef": [("ResolutionScope", "cd:ResolutionScope"), ("Name", "s"),
                ("Namespace", "s")],
    "TypeDef": [("Flags", "c4"), ("Name", "s"), ("Namespace", "s"),
                ("Extends", "cd:TypeDefOrRef"), ("FieldList", "cs:Field"),
                ("MethodList", "cs:MethodDef")],
    "Field": [("Flags", "c2"), ("Name", "s"), ("Signature", "b")],
    "MethodDef": [("RVA", "c4"), ("ImplFlags", "c2"), ("Flags", "c2"),
                  ("Name", "s"), ("Signature", "b"), ("ParamList", "cs:Param")],
    "Param": [("Flags", "c2"), ("Sequence", "c2"), ("Name", "s")],
    "InterfaceImpl": [("Class", "t:TypeDef"), ("Interface", "cd:TypeDefOrRef")],
    "MemberRef": [("Class", "cd:MemberRefParent"), ("Name", "s"),
                  ("Signature", "b")],
    "Constant": [("Type", "c1"), ("Padding", "c1"), ("Parent", "cd:HasConstant"),
                 ("Value", "b")],
    "CustomAttribute": [("Parent", "cd:HasCustomAttribute"),
                        ("Type", "cd:CustomAttributeType"), ("Value", "b")],
    "FieldMarshal": [("Parent", "cd:HasFieldMarshal"), ("NativeType", "b")],
    "DeclSecurity": [("Action", "c2"), ("Parent", "cd:HasDeclSecurity"),
                     ("PermissionSet", "b")],
    "ClassLayout": [("PackingSize", "c2"), ("ClassSize", "c4"),
                    ("Parent", "t:TypeDef")],
    "FieldLayout": [("Offset", "c4"), ("Field", "t:Field")],
    "StandAloneSig": [("Signature", "b")],
    "EventMap": [("Parent", "t:TypeDef"), ("EventList", "cs:Event")],
    "Event": [("EventFlags", "c2"), ("Name", "s"), ("EventType", "cd:TypeDefOrRef")],
    "PropertyMap": [("Parent", "t:TypeDef"), ("PropertyList", "cs:Property")],
    "Property": [("Flags", "c2"), ("Name", "s"), ("Type", "b")],
    "MethodSemantics": [("Semantics", "c2"), ("Method", "t:MethodDef"),
                        ("Association", "cd:HasSemantics")],
    "MethodImpl": [("Class", "t:TypeDef"),
                   ("MethodBody", "cd:MethodDefOrRef"),
                   ("MethodDeclaration", "cd:MethodDefOrRef")],
    "ModuleRef": [("Name", "s")],
    "TypeSpec": [("Signature", "b")],
    "ImplMap": [("MappingFlags", "c2"), ("MemberForwarded", "cd:MemberForwarded"),
                ("ImportName", "s"), ("ImportScope", "t:ModuleRef")],
    "FieldRVA": [("RVA", "c4"), ("Field", "t:Field")],
    "Assembly": [("HashAlgId", "c4"), ("MajorVersion", "c2"), ("MinorVersion", "c2"),
                 ("BuildNumber", "c2"), ("RevisionNumber", "c2"), ("Flags", "c4"),
                 ("PublicKey", "b"), ("Name", "s"), ("Culture", "s")],
    "AssemblyRef": [("MajorVersion", "c2"), ("MinorVersion", "c2"),
                    ("BuildNumber", "c2"), ("RevisionNumber", "c2"), ("Flags", "c4"),
                    ("PublicKeyOrToken", "b"), ("Name", "s"), ("Culture", "s"),
                    ("HashValue", "b")],
    "File": [("Flags", "c4"), ("Name", "s"), ("HashValue", "b")],
    "ExportedType": [("Flags", "c4"), ("TypeDefId", "c4"), ("TypeName", "s"),
                     ("TypeNamespace", "s"), ("Implementation", "cd:Implementation")],
    "ManifestResource": [("Offset", "c4"), ("Flags", "c4"), ("Name", "s"),
                         ("Implementation", "cd:Implementation")],
    "NestedClass": [("NestedClass", "t:TypeDef"), ("EnclosingClass", "t:TypeDef")],
    "GenericParam": [("Number", "c2"), ("Flags", "c2"),
                     ("Owner", "cd:TypeOrMethodDef"), ("Name", "s")],
    "MethodSpec": [("Method", "cd:MethodDefOrRef"), ("Instantiation", "b")],
    "GenericParamConstraint": [("Owner", "t:GenericParam"),
                               ("Constraint", "cd:TypeDefOrRef")],
}

# 编码索引：(用到的表, 标签位宽)
CODED = {
    "TypeDefOrRef": (["TypeDef", "TypeRef", "TypeSpec"], 2),
    "HasConstant": (["Field", "Param", "Property"], 2),
    "HasCustomAttribute": ([
        "MethodDef", "Field", "TypeRef", "TypeDef", "Param", "InterfaceImpl",
        "MemberRef", "Module", "DeclSecurity", "Property", "Event", "StandAloneSig",
        "ModuleRef", "TypeSpec", "Assembly", "AssemblyRef", "File", "ExportedType",
        "ManifestResource", "GenericParam", "GenericParamConstraint", "MethodSpec"], 5),
    "HasFieldMarshal": (["Field", "Param"], 1),
    "HasDeclSecurity": (["TypeDef", "MethodDef", "Assembly"], 2),
    "MemberRefParent": (["TypeDef", "TypeRef", "ModuleRef", "MethodDef", "TypeSpec"], 3),
    "HasSemantics": (["Event", "Property"], 1),
    "MethodDefOrRef": (["MethodDef", "MemberRef"], 1),
    "MemberForwarded": (["Field", "MethodDef"], 1),
    "Implementation": (["File", "AssemblyRef", "ExportedType"], 2),
    "CustomAttributeType": (["<unused>", "<unused>", "MethodDef", "MemberRef",
                             "<unused>"], 3),
    "ResolutionScope": (["Module", "ModuleRef", "AssemblyRef", "TypeRef"], 2),
    "TypeOrMethodDef": (["TypeDef", "MethodDef"], 1),
}


@dataclass
class TypeInfo:
    index: int
    name: str
    namespace: str
    full_name: str
    flags: int
    field_start: int
    field_end: int
    method_start: int
    method_end: int
    base_type: str = ""
    nested_in: str = ""
    fields: list["FieldInfo"] = field(default_factory=list)
    properties: list[str] = field(default_factory=list)

    @property
    def is_public(self) -> bool:
        return bool(self.flags & 0x1)

    @property
    def is_abstract(self) -> bool:
        return bool(self.flags & 0x80)


@dataclass
class FieldInfo:
    index: int
    name: str
    flags: int
    signature: bytes
    constant: object = None
    rva: int = 0


class Metadata:
    """只读的 .NET 元数据视图。"""

    def __init__(self, path: str | Path) -> None:
        self.b = Path(path).read_bytes()
        # 惰性缓存：列布局与类型表。两者在一次进程内都不变，
        # 而调用方会反复取（实测 read_row 337 万次、typedefs 数十次），
        # 不缓存会让提取从几秒变成几分钟。
        self._plans: dict[str, list[tuple[str, int]]] = {}
        self._typedefs: list[TypeInfo] | None = None
        self._parse_pe()
        self._parse_tables()

    # ------------------------------------------------------------ PE
    def _u16(self, o: int) -> int:
        return struct.unpack_from("<H", self.b, o)[0]

    def _u32(self, o: int) -> int:
        return struct.unpack_from("<I", self.b, o)[0]

    def _parse_pe(self) -> None:
        b = self.b
        e = self._u32(0x3C)
        coff = e + 4
        n_sec, opt_size = self._u16(coff + 2), self._u16(coff + 16)
        opt = coff + 20
        dd = opt + (112 if self._u16(opt) == 0x20B else 96)
        cli_rva = self._u32(dd + 14 * 8)

        self.sections = []
        sec = opt + opt_size
        for i in range(n_sec):
            o = sec + i * 40
            self.sections.append((
                self._u32(o + 12), self._u32(o + 8),   # vaddr, vsize
                self._u32(o + 20), self._u32(o + 16)))  # raddr, rsize

        md = self.rva2off(self._u32(self.rva2off(cli_rva) + 8))
        assert b[md:md + 4] == b"BSJB", "元数据签名不符"
        vlen = self._u32(md + 12)
        p = md + 16 + vlen + 2
        n_streams = self._u16(p)
        p += 2
        self.streams: dict[str, tuple[int, int]] = {}
        for _ in range(n_streams):
            off, size = self._u32(p), self._u32(p + 4)
            p += 8
            end = b.index(b"\0", p)
            name = b[p:end].decode("ascii", "ignore")
            p = end + 1
            p += (-p) % 4
            self.streams[name] = (md + off, size)

    def rva2off(self, rva: int) -> int:
        for va, vs, ra, rs in self.sections:
            if va <= rva < va + max(vs, rs):
                return ra + (rva - va)
        return -1

    # ------------------------------------------------------------ 堆
    def string(self, idx: int) -> str:
        if not idx:
            return ""
        o, _ = self.streams["#Strings"]
        end = self.b.index(b"\0", o + idx)
        return self.b[o + idx:end].decode("utf-8", "ignore")

    def blob(self, idx: int) -> bytes:
        if not idx:
            return b""
        o, _ = self.streams["#Blob"]
        p = o + idx
        b0 = self.b[p]
        if b0 & 0x80 == 0:
            n, p = b0, p + 1
        elif b0 & 0xC0 == 0x80:
            n, p = ((b0 & 0x3F) << 8) | self.b[p + 1], p + 2
        else:
            n = ((b0 & 0x1F) << 24) | (self.b[p + 1] << 16) | \
                (self.b[p + 2] << 8) | self.b[p + 3]
            p += 4
        return self.b[p:p + n]

    # ------------------------------------------------------------ 表
    def _parse_tables(self) -> None:
        key = "#~" if "#~" in self.streams else "#-"
        toff, _ = self.streams[key]
        b = self.b
        heap = b[toff + 6]
        self.str4 = bool(heap & 1)
        self.guid4 = bool(heap & 2)
        self.blob4 = bool(heap & 4)
        valid = struct.unpack_from("<Q", b, toff + 8)[0]
        present = [i for i in range(64) if valid >> i & 1]
        p = toff + 24
        self.row_counts: dict[int, int] = {}
        for t in present:
            self.row_counts[t] = self._u32(p)
            p += 4
        self.tables: dict[str, int] = {TNAME[t]: n for t, n in self.row_counts.items()}
        self.table_off: dict[str, int] = {}
        for t in present:
            nm = TNAME[t]
            self.table_off[nm] = p
            p += self.row_counts[t] * self._row_size(nm)

    def _idx_size(self, table: str) -> int:
        n = self.tables.get(table, 0)
        return 4 if n >= 0x10000 else 2

    def _coded_size(self, name: str) -> int:
        tables, bits = CODED[name]
        mx = max((self.tables.get(t, 0) for t in tables), default=0)
        return 4 if mx >= (1 << (16 - bits)) else 2

    @staticmethod
    def _split_spec(spec: str) -> tuple[str, str]:
        """拆列规格 → (kind, arg)。

        `c2` → ('c','2') · `s` → ('s','') · `cd:HasConstant` → ('cd','HasConstant')
        `cs:Field` → ('cs','Field') · `t:TypeDef` → ('t','TypeDef')

        注意两位的 kind（cd/cs）不带数字，所以不能用「取首字符」的写法。
        """
        if len(spec) >= 2 and spec[1] == ":":
            return spec[:1], spec[2:]
        if len(spec) >= 3 and spec[2] == ":":
            return spec[:2], spec[3:]
        if len(spec) >= 2 and spec[1].isdigit():
            return spec[:1], spec[1:]
        return spec, ""

    def _row_size(self, table: str) -> int:
        total = 0
        for _name, spec in SCHEMA[table]:
            kind, arg = self._split_spec(spec)
            if kind == "c":
                total += int(arg)
            elif kind == "s":
                total += 4 if self.str4 else 2
            elif kind == "g":
                total += 4 if self.guid4 else 2
            elif kind == "b":
                total += 4 if self.blob4 else 2
            elif kind in ("t", "cs"):
                total += self._idx_size(arg)
            elif kind == "cd":
                total += self._coded_size(arg)
            else:
                raise ValueError(f"未知列类型 {spec!r}")
        return total

    def _col_plan(self, table: str) -> list[tuple[str, int]]:
        """表的列布局：[(列名, 字节数)]，**算一次缓存**起来。

        `read_row` 被调用数百万次（一次提取实测 337 万次），而原来每次都要
        对每列重新 `_split_spec` 解析规格字符串并重算字节宽度 —— 光是这一项
        就占了提取耗时的大头。列宽只取决于堆尺寸标志（str4/guid4/…），
        构造后就固定不变，所以缓存是安全的。
        """
        plan = self._plans.get(table)
        if plan is None:
            plan = []
            for name, spec in SCHEMA[table]:
                kind, arg = self._split_spec(spec)
                if kind == "c":
                    n = int(arg)
                elif kind == "s":
                    n = 4 if self.str4 else 2
                elif kind == "g":
                    n = 4 if self.guid4 else 2
                elif kind == "b":
                    n = 4 if self.blob4 else 2
                elif kind in ("t", "cs"):
                    n = self._idx_size(arg)
                else:                       # cd
                    n = self._coded_size(arg)
                plan.append((name, n))
            self._plans[table] = plan
        return plan

    def read_row(self, table: str, i: int) -> dict[str, int]:
        """读第 i 行（0 基），返回 {列名: 原始值}。"""
        off = self.table_off[table] + i * self._row_size(table)
        b = self.b
        out: dict[str, int] = {}
        for name, n in self._col_plan(table):
            out[name] = int.from_bytes(b[off:off + n], "little")
            off += n
        return out

    def row_count(self, table: str) -> int:
        return self.tables.get(table, 0)

    def rows(self, table: str):
        for i in range(self.row_count(table)):
            yield self.read_row(table, i)

    def coded(self, name: str, value: int) -> tuple[str, int]:
        """解码一个编码索引 → (表名, 行号 0 基)。"""
        tables, bits = CODED[name]
        tag = value & ((1 << bits) - 1)
        idx = value >> bits
        if tag >= len(tables) or tables[tag].startswith("<"):
            return "", 0
        return tables[tag], idx - 1

    # ------------------------------------------------------------ 高层接口
    def _type_fullname(self, i: int) -> str:
        """TypeDef 第 i 行（0 基）的全名。"""
        if i < 0 or i >= self.row_count("TypeDef"):
            return ""
        r = self.read_row("TypeDef", i)
        ns = self.string(r["Namespace"])
        nm = self.string(r["Name"])
        return f"{ns}.{nm}" if ns else nm

    def typedefs(self) -> list[TypeInfo]:
        """所有类型定义（含嵌套关系与基类）。

        **结果缓存**：这个函数要遍历 9410 个 TypeDef 并补齐每类型的字段表，
        单次约 0.9 秒。调用方（如 `extract_values.var_name`）会在循环里
        反复调它 —— 实测 50 次调用累计 43 秒，占提取总耗时的绝大部分。
        类型表在一次进程内不会变，所以缓存是安全的。
        """
        if self._typedefs is not None:
            return self._typedefs
        self._typedefs = self._build_typedefs()
        return self._typedefs

    def _build_typedefs(self) -> list[TypeInfo]:
        """所有类型定义（含嵌套关系与基类）。"""
        n = self.row_count("TypeDef")
        raw = [self.read_row("TypeDef", i) for i in range(n)]
        # 嵌套：NestedClass 给 (嵌套类, 外层类)
        nested: dict[int, int] = {}
        for r in self.rows("NestedClass"):
            nested[r["NestedClass"] - 1] = r["EnclosingClass"] - 1

        # 字段/方法按「起始索引」界定区间：第 i 个类型的字段区间是
        # [FieldList_i, FieldList_{i+1})，最后一个到表尾
        fstart = [r["FieldList"] - 1 for r in raw] + [self.row_count("Field")]
        mstart = [r["MethodList"] - 1 for r in raw] + [self.row_count("MethodDef")]

        out: list[TypeInfo] = []
        for i, r in enumerate(raw):
            ns = self.string(r["Namespace"])
            nm = self.string(r["Name"])
            base_tbl, base_idx = self.coded("TypeDefOrRef", r["Extends"])
            base = self._type_fullname(base_idx) if base_tbl == "TypeDef" else ""
            t = TypeInfo(
                index=i, name=nm, namespace=ns,
                full_name=f"{ns}.{nm}" if ns else nm,
                flags=r["Flags"],
                field_start=fstart[i], field_end=fstart[i + 1],
                method_start=mstart[i], method_end=mstart[i + 1],
                base_type=base)
            if i in nested:
                t.nested_in = self._type_fullname(nested[i])
            out.append(t)

        # 常量表：编码索引 → (表, 行)
        for r in self.rows("Constant"):
            tbl, idx = self.coded("HasConstant", r["Parent"])
            if tbl != "Field":
                continue
            # 找到该字段属于哪个类型
            for t in out:
                if t.field_start <= idx < t.field_end:
                    while len(t.fields) < (idx - t.field_start + 1):
                        fi = t.field_start + len(t.fields)
                        t.fields.append(self._field_info(fi))
                    t.fields[idx - t.field_start].constant = \
                        self._const_value(r["Type"], r["Value"])
                    break

        # 补齐没有常量的字段
        for t in out:
            for fi in range(t.field_start, t.field_end):
                k = fi - t.field_start
                if k >= len(t.fields):
                    t.fields.append(self._field_info(fi))
                elif not t.fields[k].name:
                    t.fields[k] = self._field_info(fi)
        return out

    def _field_info(self, i: int) -> FieldInfo:
        if i < 0 or i >= self.row_count("Field"):
            return FieldInfo(index=i, name="", flags=0, signature=b"")
        r = self.read_row("Field", i)
        return FieldInfo(index=i, name=self.string(r["Name"]),
                         flags=r["Flags"], signature=self.blob(r["Signature"]))

    def _const_value(self, etype: int, bidx: int) -> object:
        """Constant 表的值（EType 见 ECMA-335 §II.23.1.16）。"""
        blob = self.blob(bidx)
        try:
            if etype in (0x04,):                      # I4
                return struct.unpack_from("<i", blob, 0)[0]
            if etype in (0x05,):                      # I8
                return struct.unpack_from("<q", blob, 0)[0]
            if etype in (0x06,):                      # R4
                return struct.unpack_from("<f", blob, 0)[0]
            if etype in (0x07,):                      # R8
                return struct.unpack_from("<d", blob, 0)[0]
            if etype in (0x08,):                      # String（UTF-16）
                return blob.decode("utf-16-le", "ignore")
            if etype in (0x02,):                      # Boolean
                return bool(blob[0])
            if etype in (0x03,):                      # Char
                return blob.decode("utf-16-le", "ignore")
            if etype in (0x11,):                      # U1
                return blob[0]
            if etype in (0x12,):                      # U2
                return struct.unpack_from("<H", blob, 0)[0]
            if etype in (0x13,):                      # U4
                return struct.unpack_from("<I", blob, 0)[0]
            if etype in (0x14,):                      # U8
                return struct.unpack_from("<Q", blob, 0)[0]
        except Exception:
            return None
        return None

    def method_body(self, i: int) -> bytes:
        """MethodDef 第 i 行的 IL 字节码（不含方法头）。"""
        if i < 0 or i >= self.row_count("MethodDef"):
            return b""
        r = self.read_row("MethodDef", i)
        rva = r["RVA"]
        if not rva:
            return b""
        off = self.rva2off(rva)
        if off < 0:
            return b""
        b = self.b
        b0 = b[off]
        if b0 & 3 == 2:                     # tiny 头：1 字节，长度在高 6 位
            size = b0 >> 2
            return b[off + 1:off + 1 + size]
        # fat 头：12 字节
        flags_size = struct.unpack_from("<H", b, off)[0]
        hsize = (flags_size >> 12) * 4
        code_size = struct.unpack_from("<I", b, off + 4)[0]
        return b[off + hsize:off + hsize + code_size]

    def il_int_constants(self, i: int, *, limit: int = 400) -> list[int]:
        """从方法的 IL 里提取整数常量（`ldc.i4 N` 家族）。

        卡牌数值常直接写成 `ldc.i4 6`。这个方法给出「按出现顺序的常量列表」，
        由调用方结合字段名等上下文判断哪个是基础伤害。
        """
        il = self.method_body(i)
        out: list[int] = []
        p = 0
        n = len(il)
        while p < n and len(out) < limit:
            op = il[p]
            if op == 0x20 and p + 4 < n:                    # ldc.i4
                out.append(struct.unpack_from("<i", il, p + 1)[0])
                p += 5
                continue
            if op == 0x21 and p + 8 < n:                    # ldc.i8
                out.append(struct.unpack_from("<q", il, p + 1)[0])
                p += 9
                continue
            if op == 0x22 and p + 4 < n:                    # ldc.r4
                out.append(struct.unpack_from("<f", il, p + 1)[0])
                p += 5
                continue
            if op == 0x23 and p + 8 < n:                    # ldc.r8
                out.append(struct.unpack_from("<d", il, p + 1)[0])
                p += 9
                continue
            if 0x16 <= op <= 0x1E:                          # ldc.i4.0 .. ldc.i4.8
                out.append(op - 0x16)
                p += 1
                continue
            if op == 0x15:                                  # ldc.i4.m1
                out.append(-1)
                p += 1
                continue
            p += self._il_operand_size(il, p)
        return out

    # ------------------------------------------------------------ IL 指令长度
    # 指令操作数长度表（ECMA-335 §III）。扫描 IL 必须先知道每条指令多长。
    # 只覆盖「有操作数」及会影响扫描位置的部分，其余按无操作数 1 字节算。
    _OP1: dict[int, int] = {}

    @classmethod
    def _build_op_table(cls) -> None:
        if cls._OP1:
            return
        t: dict[int, int] = {}
        # 1 字节操作数：短跳转 / 短参数 / 短变量 / 短整数
        for op in (0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x1F,
                   0x2B, 0x2C, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33,
                   0x34, 0x35, 0x36, 0x37, 0xDE):
            t[op] = 1
        # 4 字节操作数：token / 长跳转 / 长参数 / 长变量
        for op in (0x20, 0x22, 0x27, 0x28, 0x29, 0x38, 0x39, 0x3A, 0x3B,
                   0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44,
                   0x45, 0x6F, 0x70, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76,
                   0x77, 0x78, 0x79, 0x7A, 0x7B, 0x7C, 0x7D, 0x7E, 0x7F,
                   0x80, 0x81, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88,
                   0x89, 0x8A, 0x8B, 0x8C, 0x8D, 0x8E, 0x8F,
                   0xA3, 0xA4, 0xA5, 0xC2, 0xC6, 0xD0, 0xDD):
            t[op] = 4
        t[0x21] = 8        # ldc.i8
        t[0x23] = 8        # ldc.r8
        t[0xFE] = 0        # 前缀，交给 _il_operand_size2
        cls._OP1 = t

    def _il_operand_size(self, il: bytes, p: int) -> int:
        """从 p 开始的指令「总长度」（含操作码本身）。"""
        self._build_op_table()
        op = il[p]
        if op == 0xFE:                                  # 两字节操作码
            if p + 1 >= len(il):
                return 1
            op2 = il[p + 1]
            # ldarg/ldloc/starg/stloc 系列带 2 字节操作数；其余无
            two = set(range(0x06, 0x17)) | {0x0E}
            if op2 in two:
                return 4
            if op2 in (0x09, 0x0C, 0x15):               # ldarg/ldloc 短形式等
                return 3
            if op2 in (0x1C, 0x1D):                     # 带 token 的
                return 6
            return 2
        if op == 0x45:                                  # switch
            if p + 4 >= len(il):
                return 1
            n = struct.unpack_from("<I", il, p + 1)[0]
            return 1 + 4 + 4 * min(n, 4096)
        return 1 + self._OP1.get(op, 0)

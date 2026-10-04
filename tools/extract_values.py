"""从 sts2.dll 提取卡牌/遗物的**数值**（基础伤害、格挡、能力层数…）。

原理：卡牌的数值不在字段里，而是在 `get_CanonicalVars` 属性里构造
`DynamicVar` 对象。对应的 IL 形态很规整：

    ldc.i4.8                        # 数值 8
    newobj  ...DamageVar::.ctor     # → DamageVar(8)
    stelem.ref
    ldc.i4.2
    newobj  ...VulnerablePower::.ctor
    stelem.ref

所以只要反汇编 `get_CanonicalVars`，按「常数 → 变量构造」配对即可拿到准确数值。

输出：.sts2mon/card_values.json
    {"cards": {CARD_ID: {"Damage": 8, "VulnerablePower": 2, ...}}, ...}
"""
from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

from dotnet_meta import Metadata  # noqa: E402
from sts2mon import paths  # noqa: E402

# 走 paths 自动探测，不再写死盘符
DLL = paths.dll_path()
OUT = paths.data_dir() / "card_values.json"

VAR_METHOD = "get_CanonicalVars"
# 变量类型的短名（去掉命名空间）
VAR_TYPES = {
    "DamageVar", "BlockVar", "CardsVar", "EnergyVar", "GoldVar", "HealVar",
    "HpLossVar", "MaxHpVar", "RepeatVar", "ForgeVar", "StarsVar",
    "CalculatedDamageVar", "CalculatedBlockVar", "ExtraDamageVar",
    "OstyDamageVar", "PowerVar", "CalculatedVar", "IntVar", "BoolVar",
    "IfUpgradedVar", "CalculationBaseVar", "CalculationExtraVar",
    "DynamicVar", "DynamicVarSet",
}


def _compressed_int(sig: bytes, p: int) -> tuple[int, int]:
    """读 ECMA-335 的压缩无符号整数，返回 (值, 新偏移)。"""
    b0 = sig[p]
    if b0 & 0x80 == 0:
        return b0, p + 1
    if b0 & 0xC0 == 0x80:
        return ((b0 & 0x3F) << 8) | sig[p + 1], p + 2
    return (((b0 & 0x1F) << 24) | (sig[p + 1] << 16) |
            (sig[p + 2] << 8) | sig[p + 3]), p + 4


def _coded(sig: bytes, p: int, bits: int) -> tuple[int, int, int]:
    """读一个 TypeDefOrRef 编码索引 → (tag, 行号 1 基, 新偏移)。"""
    v, p = _compressed_int(sig, p)
    return v & ((1 << bits) - 1), v >> bits, p


def _type_name_from_sig(m: Metadata, sig: bytes, p: int) -> tuple[str, int]:
    """从签名里取类型名（只处理常见形态），返回 (名字, 新偏移)。"""
    if p >= len(sig):
        return "", p
    et = sig[p]
    p += 1
    if et == 0x12:                       # ELEMENT_TYPE_CLASS
        tag, idx, p = _coded(sig, p, 2)
        return _name_of(m, tag, idx), p
    if et == 0x11:                       # VALUETYPE
        tag, idx, p = _coded(sig, p, 2)
        return _name_of(m, tag, idx), p
    if et == 0x13:                       # 泛型变量 !0
        _n, p = _compressed_int(sig, p)
        return "!", p
    if et == 0x1E:                       # 泛型变量 !!0
        _n, p = _compressed_int(sig, p)
        return "!!", p
    return f"et{et:02X}", p


def _name_of(m: Metadata, tag: int, idx: int) -> str:
    """TypeDefOrRef 编码 → 短类型名。"""
    try:
        if tag == 0:                     # TypeDef
            return m.string(m.read_row("TypeDef", idx - 1)["Name"])
        if tag == 1:                     # TypeRef
            return m.string(m.read_row("TypeRef", idx - 1)["Name"])
        if tag == 2:                     # TypeSpec（递归）
            s = m.blob(m.read_row("TypeSpec", idx - 1)["Signature"])
            nm, _ = _type_name_from_sig(m, s, 0)
            return nm
    except Exception:
        pass
    return ""


def parse_typespec(m: Metadata, sig: bytes) -> str:
    """解析 TypeSpec 签名 → 可读类型名，例如 `PowerVar<VulnerablePower>`。"""
    if not sig or sig[0] != 0x15:        # GENERICINST
        nm, _ = _type_name_from_sig(m, sig, 0)
        return nm
    p = 1
    if p < len(sig) and sig[p] in (0x11, 0x12):
        p += 1
    tag, idx, p = _coded(sig, p, 2)
    base = _name_of(m, tag, idx)
    if p >= len(sig):
        return base
    p += 1                                # 泛型参数个数
    args = []
    while p < len(sig) and len(args) < 4:
        nm, p = _type_name_from_sig(m, sig, p)
        if not nm:
            break
        args.append(nm)
    return f"{base}<{','.join(args)}>" if args else base


def var_name(m: Metadata, tok: int) -> str:
    """从 newobj 的 token 里取出变量类型短名。

    复杂点在泛型：`PowerVar<VulnerablePower>` 这类是通过 **TypeSpec** 引用的
    （Class 指向 TypeSpec 而非 TypeRef），必须解析签名才能拿到类型名。
    """
    tag, idx = tok >> 24, (tok & 0xFFFFFF) - 1
    try:
        if tag == 0x06:                          # MethodDef
            for t in m.typedefs():
                if t.method_start <= idx < t.method_end:
                    return t.name
        elif tag == 0x0A:                        # MemberRef
            r = m.read_row("MemberRef", idx)
            cls_t, cls_i = m.coded("MemberRefParent", r["Class"])
            if cls_t == "TypeRef":
                return m.string(m.read_row("TypeRef", cls_i)["Name"])
            if cls_t == "TypeDef":
                return m.string(m.read_row("TypeDef", cls_i)["Name"])
            if cls_t == "TypeSpec":
                sig = m.blob(m.read_row("TypeSpec", cls_i)["Signature"])
                full = parse_typespec(m, sig)
                # `PowerVar<VulnerablePower>` → 取外层 PowerVar 与其参数
                return full
    except Exception:
        pass
    return ""


def consts_from_il(m: Metadata, mi: int) -> list[tuple[int, str]]:
    """扫描方法体，返回 [(数值, 变量类型名), ...]。

    形态：`ldc.i4 N` 之后（可能隔一条 `Decimal::.ctor`）出现
    `newobj <XxxVar>::.ctor`。用「向后看 2 条指令」的方式配对。
    """
    il = m.method_body(mi)
    if not il:
        return []
    ins: list[tuple[int, str, int]] = []          # (偏移, 操作码名, 参数)
    p = 0
    while p < len(il):
        op = il[p]
        ln = m._il_operand_size(il, p)
        arg = il[p + 1:p + ln]
        val = None
        tok = 0
        if op == 0x20 and len(arg) >= 4:
            val = struct.unpack_from("<i", arg, 0)[0]
        elif 0x16 <= op <= 0x1E:
            val = op - 0x16
        elif op == 0x15:
            val = -1
        elif op in (0x1F,) and arg:
            val = struct.unpack_from("<b", arg, 0)[0]
        if op in (0x73, 0x28, 0x6F) and len(arg) >= 4:
            tok = struct.unpack_from("<I", arg, 0)[0]
        ins.append((p, ("ldc" if val is not None else
                        "newobj" if op == 0x73 else "call"), 
                    val if val is not None else tok))
        p += ln

    out: list[tuple[int, str]] = []
    # 数值配对规则（两种形态都要覆盖，实测各占一半）：
    #   A) `ldc.i4.<值>; newobj Decimal::.ctor; ...; newobj <XxxVar>::.ctor`
    #      → 值先被转成 decimal，再作为 DynamicVar 的第二个参数（BaseValue）
    #   B) `ldc.i4.<值>; newobj <XxxVar>::.ctor`
    #      → 整数直接传给只收 int 的变量（如 EnergyVar/CardsVar）
    # 所以：记「最近一个 ldc」，遇到 Decimal::.ctor 就把它标为「已用于 decimal」，
    # 变量构造时优先取那个被转 decimal 的值，否则取最近的 ldc。
    # 数组下标（紧跟 `dup`）永远不是数值，必须排除。
    last_val: int | None = None
    decimal_val: int | None = None
    for i, (_off, kind, arg) in enumerate(ins):
        if kind == "ldc":
            prev = ins[i - 1][1] if i >= 1 else ""
            if prev != "dup":
                last_val = int(arg)
            continue
        if kind != "newobj":
            continue
        vn = var_name(m, arg)
        if vn == "Decimal":
            decimal_val = last_val
            last_val = None
            continue
        if not vn:
            continue
        # 泛型类型的名字带 arity 反引号（`PowerVar`1<VulnerablePower>`），
        # 必须剥掉才能对上白名单，否则泛型能力变量全部丢失（实测踩过：
        # 含 PowerVar 的条目为 0，`{VulnerablePower}` 永远替换不上）。
        outer = vn.split("<")[0]
        base = re.sub(r"`\d+$", "", outer)
        if base not in VAR_TYPES:
            continue
        pick = decimal_val if decimal_val is not None else last_val
        decimal_val = None
        last_val = None
        if pick is not None and not any(v == vn for _n, v in out):
            out.append((pick, vn))
    return out


def var_template_names(m: Metadata) -> dict[str, str]:
    """变量类型 → 文案模板里的变量名（`DamageVar` → `Damage`）。

    依据：每个变量类的 `.ctor` 里都会 `ldstr "<名字>"`，那个字符串就是
    本地化模板里的占位符名（`{Damage:diff()}`）。
    """
    out: dict[str, str] = {}
    for t in m.typedefs():
        if t.name not in VAR_TYPES:
            continue
        for mi in range(t.method_start, t.method_end):
            r = m.read_row("MethodDef", mi)
            if m.string(r["Name"]) != ".ctor":
                continue
            il = m.method_body(mi)
            p = 0
            while p + 4 < len(il):
                if il[p] == 0x72:                       # ldstr
                    tok = struct.unpack_from("<I", il, p + 1)[0]
                    if tok >> 24 == 0x70:
                        nm = _user_string(m, tok)
                        if nm and nm.isascii():
                            out[t.name] = nm
                    break
                p += m._il_operand_size(il, p)
            break
    return out


def _user_string(m: Metadata, tok: int) -> str:
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
    return m.b[p2:p2 + n].decode("utf-16-le", "ignore")


UPGRADE_METHOD = "OnUpgrade"


def upgrade_from_il(m: Metadata, mi: int) -> dict[str, int]:
    """从 `OnUpgrade` 的 IL 里取「各变量的升级增量」。

    形态非常规整（实测每个卡牌类都长这样）：

        callvirt  DynamicVarSet::get_Damage     # 哪个变量（按属性名）
        ldc.i4.3 ; newobj Decimal::.ctor        # 增量 +3
        callvirt  DynamicVar::UpgradeValueBy    # 应用

    所以取「get_X 调用」与紧随其后的 ldc 即可，不必真的执行代码。
    """
    il = m.method_body(mi)
    if not il:
        return {}
    ins: list[tuple[str, str, int]] = []      # (种类, 名字/字符串, 值)
    p = 0
    while p < len(il):
        op = il[p]
        ln = m._il_operand_size(il, p)
        arg = il[p + 1:p + ln]
        if op == 0x20 and len(arg) >= 4:
            ins.append(("ldc", "", struct.unpack_from("<i", arg, 0)[0]))
        elif 0x16 <= op <= 0x1E:
            ins.append(("ldc", "", op - 0x16))
        elif op == 0x15:
            ins.append(("ldc", "", -1))
        elif op == 0x72 and len(arg) >= 4:                 # ldstr：变量名
            tok = struct.unpack_from("<I", arg, 0)[0]
            ins.append(("ldstr", _user_string(m, tok), 0))
        elif op == 0x7E and len(arg) >= 4:                 # ldsfld：常量字段
            # 升级量是 `Decimal::One` / `Decimal::MinusOne` 这类静态常量时
            # 不会出现 ldc 指令（实测：早有准备、袖里乾坤都是 +1）。
            tok = struct.unpack_from("<I", arg, 0)[0]
            fname = ""
            try:
                fname = m.string(m.read_row("MemberRef", (tok & 0xFFFFFF) - 1)["Name"]) \
                    if tok >> 24 == 0x0A else ""
            except Exception:
                fname = ""
            known = {"One": 1, "Zero": 0, "MinusOne": -1, "Two": 2,
                     "MinusTwo": -2, "Three": 3, "Ten": 10}
            ins.append(("ldc", "", known.get(fname, 0)))
        elif op == 0x6F and len(arg) >= 4:                 # callvirt
            tok = struct.unpack_from("<I", arg, 0)[0]
            ins.append(("callvirt", prop_name(m, tok), 0))
        elif op == 0x73:                                   # newobj
            ins.append(("newobj", "", 0))
        else:
            ins.append(("other", "", 0))
        p += ln

    out: dict[str, int] = {}
    pending_str = ""
    for i, (kind, name, _v) in enumerate(ins):
        if kind == "ldstr":
            pending_str = name
            continue
        if kind != "callvirt" or not name.startswith("get_"):
            continue
        var = name[4:]
        if var in ("DynamicVars", "Props", "CanonicalVars"):
            continue
        # 索引器形态：`ldstr "AccuracyPower"; callvirt get_Item` ——
        # 变量名在字符串里，用 `Item` 当键没有意义（实测踩过）。
        if var == "Item":
            if not pending_str:
                continue
            var = pending_str
        # 往后找最近的 ldc（可能隔着 `newobj Decimal::.ctor`）
        for j in range(i + 1, min(i + 4, len(ins))):
            if ins[j][0] == "ldc":
                out.setdefault(var, int(ins[j][2]))
                break
    return out


def prop_name(m: Metadata, tok: int) -> str:
    """callvirt 的 token → 成员名（`get_Damage` / `UpgradeValueBy`）。"""
    tag, idx = tok >> 24, (tok & 0xFFFFFF) - 1
    try:
        if tag == 0x06:
            return m.string(m.read_row("MethodDef", idx)["Name"])
        if tag == 0x0A:
            return m.string(m.read_row("MemberRef", idx)["Name"])
    except Exception:
        pass
    return ""


def main() -> int:
    if not DLL.is_file():
        print(f"找不到 {DLL}")
        return 1
    m = Metadata(DLL)
    m.typedefs()                        # 预热

    # 卡牌 ID：类名 → 卡牌 ID 用 camel→UPPER_SNAKE
    def to_id(name: str) -> str:
        s = re.sub(r"(?<!^)(?=[A-Z])", "_", name)
        return s.upper()

    result: dict[str, dict] = {"cards": {}, "relics": {}, "meta": {}}
    types = m.typedefs()
    base_of = {"CardModel": "cards", "RelicModel": "relics"}
    n_var = 0
    for t in types:
        kind = base_of.get(t.base_type.rsplit(".", 1)[-1] if t.base_type else "")
        if not kind:
            continue
        vars_: dict[str, object] = {}
        for mi in range(t.method_start, t.method_end):
            if m.string(m.read_row("MethodDef", mi)["Name"]) != VAR_METHOD:
                continue
            for val, vn in consts_from_il(m, mi):
                # 同类型出现多次时保留第一个（基础形态）
                vars_.setdefault(vn, val)
            break
        if not vars_:
            continue
        n_var += 1
        # 升级增量：从同一个类的 OnUpgrade 里取，界面显示「升级后」的卡要用
        upg: dict[str, int] = {}
        for mi in range(t.method_start, t.method_end):
            if m.string(m.read_row("MethodDef", mi)["Name"]) != UPGRADE_METHOD:
                continue
            upg = upgrade_from_il(m, mi)
            break
        result[kind][to_id(t.name)] = {
            "class": t.name, "namespace": t.namespace, "vars": vars_,
            "upgrade": upg}

    result["meta"] = {
        "source": str(DLL), "method": VAR_METHOD,
        "classes_with_vars": n_var,
        "cards": len(result["cards"]), "relics": len(result["relics"]),
        "template_names": var_template_names(m),
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                   encoding="utf-8")

    print(f"卡牌类 {len(result['cards'])} 个带数值 · 遗物类 {len(result['relics'])} 个")
    print()
    print("样例（卡牌）:")
    for cid in ("BASH", "STRIKE_IRONCLAD", "DEFEND_IRONCLAD", "NEUTRALIZE",
                "SURVIVOR", "PREPARED", "DAGGER_SPRAY"):
        v = result["cards"].get(cid)
        if v:
            print(f"   {cid:<20} {v['vars']}")
    print()
    print("样例（遗物）:")
    for rid in ("AKABEKO", "POTION_BELT"):
        v = result["relics"].get(rid)
        if v:
            print(f"   {rid:<20} {v['vars']}")
    print(f"\n写到 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

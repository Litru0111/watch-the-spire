"""卡牌 / 遗物效果文案查询。

数据来自 `tools/extract_effects.py` 对游戏本体与各模组 `.pck` 的提取结果
（`.sts2mon/card_effects.json`）。覆盖：

* 681 张卡牌（667 张有中文效果描述，含 mod）
* 305 件遗物（全部有中文效果描述）
* 每条的 7 种语言原文（de/en/es/fr/ja/ru/zh）

**已知局限（必须如实说明）**：
* 文案里的 `{Damage:diff()}` 是**模板变量**，实际数值由游戏运行时填充，
  这些数值编译在 C# 程序集里、`.pck` 中读不到，所以本模块只能给「文本」，
  给不了「打击造成 6 点伤害」这种**具体数值**。
* 个别卡牌没有描述（未实装或纯被动），`effect()` 会返回空串。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import paths

_HERE = Path(__file__).resolve().parent
DEFAULT_JSON = _HERE.parent / ".sts2mon" / "card_effects.json"


def _data_json(name: str) -> Path:
    """数据目录下的提取文件（受 STS2MON_DATA 影响）。"""
    return paths.data_dir() / name

# [gold]格挡[/gold] 之类的富文本标记
_TAG = re.compile(r"\[/?[a-zA-Z_]{1,24}\]")
# {Damage:diff()} / {Cards} 之类模板变量
_VAR = re.compile(r"\{([^{}]{0,120})\}")
# `:diff()` `:energyIcons()` 这类函数调用后缀
_FN = re.compile(r":[a-zA-Z_]+\([^)]*\)")

# 图标类变量：游戏里画一个图标，原文往往已经写了对应的词
# （`[gold]忍术[/gold]{RenShu:renShu()}` → 「忍术」+ 图标），
# 所以渲染成**空串**，否则会出现「忍术忍术」（实测踩过）。
_ICON_VARS = {"RenShu", "Kela", "energyIcons", "singleStarIcon", "starIcon",
              "singleKelaIcon", "secondaryResourceIcons", "energyPrefix"}

# 状态名对照表：文案里形如 `{VulnerablePower}层[gold]易伤[/gold]`，
# `tools/build_power_names.py` 会从全部文案里把这些配对挖出来写入
# `.sts2mon/power_names.json`。挖不到时退回英文名。
_POWER_ZH_FALLBACK = {
    "Vulnerable": "易伤", "Weak": "虚弱", "Strength": "力量", "Dexterity": "敏捷",
    "Frail": "脆弱", "Poison": "中毒", "Vigor": "活力", "Thorns": "荆棘",
    "Artifact": "人造制品", "Fasting": "禁食", "Intangible": "无形",
    "Afterimage": "余像", "Combats": "战斗",
}


def _power_zh(name: str) -> str:
    """状态/能力名 → 中文（优先用挖出来的对照表）。"""
    tbl = _table()
    return (tbl.get(name) or _POWER_ZH_FALLBACK.get(name)
            or _POWER_ZH_FALLBACK.get(name.rstrip("s")) or "")


@lru_cache(maxsize=4)
def _table(path: str = "") -> dict[str, str]:
    p = Path(path) if path else _data_json("power_names.json")
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _fmt_var(inner: str) -> str:
    """把模板变量变成可读文本。

    关键取舍：**数值读不到**（编译在 C# 里），所以一律用 `?` 占位。

    为什么状态名也用 `?` 而不是翻成中文：原文里变量与紧跟的名词**本来就是
    同一个词**，例如 `{VulnerablePower}层[gold]易伤[/gold]` —— 若把变量翻成
    「易伤」，就成了「给予易伤层易伤」（实测踩过）。保留 `?` 反而是清爽的
    「给予?层易伤」。状态名的中文对照仍可通过 `power_name()` 单独查到。
    """
    body = inner.strip()
    if not body:
        return "?"
    # IfUpgraded 是「升级前后」分支：show:A|B → A 为升级后、B 为未升级
    m = re.match(r"(?i)IfUpgraded\s*:\s*(?:show|choose)\s*:\s*(.+)$", body)
    if m:
        opts = m.group(1).split("|")
        if len(opts) == 1:
            return "（升级后 " + (opts[0] or "?") + "）"
        return "（未升级 " + (opts[1] or "?") + " / 升级后 " + (opts[0] or "?") + "）"
    # choose(n):A|B —— 取第一个非空分支
    m = re.match(r"[A-Za-z_]+\s*:\s*choose\s*\(\s*\d+\s*\)\s*:\s*(.*)$", body)
    if m:
        opts = [x for x in m.group(1).split("|") if x]
        return opts[0] if opts else "?"
    name = body.split(":")[0].strip()
    if not name:
        return "?"
    if name in _ICON_VARS:
        return ""      # 图标：原文已有对应文字，渲染成空避免重复
    # 其余（数值、状态层数）统一占位
    return "?"


def power_name(var: str) -> str:
    """状态/能力变量名 → 中文（`VulnerablePower` → 易伤）。

    对照表由 `tools/build_power_names.py` 从文案里自动挖出。
    """
    v = str(var or "").strip()
    if not v:
        return ""
    return _power_zh(v) or _power_zh(v.rstrip("s"))


def readable(text: str) -> str:
    """把原始文案转成适合界面显示的样子（去标记、变量通俗化）。"""
    if not text:
        return ""
    s = _VAR.sub(lambda m: _fmt_var(m.group(1)), text)
    s = _TAG.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


@lru_cache(maxsize=4)
def _values(path: str = "") -> dict[str, dict[str, Any]]:
    """卡牌/遗物的**数值**表（由 tools/extract_values.py 从 sts2.dll 提取）。

    数值不在字段里，而是在 `getCanonicalVars` 里构造 `DamageVar(6)` 这样的
    动态变量；提取器反汇编 IL 拿到它们。返回 {"cards": {...}, "relics": {...}}
    """
    p = Path(path) if path else _data_json("card_values.json")
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def upgrade_values(card_id: str, path: str = "") -> dict[str, int]:
    """该卡牌升级时各变量的**增量**（如打击的伤害 +3）。

    数据来自 `OnUpgrade` 的 IL（`UpgradeValueBy(3)`），不是猜的。
    键与 `values()` 一致（已剥 `Var` 后缀），便于直接相加。
    """
    d = _values(path)
    e = None
    for table in ("cards", "relics"):
        for k in _candidates(card_id):
            e = (d.get(table) or {}).get(k)
            if e:
                break
        if e:
            break
    if not e:
        return {}
    out: dict[str, int] = {}
    for k, v in (e.get("upgrade") or {}).items():
        short = k[:-3] if k.endswith("Var") else k
        out.setdefault(short, int(v))
        out.setdefault(k, int(v))
    return out


def is_upgradable(card_id: str, path: str = "") -> bool:
    """这张牌是否有升级增量（用在牌组里标 `+`）。"""
    return bool(upgrade_values(card_id, path))


def upgraded_name(card_id: str, name: str = "", path: str = "") -> str:
    """升级后的卡名：`早有准备` → `早有准备+`。

    游戏里卡牌升级**不换 ID**（还是 `CARD.PREPARED`），只在显示名后加 `+`，
    所以这里也只是加后缀，不去查另一个 ID（实测确认过）。
    """
    nm = name or _display_name(card_id, path)
    if not nm:
        return ""
    return nm if nm.endswith("+") else nm + "+"


def _display_name(card_id: str, path: str = "") -> str:
    e = _get(card_id, path)
    return (e.get("name") or e.get("title") or "") if e else ""


def _var_aliases(name: str) -> list[str]:
    """一个变量名的所有等价写法。

    同一个能力变量在不同地方会以不同形态出现，必须全部覆盖：
      * 基础值表：`PowerVar`1<AccuracyPower>`（泛型外+内层）
      * 升级增量：`AccuracyPower`（OnUpgrade 里的字符串）
      * 文案模板：`{AccuracyPower}` / `{Accuracy:diff()}`
    所以 `AccuracyPower` 要同时能匹配 `AccuracyPower` 与 `Accuracy`。
    """
    out: list[str] = []
    n = str(name or "").strip()
    if not n:
        return out
    out.append(n)
    if n.endswith("Var"):
        out.append(n[:-3])
    if n.endswith("Power"):
        out.append(n[:-5])          # AccuracyPower → Accuracy
        out.append(n[:-5] + "Var")
    out.append(n + "Var")
    seen: set[str] = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


def _lookup(mapping: dict[str, Any], name: str) -> Any:
    """按等价写法在映射里取值（找不到返回 None）。"""
    for k in _var_aliases(name):
        if k in mapping:
            return mapping[k]
    return None


def effect_upgraded(card_id: str, path: str = "") -> str:
    """升级后的效果文案：把基础值加上升级增量再套进模板。

    例：打击 → 「造成9点伤害。」（基础 6 + 升级 3）

    取不到升级增量时退回基础文案（并保留 `?` 占位，不编造数字）。
    """
    e = _get(card_id, path)
    if not e:
        return ""
    raw = e.get("description") or ""
    if not raw:
        return ""
    vals = dict(values(card_id, path))
    # 把升级增量加到对应的基础值上。键的形态可能不同（见 _var_aliases），
    # 所以两边的每一种写法都要试，避免「升级了但数值没变」（实测踩过）。
    for k, delta in upgrade_values(card_id, path).items():
        target = None
        if k in vals:
            target = k
        else:
            for cand in _var_aliases(k):
                if cand in vals:
                    target = cand
                    break
        if target is None:
            # 值表里找不到同名键：可能是泛型外层的写法，尝试按内层名反查
            for vk in list(vals):
                if any(a in vk for a in _var_aliases(k)):
                    target = vk
                    break
        if target is not None:
            vals[target] = int(vals[target]) + int(delta)
    if not vals:
        return readable(raw)

    def sub(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if not inner:
            return "?"
        name = inner.split(":")[0].split(".")[0].strip()
        got = _lookup(vals, name)
        if got is not None:
            return str(got)
        return _fmt_var(inner)

    s = _VAR.sub(sub, raw)
    s = _TAG.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def badge_name(badge_id: str, rarity: str = "", path: str = "") -> str:
    """对局结束的称号 → 中文名（按稀有度取对应档）。

    数据来自 pck 的 `ELITE.bronzeTitle` 这类键，每档一条：
        精英猎手（bronze，3 个精英） / 精英杀手（silver，6 个） / 精英屠夫（gold，9 个）
    只有不分档的称号（如 `CCCCOMBO`）才退回普通 title。
    """
    e = _get(badge_id, path)
    if not e:
        return ""
    tiers = e.get("badge_tiers") or {}
    rar = str(rarity or "").lower()
    if rar and tiers.get(rar, {}).get("title"):
        return tiers[rar]["title"]
    for r in ("gold", "silver", "bronze"):
        if tiers.get(r, {}).get("title"):
            return tiers[r]["title"]
    return e.get("title") or e.get("name") or ""


def badge_info(badge_id: str, rarity: str = "", path: str = "") -> dict[str, Any]:
    """称号完整信息：中文名、描述、该稀有度的说明。"""
    e = _get(badge_id, path)
    if not e:
        return {"id": badge_id, "name": badge_id, "rarity": rarity, "desc": ""}
    tiers = e.get("badge_tiers") or {}
    rar = str(rarity or "").lower()
    t = tiers.get(rar) or tiers.get("bronze") or {}
    return {
        "id": badge_id,
        "name": badge_name(badge_id, rarity, path) or badge_id,
        "rarity": rar,
        "desc": readable(t.get("description") or e.get("description") or ""),
    }


def badges(badge_list: Any, path: str = "") -> list[dict[str, Any]]:
    """把存档里的 `[{"id":..., "rarity":...}]` 转成带中文名的列表。"""
    out: list[dict[str, Any]] = []
    for b in (badge_list or []):
        if isinstance(b, dict):
            bid, rar = str(b.get("id") or ""), str(b.get("rarity") or "")
        else:
            bid, rar = str(b or ""), ""
        if not bid:
            continue
        out.append(badge_info(bid, rar, path))
    return out


def values(card_or_relic_id: str, path: str = "") -> dict[str, int]:
    """取某项的数值，键按**文案模板里的变量名**归一化。

    提取器给出的键是**类型名**（`DamageVar` / `BlockVar` / `CardsVar`），
    而文案模板里用的是**短名**（`Damage` / `Block` / `Cards`），
    这里统一去掉 `Var` 后缀，否则替换会全部落空（实测踩过）。

    例：`values('BASH')` → `{'Damage': 8}`
    """
    d = _values(path)
    e = None
    for table in ("cards", "relics"):
        for k in _candidates(card_or_relic_id):
            e = (d.get(table) or {}).get(k)
            if e:
                break
        if e:
            break
    if not e:
        return {}
    out: dict[str, int] = {}
    for k, v in (e.get("vars") or {}).items():
        short = k[:-3] if k.endswith("Var") else k
        out.setdefault(short, v)
        out.setdefault(k, v)          # 两种写法都留，方便调用方
        # 泛型能力变量形如 `PowerVar<VulnerablePower>`：外层是容器，
        # 尖括号里才是「哪个能力」，两者都登记，否则文案里的
        # `{VulnerablePower}` 匹配不上（实测踩过）。
        if "<" in k:
            outer, _, inner = k.partition("<")
            inner = inner.rstrip(">")
            for nm in (outer, outer[:-3] if outer.endswith("Var") else outer,
                       inner, inner[:-5] if inner.endswith("Power") else inner):
                if nm:
                    out.setdefault(nm, v)
    return out


def effect_with_values(card_or_relic_id: str, path: str = "") -> str:
    """带数值的效果文案 —— 把 `{Damage:diff()}` 换成真实数字。

    例：痛击 → 「造成8点伤害。给予2层易伤。」

    只替换**确实出现在该条文案里**的变量名，所以不会误伤。
    数值缺失（未提取到）时退回 `?` 占位。
    """
    e = _get(card_or_relic_id, path)
    if not e:
        return ""
    raw = e.get("description") or ""
    if not raw:
        return ""
    vals = values(card_or_relic_id, path)
    if not vals:
        return readable(raw)

    def sub(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        if not inner:
            return "?"
        name = inner.split(":")[0].split(".")[0].strip()
        # 变量名可能带 Power / Var 后缀（`VulnerablePower` → `Vulnerable`）
        for cand in (name, name[:-5] if name.endswith("Power") else name,
                     name[:-3] if name.endswith("Var") else name):
            if cand in vals:
                return str(vals[cand])
        return _fmt_var(inner)

    s = _VAR.sub(sub, raw)
    s = _TAG.sub("", s)
    return re.sub(r"[ \t]+", " ", s).strip()


def has_values(card_or_relic_id: str, path: str = "") -> bool:
    """这项是否提取到了数值。"""
    return bool(values(card_or_relic_id, path))


@lru_cache(maxsize=4)
def _load(path: str = "") -> dict[str, dict[str, Any]]:
    p = Path(path) if path else _data_json("card_effects.json")
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return d.get("文案") or {}


def _candidates(cid: str) -> list[str]:
    """一个 ID 的所有可能键形态，按「越接近原样越优先」排序。

    注意不能激进剥离前缀：遗物 `POTION_BELT`（药水腰带）合法地以 `POTION_`
    开头，剥掉就查不到了（实测踩过）。所以这里给出候选列表逐个试。
    """
    s = str(cid or "").strip()
    if not s:
        return []
    out: list[str] = []
    if "." in s:
        out.append(s.split(".")[-1])          # CARD.BASH → BASH
    out.append(s)
    for pre in ("CARD_", "RELIC_", "POTION_", "MONSTER_", "ENCOUNTER_", "EVENT_"):
        if s.startswith(pre) and len(s) > len(pre):
            out.append(s[len(pre):])
    seen: set[str] = set()
    return [x for x in out if x and not (x in seen or seen.add(x))]


def _get(cid: str, path: str = "") -> dict[str, Any]:
    d = _load(path)
    for k in _candidates(cid):
        e = d.get(k)
        if e:
            return e
    return {}


def _norm(cid: str) -> str:
    """归一化成表里的键（查得到就用表里的键，查不到退回原样）。"""
    c = _candidates(cid)
    if not c:
        return ""
    d = _load()
    for k in c:
        if k in d:
            return k
    return c[0]


def effect(card_or_relic_id: str, *, plain: bool = True,
           path: str = "") -> str:
    """取效果文案。

    plain=True 时把模板变量转成可读文本（推荐界面用）；
    plain=False 返回原始模板（含 `{Damage:diff()}`），便于自行处理。

    卡牌与遗物共用一张表；ID 可写成 `BASH` / `CARD.BASH` / `RELIC.AKABEKO`。
    """
    e = _get(card_or_relic_id, path)
    if not e:
        return ""
    raw = e.get("description") or ""
    return readable(raw) if plain else raw


def info(card_or_relic_id: str, path: str = "") -> dict[str, Any]:
    """完整信息：名称、类型、效果、全部语言原文。"""
    e = _get(card_or_relic_id, path)
    if not e:
        return {}
    return {
        "id": _norm(card_or_relic_id),
        "name": e.get("name") or "",
        "kind": e.get("kind") or "?",
        "effect": readable(e.get("description") or ""),
        "effect_raw": e.get("description") or "",
        "flavor": readable(e.get("flavor") or ""),
        "lang": e.get("lang") or "?",
        "all_langs": e.get("all_langs") or {},
        "source": e.get("source") or "?",
    }


def effects_for(ids: list[str], *, path: str = "") -> dict[str, str]:
    """批量取效果（给界面一次拿一整副牌用）。"""
    return {str(i): effect(i, path=path) for i in (ids or [])}


def search(keyword: str, *, kind: str = "", limit: int = 30,
           path: str = "") -> list[dict[str, Any]]:
    """按关键词搜效果文本 / 名称（kind 可限 card / relic）。"""
    kw = str(keyword or "").strip()
    if not kw:
        return []
    out: list[dict[str, Any]] = []
    for cid, e in _load(path).items():
        if kind and (e.get("kind") or "") != kind:
            continue
        name = e.get("name") or ""
        plain = readable(e.get("description") or "")
        if kw in name or kw in plain or kw in cid:
            out.append({"id": cid, "name": name, "kind": e.get("kind") or "?",
                        "effect": plain})
            if len(out) >= limit:
                break
    return out


def stats(path: str = "") -> dict[str, Any]:
    """覆盖率统计，便于在界面上如实标注。"""
    d = _load(path)
    cards = [e for e in d.values() if e.get("kind") == "card"]
    relics = [e for e in d.values() if e.get("kind") == "relic"]
    zh = sum(1 for e in d.values() if e.get("lang") == "zh")
    return {
        "total": len(d),
        "cards": len(cards), "cards_with_text": sum(1 for e in cards if e.get("description")),
        "relics": len(relics), "relics_with_text": sum(1 for e in relics if e.get("description")),
        "chinese": zh,
        "has_values": False,   # 具体数值读不到，见模块文档
    }

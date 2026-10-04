"""提取卡牌/遗物的效果文本（含 mod），生成可查询的效果表。

来源：游戏本体与各模组的 `.pck` 里的本地化字符串，形态为
    "LEX_NINJA2_CARD_YI_CUT.description": "给予所有敌人{VulnerablePower}层[gold]易伤[/gold]"

要点：
* 文本里带**模板变量**（`{Damage:diff()}`），实际数值由游戏运行时填充；
  这里保留原始模板，并额外抽出一份「去标记的纯文本」便于阅读。
* 同时解析 `.title` / `.flavor` 等键，与 `object-catalog.json` 的 ID 对齐。
* mod 的文本会覆盖同名键（加载顺序靠后优先），所以**后扫的模组优先**。

输出：.sts2mon/card_effects.json
    {"文案": {ID: {"description":..., "title":..., "source":...}},
     "统计": {...}}
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon import paths  # noqa: E402

# 路径一律走 sts2mon.paths（自动探测游戏目录、支持环境变量覆盖），
# 不再写死盘符 —— 否则只有作者本机能跑。
APPDATA = paths.sts2_appdata()
GAME = paths.game_dir()
WORKSHOP = paths.workshop_dir()
OUT = paths.data_dir() / "card_effects.json"

# "KEY.description": "VALUE"   或   "KEY.title": "VALUE"
# 称号是多一层点号的形态：`ELITE.bronzeTitle` / `SPEEDY.goldDescription`
# （按 bronze/silver/gold 三档各一条）。早先漏了这层，称号文案全部提取不到。
#
# 注意：稀有度只能作为**字段名的一部分**（`bronzeTitle`），
# 不能写成 `(?:bronze|silver|gold)?(?:title|...)` —— 那样会把 `bronze` 先吃掉，
# 剩下的 `Title` 不再以小写 `title` 开头，整条就匹配不上（实测踩过）。
KV = re.compile(
    rb'"([A-Za-z0-9_\.]{3,80}\.'
    rb'(?:(?:bronze|silver|gold)\.)?'
    rb'(?:(?:bronze|silver|gold))?'
    rb'(?:[Dd]escription|[Tt]itle|flavor|loss|victory))"'
    rb'\s*:\s*"((?:[^"\\]|\\.){0,600})"')

# 称号键的稀有度：`ELITE.bronzeTitle` → bronze
_RARITY = re.compile(r"\.(bronze|silver|gold)(?=[A-Z])", re.I)
# 去掉 [gold]...[/gold] 之类的富文本标记，以及模板变量
TAG = re.compile(r"\[/?[a-zA-Z_]+\]")
VAR = re.compile(r"\{[^}]{0,80}\}")


def clean(s: str) -> str:
    """去富文本标记与模板变量，留可读文本（变量位置用 ?）。"""
    s = TAG.sub("", s)
    s = VAR.sub("?", s)
    return re.sub(r"\s+", " ", s).strip()


KANA = re.compile(r"[\u3040-\u30ff]")
HANGUL = re.compile(r"[\uac00-\ud7af]")
HAN = re.compile(r"[\u4e00-\u9fff]")


def lang_of(s: str, seq: int = -1) -> str:
    """判断一段文本属于哪种语言。

    pck 里**每种语言各存一份**同一键的文本，实测固定 14 种、顺序一致：

        德 英 西 法 意 日 韩 波兰 葡 俄 西 泰 土耳其 中文

    因此**位置**比字符判断可靠得多 —— 中文永远是第 14 份（下标 13）。
    只看字符会把日文汉字（`複製薬`）误判成中文，从而取到日文标题
    （实测踩过：复制药水显示成 複製薬）。
    """
    if not s:
        return "?"
    # 先按固定位置判定，这是最可靠的
    if seq == 13:
        return "zh"
    if seq == 5:
        return "ja"
    if seq == 6:
        return "ko"
    if KANA.search(s):
        return "ja"
    if HANGUL.search(s):
        return "ko"
    if HAN.search(s):
        # 只有汉字、没有假名/谚文 —— 可能中日皆有，交给位置规则兜底
        return "zh?"
    if re.search(r"[äöüß]", s, re.I):
        return "de"
    if re.search(r"[ąćęłńóśźż]", s, re.I):
        return "pl"
    if re.search(r"[ğışçİŞĞ]", s):
        return "tr"
    if re.search(r"[\u0e00-\u0e7f]", s):
        return "th"
    if re.search(r"[áéíóúñ¿¡]", s, re.I):
        return "es"
    if re.search(r"[àâçéèêëîïôûù]", s, re.I):
        return "fr"
    if re.search(r"[а-я]", s, re.I):
        return "ru"
    if re.search(r"[a-z]", s, re.I):
        return "en"
    return "?"


def unescape(s: bytes) -> str:
    try:
        return json.loads(b'"' + s + b'"')
    except Exception:
        return s.decode("utf-8", "ignore")


def split_key(key: str) -> tuple[str, str, str]:
    """把本地化键拆成 (base, field, rarity)。

    两种形态：
        `STRIKE_IRONCLAD.description` → ("STRIKE_IRONCLAD", "description", "")
        `ELITE.bronzeTitle`           → ("ELITE", "title", "bronze")

    称号的键是**两个点**，直接用 rpartition 会得到 `ELITE.bronze` + `Title`，
    没法按 base 归组（实测踩过）。
    """
    m = re.match(r"^(.*?)\.(bronze|silver|gold)([A-Z][A-Za-z]*)$", key)
    if m:
        return m.group(1), m.group(3).lower(), m.group(2).lower()
    base, _, field = key.rpartition(".")
    return base, field, ""


def scan(path: Path, label: str, into: dict[str, dict]) -> int:
    """把一个 pck 的文本按语言收进 into。

    结构：into[BASE][field][lang] = 文本     （BASE 形如 STRIKE_IRONCLAD）

    **同一语言可能重复出现**（同一文件里同一键会有多份），所以不能
    「同语言就跳过」—— 那会把先出现的日文汉字当成中文留住。
    这里改成：按该 field 内出现的**序号**判定语言，并用更好的覆盖较差的。
    """
    blob = path.read_bytes()
    n = 0
    seen_seq: dict[tuple[str, str], int] = {}
    for m in KV.finditer(blob):
        key = m.group(1).decode("ascii", "ignore")
        val = unescape(m.group(2))
        if not val:
            continue
        base, field, rarity = split_key(key)
        if not base or not field:
            continue
        # 称号按稀有度分档，字段名带上稀有度以便分别保存
        slot = f"{field}" if not rarity else f"{rarity}{field.capitalize()}"
        entry = into.setdefault(base, {})
        langs = entry.setdefault(slot, {})
        k = (base, slot)
        seq = seen_seq.get(k, -1) + 1
        seen_seq[k] = seq
        lg = lang_of(val, seq)
        prev = langs.get(lg)
        if prev is not None:
            # 已有同语言的值：保留「更确定」的那个
            # （zh 用位置判定，优先级最高；'zh?' 是猜测，让位给确定值）
            if lg == "zh?" and prev:
                continue
            if lg == "zh" and prev and lang_of(prev, -1) == "zh?":
                langs[lg] = val
                entry.setdefault("sources", {})[lg] = label
                n += 1
            continue
        langs[lg] = val
        entry.setdefault("sources", {})[lg] = label
        n += 1
    return n


def pick(entry: dict, field: str) -> tuple[str, str]:
    """挑出要用的文本：优先简体中文，其次英文，再退到任意一种。

    返回 (文本, 语言)。`zh` 是按位置判定的确定值，`zh?` 是仅凭汉字猜的，
    所以要优先取 `zh`。
    """
    langs = entry.get(field) or {}
    for lg in ("zh", "zh?", "en", "ja", "de", "es", "fr", "ru"):
        if langs.get(lg):
            return langs[lg], lg
    for lg, v in langs.items():
        if v:
            return v, lg
    return "", "?"


def main() -> int:
    if GAME is None:
        print("找不到游戏安装目录。用 --game 或环境变量 STS2MON_GAME 指定，例如：")
        print(r'  set STS2MON_GAME=D:\SteamLibrary\steamapps\common\Slay the Spire 2')
        return 2
    raw: dict[str, dict] = {}
    # 用 paths.pck_path()（它会兼容 .pck.bytes 之类命名），而不是自己拼文件名
    pck = paths.pck_path()
    if pck is None:
        print("找不到游戏 pck（确认游戏目录下有 SlayTheSpire2.pck）")
        return 2
    targets: list[tuple[str, Path]] = [("游戏本体", pck)]
    # 创意工坊可能不存在（没订阅任何 mod 很正常），此时 WORKSHOP 为 None
    if WORKSHOP is not None:
        for p in sorted(WORKSHOP.glob("*/*.pck")):
            if p.stat().st_size > 100_000:
                targets.append((p.parent.name, p))
    else:
        print("（未找到创意工坊目录，只提取游戏本体）")
    total = 0
    print(f"扫描 {len(targets)} 个 pck…")
    for label, p in targets:
        try:
            n = scan(p, label, raw)
        except Exception as exc:
            print(f"  {label:<16} 失败: {exc!r}")
            continue
        if n:
            print(f"  {label:<16} 提取 {n} 条文本")
        total += n

    # 与 object-catalog 对齐，区分卡牌/遗物
    cat = APPDATA / "DanmakuSpire" / "object-catalog.json"
    kinds: dict[str, str] = {}
    names: dict[str, str] = {}
    if cat.is_file():
        d = json.loads(cat.read_text(encoding="utf-8-sig"))
        objs = d.get("objects") if isinstance(d, dict) else d
        KN = {1: "card", 2: "relic", 3: "monster", 4: "event", 0: "character"}
        for o in objs:
            if isinstance(o, dict) and o.get("id"):
                kinds[o["id"]] = KN.get(o.get("kind"), "?")
                names[o["id"]] = o.get("name") or ""

    # 压平成「每个对象一条」：优先中文，保留全部语言备查
    out: dict[str, dict] = {}
    lang_stat: dict[str, int] = {}
    for base, entry in raw.items():
        desc, dl = pick(entry, "description")
        title, tl = pick(entry, "title")
        flavor, _ = pick(entry, "flavor")
        # 称号（badge）：标题/描述按 bronze/silver/gold 分档存，
        # 键名是 `bronzeTitle` / `goldDescription`，普通的 pick 取不到。
        tiers: dict[str, dict[str, str]] = {}
        for rar in ("bronze", "silver", "gold"):
            t, tlang = pick(entry, f"{rar}Title")
            dsc, _ = pick(entry, f"{rar}Description")
            if t or dsc:
                tiers[rar] = {"title": t, "description": dsc,
                              "description_plain": clean(dsc), "lang": tlang}
        if tiers and not title:
            # 整体条目用最高档的标题当名字（没有就取最低档）
            for rar in ("gold", "silver", "bronze"):
                if tiers.get(rar, {}).get("title"):
                    title = tiers[rar]["title"]
                    tl = tiers[rar]["lang"]
                    break
            if not desc:
                desc = tiers.get("bronze", {}).get("description", "")
                dl = tiers.get("bronze", {}).get("lang", dl)
        if not desc and not title and not tiers:
            continue
        lang_stat[dl] = lang_stat.get(dl, 0) + 1
        out[base] = {
            "kind": kinds.get(base, "?"),
            "name": names.get(base) or title or base,
            "title": title,
            "description": desc,
            "description_plain": clean(desc),
            "flavor": flavor,
            "badge_tiers": tiers,          # 称号：各稀有度的中英文案
            "lang": dl,
            "langs": {k: sorted(v.keys()) for k, v in entry.items()
                      if isinstance(v, dict)},
            "all_langs": {f: v for f, v in entry.items() if isinstance(v, dict)},
            "source": (entry.get("sources") or {}).get(dl, "?"),
        }

    cards = [c for c, e in out.items() if e["kind"] == "card"]
    relics = [c for c, e in out.items() if e["kind"] == "relic"]
    print()
    print(f"合计 {len(out)} 个对象 · 带描述 {sum(1 for e in out.values() if e['description'])}")
    print(f"  卡牌 {len(cards)}（有描述 {sum(1 for c in cards if out[c]['description'])}）")
    print(f"  遗物 {len(relics)}（有描述 {sum(1 for c in relics if out[c]['description'])}）")
    print(f"  描述语言分布: {lang_stat}")
    print()
    print("样例：")
    for cid in ("STRIKE_IRONCLAD", "BASH", "AKABEKO", "LEX_NINJA2_CARD_YI_CUT"):
        e = out.get(cid)
        if e:
            print(f"  [{e['kind']}] {e['name']} ({cid}) [{e['lang']}]")
            print(f"      {e['description_plain']}")

    OUT.write_text(json.dumps(
        {"文案": out,
         "统计": {"对象数": len(out), "卡牌": len(cards), "遗物": len(relics),
                  "描述语言": lang_stat}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n写到 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

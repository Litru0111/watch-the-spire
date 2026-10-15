"""从游戏 .pck 提取**名称库**（ID -> 中文名），不依赖任何 mod。

## 为什么需要它

名称以前来自 `DanmakuSpire` 模组导出的 `object-catalog.json`。
那意味着：没装 mod 就只能显示英文 ID，而 mod 的译名还可能和游戏本体不一致
（实测 `SILENT` 本体是「静默猎手」，catalog 写成「夜隐猫忍」）。
本脚本直接从游戏本体的本地化表取名字，mod 变成可选。

## 本地化表长什么样

pck 里的键形如 `"AGGRESSION.title": "好勇斗狠"`，同一个键**按语言各存一份**，
顺序是：

    德 英 西 法 意 日 韩 波兰 葡 俄 西 泰 土耳其 中文

**但份数不固定**：有的语言没翻译就整份缺失（实测 13 份的情况是少了土耳其语），
于是中文会从下标 13 **前移**到 12。所以：

* 按固定下标取中文是**错的** —— `ARMAMENTS` 的「武装」同时出现在下标 5（日文，
  同形汉字）和 13（中文），只看下标会取错或取不到。
* 可靠判据是**字符集**：中文是唯一「含汉字且不含假名/谚文」的语言。
  多个候选时取**最大下标**（中文排在日文之后）。

分类靠两条线索，**都不能省**：

* **键名规则**：怪物用 `.name`（`FOGMOG.name`），其余用 `.title`；
  带 `_NORMAL/_ELITE/_BOSS` 后缀的是**遭遇**（编队）而非单体。
* **`.NET` 程序集基类**：键名规则分不出「卡牌」和「遗物」——
  两者都只有 `.title`。而 `glossary.is_card()` 要用这个区分
  （实时链路靠它判断一次伤害是我方出牌还是敌方攻击，判错就会把
  敌方伤害算到我方头上）。所以这里读 `sts2.dll` 的类型表，取
  `CardModel` / `RelicModel` / `MonsterModel` 的子类名做权威分类。

输出：`.sts2mon/name_library.json`
    {"names": {ID: {"name": 简体中文, "kind": 分类, "slot": 命中下标, "total": 该键份数}},
     "stats": {...}}

只保留**简体中文**：其余 13 种语言在本项目里用不到，存着会让文件大 10 倍以上。
`slot` / `total` 保留下来，是为了万一以后要复核「这份是不是取错了语言」。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotnet_meta import Metadata  # noqa: E402
from sts2mon import paths  # noqa: E402

# 路径一律走 sts2mon.paths（自动探测游戏目录、支持环境变量覆盖），
# 不写死盘符 —— 否则只有作者本机能跑。
GAME = paths.game_dir()
DLL = paths.dll_path()
WORKSHOP = paths.workshop_dir()
OUT = paths.data_dir() / "name_library.json"

# "KEY.title": "VALUE" / "KEY.name": "VALUE"
KV = re.compile(rb'"([A-Za-z0-9_\.]{3,90})"\s*:\s*"((?:[^"\\]|\\.){0,400})"')

# 语言槽位顺序（用于按位置兜底与输出 langs 数组）
LANGS = ["de", "en", "es", "fr", "it", "ja", "ko", "pl", "pt", "ru", "es2", "th", "tr", "zh"]

# 字号/标签清理：[gold]...[/gold]、模板变量 {Damage}
TAG = re.compile(r"\[/?[a-zA-Z_][a-zA-Z0-9_]*\]")
VAR = re.compile(r"\{[^}]{0,80}\}")
KANA = re.compile(r"[\u3040-\u30ff]")          # 平假名 + 片假名
HANGUL = re.compile(r"[\uac00-\ud7af]")
HAN = re.compile(r"[\u4e00-\u9fff]")


def clean(s: str) -> str:
    s = TAG.sub("", s)
    s = VAR.sub("?", s)
    return re.sub(r"\s+", " ", s).strip()


def is_chinese(s: str) -> bool:
    """含汉字、且不含假名/谚文 —— 中文的唯一可靠特征。

    日文汉字（`武装`/`複製薬`）会被 KANA 排除掉（有假名），
    同形的纯汉字日文名（`巨人`）无法与中文区分，所以多候选时取最大下标。
    """
    return bool(s) and bool(HAN.search(s)) and not KANA.search(s) and not HANGUL.search(s)


def unescape(b: bytes) -> str:
    try:
        return json.loads(b'"' + b + b'"')
    except Exception:
        return b.decode("utf-8", "ignore")


# 分类：键名规则（分不出卡牌/遗物，交给 .NET 类型表）
def classify(base: str, field: str) -> str:
    if field == "name":
        return "monster"                       # 怪物用 .name
    if base.endswith(("_NORMAL", "_ELITE", "_WEAK", "_HARD", "_BOSS")):
        return "encounter"
    return "object"                            # 卡牌/遗物/事件/角色/药水，待 .NET 细化


# .NET 基类 -> 我们的分类
BASE_KIND = {
    "CardModel": "card",
    "RelicModel": "relic",
    "MonsterModel": "monster",
    "PotionModel": "potion",
    "CharacterModel": "character",
    "EventModel": "event",
    "EncounterModel": "encounter",
    "PowerModel": "power",
}


def dotnet_kinds() -> tuple[dict[str, str], dict[str, int]]:
    """读 sts2.dll，把「类名 -> 分类」map 出来。

    类名转 ID 的规则与 `extract_values.py` 一致（camelCase -> UPPER_SNAKE），
    这样和本地化键能对上。
    """
    if not DLL or not DLL.is_file():
        print("  [!] 找不到 sts2.dll，卡牌/遗物无法用基类区分")
        return {}, {}
    m = Metadata(DLL)
    m.typedefs()                               # 预热（约 0.9 秒，内部有缓存）

    def to_id(name: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()

    out: dict[str, str] = {}
    stats: Counter = Counter()
    for t in m.typedefs():
        if not t.base_type:
            continue
        base = t.base_type.rsplit(".", 1)[-1]
        kind = BASE_KIND.get(base)
        if not kind:
            continue
        out.setdefault(to_id(t.name), kind)
        stats[kind] += 1
    return out, dict(stats)


def scan(path: Path, label: str, into: dict[str, dict[str, list[bytes]]],
         origin: dict[str, str]) -> int:
    """把一个 pck 收进 into[base][field] = [按出现顺序的原始值]。

    **本体优先**：同一 base 已被更早的 pck（游戏本体）收录过就跳过，
    不让后扫的模组覆盖本体译名。早先这里是无条件 append，结果
    `AGGRESSION` 的 source 变成了某个模组 ID，而译名其实来自本体。
    玩家装模组是为了玩模组，不是为了改本体的卡名。
    """
    blob = path.read_bytes()
    n = 0
    for m in KV.finditer(blob):
        key = m.group(1).decode("ascii", "ignore")
        if "." not in key:
            continue
        base, _, field = key.rpartition(".")
        if field not in ("title", "name"):
            continue
        if base not in origin:                 # 本体已经定过这个名字了
            into.setdefault(base, {}).setdefault(field, []).append(m.group(2))
        n += 1
    return n


def main() -> int:
    if GAME is None:
        print("找不到游戏安装目录。用 --game 或环境变量 STS2MON_GAME 指定，例如：")
        print(r'  set STS2MON_GAME=D:\SteamLibrary\steamapps\common\Slay the Spire 2')
        return 2
    # 用 paths.pck_path()（它会兼容 .pck.bytes 之类命名），而不是自己拼文件名
    pck = paths.pck_path()
    if pck is None:
        print("找不到游戏 pck（确认游戏目录下有 SlayTheSpire2.pck）")
        return 2
    raw: dict[str, dict[str, list[bytes]]] = {}
    origin: dict[str, str] = {}                # base -> 首次出现的 pck 标签
    targets: list[tuple[str, Path]] = [("游戏本体", pck)]
    # 创意工坊可能不存在（没订阅任何 mod 很正常），此时 WORKSHOP 为 None
    if WORKSHOP is not None:
        for p in sorted(WORKSHOP.glob("*/*.pck")):
            if p.stat().st_size > 100_000:
                targets.append((p.parent.name, p))
    else:
        print("（未找到创意工坊目录，只提取游戏本体）")

    print(f"扫描 {len(targets)} 个 pck（本体优先，模组不覆盖本体译名）…")
    for label, p in targets:
        if not p.is_file():
            continue
        try:
            n = scan(p, label, raw, origin)
        except Exception as exc:
            print(f"  {label:<18} 失败: {exc!r}")
            continue
        # 记录每个 base 的出处（首次出现者，即本体）
        for base in raw:
            origin.setdefault(base, label)
        print(f"  {label:<18} {n} 条 name/title 文本")

    print()
    print("读 sts2.dll 的类型表（区分卡牌 / 遗物 / 怪物）…")
    dkinds, dstats = dotnet_kinds()
    print(f"  类型分类: {dstats}")
    print(f"  共 {len(dkinds)} 个类名 -> 分类")

    names: dict[str, dict] = {}
    stats = Counter()
    ambiguous: list[dict] = []

    for base, fields in raw.items():
        # 怪物优先 .name；其余用 .title
        for field in ("name", "title"):
            vals = fields.get(field)
            if not vals:
                continue
            cands = [(i, clean(unescape(v))) for i, v in enumerate(vals)]
            zh = [(i, s) for i, s in cands if is_chinese(s)]
            if not zh:
                continue
            slot, name = max(zh, key=lambda t: t[0])      # 多候选取最大下标
            if len(zh) > 1:
                ambiguous.append({"id": base, "field": field,
                                  "picked": name, "slot": slot,
                                  "others": [s for i, s in zh if i != slot][:4]})
                stats["多候选"] += 1
            # 分类优先级：.NET 基类 > 键名规则
            kind = dkinds.get(base) or classify(base, field)
            names[base] = {
                "name": name,          # 只留简体中文
                "kind": kind,
                "field": field,
                "slot": slot,          # 中文命中的下标（复核用）
                "total": len(vals),    # 该键共有几份（复核用）
                "source": origin.get(base, label),
            }
            stats[kind] += 1
            break

    payload = {
        "names": names,
        "stats": {
            "总数": len(names),
            "按分类": {k: v for k, v in sorted(stats.items()) if k != "多候选"},
            "多候选": stats["多候选"],
            "来源": [label for label, _ in targets],
            "dotnet_types": dstats,
        },
        "ambiguous": ambiguous,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print()
    print(f"名称库: {len(names)} 个 ID")
    for k, v in sorted(stats.items()):
        print(f"   {k:<12} {v}")
    print(f"   {'多候选':<12} {stats['多候选']}（已取最大下标，明细见 ambiguous 字段）")
    print(f"   -> {OUT}  {OUT.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

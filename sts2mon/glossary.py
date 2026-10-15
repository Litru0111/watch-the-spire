"""ID -> 中文名解析。

数据来自游戏内 mod 导出的 object-catalog.json（1164 个对象），
另外内建一份兜底表，用于 catalog 缺失时的常见 ID。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from . import paths

# kind 取值（来自 object-catalog 观测）
KIND_CHARACTER = 0
KIND_CARD = 1
KIND_RELIC = 2
KIND_MONSTER = 3
KIND_EVENT = 4

KIND_NAMES = {
    KIND_CHARACTER: "角色",
    KIND_CARD: "卡牌",
    KIND_RELIC: "遗物",
    KIND_MONSTER: "怪物",
    KIND_EVENT: "事件",
}

# 前缀 -> kind，用于从 "CARD.XXX" 这类 ID 反推类型
PREFIX_KIND = {
    "CARD": KIND_CARD,
    "RELIC": KIND_RELIC,
    "MONSTER": KIND_MONSTER,
    "ENCOUNTER": KIND_MONSTER,
    "EVENT": KIND_EVENT,
    "CHARACTER": KIND_CHARACTER,
    "POTION": KIND_RELIC,
    "ACT": KIND_EVENT,
}

# 兜底中文名（catalog 里没有、但日志/统计里常见的 ID）
FALLBACK: dict[str, str] = {
    # 地图点类型
    "ancient": "远古",
    "monster": "普通战斗",
    "elite": "精英",
    "boss": "Boss",
    "shop": "商店",
    "rest_site": "休息处",
    "treasure": "宝箱",
    "unknown": "未知",
    "event": "事件",
    # 角色
    "IRONCLAD": "铁甲战士",
    "SILENT": "静默猎手",
    "REGENT": "储君",
    "NECROBINDER": "亡灵契约师",
    "DEFECT": "故障机器人",
    # 遭遇后缀
    "WEAK": "弱",
    "NORMAL": "普通",
    # 状态
    "NONE.NONE": "无",
    "NONE": "无",
    # 常见状态/增益
    "WEAK_POWER": "虚弱",
    "VULNERABLE_POWER": "易伤",
    "FRAIL_POWER": "脆弱",
    "STRENGTH_POWER": "力量",
    "DEXTERITY_POWER": "敏捷",
    "POISON_POWER": "中毒",
    "Vulnerable": "易伤",
    "Weak": "虚弱",
    "Frail": "脆弱",
    "Strength": "力量",
    "Dexterity": "敏捷",
    "Poison": "中毒",
    "Other": "其他",
}

# 遭遇名后缀 -> 中文（STS2 内部用 `_WEAK` / `_NORMAL` / `_BOSS` 等区分强度）
ENCOUNTER_SUFFIX = {
    "WEAK": "（弱）",
    "NORMAL": "（普通）",
    "ELITE": "（精英）",
    "BOSS": "（Boss）",
    "HARD": "（困难）",
}

# 游戏内部用「复数/群体名」表示编队遭遇（EXOSKELETONS、CORPSE_SLUGS…），
# 而 object-catalog 只收录了单体怪物条目，所以需要这张映射表补全中文。
# 来源标注：
#   [目录] = 由 object-catalog 里的单体条目直接推出
#   [待核对] = 目录中查无此 ID，译名为推测，请以游戏内文本为准
ENCOUNTER_OVERRIDES: dict[str, str] = {
    # ---- 由目录单体条目推出 ----
    "LEAF_SLIME": "树叶史莱姆",          # [目录] LEAF_SLIME_M/S
    "TWIG_SLIME": "树枝史莱姆",          # [目录] TWIG_SLIME_M/S
    "SLIME": "史莱姆",                   # [目录] 若干史莱姆条目
    "EXOSKELETON": "外骨骼虫",           # [目录] EXOSKELETON
    "CORPSE_SLUG": "噬尸蛞蝓",           # [目录] CORPSE_SLUG
    "TOADPOLE": "蟾蜍蝌蚪",              # [目录] TOADPOLE
    "SPINY_TOAD": "棘刺蟾蜍",            # [目录] SPINY_TOAD
    "NIBBIT": "小啃兽",                  # [目录] NIBBIT
    "BOWLBUG": "盛碗虫",                 # [目录] BOWLBUG_EGG/SILK/NECTAR/ROCK
    "CHOMPER": "啃咬机",                 # [目录] CHOMPER
    "INKLET": "墨宝",                    # [目录] INKLET
    "INFESTED_PRISM": "感染棱柱",        # [目录] INFESTED_PRISM
    "TWO_TAILED_RAT": "双尾鼠",          # [目录] TWO_TAILED_RAT
    "SCROLL_OF_BITING": "咬人卷轴",      # [目录] SCROLL_OF_BITING
    "MYTE": "异螨",                      # [目录] MYTE
    "FUZZY_WURM_CRAWLER": "毛绒伏地虫",  # [目录] FUZZY_WURM_CRAWLER
    "CALCIFIED_CULTIST": "钙化邪教徒",   # [目录] CALCIFIED_CULTIST
    "DAMP_CULTIST": "潮湿邪教徒",        # [目录] DAMP_CULTIST
    "CULTIST": "邪教徒",                 # [目录] 由上面两条推出
    "AXEBOT": "巨斧机器人",              # [目录] AXEBOT
    "PHANTASMAL_GARDENER": "花园幽灵鳗",  # [目录] PHANTASMAL_GARDENER
    "DECIMILLIPEDE": "残杀千足虫",       # [目录] DECIMILLIPEDE_SEGMENT_*
    "VINE_SHAMBLER": "藤蔓蹒跚者",       # [目录] VINE_SHAMBLER
    "CUBEX_CONSTRUCT": "立柱构造体",     # [目录] CUBEX_CONSTRUCT
    "SEAPUNK": "海洋混混",               # [目录] SEAPUNK
    "SEWER_CLAM": "下水道蚌",            # [目录] SEWER_CLAM
    "SHRINKER_BEETLE": "缩小甲虫",       # [目录] SHRINKER_BEETLE
    "SLUMBERING_BEETLE": "熟睡甲虫",     # [目录] SLUMBERING_BEETLE
    "THIEVING_HOPPER": "偷窃草蜢",       # [目录] THIEVING_HOPPER
    "TUNNELER": "地道虫",                # [目录] TUNNELER
    "LOUSE_PROGENITOR": "虱虫之祖",      # [目录] LOUSE_PROGENITOR
    "FOGMOG": "雾菇",                    # [目录] FOGMOG
    "MAWLER": "蛮兽",                    # [目录] MAWLER
    "OVICOPTER": "直飞产卵虫",           # [目录] OVICOPTER
    "GREMLIN_MERC": "地精佣兵",          # [目录] GREMLIN_MERC
    "SOUL_FYSH": "灵魂异鱼",             # [目录] SOUL_FYSH
    "HAUNTED_SHIP": "幽灵船",            # [目录] HAUNTED_SHIP
    "THE_OBSCURA": "胧光怪",             # [目录] THE_OBSCURA
    "HUNTER_KILLER": "猎人杀手",         # [目录] HUNTER_KILLER
    "CEREMONIAL_BEAST": "仪式兽",        # [目录] CEREMONIAL_BEAST
    "THE_INSATIABLE": "无厌沙虫",        # [目录] THE_INSATIABLE
    "BYGONE_EFFIGY": "旧日雕像",         # [目录] BYGONE_EFFIGY
    "PHROG_PARASITE": "异蛙寄生虫",      # [目录] PHROG_PARASITE
    "VANTOM": "墨影幻灵",                # [目录] VANTOM
    # 复数形式（目录里只有单数条目，靠去复数推导不到时兜底）
    "SLUGS": "蛞蝓",                     # [目录] CORPSE_SLUG 噬尸蛞蝓
    "PRISMS": "棱柱",                    # [目录] INFESTED_PRISM 感染棱柱
    "CRABS": "蟹",                       # [待核对]
    # ---- 目录中查无此 ID，译名待核对 ----
    "KAISER_CRAB": "帝皇蟹",             # 译名来自游戏内 rdps 房间标签（官方 UI 文本）
    "CONSTRUCT_MENAGERIE": "构装体动物园",  # [待核对]
    "OVERGROWTH_CRAWLERS": "蔓生爬虫群",    # [待核对]
    "RUBY_RAIDER": "红宝石劫掠者",        # [待核对] 单体名见 AXE/ASSASSIN/BRUTE_RUBY_RAIDER
    "RUBY_RAIDERS": "红宝石劫掠团",       # [待核对]
    "THE_KIN": "同族",                   # [待核对] 单体名见 KIN_FOLLOWER / KIN_PRIEST
    "KNIGHTS": "骑士团",                 # [待核对]
    "MYSTERIOUS_KNIGHT": "神秘骑士",      # [待核对]
    "EVENT_ENCOUNTER": "事件遭遇",        # [待核对]
}

# 明确「译名未经官方文本核对」的 ID，界面上会加 ? 标记
UNVERIFIED: set[str] = {
    "CONSTRUCT_MENAGERIE", "OVERGROWTH_CRAWLERS", "RUBY_RAIDER", "RUBY_RAIDERS",
    "THE_KIN", "KNIGHTS", "MYSTERIOUS_KNIGHT", "EVENT_ENCOUNTER", "CRABS",
}

DEFAULT_CATALOGS = [
    paths.sts2_appdata() / "DanmakuSpire" / "object-catalog.json",
]

# 名称库的 kind 字符串 -> 本模块的 kind 常量
LIB_KIND = {
    "card": KIND_CARD,
    "relic": KIND_RELIC,
    "monster": KIND_MONSTER,
    "encounter": KIND_MONSTER,
    "event": KIND_EVENT,
    "character": KIND_CHARACTER,
    "potion": KIND_RELIC,
    "power": KIND_RELIC,
}


def name_library_path() -> Path:
    """自建名称库的位置（`sts2mon extract` 生成）。每次调用都重新求值。"""
    return paths.data_dir() / "name_library.json"


# 归一化重名时的 kind 优先级：卡牌 > 遗物 > 怪物/遭遇 > 角色 > 事件 > 其它。
#
# 中文重名很常见（5 张「打击」、5 张「防御」），而 `canonical()` 只能给
# 一个答案。选错的后果不是「名字不好看」，而是**功能出错**：
# `is_card()` 靠 `canonical()` 判定一次出牌是不是卡牌，
# 判错就会把敌方伤害算到我方头上（见 adapters/damage_meter 的 actor_kind）。
# 实测 `进阶之灾` 被杂项 `LEVEL_05` 抢走，导致真卡 `ASCENDERS_BANE`
# 判成非卡牌，模拟器随即报假 bug。
_NAME_KIND_RANK = {
    KIND_CARD: 0,
    KIND_RELIC: 1,
    KIND_MONSTER: 2,
    KIND_CHARACTER: 3,
    KIND_EVENT: 4,
}
_NAME_KIND_RANK_DEFAULT = 5


def split_id(raw: str) -> tuple[str | None, str]:
    """'CARD.SHRUG_IT_OFF' -> ('CARD', 'SHRUG_IT_OFF')；'SHRUG_IT_OFF' -> (None, ...)"""
    if "." in raw:
        prefix, _, rest = raw.partition(".")
        if prefix.isupper() and rest:
            return prefix, rest
    return None, raw


class Glossary:
    """ID -> 中文名。线程内缓存，读不到 catalog 时退回兜底表。"""

    def __init__(self, catalog_paths: list[Path] | None = None) -> None:
        self.by_id: dict[str, str] = {}
        self.by_kind: dict[int, dict[str, str]] = {}
        self.by_name: dict[str, str] = {}     # 中文名 -> 规范 ID（用于归一化）
        self._name_kind: dict[str, int | None] = {}   # 重名裁决用：名 -> kind
        self._owners: dict[str, str] = {}     # 卡牌/遗物 ID -> 归属角色名
        self._duplicate_names: set[str] = set()   # 对应多张卡的重复中文名
        self._potion_cache: dict[str, str] | None = None  # 药水中文名（懒加载）
        self.mod_objects: dict[str, str] = {}
        self.source: str | None = None
        self.library_size = 0        # 自建名称库条目数（0 = 没跑过 extract）
        self.catalog_loaded = False  # 是否读到 DanmakuSpire 的 catalog（可选）
        # 加载顺序 = 优先级：先自建名称库（游戏本体），再 mod 的 catalog。
        # 早先只认 catalog，于是没装 DanmakuSpire 就只能显示英文 ID。
        self._load_name_library(name_library_path())
        self._load(catalog_paths or DEFAULT_CATALOGS)
        self._compute_duplicate_names()

    @staticmethod
    def _rank(oid: str, kind: int | None) -> tuple[int, int, str]:
        """重名裁决的排序键（越小越优先）。"""
        return (_NAME_KIND_RANK.get(kind, _NAME_KIND_RANK_DEFAULT)
                if kind is not None else _NAME_KIND_RANK_DEFAULT,
                # 同 kind 内优先更短的 ID（`STRIKE` 优于 `STRIKE_IRONCLAD`）
                len(oid), oid)

    def _register_name(self, name: str, oid: str, kind: int | None) -> None:
        """登记「中文名 -> 规范 ID」，重名时按 kind 优先级裁决。"""
        for key in (name, name.lower()):
            cur = self.by_name.get(key)
            if cur is None:
                self.by_name[key] = oid
                self._name_kind[key] = kind
                continue
            if self._rank(oid, kind) < self._rank(cur, self._name_kind.get(key)):
                self.by_name[key] = oid
                self._name_kind[key] = kind

    def _register(self, oid: str, name: str, kind: int | None,
                  owner: str = "", from_mod: bool = False) -> None:
        """登记一个 ID。**先到先得**，所以调用顺序就是优先级。"""
        self.by_id.setdefault(oid, name)
        # 反向索引：本地化名 -> 规范 ID（rdps 等数据源只给本地化名）
        self._register_name(name, oid, kind)
        if owner and kind in (KIND_CARD, KIND_RELIC):
            self._owners.setdefault(oid, owner)
        if kind is not None:
            self.by_kind.setdefault(kind, {})[oid] = name
            # 同时登记带前缀形式，方便 CARD.XXX 查询
            for prefix, k in PREFIX_KIND.items():
                if k == kind:
                    self.by_id.setdefault(f"{prefix}.{oid}", name)
        if from_mod:
            self.mod_objects[oid] = name

    def _load_name_library(self, p: Path) -> bool:
        """加载自建名称库（游戏本体译名）。返回是否加载成功。

        这份是**主源**：它直接从游戏 `.pck` 的本地化表取名字，
        不依赖任何模组，而且译名与游戏本体一致
        （实测 `SILENT` 本体是「静默猎手」，DanmakuSpire 的 catalog 写「夜隐猫忍」）。
        """
        if not p.is_file():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return False
        entries = data.get("names")
        if not isinstance(entries, dict) or not entries:
            return False
        for oid, e in entries.items():
            if not isinstance(e, dict):
                continue
            nm = (e.get("name") or "").strip()
            if not oid or not nm:
                continue
            kind = LIB_KIND.get(str(e.get("kind") or ""))
            # 遭遇键**原样保留**（不剥 _WEAK/_NORMAL/_BOSS）：
            # 游戏给每个强度单独写了原文，比「基础名+（弱）」信息量大得多，
            # 例如 NIBBITS_NORMAL=「两只小啃兽」、NIBBITS_WEAK=「一只落单的小啃兽」。
            # 早期用「剥后缀再补（弱）」是因为当时只有单体怪物条目的译名可用。
            # 精确键查不到时仍会退回那套后缀逻辑（见 encounter()）。
            self._register(oid, nm, kind)
        self.source = str(p)
        self.library_size = len(entries)
        return True

    def _load(self, paths: list[Path]) -> None:
        for p in paths:
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            objs = data.get("objects") if isinstance(data, dict) else data
            if not isinstance(objs, list):
                continue
            for o in objs:
                oid, name, kind = o.get("id"), o.get("name"), o.get("kind")
                if not oid or not name:
                    continue
                self._register(oid, name, kind if isinstance(kind, int) else None,
                               owner=str(o.get("ownerCharacterName") or ""),
                               from_mod=bool(o.get("fromMod")))
            # 名称库已经定过 source 就别覆盖（它是主源）
            if self.source is None:
                self.source = str(p)
            self.catalog_loaded = True

    # ---------- 归一化 ----------
    def canonical(self, raw: str | None, kind: int | None = None) -> str:
        """把「本地化名或任何 ID 形式」归一化成规范 ID。

        例：'小刀' -> 'SHIV'，'CARD.SHIV' -> 'SHIV'，'SHIV' -> 'SHIV'。
        查不到时原样返回。
        """
        if not raw:
            return raw or ""
        _, bare = split_id(raw)
        if bare in self.by_id:
            return bare
        if bare in self.by_name:            # 本地化名 -> 规范 ID
            return self.by_name[bare]
        for key in (raw, raw.lower(), bare.lower()):
            hit = self.by_name.get(key)
            if hit:
                return hit
        return bare

    # ---------- 查询 ----------
    def name(self, raw: str | None, default: str = "?") -> str:
        if not raw:
            return default
        if raw in self.by_id:
            return self.by_id[raw]
        prefix, bare = split_id(raw)
        if bare in self.by_id:
            return self.by_id[bare]
        if prefix in ("ENCOUNTER", "MONSTER"):
            base, suffix = self._strip_encounter_suffix(bare)
            if base:
                zh = self.by_id.get(base) or self.by_id.get(f"MONSTER.{base}")
                if zh:
                    return zh + suffix
        # 药水 / 自定义修正项等：object-catalog 里**没有**这些类别
        # （它只含卡牌/遗物/怪物/事件），名称库查不到，得从 pck 提取的文案表里取。
        # 不查这张表就只能显示英文 ID（Energy Potion / MODIFIER.DRAFT），实测踩过。
        extra = self._extra_names()
        hit = extra.get(bare) or extra.get(raw)
        if hit:
            return hit
        if raw in FALLBACK:
            return FALLBACK[raw]
        if bare in FALLBACK:
            return FALLBACK[bare]
        return self._prettify(bare)

    def _extra_names(self) -> dict[str, str]:
        """补充名称表：pck 文案里所有带中文 title 的对象（药水、修正项、状态…）。

        该表由 tools/extract_effects.py 提取。名称库（object-catalog）只覆盖
        卡牌/遗物/怪物/事件，药水与自定义修正项都不在其中，所以必须并入 ——
        否则 `MODIFIER.DRAFT` 只能显示成英文 ID。
        """
        if self._potion_cache is not None:
            return self._potion_cache
        out: dict[str, str] = {}
        # 走 paths：这样 STS2MON_DATA 一改，提取出来的文案就跟着换位置
        p = paths.data_dir() / "card_effects.json"
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                for k, e in (data.get("文案") or {}).items():
                    # 只要中文标题；`zh?` 是仅凭汉字判定的（多为 mod 文案）也算
                    if e.get("lang") not in ("zh", "zh?"):
                        continue
                    title = (e.get("title") or "").strip()
                    if not title:
                        continue
                    out[k] = title
                    # 带前缀的键（MODIFIER.DRAFT / EVENT.NEOW）也登记裸 ID
                    if "." in k:
                        out.setdefault(k.split(".")[-1], title)
            except Exception:
                pass
        self._potion_cache = out
        return out

    # 旧名保留，避免外部引用失效
    _potion_names = _extra_names

    def encounter(self, raw: str | None) -> str:
        """遭遇名 -> 中文，例如 SEAPUNK_WEAK -> 海洋混混（弱）
        解析顺序：
          1. 名称库精确命中
          2. 剥离强度后缀（_WEAK/_NORMAL/_ELITE/_BOSS）后查名称库与遭遇映射表，
             再尝试去复数（EXOSKELETONS -> EXOSKELETON）
          3. 逐段最长匹配组合（应对 A_B_C 型编队名）
          4. 兜底英文
        目录里查不到的译名会加 `?` 标记，提示待核对。
        """
        if not raw:
            return "?"
        _, bare = split_id(raw)
        if bare in self.by_id:
            return self.by_id[bare]

        base, suffix = self._strip_encounter_suffix(bare)

        # 2. 去后缀（+去复数）精确命中
        for cand in self._singular_variants(base or bare):
            zh = self.by_id.get(cand) or self.by_id.get(f"MONSTER.{cand}")
            if zh:
                return zh + suffix
            if cand in ENCOUNTER_OVERRIDES:
                return self._mark(ENCOUNTER_OVERRIDES[cand], cand) + suffix

        # 3. 逐段最长匹配组合
        parts = (base or bare).split("_")
        if len(parts) > 1:
            pieces: list[str] = []
            unverified = False
            i = 0
            while i < len(parts):
                hit = self._match_span(parts, i)
                if hit:
                    zh, span, unv = hit
                    pieces.append(zh)
                    unverified = unverified or unv
                    i += span
                else:
                    token = parts[i]
                    if len(token) >= 3:
                        pieces.append(self._prettify(token))
                    i += 1
            if pieces:
                return "+".join(pieces) + ("?" if unverified else "") + suffix

        return self._prettify(bare) + suffix

    def _match_span(self, parts: list[str], i: int) -> tuple[str, int, bool] | None:
        """从 parts[i] 开始取最长可解析片段，返回 (中文, 消耗段数, 是否待核对)。"""
        for size in range(len(parts) - i, 0, -1):
            cand = "_".join(parts[i:i + size])
            if len(cand) < 3:            # 忽略 S / M 之类 1-2 字符别名碎片
                continue
            zh = self.by_id.get(cand) or self.by_id.get(f"MONSTER.{cand}")
            if zh:
                return zh, size, False
            if cand in ENCOUNTER_OVERRIDES:
                return ENCOUNTER_OVERRIDES[cand], size, cand in UNVERIFIED
            if cand.endswith("S") and len(cand) > 4:
                sing = cand[:-1]
                zh2 = self.by_id.get(sing) or self.by_id.get(f"MONSTER.{sing}") \
                    or ENCOUNTER_OVERRIDES.get(sing)
                if zh2:
                    return zh2, size, sing in UNVERIFIED and sing not in self.by_id
        return None

    @staticmethod
    def _singular_variants(token: str) -> list[str]:
        """生成候选形式：原样 / 去复数 S / 各段去复数。"""
        out = [token]
        if token.endswith("S") and len(token) > 4:
            out.append(token[:-1])
        parts = token.split("_")
        if len(parts) > 1 and parts[-1].endswith("S") and len(parts[-1]) > 4:
            out.append("_".join(parts[:-1] + [parts[-1][:-1]]))
        return out

        return self._prettify(bare) + suffix

    def is_known(self, raw: str | None) -> bool:
        """该 ID 是否在名称库里（用于过滤脏数据条目）。"""
        if not raw:
            return False
        _, bare = split_id(raw)
        return bare in self.by_id or bare in self.by_kind.get(KIND_CARD, {}) \
            or bare in self.by_kind.get(KIND_MONSTER, {}) \
            or bare in self.by_kind.get(KIND_RELIC, {})

    def kind_of(self, raw: str | None) -> int | None:
        """这个 ID/名字是什么类别（KIND_CARD / KIND_MONSTER …），查不到返回 None。

        用于区分「卡牌」与「怪物」：DamageMeter 的事件只给本地化名，
        而**怪物名也在名称库里**，所以不能靠「认不认识」来判断敌我。
        """
        if not raw:
            return None
        _, bare = split_id(raw)
        for kind, table in self.by_kind.items():
            if bare in table:
                return kind
        # 本地化名反向查
        cid = self.by_name.get(bare) or self.by_name.get(bare.lower())
        if cid:
            for kind, table in self.by_kind.items():
                if cid in table:
                    return kind
        return None

    def is_card(self, raw: str | None) -> bool:
        """是不是卡牌（怪物名、遭遇名都会返回 False）。"""
        return self.kind_of(raw) == KIND_CARD

    def is_monster(self, raw: str | None) -> bool:
        """是不是怪物/敌人名。"""
        return self.kind_of(raw) == KIND_MONSTER

    def label(self, raw: str | None) -> str:
        """用于表格展示的名字。

        基础卡有角色专属版本（STRIKE_IRONCLAD / STRIKE_SILENT 都叫「打击」），
        只显示名字会出现看起来重复的行，所以**仅对重名卡**带上归属角色，
        其余保持干净（比如「中和」不会变成「中和（静默猎手）」）。
        """
        if not raw:
            return "?"
        _, bare = split_id(raw)
        # 已经给的是中文名时，先归一化成规范 ID，才能查到归属
        cid = bare if bare in self._owners or bare in self.by_id else self.canonical(raw)
        name = self.by_id.get(cid) or self.name(raw)
        owner = self.owner_of(cid)
        if name in self._duplicate_names and owner:
            return f"{name}（{owner}）"
        return name

    def owner_of(self, card_id: str | None) -> str:
        """卡牌的归属角色中文名（无归属则返回空串）。"""
        if not card_id:
            return ""
        return self._owners.get(card_id, "")

    def _compute_duplicate_names(self) -> None:
        """找出「同一个中文名对应多张不同卡」的名字集合。"""
        from collections import Counter
        seen: Counter[str] = Counter()
        for cid, name in self.by_kind.get(KIND_CARD, {}).items():
            seen[name] += 1
        self._duplicate_names = {n for n, c in seen.items() if c > 1}

    @staticmethod
    def _mark(name: str, key: str) -> str:
        return name + "?" if key in UNVERIFIED else name

    def is_verified(self, raw: str | None) -> bool:
        """该 ID 的中文名是否来自官方目录（而非推测映射）。"""
        if not raw:
            return False
        _, bare = split_id(raw)
        if bare in self.by_id:
            return True
        base, _ = self._strip_encounter_suffix(bare)
        if base and (base in self.by_id or base in UNVERIFIED):
            return base not in UNVERIFIED
        for part in bare.split("_"):
            if part in UNVERIFIED and part not in self.by_id:
                return False
        return bare in ENCOUNTER_OVERRIDES and bare not in UNVERIFIED

    def card(self, raw: str | None) -> str:
        return self.name(raw)

    def char(self, raw: str | None) -> str:
        if not raw:
            return "?"
        prefix, bare = split_id(raw)
        return self.by_id.get(bare) or self.by_id.get(f"CHARACTER.{bare}") or self.name(raw)

    @staticmethod
    def _strip_encounter_suffix(bare: str) -> tuple[str | None, str]:
        for suf, zh in ENCOUNTER_SUFFIX.items():
            if bare.endswith("_" + suf):
                return bare[: -(len(suf) + 1)], zh
        return None, ""

    @staticmethod
    def _prettify(bare: str) -> str:
        """SHRUG_IT_OFF -> Shrug It Off（找不到中文时的英文兜底）"""
        if not bare or bare == "?":
            return bare
        if re.fullmatch(r"[A-Z0-9_+]+", bare):
            return bare.replace("_", " ").title()
        return bare

    # ---------- 统计 ----------
    @property
    def size(self) -> int:
        return len(self.by_id)

    def stats(self) -> dict[str, int]:
        return {KIND_NAMES.get(k, str(k)): len(v) for k, v in sorted(self.by_kind.items())}


@lru_cache(maxsize=1)
def default_glossary() -> Glossary:
    return Glossary()

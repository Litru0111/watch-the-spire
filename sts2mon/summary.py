"""局后总结与数值预测。

两个独立能力：
1. `summarize(store, run_id)` —— 对局结束后的整体评估：
   多维评分 + 等级、全局路线、数值变化曲线、卡牌抓取与事件选择汇总。
2. `predict_live(snapshot)` / `hp_projection(...)` —— 我方数值预测：
   按目前掉血速度外推，判断「这一局是否会因为血量耗尽而结束」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import effects
from .glossary import Glossary, default_glossary
from .model import Store

# ---------------------------------------------------------------- 评分权重
#
# **遗物单开一栏**（遗物是全局增益，不属于牌组）。
#
# 「牌组质量」用**卡牌强度**衡量（某张牌 = 抓了它的对局里通关的比例，
# 再做收缩估计），但当前只作为**参考指标、不计分**：
# 本机样本下通关只有十几个，收缩后几乎所有对局都挤在总体均值附近，
# 分位排序没有信息量。等通关样本攒到 DECk_SCORING_MIN_WINS 以上再启用。
WEIGHTS = {
    "progress": 30,      # 推进深度
    "combat": 25,        # 战斗效率
    "survival": 20,      # 生存能力
    "economy": 15,       # 资源管理
    "relics": 10,        # 遗物
}
GRADES = [(92, "S"), (84, "A"), (74, "B"), (62, "C"), (48, "D"), (0, "E")]

# 卡牌强度收缩估计的强度系数：k 越大越保守（越偏向总体均值）。
# 本机样本下 k=8 能把单卡估计的跨度从 100% 压到约 22%，
# 既保留强弱区分、又不至于把噪声当成结论。样本不足时自动偏向均值。
CARD_SHRINK_K = 8.0

# 通关样本达到这个数量后，「牌组质量」才计入总分。
# 依据：13 个通关样本下 99/105 局的值完全相同（都等于总体通关率），
# 排序没有意义；要拉开区分度大约需要几百个通关样本。
DECK_SCORING_MIN_WINS = 300

# 遗物评分兜底满分线（校准数据不足时用）
RELIC_FULL = 14.0

# 每个 Act 大致楼层数（用于深度评分；数据来自本机 97 场统计）
ACT_FLOOR_SPAN = {1: 17, 2: 15, 3: 16, 4: 10}
MAX_ACTS = 4

# 战斗效率参考值（本机 97 场均值，用于把「每回合伤害」映射成 0~1）
DPT_REFERENCE = 15.0
DMG_PER_FLOOR_REF = 14.0


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class ScoreItem:
    key: str
    label: str
    score: float          # 0 ~ 满分
    full: int             # 满分
    detail: str = ""

    @property
    def ratio(self) -> float:
        return round(self.score / self.full * 100, 1) if self.full else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "score": round(self.score, 1),
                "full": self.full, "ratio": self.ratio, "detail": self.detail}


# ---------------------------------------------------------------- 提取器

def _card_drafts(store: Store, run_id: str, g: Glossary,
                 player_id: str | None = None) -> list[dict[str, Any]]:
    """卡牌抓取记录：每次三选一/获得卡牌都记一条。

    player_id=None 时取「第一顺位玩家」（兼容旧的单人调用）。
    """
    if player_id is None:
        ps = store.players(run_id)
        player_id = ps[0]["player_id"] if ps else None
    out: list[dict[str, Any]] = []
    for n in store.nodes(run_id):
        stats = [s for s in (n.get("player_stats") or [])
                 if player_id is None or s["player_id"] == player_id]
        for s in stats:
            choices = s.get("card_choices") or []
            offers = [c for c in choices if c.get("card")]
            for c in offers:
                cid = c["card"]
                # 动作类型以适配器给的 kind 为准（变化 ≠ 跳过）
                kind = c.get("kind") or ("picked" if c.get("picked") else "skipped")
                # 升级：卡牌升级不换 ID，显示名加 `+`，效果用升级后的数值
                name = g.label(cid)
                eff = effects.effect_with_values(cid)
                if kind == "upgraded":
                    name = effects.upgraded_name(cid, name)
                    up = effects.effect_upgraded(cid)
                    if up:
                        eff = up
                out.append({
                    "floor": n["ord"], "act": n["act"],
                    "player_id": s["player_id"],
                    "type": "choice", "kind": kind,
                    "card_id": cid, "name": name,
                    "picked": bool(c.get("picked")),
                    "upgraded": kind == "upgraded",
                    "effect": eff,
                    "effect_base": effects.effect_with_values(cid),
                    "effect_upgraded": effects.effect_upgraded(cid),
                    "into_name": c.get("into_name") or "",
                    "from_name": c.get("from_name") or "",
                    "room": n.get("room_label") or n.get("type_label") or "",
                })
            # 不在三选一里、但确实进了牌组的（事件/商店/宝箱）
            offered_ids = {c["card"] for c in offers}
            for cid in (s.get("cards_gained") or []):
                if cid not in offered_ids:
                    out.append({
                        "floor": n["ord"], "act": n["act"],
                        "player_id": s["player_id"],
                        "type": "gained", "kind": "gained",
                        "card_id": cid, "name": g.label(cid),
                        "picked": True,
                        "room": n.get("room_label") or n.get("type_label") or "",
                    })
            for cid in (s.get("cards_removed") or []):
                out.append({
                    "floor": n["ord"], "act": n["act"],
                    "player_id": s["player_id"],
                    "type": "removed", "kind": "removed",
                    "card_id": cid, "name": g.label(cid),
                    "picked": False,
                    "room": n.get("room_label") or n.get("type_label") or "",
                })
            # 开场变化（cards_transformed）：原牌 → 新牌，标成「变化」
            for c in (s.get("card_choices") or []):
                if c.get("into") and c.get("kind") == "transformed":
                    out.append({
                        "floor": n["ord"], "act": n["act"],
                        "player_id": s["player_id"],
                        "type": "transformed", "kind": "transformed",
                        "card_id": c["card"], "name": g.label(c["card"]),
                        "picked": False, "into_name": c.get("into_name") or "",
                        "room": n.get("room_label") or n.get("type_label") or "",
                    })
    # 变化行会把原牌重复一次（既在 offers 里又在 transformed 里），去重
    seen: set[tuple] = set()
    uniq: list[dict[str, Any]] = []
    for r in out:
        key = (r["player_id"], r["floor"], r["card_id"], r.get("kind"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    out = uniq
    out.sort(key=lambda r: (r["floor"] or 0))
    return out


def _event_choices(store: Store, run_id: str, g: Glossary,
                   player_id: str | None = None) -> list[dict[str, Any]]:
    """事件 / 遗物 / 远古选择记录（按玩家分流）。"""
    if player_id is None:
        ps = store.players(run_id)
        player_id = ps[0]["player_id"] if ps else None
    out: list[dict[str, Any]] = []
    for n in store.nodes(run_id):
        s = next((x for x in (n.get("player_stats") or [])
                  if x["player_id"] == player_id), None)
        if s is None:
            continue
        ptype = n.get("point_type") or ""
        room = n.get("room_label") or g.name(ptype)
        events: list[dict[str, Any]] = []
        relics: list[dict[str, Any]] = []
        for c in (s.get("card_choices") or []):
            if c.get("event"):
                events.append({"kind": "event", "id": c["event"],
                               "name": g.name(c["event"])})
            if c.get("relic"):
                relics.append({"kind": "relic", "id": c["relic"],
                               "name": g.name(c["relic"])})
        potions = [{"kind": "potion", "id": p, "name": g.name(p)}
                   for p in (s.get("potions") or []) if p]
        if events or relics or potions or ptype in ("unknown", "event", "ancient"):
            out.append({
                "floor": n["ord"], "act": n["act"], "type": ptype,
                "type_label": g.name(ptype), "room": room,
                "room_id": n.get("room_id") or "",
                "player_id": player_id,
                "events": events, "relics": relics, "potions": potions,
                "hp_after": s.get("hp"), "gold": s.get("gold"),
            })
    return out


def _hp_curve(store: Store, run_id: str,
              player_id: str | None = None) -> list[dict[str, Any]]:
    """HP / 金币 / 伤害随楼层的数值变化（某位玩家的视角）。"""
    if player_id is None:
        ps = store.players(run_id)
        player_id = ps[0]["player_id"] if ps else None
    out: list[dict[str, Any]] = []
    for n in store.nodes(run_id):
        if not n.get("visited"):
            continue
        s = next((x for x in (n.get("player_stats") or [])
                  if x["player_id"] == player_id), None)
        if s is None:
            continue
        out.append({
            "floor": n["ord"], "act": n["act"],
            "type": n.get("point_type"), "room": n.get("room_label") or "",
            "hp": s.get("hp"), "max_hp": s.get("max_hp"),
            "gold": s.get("gold"),
            "damage_taken": s.get("damage_taken") or 0,
            "hp_healed": s.get("hp_healed") or 0,
        })
    return out


def _route(store: Store, run_id: str, g: Glossary) -> list[dict[str, Any]]:
    """全局路线：按 Act 分组的地图点序列。"""
    acts: dict[int, list[dict[str, Any]]] = {}
    for n in store.nodes(run_id):
        if not n.get("visited"):
            continue
        acts.setdefault(int(n["act"] or 1), []).append({
            "floor": n["ord"], "type": n.get("point_type"),
            "type_label": n.get("type_label") or g.name(n.get("point_type")),
            "room": n.get("room_label") or "",
            "turns": n.get("turns_taken") or 0,
            "hp": n.get("hp_after"), "max_hp": n.get("max_hp"),
            "damage_taken": n.get("damage_taken") or 0,
            "gold": n.get("gold"),
            "gained": [g.label(c) for c in (n.get("cards_gained") or [])],
            "relics": [g.name(r) for r in (n.get("relics_gained") or [])],
        })
    return [{"act": a, "floors": len(v), "nodes": v} for a, v in sorted(acts.items())]


# 角色初始生命上限（游戏默认值）。
# 说明：DamageMeter / rdps / .run 三个数据源都不记录「实时 HP」，
# 所以实时预测只能按角色基线推算，并在界面上明确标注这是假设值。
CHARACTER_BASE_HP = {
    "IRONCLAD": 80,
    "SILENT": 70,
    "DEFECT": 75,
    "REGENT": 75,
    "NECROBINDER": 72,
}
DEFAULT_BASE_HP = 75


def base_hp_for(character: str | None) -> tuple[int, str]:
    """返回 (基线生命上限, 说明)。"""
    if not character:
        return DEFAULT_BASE_HP, "未知角色，按默认 75 推算"
    key = character.split(".")[-1].upper()
    if key in CHARACTER_BASE_HP:
        return CHARACTER_BASE_HP[key], f"{key} 的默认生命上限 {CHARACTER_BASE_HP[key]}"
    return DEFAULT_BASE_HP, f"未知角色 {key}，按默认 {DEFAULT_BASE_HP} 推算"


# ---------------------------------------------------------------- 分位校准
# 用「本机历史对局的分布」做参照，把原始指标映射成分位（0~100），
# 这样分数是可解释的（60 分 = 好于 60% 的历史对局），而不是拍脑袋的绝对值。
#
# 注意：只有带 rdps 战斗明细的对局才有「输出」数据（存档本身不记录我方输出），
# 所以战斗效率只在「有数据的对局」之间比较，避免拿 0 去比造成虚高。
_METRIC_KEYS = ("depth_ratio", "dpt", "taken_per_floor", "econ_ratio",
                "deck_strength", "relic_count")


def card_strength_table(store: Store, g: Glossary | None = None,
                        k: float = CARD_SHRINK_K) -> dict[str, Any]:
    """全库卡牌强度表（收缩估计）。

    某张牌的「原始强度」= 抓了它的对局里最终通关的比例。
    但样本少时这个比例噪声极大（赢 1 局 vs 赢 3 局差很多），
    所以做**收缩估计**：把每张牌朝总体通关率拉，样本越少拉得越狠。

        strength = (wins + k * prior) / (picks + k)

    这样 1 次抓取 0 胜的牌不会被当成「弱牌」，而是接近总体均值；
    只有抓取很多次、且胜率稳定偏离均值的牌才会被拉开。

    返回 {"prior": 总体通关率, "cards": {card_id: {...}}, "k": k}
    """
    g = g or default_glossary()
    runs = store.all_run_rows()
    n_runs = len(runs)
    if not n_runs:
        return {"prior": 0.0, "cards": {}, "k": k, "runs": 0}
    wins_total = sum(1 for r in runs if r.get("win"))
    prior = wins_total / n_runs

    picked: dict[str, set[str]] = {}
    won: dict[str, set[str]] = {}
    for r in runs:
        rid = r["run_id"]
        won_run = bool(r.get("win"))
        # 用整副牌（收尾时的 deck）而不是抓取流水：更贴近「最终牌组」
        for p in store.players(rid):
            for raw in (p.get("deck") or []):
                cid = str(raw).split(".")[-1]
                if not cid:
                    continue
                picked.setdefault(cid, set()).add(rid)
                if won_run:
                    won.setdefault(cid, set()).add(rid)

    cards: dict[str, Any] = {}
    for cid, rids in picked.items():
        n = len(rids)
        w = len(won.get(cid, ()))
        strength = (w + k * prior) / (n + k) if (n + k) else prior
        cards[cid] = {"picks": n, "wins": w,
                      "raw": (w / n) if n else prior,
                      "strength": strength,
                      "low_sample": n < 5}
    return {"prior": prior, "cards": cards, "k": k, "runs": n_runs,
            "wins_total": wins_total}


def deck_strength(drafts: list[dict[str, Any]], table: dict[str, Any],
                  g: Glossary | None = None) -> dict[str, Any]:
    """一副牌的强度 = 其中各牌收缩后强度的平均。

    单张牌的胜率不可靠，但**多张牌平均之后噪声互相抵消**，
    作为「整副牌的强度」指标是可用的。

    没抓到牌（例如整局没走战斗）时返回 available=False。
    """
    cards = table.get("cards") or {}
    prior = float(table.get("prior") or 0.0)
    picks = [d for d in drafts
             if d.get("picked") and d.get("kind") in ("picked", "gained", "upgraded")]
    if not picks:
        return {"available": False, "strength": 0.0, "prior": prior,
                "n": 0, "known": 0, "weakest": [], "strongest": []}
    vals: list[float] = []
    detail: list[tuple[str, float, int]] = []
    for d in picks:
        # 卡表用「裸 ID」建（players.deck 里是 `CARD.PREPARED`，已剥前缀），
        # 而抓取记录里可能带前缀 —— 这里必须归一化，否则永远查不到（实测踩过）
        raw_cid = str(d.get("card_id") or "")
        cid = raw_cid.split(".")[-1] if "." in raw_cid else raw_cid
        info = cards.get(cid) or cards.get(raw_cid)
        s = float(info["strength"]) if info else prior
        vals.append(s)
        detail.append((cid, s, int(info["picks"]) if info else 0))
    detail.sort(key=lambda x: x[1])
    return {
        "available": True,
        "strength": sum(vals) / len(vals),
        "prior": prior,
        "n": len(picks),
        # 「已知强度」= 库里至少抓过 5 次的牌（样本够），用于判断置信度
        "known": sum(1 for _, _, p in detail if p >= 5),
        "weakest": [{"card_id": c, "name": (g or default_glossary()).label(c),
                     "strength": s, "picks": p, "known": p >= 5}
                    for c, s, p in detail[:3]],
        "strongest": [{"card_id": c, "name": (g or default_glossary()).label(c),
                       "strength": s, "picks": p, "known": p >= 5}
                      for c, s, p in detail[-3:][::-1]],
    }


def _percentile(values: list[float], x: float, *, valid_only: bool = False) -> float:
    """x 在 values 中的分位（0~1）。

    valid_only=True 时忽略 0 值（表示「该数据源没有这项数据」），
    样本太少则退回固定参考值。
    """
    pool = [v for v in values if v > 0] if valid_only else list(values)
    if len(pool) < 8:
        return _clamp(x)
    return sum(1 for v in pool if v <= x) / len(pool)


def _raw_metrics(store: Store, run_id: str, g: Glossary,
                 player_id: str | None = None,
                 table: dict[str, Any] | None = None) -> dict[str, float]:
    """一个对局（或某位玩家）的原始指标，与分位校准解耦。

    player_id 指定时只统计该玩家的战斗/数值——多人对局每人一套。
    table 是卡牌强度表；批量算分时必须由调用方传进来复用，
    否则每局都要重建全库卡表（O(n²)，实测很慢）。
    """
    row = store.run_row(run_id)
    if not row:
        return {}
    combats = store.combats(run_id, player_id)
    nodes = [n for n in store.nodes(run_id, player_id) if n.get("visited")]
    floors = len(nodes)
    act_reached = max((int(n["act"] or 1) for n in nodes), default=1)
    span = sum(ACT_FLOOR_SPAN.get(a, 15) for a in range(1, act_reached + 1))
    depth = _clamp(floors / max(span, 1))
    if row.get("win"):
        depth = 1.0
    # 死在当前层段的 Boss 手里：人到了门口但没打过去，不算走完这一段
    elif floors >= span - 1:
        depth = min(depth, 0.92)

    total_turns = sum(int(c["turns"] or 0) for c in combats)
    total_dealt = sum(int(c["dmg_dealt"] or 0) for c in combats)
    dpt = (total_dealt / total_turns) if total_turns else 0.0
    avg_turns = (total_turns / len(combats)) if combats else 0.0

    taken = sum(int(c["dmg_taken"] or 0) for c in combats)
    taken_per_floor = taken / floors if floors else 0.0

    gold_gained = sum(int(n.get("gold_gained") or 0) for n in nodes)
    gold_spent = sum(int(n.get("gold_spent") or 0) for n in nodes)
    spend_ratio = (gold_spent / gold_gained) if gold_gained else 0.0
    shops = sum(1 for n in nodes if n.get("point_type") == "shop")
    econ = _clamp(0.6 * (1.0 - abs(spend_ratio - 0.65) / 0.65) + 0.4 * _clamp(shops / 3.0))

    drafts = _card_drafts(store, run_id, g, player_id)
    # 牌组质量 = 卡牌强度（收缩后的平均），不再用遗物数
    table = table if table is not None else card_strength_table(store, g)
    deck = deck_strength(drafts, table, g)
    # 遗物单开一栏
    relics = 0
    rp = next((p for p in store.players(run_id) if p["player_id"] == player_id), None)
    if rp is not None:
        relics = len(rp.get("relics") or [])
    elif player_id is None:
        relics = row.get("relic_count") or 0

    return {
        "depth_ratio": depth,
        "dpt": dpt,
        "avg_turns": avg_turns,
        "taken_per_floor": taken_per_floor,
        "econ_ratio": econ,
        "deck_strength": float(deck.get("strength") or 0.0),
        "deck_detail": deck,
        "relic_count": float(relics),
        "total_dealt": total_dealt,
        "total_taken": taken,
    }


def calibration(store: Store, g: Glossary | None = None,
                limit: int = 300) -> dict[str, list[float]]:
    """收集历史对局的指标分布，作为评分参照。

    卡牌强度表在这里**只建一次**并复用：它对每局都要扫全库牌组，
    放在循环里会变成 O(n²)（实测几十秒），批量算分必须共享。
    """
    g = g or default_glossary()
    out: dict[str, list[float]] = {k: [] for k in _METRIC_KEYS}
    table = card_strength_table(store, g)
    for rid in store.run_ids()[:limit]:
        m = _raw_metrics(store, rid, g, None, table)
        if not m:
            continue
        for k in _METRIC_KEYS:
            if k in m:
                out[k].append(m[k])
    return out


def score_run(store: Store, run_id: str, g: Glossary | None = None,
              calib: dict[str, list[float]] | None = None,
              player_id: str | None = None) -> dict[str, Any]:
    """对局评分：各维度分位 × 权重。

    player_id 指定时，只统计该玩家的战斗与数值（多人对局每人一套评分）。
    """
    g = g or default_glossary()
    row = store.run_row(run_id)
    if not row:
        return {}
    m = _raw_metrics(store, run_id, g, player_id)
    if not m:
        return {}
    calib = calib if calib is not None else calibration(store, g)
    won = bool(row.get("win"))

    # 推进深度：走到哪算哪；通关直接满分。
    # 死在层段 Boss 手里 = 到了门口没进去，按 92% 上限处理（已在 _raw_metrics 里做）
    depth_pct = 1.0 if won else _percentile(calib.get("depth_ratio", []),
                                            m.get("depth_ratio", 0.0))
    # 战斗效率：只在「有输出数据」的对局之间比较（存档不记录我方输出）
    combat_pct = _percentile(calib.get("dpt", []), m.get("dpt", 0.0), valid_only=True)
    # 生存：承伤越低越好 → 用反向分位
    taken_pct = _percentile(calib.get("taken_per_floor", []),
                            m.get("taken_per_floor", 0.0), valid_only=True)
    clean_pct = 1.0 - taken_pct
    if won:
        clean_pct = 1.0
    econ_pct = _percentile(calib.get("econ_ratio", []), m.get("econ_ratio", 0.0))
    deck_pct = _percentile(calib.get("deck_strength", []), m.get("deck_strength", 0.0))
    relic_pct = _percentile(calib.get("relic_count", []), m.get("relic_count", 0.0))
    dd = m.get("deck_detail") or {}
    # 通关样本够多时，「牌组质量」才计入总分（见 DECK_SCORING_MIN_WINS）
    table = card_strength_table(store, g)
    wins_total = int(table.get("wins_total") or 0)
    deck_scored = wins_total >= DECK_SCORING_MIN_WINS

    items = [
        ScoreItem("progress", "推进深度", depth_pct * WEIGHTS["progress"],
                  WEIGHTS["progress"],
                  f"{max((int(n['act'] or 1) for n in store.nodes(run_id)), default=1)} 个 Act"
                  f" · {int(row.get('floors') or 0)} 层"
                  + ("（通关）" if won else "")
                  + f" · 好于 {depth_pct*100:.0f}% 的历史对局"),
        ScoreItem("combat", "战斗效率", combat_pct * WEIGHTS["combat"], WEIGHTS["combat"],
                  (f"每回合输出 {m['dpt']:.1f} · 场均 {m['avg_turns']:.1f} 回合"
                   if m["dpt"] > 0 else "该对局无输出数据（缺 rdps 战斗明细）")
                  + f" · 好于 {combat_pct*100:.0f}% 有数据的对局"),
        ScoreItem("survival", "生存能力", clean_pct * WEIGHTS["survival"],
                  WEIGHTS["survival"],
                  f"每层承伤 {m['taken_per_floor']:.1f}"
                  + (f" · 死于 {row['killed_by']}" if row.get("killed_by") else " · 通关")
                  + f" · 好于 {clean_pct*100:.0f}%"),
        ScoreItem("economy", "资源管理", econ_pct * WEIGHTS["economy"], WEIGHTS["economy"],
                  f"结余 {max((n.get('gold') or 0) for n in store.nodes(run_id)) if store.nodes(run_id) else 0}"
                  f" · 好于 {econ_pct*100:.0f}%"),
        # 遗物单开一栏（遗物是全局增益，不属于牌组）
        ScoreItem("relics", "遗物", relic_pct * WEIGHTS["relics"], WEIGHTS["relics"],
                  f"{int(m.get('relic_count') or 0)} 个"
                  f" · 好于 {relic_pct*100:.0f}% 的历史对局"),
    ]
    # 牌组质量：样本足够时参与计分，否则只作为参考项展示
    deck_item = ScoreItem("deck", "牌组质量",
                          deck_pct * WEIGHTS["deck"] if deck_scored else 0.0,
                          WEIGHTS.get("deck", 0) if deck_scored else 0,
                          _deck_detail_text(dd, deck_pct, wins_total, deck_scored))
    if deck_scored:
        items.append(deck_item)
    total = sum(i.score for i in items)
    grade = next(gr for thr, gr in GRADES if total >= thr)
    # 牌组质量：样本不足时不计分，但作为**参考项**一并返回，
    # 界面上单独显示并标注置信度与解锁条件
    references = [] if deck_scored else [deck_item.to_dict()]
    return {
        "total": round(total, 1),
        "grade": grade,
        "items": [i.to_dict() for i in items],
        "references": references,
        "deck_scored": deck_scored,
        "deck_wins_total": wins_total,
        "deck_wins_needed": DECK_SCORING_MIN_WINS,
        "verdict": _verdict(grade, won, row, items),
        "calibration_runs": len(calib.get("depth_ratio", [])),
        "method": "各维度按本机历史对局分布取分位，再加权求和（100 分制）；"
                  "牌组质量为参考指标，通关样本足够后才计入总分",
    }


def _badges_of(players: list[dict[str, Any]], player_id: str | None) -> list[Any]:
    """取该玩家的称号原始列表（找不到就取第一位）。

    多人局里称号是**每人一套**（伤害第一、弱化专家这类本来就是多人专属），
    所以要按 player_id 取，不能混在一起。
    """
    if not players:
        return []
    if player_id:
        for p in players:
            if str(p.get("player_id")) == str(player_id):
                return p.get("badges") or []
    return players[0].get("badges") or []


def _deck_detail_text(dd: dict[str, Any], pct: float,
                      wins_total: int = 0, scored: bool = False) -> str:
    """牌组质量那一栏的说明文字。

    样本不足时必须**明说「仅供参考」**，不能让人误以为这是可靠评分。
    """
    if not dd.get("available"):
        return "该局没有抓牌记录，无法评估牌组强度"
    strength = float(dd.get("strength") or 0)
    prior = float(dd.get("prior") or 0)
    n = int(dd.get("n") or 0)
    known = int(dd.get("known") or 0)
    tail = ""
    top = dd.get("strongest") or []
    weak = dd.get("weakest") or []
    if top:
        tail += " · 最强 " + "、".join(f"{x['name']}" for x in top[:2])
    if weak and weak[0].get("card_id") != (top[0]["card_id"] if top else None):
        tail += " · 最弱 " + "、".join(f"{x['name']}" for x in weak[:2])
    head = (f"牌组平均强度 {strength*100:.1f}%（总体 {prior*100:.1f}%）"
            f" · {n} 张中 {known} 张样本充足")
    if scored:
        return head + tail + f" · 好于 {pct*100:.0f}%"
    # 未计分：说明原因与解锁条件
    return (head + " · **样本不足，仅供参考、不计分**"
            f"（通关样本 {wins_total}，满 {DECK_SCORING_MIN_WINS} 后启用）" + tail)


def _verdict(grade: str, won: bool, row: dict[str, Any],
             items: list[ScoreItem]) -> str:
    """给一句人话评语。"""
    best = max(items, key=lambda i: i.ratio)
    worst = min(items, key=lambda i: i.ratio)
    parts = []
    if won:
        parts.append("通关成功")
    elif row.get("killed_by"):
        parts.append(f"止步于 {row['killed_by']}")
    else:
        parts.append("对局未完成")
    parts.append(f"整体 {grade} 级")
    parts.append(f"最强项是{best.label}（{best.ratio}%）")
    if worst.ratio < 60:
        parts.append(f"短板在{worst.label}（{worst.ratio}%）")
    return "，".join(parts) + "。"


# ---------------------------------------------------------------- 总结装配

def summarize_player(store: Store, run_id: str, player_id: str,
                     g: Glossary | None = None,
                     calib: dict[str, list[float]] | None = None) -> dict[str, Any]:
    """单个玩家的局后总结（多人对局里每位玩家各一份）。"""
    g = g or default_glossary()
    row = store.run_row(run_id)
    if not row:
        return {}
    rp = next((p for p in store.players(run_id) if p["player_id"] == player_id), None)
    nodes = [n for n in store.nodes(run_id, player_id) if n.get("visited")]
    combats = store.combats(run_id, player_id)
    hp_curve = _hp_curve(store, run_id, player_id)
    drafts = _card_drafts(store, run_id, g, player_id)
    events = _event_choices(store, run_id, g, player_id)

    hp_vals = [c["hp"] for c in hp_curve if c["hp"] is not None]
    total_turns = sum(int(c["turns"] or 0) for c in combats)
    total_dealt = sum(int(c["dmg_dealt"] or 0) for c in combats)
    damage_taken = sum(int(c["damage_taken"] or 0) for c in hp_curve)
    peak = max(hp_curve, key=lambda c: c["damage_taken"], default=None)
    picked = [d for d in drafts if d["picked"] and d["type"] != "removed"]
    skipped = [d for d in drafts if d["type"] == "choice" and not d["picked"]]

    return {
        "player_id": player_id,
        "name": (rp or {}).get("name") or "",
        "character": (rp or {}).get("character") or row.get("character"),
        "character_name": (rp or {}).get("character_name") or "",
        "is_local": bool((rp or {}).get("is_local")),
        # 原始称号（存档形态：[{"id","rarity"}]）
        "badges": (rp or {}).get("badges") or [],
        # 带中文名与说明的称号（界面直接用这个）
        "badge_list": effects.badges((rp or {}).get("badges") or []),
        "score": score_run(store, run_id, g, calib=calib, player_id=player_id),
        "stats": {
            "floors": len(hp_curve), "combats": len(combats), "turns": total_turns,
            "damage_dealt": total_dealt, "damage_taken": damage_taken,
            "dpt": round(total_dealt / total_turns, 1) if total_turns else 0.0,
            "hp_start": hp_vals[0] if hp_vals else None,
            "hp_end": hp_vals[-1] if hp_vals else None,
            "hp_min": min(hp_vals) if hp_vals else None,
            "max_hp": max((c["max_hp"] or 0) for c in hp_curve) or None,
            "relics": len((rp or {}).get("relics") or []),
            "deck_size": len((rp or {}).get("deck") or []),
            "cards_picked": len(picked),
            "cards_offered": len(picked) + len(skipped),
            "events": len([e for e in events if e["type"] in ("unknown", "event")]),
            "roughest_floor": peak["floor"] if peak else None,
            "roughest_floor_damage": peak["damage_taken"] if peak else 0,
        },
        "hp_curve": hp_curve,
        "card_drafts": drafts,
        "event_choices": events,
    }


def summarize(store: Store, run_id: str, g: Glossary | None = None,
              calib: dict[str, list[float]] | None = None) -> dict[str, Any]:
    """整场总结：队级汇总 + 每位玩家各一份（多人对局的关键）。"""
    g = g or default_glossary()
    row = store.run_row(run_id)
    if not row:
        return {}
    players = store.players(run_id) or [{"player_id": "", "character_name": ""}]
    per_player = [summarize_player(store, run_id, p["player_id"], g, calib)
                  for p in players]
    lead = per_player[0] if per_player else {}
    hp_curve = lead.get("hp_curve", [])
    drafts = lead.get("card_drafts", [])
    events = lead.get("event_choices", [])
    route = _route(store, run_id, g)
    combats = store.combats(run_id)

    # 关键统计
    damage_taken = sum(c["damage_taken"] or 0 for c in hp_curve)
    hp_vals = [c["hp"] for c in hp_curve if c["hp"] is not None]
    total_turns = sum(int(c["turns"] or 0) for c in combats)
    total_dealt = sum(int(c["dmg_dealt"] or 0) for c in combats)
    peak_damage_floor = max(hp_curve, key=lambda c: c["damage_taken"], default=None)

    picked_cards = [d for d in drafts if d["picked"] and d["type"] != "removed"]
    skipped = [d for d in drafts if d["type"] == "choice" and not d["picked"]]

    return {
        "run_id": run_id,
        "seed": row.get("seed"),
        "character": row.get("character_name") or row.get("character"),
        "ascension": row.get("ascension"),
        "game_mode": row.get("game_mode") or "standard",
        "modifiers": row.get("modifiers") or [],
        "modifier_names": [g.name(m) for m in (row.get("modifiers") or [])],
        "win": None if row.get("win") is None else bool(row.get("win")),
        "abandoned": bool(row.get("abandoned")),
        "killed_by": row.get("killed_by") or "",
        "run_time": row.get("run_time") or 0,
        "multiplayer": bool(row.get("multiplayer")) or len(players) > 1,
        "player_count": max(len(players), 1),
        "local_player": row.get("local_player") or "",
        "players": [{"player_id": p.get("player_id") or "",
                     "name": p.get("name") or "",
                     "character": p.get("character") or "",
                     "character_name": p.get("character_name") or "",
                     "is_local": bool(p.get("is_local")),
                     "deck_ids": [c.split(".")[-1] for c in (p.get("deck") or [])],
                     "deck_labels": [g.label(c) for c in (p.get("deck") or [])],
                     "relics": [g.name(r) for r in (p.get("relics") or [])],
                     "badges": p.get("badges") or []}
                    for p in players],
        # 整队视角（队长）+ 每位玩家各自的总结
        "score": lead.get("score") or score_run(store, run_id, g, calib=calib),
        "stats": lead.get("stats") or {},
        "stats": {
            "floors": len(hp_curve), "combats": len(combats), "turns": total_turns,
            "damage_dealt": total_dealt, "damage_taken": damage_taken,
            "dpt": round(total_dealt / total_turns, 1) if total_turns else 0.0,
            "hp_start": hp_vals[0] if hp_vals else None,
            "hp_end": hp_vals[-1] if hp_vals else None,
            "hp_min": min(hp_vals) if hp_vals else None,
            "max_hp": max((c["max_hp"] or 0) for c in hp_curve) or None,
            "relics": row.get("relic_count") or 0,
            "deck_size": row.get("deck_size") or 0,
            "cards_picked": len(picked_cards),
            "cards_offered": len(picked_cards) + len(skipped),
            "events": len([e for e in events if e["type"] in ("unknown", "event")]),
            "roughest_floor": peak_damage_floor["floor"] if peak_damage_floor else None,
            "roughest_floor_damage": peak_damage_floor["damage_taken"]
            if peak_damage_floor else 0,
        },
        "route": route,
        "hp_curve": hp_curve,
        "card_drafts": drafts,
        "event_choices": events,
        "per_player": per_player,
        # 顶层也给一份「当前视角玩家」的称号，界面抬头直接用
        "badge_list": next((p.get("badge_list") for p in per_player
                            if str(p.get("player_id")) == str(row.get("local_player") or "")), None)
                      or next((p.get("badge_list") for p in per_player if p.get("is_local")), None)
                      or (per_player[0].get("badge_list") if per_player else []),
    }


# ---------------------------------------------------------------- 数值预测

def hp_projection(hp: int | None, max_hp: int | None, damage_taken: int,
                  floors_done: int, act: int = 1, floor: int = 1,
                  heal_per_rest: float = 0.28) -> dict[str, Any]:
    """按目前掉血速度外推生存情况。

    返回 estimated_floors_left（还能撑几层）、will_die（是否因血量耗尽结束）、
    以及预测的 HP 轨迹点，供前端画虚线。
    """
    if hp is None or not max_hp or floors_done <= 0:
        return {"available": False,
                "reason": "数据源未提供我方 HP（DamageMeter 只统计伤害）"}
    taken_per_floor = damage_taken / floors_done
    # 休息处回血：按每层出现概率折算成「每层平均回血」
    rest_share = 0.12                       # 实测约 1/8 的楼层是休息处
    avg_heal = max_hp * heal_per_rest * rest_share
    net_loss = taken_per_floor - avg_heal
    if net_loss <= 0.05:
        return {"available": True, "taken_per_floor": round(taken_per_floor, 2),
                "net_loss_per_floor": round(net_loss, 2), "floors_left": None,
                "will_die": False, "confidence": "low",
                "note": "当前掉血速度低于预估回血，未见血量耗尽的迹象"}
    floors_left = int(hp / net_loss)
    # 本 Act 剩余层数（决定「会不会在本层段内死」）
    span = ACT_FLOOR_SPAN.get(act, 15)
    floors_in_act_left = max(span - (floor - 1), 0)
    will_die = floors_left < floors_in_act_left
    trail = []
    cur = float(hp)
    for i in range(1, min(floors_left + 3, floors_in_act_left + 1)):
        cur = max(cur - net_loss, 0.0)
        trail.append({"step": i, "floor": floor + i, "hp": round(cur, 1)})
    confidence = "high" if floors_done >= 8 else ("medium" if floors_done >= 4 else "low")
    return {
        "available": True,
        "taken_per_floor": round(taken_per_floor, 2),
        "net_loss_per_floor": round(net_loss, 2),
        "floors_left": floors_left,
        "floors_in_act_left": floors_in_act_left,
        "will_die": will_die,
        "confidence": confidence,
        "trail": trail,
        "note": ("按当前速度，血量会在本层段内耗尽"
                 if will_die else f"按当前速度还能撑约 {floors_left} 层"),
    }

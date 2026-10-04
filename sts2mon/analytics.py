"""全局统计聚合：跨对局的胜率、死亡热点、卡牌表现、回合曲线。

数据从 Store（SQLite）读取，所以「实时对局」结束后同样会被计入统计。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .glossary import Glossary, default_glossary
from .model import Store


def _rate(part: int, total: int) -> float:
    return round(part / total * 100, 1) if total else 0.0


class Analytics:
    def __init__(self, store: Store, glossary: Glossary | None = None) -> None:
        self.store = store
        self.g = glossary or default_glossary()

    # ------------------------------------------------------------ 总览
    def overview(self) -> dict[str, Any]:
        runs = self.store.all_run_rows()
        finished = [r for r in runs if r["win"] is not None]
        wins = [r for r in finished if r["win"]]
        by_char: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in finished:
            by_char[r["character_name"] or r["character"] or "未知"].append(r)
        by_asc: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for r in finished:
            by_asc[int(r["ascension"] or 0)].append(r)

        total_time = sum(int(r["run_time"] or 0) for r in runs)
        return {
            "runs": len(runs),
            "finished": len(finished),
            "wins": len(wins),
            "losses": len(finished) - len(wins),
            "live": sum(1 for r in runs if r["live"]),
            "win_rate": _rate(len(wins), len(finished)),
            "abandoned": sum(1 for r in runs if r["abandoned"]),
            "total_floors": sum(int(r["floors"] or 0) for r in runs),
            "total_combats": sum(int(r["combats"] or 0) for r in runs),
            "total_turns": sum(int(r["turns"] or 0) for r in runs),
            "total_damage_dealt": sum(int(r["damage_dealt"] or 0) for r in runs),
            "total_damage_taken": sum(int(r["damage_taken"] or 0) for r in runs),
            "total_time": total_time,
            "avg_run_time": round(total_time / len(runs)) if runs else 0,
            "by_character": [
                {
                    "character": k,
                    "runs": len(v),
                    "wins": sum(1 for r in v if r["win"]),
                    "win_rate": _rate(sum(1 for r in v if r["win"]), len(v)),
                    "avg_floors": round(sum(int(r["floors"] or 0) for r in v) / len(v), 1),
                }
                for k, v in sorted(by_char.items(), key=lambda kv: -len(kv[1]))],
            "by_ascension": [
                {"ascension": k, "runs": len(v),
                 "wins": sum(1 for r in v if r["win"]),
                 "win_rate": _rate(sum(1 for r in v if r["win"]), len(v))}
                for k, v in sorted(by_asc.items())],
        }

    # ------------------------------------------------------------ 排行榜 / 热点
    def top_runs(self, key: str = "damage_dealt", limit: int = 10) -> list[dict[str, Any]]:
        runs = self.store.all_run_rows()
        rows = sorted(runs, key=lambda r: int(r[key] or 0), reverse=True)[:limit]
        return [{"run_id": r["run_id"], "character": r["character_name"] or r["character"],
                 "ascension": r["ascension"], "win": bool(r["win"]) if r["win"] is not None else None,
                 "value": int(r[key] or 0), "floors": r["floors"]} for r in rows]

    def death_hotspots(self, limit: int = 15) -> dict[str, Any]:
        """死在哪：按被杀遭遇、按楼层深度。"""
        runs = self.store.all_run_rows()
        by_encounter: Counter[str] = Counter()
        for r in runs:
            if r["win"] is not None and not r["win"] and r["killed_by"]:
                by_encounter[r["killed_by"]] += 1
        depth: Counter[int] = Counter()
        act_death: Counter[int] = Counter()
        for r in runs:
            if r["win"] is None or r["win"]:
                continue
            depth[int(r["current_floor"] or r["floors"] or 0)] += 1
            act_death[int(r["current_act"] or 0)] += 1
        return {
            "by_encounter": [{"name": k, "count": v} for k, v in by_encounter.most_common(limit)],
            "by_floor": [{"floor": k, "count": v} for k, v in sorted(depth.items())],
            "by_act": [{"act": k, "count": v} for k, v in sorted(act_death.items())],
        }

    def floor_curve(self) -> dict[str, Any]:
        """平均 HP / 受伤随楼层的曲线（衡量每层压力）。"""
        runs = self.store.all_run_rows()
        hp_sum: dict[int, float] = defaultdict(float)
        hp_cnt: dict[int, int] = defaultdict(int)
        dmg_sum: dict[int, float] = defaultdict(float)
        cnt: dict[int, int] = defaultdict(int)
        type_counter: dict[int, Counter[str]] = defaultdict(Counter)
        for r in runs[:200]:
            for n in self.store.nodes(r["run_id"]):
                f = int(n["ord"] or 0)
                if n["hp_after"] is not None:
                    hp_sum[f] += n["hp_after"]
                    hp_cnt[f] += 1
                dmg_sum[f] += int(n["damage_taken"] or 0)
                cnt[f] += 1
                type_counter[f][n["point_type"] or "?"] += 1
        return {
            "floors": [
                {"floor": f,
                 "avg_hp": round(hp_sum[f] / hp_cnt[f], 1) if hp_cnt[f] else None,
                 "avg_damage_taken": round(dmg_sum[f] / cnt[f], 1) if cnt[f] else 0,
                 "samples": cnt[f],
                 "types": dict(type_counter[f].most_common(4))}
                for f in sorted(cnt)],
        }

    # ------------------------------------------------------------ 卡牌
    def card_performance(self, limit: int = 40, order: str = "damage") -> list[dict[str, Any]]:
        """跨对局卡牌表现。

        order='damage'  —— 按总伤害（默认）
        order='plays'   —— 按出牌次数（只有实时/rdps 源才有次数，历史存档没有）
        """
        rows = self.store.conn.execute(
            """SELECT card_id, MAX(name) name,
                      SUM(plays) plays, SUM(damage) damage, SUM(block) block,
                      SUM(energy) energy, COUNT(DISTINCT run_id) runs
               FROM card_stats GROUP BY card_id""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            plays = int(d["plays"] or 0)
            d["name"] = self.g.label(d["card_id"]) or d["name"]
            d["damage_per_play"] = round(int(d["damage"] or 0) / plays, 2) if plays else None
            d["block_per_play"] = round(int(d["block"] or 0) / plays, 2) if plays else None
            out.append(d)
        if order == "plays":
            out.sort(key=lambda d: (-int(d["plays"] or 0), -int(d["damage"] or 0)))
        else:
            out.sort(key=lambda d: (-int(d["damage"] or 0), -int(d["plays"] or 0)))
        return out[:limit]

    def data_sources(self) -> list[dict[str, Any]]:
        """各数据源贡献了多少场对局（用于透明地展示数据可信度）。"""
        rows = self.store.conn.execute(
            """SELECT COALESCE(source,'?') source, COUNT(*) n FROM runs GROUP BY source""").fetchall()
        cards = self.store.conn.execute(
            """SELECT COUNT(*) n, SUM(CASE WHEN plays>0 THEN 1 ELSE 0 END) with_plays
               FROM (SELECT card_id, SUM(plays) plays FROM card_stats GROUP BY card_id)""").fetchone()
        return [dict(r) for r in rows] + [{"source": "card_stats",
                                           "n": (cards["n"] if cards else 0),
                                           "with_plays": (cards["with_plays"] if cards else 0)}]

    def card_pick_rate(self, limit: int = 30) -> list[dict[str, Any]]:
        """选牌倾向：存档里每次战斗后的三选一，统计被拿走的比例。"""
        picked: Counter[str] = Counter()
        offered: Counter[str] = Counter()
        for r in self.store.all_run_rows():
            for n in self.store.nodes(r["run_id"]):
                for ch in (n.get("card_choices") or []):
                    cid = ch.get("card")
                    if not cid:
                        continue
                    offered[cid] += 1
                    if ch.get("picked"):
                        picked[cid] += 1
        rows = [{"card_id": cid, "name": self.g.label(cid), "offered": offered[cid],
                 "picked": picked[cid], "pick_rate": _rate(picked[cid], offered[cid])}
                for cid in offered if offered[cid] >= 2]
        rows.sort(key=lambda x: (-x["offered"], -x["pick_rate"]))
        return rows[:limit]

    # ------------------------------------------------------------ 逐场战斗
    def encounter_stats(self, limit: int = 30) -> list[dict[str, Any]]:
        """每个遭遇的战绩：平均回合数、平均受伤、死亡率。"""
        rows = self.store.conn.execute(
            """SELECT encounter_id, MAX(name) name, COUNT(*) n,
                      AVG(turns) avg_turns, AVG(dmg_taken) avg_taken,
                      AVG(dmg_dealt) avg_dealt, MAX(dmg_taken) max_taken,
                      SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) losses
               FROM combats GROUP BY encounter_id
               HAVING n >= 1 ORDER BY n DESC LIMIT ?""", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["name"] = d["name"] or self.g.encounter(d["encounter_id"])
            d["avg_turns"] = round(float(d["avg_turns"] or 0), 1)
            d["avg_taken"] = round(float(d["avg_taken"] or 0), 1)
            d["avg_dealt"] = round(float(d["avg_dealt"] or 0), 1)
            d["death_rate"] = _rate(int(d["losses"] or 0), int(d["n"] or 0))
            out.append(d)
        return out

    def turn_curve(self, run_id: str, node_id: str | None = None) -> dict[str, Any]:
        """一场（或某节点）战斗的逐回合曲线，供图表使用。"""
        turns = self.store.turn_stats(run_id, node_id)
        by_turn: dict[int, dict[str, Any]] = {}
        for t in turns:
            turn = int(t["turn"] or 0)
            row = by_turn.setdefault(turn, {"turn": turn, "dealt": 0, "taken": 0,
                                            "block": 0, "cards": 0, "energy": 0,
                                            "hp": None, "enemy_hp": None,
                                            "enemy_dealt": 0})
            if t["side"] == "player":
                row["dealt"] += int(t["dmg_dealt"] or 0)
                row["taken"] += int(t["dmg_taken"] or 0)
                row["block"] += int(t["block_gained"] or 0)
                row["cards"] += int(t["cards_played"] or 0)
                row["energy"] += int(t["energy_spent"] or 0)
                if t["hp"] is not None:
                    row["hp"] = t["hp"]
            elif t["side"] == "enemy":
                row["enemy_dealt"] += int(t["dmg_dealt"] or 0)
                if t["hp"] is not None:
                    row["enemy_hp"] = (row["enemy_hp"] or 0) + t["hp"]
        return {"run_id": run_id, "node_id": node_id,
                "turns": [by_turn[k] for k in sorted(by_turn)]}

    # ------------------------------------------------------------ 完整导出
    def export(self) -> dict[str, Any]:
        return {
            "overview": self.overview(),
            "top_damage_runs": self.top_runs("damage_dealt", 10),
            "top_floor_runs": self.top_runs("floors", 10),
            "death_hotspots": self.death_hotspots(),
            "floor_curve": self.floor_curve(),
            "card_performance": self.card_performance(),
            "card_pick_rate": self.card_pick_rate(),
            "encounter_stats": self.encounter_stats(),
            "data_sources": self.data_sources(),
            "glossary_stats": self.g.stats(),
        }

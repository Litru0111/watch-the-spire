"""analytics 适配器：给历史对局补上**完整地图布局**。

数据源
------
`%APPDATA%/SlayTheSpire2/analytics/<YYYYMMDD_HHMMSS>_<角色>_<结果>.json`

里面含官方记录的地图结构：
    map_acts[].width / height
    map_acts[].nodes[]      {coord:[x,y], type:"M"/"E"/"R"/"T"/"S"/"V"..., children:[[x,y]]}
    map_acts[].start_coords 起点
    map_acts[].visited_coords 实际走过的路线（有序）

与 `.run` 对局的关联方式：文件名里的时间戳（本地时间）≈ `.run` 的 start_time（unix 秒）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..glossary import Glossary, default_glossary
from ..model import ActState, MapNode, RunState
from .. import paths

DEFAULT_ROOT = paths.sts2_appdata() / "analytics"

# analytics 里的地图点类型字母 -> STS2 的 point_type
LETTER_TO_TYPE = {
    "M": "monster",      # Monster
    "E": "elite",        # Elite
    "B": "boss",         # Boss
    "R": "rest_site",    # Rest
    "T": "treasure",     # Treasure
    "S": "shop",         # Shop
    "V": "event",        # e Vent / unknown
    "?": "unknown",
    "A": "ancient",
}


class AnalyticsAdapter:
    """读取 analytics 对局报告里的地图布局。"""

    def __init__(self, root: str | Path | None = None,
                 glossary: Glossary | None = None) -> None:
        self.root = Path(root) if root else DEFAULT_ROOT
        self.g = glossary or default_glossary()
        self._index: dict[int, Path] | None = None

    # ------------------------------------------------------------ 索引
    def index(self) -> dict[int, Path]:
        """返回 {start_time(unix秒): 文件路径}。"""
        if self._index is not None:
            return self._index
        out: dict[int, Path] = {}
        if self.root.is_dir():
            for p in self.root.glob("*.json"):
                stem = p.stem
                ts_part = stem.split("_")[0]
                if len(ts_part) != 8 or not ts_part.isdigit():
                    continue
                time_part = stem.split("_")[1] if "_" in stem else ""
                try:
                    dt = datetime.strptime(ts_part + (time_part or "000000"),
                                           "%Y%m%d%H%M%S")
                except ValueError:
                    continue
                out[int(dt.timestamp())] = p
        self._index = out
        return out

    def find_for_run(self, start_time: float, tolerance: int = 900) -> Path | None:
        """按开始时间就近匹配（容差默认 15 分钟）。"""
        if not start_time:
            return None
        best, best_delta = None, tolerance + 1
        for ts, path in self.index().items():
            delta = abs(ts - int(start_time))
            if delta < best_delta:
                best, best_delta = path, delta
        return best

    def load(self, path: str | Path) -> dict[str, Any] | None:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except Exception:
            return None

    # ------------------------------------------------------------ 应用
    def enrich(self, run: RunState) -> bool:
        """把完整地图（含未走的点）合并进 RunState。返回是否成功。"""
        path = self.find_for_run(run.start_time)
        if not path:
            return False
        data = self.load(path)
        if not data:
            return False
        acts = data.get("map_acts") or []
        if not acts:
            return False

        matched_acts = 0
        for i, act_data in enumerate(acts):
            act_no = int(act_data.get("act", i)) + 1
            target = run.act(act_no)
            if target is None:
                continue
            nodes_raw = act_data.get("nodes") or []
            if not nodes_raw:
                continue
            visited = [tuple(c) for c in (act_data.get("visited_coords") or [])]
            visited_set = set(visited)
            starts = {tuple(c) for c in (act_data.get("start_coords") or [])}

            # 按 y 再按 x 排序 = 自下而上、从左到右的楼层顺序
            ordered = sorted(
                nodes_raw,
                key=lambda n: (int(n["coord"][1]), int(n["coord"][0])))
            # 走过的点按路线顺序排前面，其余按坐标顺序，保证楼层号与存档一致
            route_order = {c: idx for idx, c in enumerate(visited)}

            by_coord: dict[tuple[int, int], MapNode] = {}
            for n in ordered:
                coord = tuple(n["coord"])
                letter = str(n.get("type") or "?")
                ptype = LETTER_TO_TYPE.get(letter, "unknown")
                node = MapNode(
                    node_id=f"a{act_no}c{coord[0]}r{coord[1]}",
                    act=act_no,
                    point_type=ptype,
                    type_label=self.g.name(ptype),
                    col=int(coord[0]),
                    row=int(coord[1]),
                    visited=coord in visited_set,
                    is_start=coord in starts,
                    children=[f"a{act_no}c{c[0]}r{c[1]}" for c in (n.get("children") or [])],
                )
                by_coord[coord] = node
            # 反向补 parents
            for coord, node in by_coord.items():
                for child in node.children:
                    cn = next((x for x in by_coord.values() if x.node_id == child), None)
                    if cn is not None and node.node_id not in cn.parents:
                        cn.parents.append(node.node_id)

            # 把存档里的逐层数据（房间、选牌…）与「每位玩家的数值」按路线顺序贴到已走过的点上
            old_nodes = list(target.nodes)
            for idx, coord in enumerate(visited):
                node = by_coord.get(coord)
                if node is None or idx >= len(old_nodes):
                    continue
                src = old_nodes[idx]
                node.room_id = src.room_id
                node.room_label = src.room_label
                node.turns_taken = src.turns_taken
                # 玩家数值与战斗记录整段搬过来（多人局每人一条）
                node.player_stats = list(src.player_stats)
                for s in node.player_stats:
                    s.order = node.order or (idx + 1)
                    s.act = act_no
                node.turn_stats = list(src.turn_stats)
                node.combats = list(src.combats)
                for c in node.combats:
                    c.node_id = node.node_id
                    c.act = act_no
                    c.floor = int(coord[1])

            # 楼层号：已走过的点按路线顺序编号（与存档楼层一致），未走的点为 0
            for idx, coord in enumerate(visited):
                node = by_coord.get(coord)
                if node is not None:
                    node.order = idx + 1
            target.nodes = sorted(
                by_coord.values(),
                key=lambda n: (n.order == 0, n.order, int(n.row or 0), int(n.col or 0)))
            target.act_id = target.act_id or str(act_data.get("act", i))
            matched_acts += 1

        run.notes["analytics_file"] = str(path)
        run.notes["map_enriched"] = matched_acts
        return matched_acts > 0

    def available(self) -> int:
        return len(self.index())


def attach_analytics_maps(runs: list[RunState], root: str | Path | None = None,
                          glossary: Glossary | None = None) -> int:
    ad = AnalyticsAdapter(root, glossary)
    hits = 0
    for r in runs:
        try:
            if ad.enrich(r):
                hits += 1
        except Exception as exc:
            print(f"[analytics] 跳过 {r.run_id}: {exc!r}")
    return hits

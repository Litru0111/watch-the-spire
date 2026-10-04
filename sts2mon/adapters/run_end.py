"""对局结束检测器 —— 实时发现「这一局打完了」。

信号来源（按可靠性）：
  1. `history/<start_time>.run` 出现  —— **最可靠**：游戏在一局结束时才写这个文件，
     里面带 win / killed_by_encounter / run_time / 完整逐层数据
  2. `current_run.save` 消失          —— 进行中存档被删除
  3. `DamageMeter` 的 tracking 变 false 且文件不再更新
  4. `skada_run_token.json` 被清空

检测到之后做两件事：
  * 发 RUN_END 事件（带胜负/死因），让实时面板立刻显示结局
  * 解析该 .run 并落库（含完整逐层数据），把实时对局替换成权威历史数据
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..events import Event, K
from ..glossary import Glossary, default_glossary
from .history import HistoryAdapter, find_run_files


class RunEndWatcher:
    """监视 history 目录，发现新落盘的对局存档就报告「这局结束了」。"""

    def __init__(self, glossary: Glossary | None = None,
                 history_dir: Path | None = None) -> None:
        self.g = glossary or default_glossary()
        self.history = HistoryAdapter(self.g)
        self.dir = history_dir
        self._known: set[str] = set()
        self._initialized = False

    def _files(self) -> list[Path]:
        if self.dir:
            return sorted(self.dir.glob("*.run"))
        return find_run_files()

    def prime(self) -> None:
        """记录当前已存在的存档，避免把「启动前就有的」误判成刚结束。"""
        self._known = {p.name for p in self._files()}
        self._initialized = True

    def poll(self) -> list[tuple[Path, dict[str, Any]]]:
        """返回自上次调用以来**新出现**的对局存档。

        每个元素是 (路径, 结局信息)。结局信息形如：
            {"run_id", "seed", "win", "abandoned", "killed_by",
             "run_time", "floors", "start_time"}
        """
        if not self._initialized:
            self.prime()
            return []
        out: list[tuple[Path, dict[str, Any]]] = []
        for p in self._files():
            if p.name in self._known:
                continue
            self._known.add(p.name)
            info = self.inspect(p)
            if info:
                out.append((p, info))
        return out

    def inspect(self, path: Path) -> dict[str, Any] | None:
        """读一个 .run，抽出结局信息（不解析完整对局，够快）。"""
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
        killed = ""
        enc = _bare(data.get("killed_by_encounter"))
        if enc and enc.upper() not in ("NONE", "NONE.NONE"):
            killed = self.g.encounter(enc)
        else:
            ev = _bare(data.get("killed_by_event"))
            if ev and ev.upper() not in ("NONE", "NONE.NONE"):
                killed = self.g.name(ev)
        mph = data.get("map_point_history") or []
        return {
            "run_id": str(data.get("start_time") or path.stem),
            "seed": path.stem,
            "start_time": data.get("start_time"),
            "win": bool(data.get("win")),
            "abandoned": bool(data.get("was_abandoned")),
            "killed_by": killed,
            "killed_by_encounter": enc,
            "run_time": int(data.get("run_time") or 0),
            "ascension": int(data.get("ascension") or 0),
            "floors": sum(len(a or []) for a in mph),
            "path": str(path),
        }

    def end_event(self, info: dict[str, Any], run_id: str = "") -> Event:
        """把结局信息转成 RUN_END 事件。"""
        return Event(
            kind=K.RUN_END, run_id=info.get("run_id") or run_id,
            ts=time.time(), source="run_history",
            data={"win": info.get("win"), "abandoned": info.get("abandoned"),
                  "killed_by": info.get("killed_by"), "run_time": info.get("run_time"),
                  "floors": info.get("floors"), "seed": info.get("seed"),
                  "from_history_file": True})


def _bare(raw: Any) -> str:
    """`ENCOUNTER.CEREMONIAL_BEAST_BOSS` → `CEREMONIAL_BEAST_BOSS`。"""
    s = str(raw or "")
    if "." in s:
        s = s.split(".")[-1]
    return s if s.upper() != "NONE" else ""

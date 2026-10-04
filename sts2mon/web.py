"""零依赖 Web 仪表盘：标准库 http.server + 单文件前端。

路由
----
GET /                     仪表盘页面
GET /api/overview         跨对局全局统计
GET /api/runs             对局列表
GET /api/run?id=          单场对局：地图、路线、逐层、战斗、逐回合
GET /api/live             当前实时状态（正在进行的对局 / 战斗）
GET /api/events?id=&after= 实时事件流（增量拉取）
GET /api/export           全量 JSON 导出
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import effects, paths
from .analytics import Analytics
from .glossary import Glossary, default_glossary
from .live import start_replay
from .model import RunState, Store
from .summary import summarize

ASSETS = Path(__file__).resolve().parent / "assets"


class Dashboard:
    """把 Store / Analytics / 实时状态打包成 HTTP 服务。"""

    def __init__(self, store: Store, live_provider: Callable[[], dict[str, Any]] | None = None,
                 glossary: Glossary | None = None,
                 live_state: Any = None, sandbox_dir: str | Path | None = None) -> None:
        self.store = store
        self.g = glossary or default_glossary()
        self.analytics = Analytics(store, self.g)
        self.live_provider = live_provider
        self.live_state = live_state
        self.sandbox_dir = Path(sandbox_dir) if sandbox_dir else paths.sandbox_dir()
        self.started = time.time()
        self.log: list[str] = []
        self.replay_thread: threading.Thread | None = None
        # 评分校准分布：算一次缓存起来（否则每局总结都要扫全库）
        self._calib_cache: dict[str, Any] | None = None
        self._calib_stamp = 0.0

    def calibration(self) -> dict[str, list[float]]:
        """评分参照分布，带缓存（按对局数变化失效）。"""
        n = len(self.store.run_ids())
        if self._calib_cache is None or n != self._calib_stamp:
            from .summary import calibration as build_calibration
            self._calib_cache = build_calibration(self.store, self.g)
            self._calib_stamp = n
        return self._calib_cache

    # ------------------------------------------------------------ 数据组装
    def run_detail(self, run_id: str, player_id: str | None = None) -> dict[str, Any] | None:
        """对局详情。

        player_id 指定时只返回该玩家的战斗/数值/总结（多人对局每人一套）。
        """
        row = self.store.run_row(run_id)
        if not row:
            return None
        players = self.store.players(run_id)
        if player_id is None:
            # 默认视角要选**本机玩家**，不能取 players[0]：
            # 多人局里玩家顺序不固定，取第一个会看到队友的数据。
            row = self.store.run_row(run_id) or {}
            local = str(row.get("local_player") or "")
            me = next((p for p in players if p.get("is_local")), None)
            if me is None and local:
                me = next((p for p in players
                           if str(p.get("player_id")) == local), None)
            player_id = (me or (players[0] if players else {})).get("player_id") \
                if players else None
        nodes = self.store.nodes(run_id, player_id)
        combats = {c["node_id"]: c for c in self.store.combats(run_id, player_id)}
        for n in nodes:
            c = combats.get(n["node_id"])
            if c:
                c["turn_stats"] = self.store.turn_stats(run_id, n["node_id"], player_id)
                c["card_stats"] = self.store.card_stats(run_id, n["node_id"], player_id)
            n["combat"] = c
        acts: dict[int, list[dict[str, Any]]] = {}
        for n in nodes:
            acts.setdefault(int(n["act"] or 1), []).append(n)
        detail = {
            "run": row,
            "players": players,
            "player_id": player_id,
            "acts": [{"act": a, "nodes": v} for a, v in sorted(acts.items())],
            "combats": sorted(combats.values(), key=lambda c: (c["act"] or 0, c["floor"] or 0)),
            "card_stats": self.store.card_stats(run_id, player_id=player_id),
        }
        # 局后总结：评分 / 路线 / 数值曲线 / 抓牌 / 事件选择
        try:
            full = summarize(self.store, run_id, self.g, calib=self.calibration())
            # 单人局直接用整份；多人局把「该玩家那一份」提到顶层，同时保留队级信息
            if player_id and len(players) > 1:
                mine = next((p for p in full.get("per_player", [])
                             if p["player_id"] == player_id), None)
                if mine:
                    full = {**full, **mine, "team_score": full.get("score"),
                            "per_player": full.get("per_player")}
            detail["summary"] = full
        except Exception as exc:
            detail["summary_error"] = repr(exc)
        return detail

    def live_snapshot(self) -> dict[str, Any]:
        base: dict[str, Any] = {"available": False, "server_time": time.time()}
        if self.live_provider:
            try:
                base.update(self.live_provider() or {})
            except Exception as exc:
                base["error"] = repr(exc)
        return base

    # ------------------------------------------------------------ HTTP
    def make_handler(dash: "Dashboard"):
        class Handler(BaseHTTPRequestHandler):
            server_version = "sts2mon/0.1"

            def log_message(self, fmt: str, *args: Any) -> None:  # 安静点
                pass

            # ---- 工具 ----
            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, obj: Any, code: int = 200) -> None:
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _html(self, text: str, code: int = 200) -> None:
                self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

            def do_GET(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                q = parse_qs(u.query)
                path = u.path

                if path in ("/", "/index.html"):
                    f = ASSETS / "dashboard.html"
                    if not f.is_file():
                        return self._html("<h1>dashboard.html 缺失</h1>", 500)
                    return self._html(f.read_text(encoding="utf-8"))

                if path == "/api/overview":
                    return self._json({"overview": dash.analytics.overview(),
                                       "death_hotspots": dash.analytics.death_hotspots(),
                                       "floor_curve": dash.analytics.floor_curve(),
                                       "card_performance": dash.analytics.card_performance(25),
                                       "card_pick_rate": dash.analytics.card_pick_rate(20),
                                       "encounter_stats": dash.analytics.encounter_stats(20),
                                       "glossary": dash.g.stats()})

                if path == "/api/runs":
                    rows = dash.store.all_run_rows()
                    # 对局 id → 称号（每人一套，取本机玩家的；单人局即唯一玩家）。
                    # 预先算好，避免每行都去查一次 players 表（N+1 查询）。
                    badge_by_run: dict[str, list] = {}
                    for r in rows:
                        ps = dash.store.players(str(r["run_id"]))
                        mine = next((p for p in ps if p.get("is_local")), None) \
                            or (ps[0] if ps else None)
                        if mine and mine.get("badges"):
                            badge_by_run[str(r["run_id"])] = effects.badges(mine["badges"])

                    def char_of(r):
                        """对局的角色显示名。

                        注意占位符 `?` 必须**当未知处理**：早期实时采集在还没拿到
                        角色 ID 时把词典兜底的 `?` 写进了库，界面上就显示成一堆
                        「?」（实测踩过）。这里逐级回退：库里名字 → 库里角色 ID
                        → 玩家表 → 名称表解析，全都拿不到才留空由前端显示「未知」。
                        """
                        for v in (r.get("character_name"), r.get("character")):
                            s = str(v or "").strip()
                            if s and s not in ("?", "？", "未知"):
                                nm = dash.g.char(s) if s.upper() == s else s
                                if nm and nm not in ("?", "？"):
                                    return nm
                        for p in dash.store.players(str(r["run_id"])):
                            s = str(p.get("character") or "").strip()
                            if s:
                                nm = dash.g.char(s)
                                if nm and nm not in ("?", "？"):
                                    return nm
                        return ""

                    return self._json({"runs": [
                        {"run_id": r["run_id"], "character": char_of(r),
                         "ascension": r["ascension"], "win": None if r["win"] is None else bool(r["win"]),
                         "live": bool(r["live"]), "abandoned": bool(r["abandoned"]),
                         "floors": r["floors"], "combats": r["combats"], "turns": r["turns"],
                         "damage_dealt": r["damage_dealt"], "damage_taken": r["damage_taken"],
                         "run_time": r["run_time"], "start_time": r["start_time"],
                         "killed_by": r["killed_by"], "seed": r["seed"],
                         "multiplayer": bool(r.get("multiplayer")),
                         "player_count": r.get("player_count") or 1,
                         # 对局模式与自定义修正项：界面用来区分「标准 / 自定义」
                         "game_mode": r.get("game_mode") or "standard",
                         "modifiers": r.get("modifiers") or [],
                         "modifier_names": [dash.g.name(m)
                                            for m in (r.get("modifiers") or [])],
                         # 对局结束获得的称号（每人一套，列表取本机/第一位）
                         "badges": badge_by_run.get(str(r["run_id"]), []),
                         "deck_size": r["deck_size"], "relic_count": r["relic_count"]}
                        for r in rows]})

                if path == "/api/run":
                    rid = (q.get("id") or [""])[0]
                    pid = (q.get("player") or [""])[0] or None
                    detail = dash.run_detail(rid, pid)
                    return self._json(detail or {"error": "not found"}, 200 if detail else 404)

                if path == "/api/players":
                    rid = (q.get("id") or [""])[0]
                    return self._json({"players": dash.store.players(rid),
                                       "multiplayer": bool(
                                           (dash.store.run_row(rid) or {}).get("multiplayer"))})

                if path == "/api/live":
                    return self._json(dash.live_snapshot())

                if path == "/api/events":
                    rid = (q.get("id") or [""])[0]
                    after = int((q.get("after") or ["0"])[0] or 0)
                    limit = int((q.get("limit") or ["200"])[0] or 200)
                    # node / encounter：只取当前这场战斗的事件，
                    # 否则上一场的回合会混进实时事件流
                    node = (q.get("node") or [""])[0] or None
                    enc = (q.get("encounter") or [""])[0] or None
                    return self._json({"events": dash.store.events(
                        rid, limit, after, node_id=node, encounter_id=enc)})

                if path == "/api/export":
                    return self._json(dash.analytics.export())

                if path == "/api/glossary":
                    # 前端要按 ID 查中文名（逐层数据的「获得」列原先显示原始 ID）。
                    # 名称表本体可能很大，但都是短字符串，一次拉取后前端会缓存。
                    names = dict(dash.g.by_id)
                    # 补上药水等名称库**没有**的类别（名称库只含卡牌/遗物/怪物/事件）
                    try:
                        names.update(dash.g._potion_names())
                    except Exception:
                        pass
                    return self._json({"size": dash.g.size, "kinds": dash.g.stats(),
                                       "source": dash.g.source, "names": names})

                if path == "/api/health":
                    return self._json({"ok": True, "uptime": round(time.time() - dash.started, 1),
                                       "runs": len(dash.store.run_ids())})

                return self._json({"error": "unknown route", "path": path}, 404)

            def do_POST(self) -> None:  # noqa: N802
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/api/replay":
                    return self._replay(q)
                return self._json({"error": "unknown route", "path": u.path}, 404)

            def _replay(self, q: dict[str, list[str]]) -> None:
                if dash.live_state is None:
                    return self._json({"error": "服务端未启用实时模块"}, 400)
                seed = (q.get("seed") or [""])[0].strip()
                speed = float((q.get("speed") or ["4"])[0] or 4)
                if not seed:
                    return self._json({"error": "缺少 seed 参数"}, 400)
                # 用 seed 找到 run_id（seed 是存档里的字符串，如 QUXJJ78BYP）
                target = None
                for r in dash.store.all_run_rows():
                    if str(r["seed"]) == seed or r["run_id"] == seed:
                        target = r["run_id"]
                        break
                if not target:
                    return self._json({"error": f"没有找到种子为 {seed} 的对局"}, 404)
                if dash.replay_thread and dash.replay_thread.is_alive():
                    return self._json({"error": "已有回放在进行中"}, 409)
                try:
                    dash.live_state.reset()
                    result = {}

                    def run() -> None:
                        try:
                            result.update(start_replay(dash.store, target, dash.live_state,
                                                       dash.sandbox_dir, speed=speed))
                        except Exception as exc:
                            dash.log.append(f"replay error: {exc!r}")

                    dash.replay_thread = threading.Thread(target=run, name="sts2mon-replay",
                                                          daemon=True)
                    dash.replay_thread.start()
                    # 等一小会儿，让前端拿到回合数预估
                    dash.replay_thread.join(timeout=1.5)
                    return self._json({"ok": True, "run_id": target, **result})
                except Exception as exc:
                    return self._json({"error": repr(exc)}, 500)

        return Handler

    def serve(self, host: str = "127.0.0.1", port: int = 8770,
              background: bool = False) -> ThreadingHTTPServer:
        httpd = ThreadingHTTPServer((host, port), self.make_handler())
        if background:
            t = threading.Thread(target=httpd.serve_forever, name="sts2mon-web", daemon=True)
            t.start()
        return httpd

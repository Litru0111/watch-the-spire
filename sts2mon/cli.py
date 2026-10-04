"""sts2mon 命令行入口。"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from .adapters import (AnalyticsAdapter, CombatStateAdapter, CurrentRunAdapter,
                       HistoryAdapter, RdpsAdapter, RunEndWatcher, RunTokenAdapter,
                       find_current_run_files, find_run_files)
from .analytics import Analytics
from .events import Event, EventBus, K
from .glossary import Glossary, default_glossary
from .live import LiveState, start_replay
from .model import Store
from . import paths
from .web import Dashboard

# 数据目录统一由 paths 解析（显式参数 > STS2MON_DATA > 项目根/.sts2mon）。
# 早先这里写的是 `Path.cwd() / ".sts2mon"`，既会在别的目录里乱建文件，
# 又是 **import 期求值**、进程内改不回来 —— 实测从 C:\ 启动会生成
# `C:\.sts2mon\sts2mon.db`。这里只在「命令真正执行时」取默认值。
def _default_db() -> Path:
    return paths.db_path()


def _default_jsonl() -> Path:
    return paths.jsonl_path()


def _default_sandbox() -> Path:
    return paths.sandbox_dir()


# ------------------------------------------------------------------ 基础设施

class Ctx:
    def __init__(self, db: Path | None = None, quiet: bool = False) -> None:
        # db 为空时按 paths 的规则解析（环境变量 > 项目根），而不是绑死 cwd
        self.db = Path(db) if db else paths.db_path()
        self.store = Store(self.db)
        self.g: Glossary = default_glossary()
        # 本机身份要在**启动时**就确定：玩的时候 current_run.save 还没写
        # （离开该层才写），没有它就无法判断多人局里谁是本机 ——
        # 实测表现为「实时面板本机 HP 一直为空、三名玩家 is_local 全是 0」。
        # 兜底来源：存档目录 `steam/<id>/` 里的那个账号（只会是你自己）。
        self.live = LiveState(self.g, local_player_id=self._known_local_id())
        self.quiet = quiet

    def _known_local_id(self) -> str:
        """启动时能确定的本机 Steam ID（取不到返回空串）。"""
        for f in find_current_run_files():
            try:
                sid = CurrentRunAdapter(glossary=self.g, path=f).local_steam_id()
            except Exception:
                sid = ""
            if sid:
                return sid
        return paths.local_steam_id_hint()

    def say(self, *a) -> None:
        if not self.quiet:
            print(*a, flush=True)

    def close(self) -> None:
        self.store.close()


def _banner(ctx: Ctx, title: str) -> None:
    g = ctx.g
    kinds = " / ".join(f"{k}{v}" for k, v in g.stats().items())
    print(f"🐋 杀戮尖塔2 数据监控 · {title}")
    print(f"   数据库 : {ctx.db}")
    print(f"   名称库 : {g.size} 个对象（{kinds}）")
    print()


# ------------------------------------------------------------------ doctor

def cmd_clean(args) -> int:
    """清理实时（live-*）对局数据；--all 则清空整库。"""
    ctx = Ctx(args.db)
    _banner(ctx, "清理数据")
    ids = [r for r in ctx.store.run_ids()
           if r.startswith("live-") or r == "live"]
    if args.all:
        for t in ("events", "turn_stats", "card_stats", "combats", "nodes", "runs"):
            ctx.store.execute(f"DELETE FROM {t}")
        print("   已清空全部对局数据")
    else:
        for rid in ids:
            for t in ("events", "turn_stats", "card_stats", "combats", "nodes", "runs"):
                ctx.store.execute(f"DELETE FROM {t} WHERE run_id = ?", (rid,))
        print(f"   已清理 {len(ids)} 场实时对局" + (f"：{', '.join(ids[:5])}" if ids else ""))
    print(f"   剩余对局: {len(ctx.store.run_ids())} 场")
    ctx.close()
    return 0


def cmd_extract(args) -> int:
    """从游戏文件提取「文案」与「数值」，写入数据目录。

    公开仓库**不包含**这些提取结果 —— 它们是游戏的本地化文本与编译常量，
    属于游戏版权内容，随仓库分发有法律风险。所以改成本地生成：
    用户装过游戏就能一条命令重建，且不需要任何第三方库。
    """
    import subprocess
    from pathlib import Path as _P

    out_dir = paths.data_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    tools = _P(__file__).resolve().parent.parent / "tools"
    print("🐋 Watch the Spire · 数据提取")
    print(f"   数据目录 : {out_dir}")
    print(f"   工具目录 : {tools}")
    print()

    g = paths.game_dir(getattr(args, "game", None))
    if not g:
        print("[✗] 找不到游戏安装目录。")
        print(f"    请用 --game 指定，或设环境变量 {paths.ENV_GAME}，例如：")
        print(f'      set {paths.ENV_GAME}=D:\\SteamLibrary\\steamapps\\common\\Slay the Spire 2')
        return 2
    print(f"   游戏目录 : {g}")
    dll, pck = paths.dll_path(), paths.pck_path()
    print(f"   sts2.dll : {dll or '（未找到）'}")
    print(f"   游戏 pck : {pck or '（未找到）'}")
    if not dll:
        print("[✗] 缺少 sts2.dll，无法提取数值。确认游戏目录下存在 "
              "data_sts2_windows_x86_64/sts2.dll")
        return 2
    if not pck:
        print("[!] 缺少 .pck，文案提取会跳过（卡牌名与效果将显示为英文 ID）")
    print()

    # 两个提取器都是独立脚本（依赖同目录的 dotnet_meta / il_disasm），
    # 直接以子进程调用，环境变量把路径传进去，避免它们各自再写死盘符。
    env = dict(os.environ)
    env[paths.ENV_GAME] = str(g)
    env[paths.ENV_DATA] = str(out_dir)
    env["PYTHONIOENCODING"] = "utf-8"
    steps = [("文案（pck 本地化）", "extract_effects.py"),
             ("数值（.NET 程序集）", "extract_values.py"),
             ("状态名对照（从文案挖）", "build_power_names.py")]
    failed = 0
    for label, script in steps:
        path = tools / script
        if not path.is_file():
            print(f"[!] 跳过 {label}：找不到 {script}")
            continue
        if not args.force and (out_dir / "card_effects.json").is_file() \
                and script == "extract_effects.py":
            print(f"[·] {label}: 已有数据，跳过（--force 可强制重跑）")
            continue
        print(f"[…] {label} —— {script}")
        r = subprocess.run([sys.executable, "-X", "utf8", str(path)],
                           cwd=str(tools), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()][-3:]
        for ln in tail:
            print(f"      {ln}")
        if r.returncode != 0:
            failed += 1
            print(f"      [!] 退出码 {r.returncode}")
            for ln in (r.stderr or "").strip().splitlines()[-4:]:
                print(f"      {ln}")
    print()
    for name in ("card_effects.json", "card_values.json", "power_names.json"):
        f = out_dir / name
        print(f"   {'✓' if f.is_file() else '✗'} {name}"
              + (f"  {f.stat().st_size // 1024} KB" if f.is_file() else "  缺失"))
    return 1 if failed else 0


def cmd_doctor(args) -> int:
    ctx = Ctx(args.db)
    _banner(ctx, "环境检查")
    ok = True

    # 0. 路径解析结果 —— 排查「数据/游戏没找到」时第一眼看这里
    info = paths.explain()
    print("路径:")
    for k in ("项目根", "数据目录", "数据目录来源", "游戏目录", "游戏目录来源"):
        print(f"    {k:<10} {info[k]}")
    if "未找到" in info["游戏目录"]:
        print(f"    → 设 {paths.ENV_GAME} 指向游戏安装目录（含 data_sts2_windows_x86_64/）")
    for label, key in (("sts2.dll", "sts2.dll"), ("游戏 pck", "游戏 pck")):
        mark = "✓" if info[key] != "未找到" else "!"
        print(f"[{mark}] {label}: {info[key]}")
    print()

    # 1. 名称库
    if ctx.g.source:
        print(f"[✓] 名称库来源: {ctx.g.source}")
    else:
        print("[!] 未找到 object-catalog.json（中文名会退化为英文 ID）")
        print("    这是游戏内 mod 导出的文件；订阅 DanmakuSpire 后进一次游戏即可生成。")

    # 1b. 提取数据（公开版不含，需要本地跑一次 extract）
    fx = paths.data_dir() / "card_effects.json"
    cv = paths.data_dir() / "card_values.json"
    if fx.is_file() and cv.is_file():
        print(f"[✓] 提取数据: {fx.name} ({fx.stat().st_size // 1024} KB) · "
              f"{cv.name} ({cv.stat().st_size // 1024} KB)")
    else:
        missing = [p.name for p in (fx, cv) if not p.is_file()]
        print(f"[!] 提取数据缺失: {', '.join(missing)}")
        print("    运行 `python -m sts2mon extract` 生成（需要游戏本体，约 30 秒）")
        print("    —— 不影响采集，但卡牌效果与数值会显示为空白")

    # 2. 历史对局
    files = find_run_files()
    print(f"[{'✓' if files else '!'}] 历史对局存档: {len(files)} 个"
          + (f"，最新 {files[-1].name}" if files else "（未找到 .run 文件）"))
    if files:
        try:
            run = HistoryAdapter(ctx.g).parse_file(files[-1])
            print(f"    样例解析: 种子={run.seed} 角色={run.character_name} "
                  f"层数={len(run.floors)} 战斗={len(run.combats)} 胜={run.win}")
        except Exception as exc:
            ok = False
            print(f"    [!] 解析失败: {exc!r}")

    # 3. 实时状态文件
    ad = CombatStateAdapter(glossary=ctx.g)
    if ad.exists():
        st = ad.read() or {}
        age = time.time() - ad.mtime()
        print(f"[✓] 实时状态文件: {ad.path}")
        print(f"    回合={st.get('turn')} 遭遇={st.get('encounter')} "
              f"tracking={st.get('tracking')} 事件数={len(st.get('events') or [])}")
        print(f"    文件更新于 {age:.0f} 秒前"
              + ("（游戏可能正在运行）" if age < 120 else "（游戏当前未运行，属正常）"))
    else:
        print(f"[!] 实时状态文件不存在: {ad.path}")
        print("    需要安装 DamageMeter mod（订阅物品 3747557283），运行游戏后生成。")

    # 4. rdps 明细
    rd = RdpsAdapter(glossary=ctx.g)
    runs = rd.available_runs()
    print(f"[{'✓' if runs else '!'}] rdps 战斗明细: {len(runs)} 场")

    # 5. 数据库
    n = len(ctx.store.run_ids())
    print(f"[✓] 数据库可写: {ctx.db}（已有 {n} 场对局）")

    # 6. Web 端口
    import socket
    port = args.port
    with socket.socket() as s:
        free = s.connect_ex(("127.0.0.1", port)) != 0
    print(f"[{'✓' if free else '!'}] 仪表盘端口 {port}: "
          + ("可用" if free else "被占用（换 --port）"))

    print()
    print("结论:", "一切就绪 ✔" if ok else "存在需要处理的问题 ↑")
    ctx.close()
    return 0 if ok else 1


# ------------------------------------------------------------------ ingest

def cmd_ingest(args) -> int:
    ctx = Ctx(args.db)
    _banner(ctx, "导入历史对局")
    files = find_run_files()
    if args.limit:
        files = files[-args.limit:]
    adapter = HistoryAdapter(ctx.g)
    rdps = RdpsAdapter(glossary=ctx.g)
    amap = AnalyticsAdapter(glossary=ctx.g)
    ok = fail = enriched = mapped = 0
    for f in files:
        try:
            run = adapter.parse_file(f)
            if rdps.enrich(run):
                enriched += 1
            if amap.enrich(run):
                mapped += 1
            ctx.store.save_run(run)
            ok += 1
            if args.verbose:
                s = run.summary()
                ctx.say(f"  + {run.run_id:>12} {s['character']:<6} A{s['ascension']} "
                        f"{s['floors']:>3}层 {s['combats']:>2}战 "
                        f"{'胜' if s['win'] else '负'} 输出{s['damage_dealt']:>6}"
                        f"{'  地图✓' if run.notes.get('map_enriched') else ''}")
        except Exception as exc:
            fail += 1
            print(f"  ! {f.name}: {exc!r}")
    ctx.say(f"\n导入完成: 成功 {ok}，失败 {fail}")
    ctx.say(f"  其中 {enriched} 场补充了 rdps 战斗明细，{mapped} 场补充了完整地图布局")
    ov = Analytics(ctx.store, ctx.g).overview()
    ctx.say(f"全局: {ov['runs']} 场 · 胜率 {ov['win_rate']}% · "
            f"共 {ov['total_floors']} 层 / {ov['total_combats']} 战 / {ov['total_turns']} 回合")
    ctx.close()
    return 0 if not fail else 1


# ------------------------------------------------------------------ stats / export

def cmd_stats(args) -> int:
    ctx = Ctx(args.db)
    a = Analytics(ctx.store, ctx.g)
    ov = a.overview()
    print(f"🐋 全局统计 · {ov['runs']} 场对局")
    print(f"   胜负      : {ov['wins']} 胜 / {ov['losses']} 负  （胜率 {ov['win_rate']}%）")
    print(f"   规模      : {ov['total_floors']} 层 · {ov['total_combats']} 战 · {ov['total_turns']} 回合")
    print(f"   伤害      : 输出 {ov['total_damage_dealt']:,} / 承受 {ov['total_damage_taken']:,}")
    print(f"   时长      : {ov['total_time'] // 3600} 小时（场均 {ov['avg_run_time'] // 60} 分钟）")
    print("\n   角色胜率:")
    for c in ov["by_character"]:
        print(f"     {c['character']:<8} {c['runs']:>3} 场  胜率 {c['win_rate']:>5}%  "
              f"平均 {c['avg_floors']} 层")
    dh = a.death_hotspots()
    if dh["by_encounter"]:
        print("\n   死亡热点:")
        for e in dh["by_encounter"][:8]:
            print(f"     {e['name']:<24} {e['count']} 次")
    print("\n   卡牌表现 Top10:")
    for c in a.card_performance(10):
        per = c["damage_per_play"]
        per_txt = f"每次{per}" if per is not None else "每次—（该数据源无出牌次数）"
        print(f"     {c['name']:<14} 打出{c['plays'] if c['plays'] else '—':>6}  "
              f"伤害{c['damage']:>7}  {per_txt}")
    print("\n   数据源:")
    for s in a.data_sources():
        print(f"     {s['source']:<14} {s['n']:>4} 条"
              + (f"（其中有出牌次数的 {s.get('with_plays', 0)}）" if "with_plays" in s else ""))
    if args.json:
        Path(args.json).write_text(json.dumps(a.export(), ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n   已写出 JSON: {args.json}")
    ctx.close()
    return 0


def cmd_export(args) -> int:
    ctx = Ctx(args.db)
    a = Analytics(ctx.store, ctx.g)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        out.write_text(json.dumps(a.export(), ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        rows = a.card_performance(1000)
        with out.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["empty"])
            w.writeheader()
            w.writerows(rows)
    print(f"已导出: {out}")
    ctx.close()
    return 0


# ------------------------------------------------------------------ serve / watch / demo

def _start_web(ctx: Ctx, host: str, port: int, sandbox: Path | None = None) -> Dashboard:
    # sandbox 为空时走 paths 的默认（<数据目录>/replay），不再依赖 cwd
    dash = Dashboard(ctx.store, live_provider=lambda: ctx.live.snapshot(),
                     glossary=ctx.g, live_state=ctx.live,
                     sandbox_dir=sandbox or paths.sandbox_dir())
    dash.serve(host, port, background=True)
    print(f"   仪表盘   : http://{host}:{port}")
    return dash


def cmd_serve(args) -> int:
    ctx = Ctx(args.db)
    _banner(ctx, "仪表盘服务")
    if not ctx.store.run_ids():
        print("   [!] 数据库还是空的，先跑一次 `python -m sts2mon ingest`\n")
    _start_web(ctx, args.host, args.port, args.sandbox)
    if args.watch:
        return _run_watch(ctx, args)
    print("\n   按 Ctrl+C 退出")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n   已退出")
    ctx.close()
    return 0


def cmd_watch(args) -> int:
    ctx = Ctx(args.db)
    _banner(ctx, "实时监控")
    _start_web(ctx, args.host, args.port, args.sandbox)
    return _run_watch(ctx, args)


def _run_watch(ctx: Ctx, args) -> int:
    adapter = CombatStateAdapter(glossary=ctx.g)
    # serve --watch 没有定义 --jsonl/--interval/--duration，这里统一兜底
    jsonl = Path(getattr(args, "jsonl", None) or paths.jsonl_path())
    interval = float(getattr(args, "interval", 0.5) or 0.5)
    duration = getattr(args, "duration", None)

    bus = EventBus(str(jsonl))
    ctx.live.attach(bus)

    # 事件落库：按数量或按时间双触发。
    # 关键：只写「新增」事件（用已落库条数做水位线），
    # 否则每次 flush 都把最近 200 条重插一遍，事件表会爆炸。
    counter = {"n": 0, "last": time.time(), "persisted": 0, "run_id": ""}

    def flush(run=None) -> None:
        target = run or ctx.live.tracker.run
        if not target.run_id or not target.floors:
            return
        ctx.store.save_run(target)
        if target.run_id != counter["run_id"]:
            counter["run_id"] = target.run_id
            counter["persisted"] = 0        # 换局：事件水位线重置
        # 每条事件自带 node_id / encounter_id（由 Tracker 填），
        # 这样前端能按节点过滤，只显示当前战斗的回合事件流。
        all_events = [r for c in target.combats for r in c.events]
        if len(all_events) > counter["persisted"]:
            ctx.store.add_events(target.run_id, all_events[counter["persisted"]:])
            counter["persisted"] = len(all_events)
            ctx.store.prune_events(target.run_id, keep=4000)

    # 一局结束（或游戏里开了新一局）时，把上一局完整落库并收尾
    def _finish(run) -> None:
        run.live = False
        flush(run)
        closed = ctx.store.close_stale_live_runs(keep=ctx.live.tracker.run.run_id)
        if closed:
            print(f"   [i] 已收尾停止更新的实时对局: {', '.join(closed)}")

    ctx.live.set_finish_hook(_finish)

    def persist(ev) -> None:
        counter["n"] += 1
        now = time.time()
        if counter["n"] % 10 == 0 or now - counter["last"] >= 3:
            counter["last"] = now
            flush()

    bus.subscribe(persist)

    print(f"   数据源   : {adapter.path}")
    print(f"   轮询间隔 : {interval}s   事件日志: {jsonl}")
    if not adapter.exists():
        print("   [!] 状态文件还不存在：装上 DamageMeter mod 并启动游戏后会自动出现。")
    print("   说明     : 长时间挂着一个进程即可；游戏里开新对局会自动识别并切换，"
          "上一局自动落库")
    print("\n   等待游戏数据…（Ctrl+C 退出）\n")
    prev: dict | None = None
    last_mtime = 0.0
    warned = False
    run_token = RunTokenAdapter(glossary=ctx.g)
    # 真实 HP 的唯一来源：进行中存档（DamageMeter 完全不记录 HP）
    run_save = CurrentRunAdapter(glossary=ctx.g)
    prev_save = None
    save_mtime = 0.0
    # 对局结束检测：一局打完时游戏会写 history/<start_time>.run
    end_watcher = RunEndWatcher(ctx.g)
    end_watcher.prime()
    # 结束时要解析完整存档并落库，这里备好适配器
    hist_adapter = HistoryAdapter(ctx.g)
    rdps_adapter = RdpsAdapter(glossary=ctx.g)
    # 已落库的历史对局（避免重复 ingest 刚结束的那局）
    ingested_seeds: set[str] = set()

    # ---- 启动补漏：把「还没入库」的对局存档补上 ----
    # 常见于「监控在上一局打完之后才启动」：那时 .run 已经存在，
    # prime() 会把它排除，于是这一局永远不会被 ingest（实测踩过）。
    # 做法：把每个 .run 的 seed 与库里的 run_id 对比，缺的就补。
    def catch_up_history() -> None:
        known = {r["run_id"] for r in ctx.store.all_run_rows()}
        added = 0
        for f in find_run_files():
            try:
                run = hist_adapter.parse_file(f)
            except Exception:
                continue
            if run.run_id in known:
                continue
            try:
                rdps_adapter.enrich(run)
                ctx.store.save_run(run)
                added += 1
            except Exception as exc:
                print(f"   [!] 补录 {f.name} 失败: {exc!r}")
        if added:
            print(f"   ↩ 启动补漏：补录了 {added} 场未入库的对局")

    catch_up_history()

    # 同一局在不同阶段会有三套 run_id（run-<t> / live-<t> / 裸种子），
    # 按 seed 合并掉重复行；同时清掉早期写进去的角色占位符 `?`。
    try:
        seeds = {f.stem for f in find_run_files()}
        gone = ctx.store.prune_orphan_live_runs(known_seeds=seeds)
        if gone:
            print(f"   ⌫ 清理无权威数据的实时残片 {len(gone)} 条: {', '.join(gone[:4])}")
        moved = ctx.store.collapse_duplicate_runs()
        if moved:
            print(f"   ↔ 合并重复对局 {len(moved)} 条: "
                  + ", ".join(f"{a}→{b}" for a, b in moved[:4])
                  + (" …" if len(moved) > 4 else ""))
        fixed = ctx.store.backfill_character_names()
        if fixed:
            print(f"   ✎ 修正角色占位符/补名 {fixed} 处")
    except Exception as exc:
        print(f"   [!] 对局去重失败: {exc!r}")

    last_token: dict | None = None
    token_run = ""
    token_pending = False
    if run_save.exists():
        print(f"   真实HP源 : {run_save.path}")
        snap0 = run_save.snapshot(run_save.read())
        if snap0.get("hp") is not None:
            print(f"   当前状态 : HP {snap0['hp']}/{snap0.get('max_hp')} · "
                  f"金币 {snap0.get('gold')} · 第 {snap0.get('floor')} 层")
    else:
        print("   [!] 没找到进行中存档（current_run.save），实时 HP 将不可用")
    try:
        while True:
            # ---- 对局结束检测：history/<start>.run 落盘 = 这一局打完了 ----
            for path, info in end_watcher.poll():
                cur_run = ctx.live.tracker.run
                # 对齐：`.run` 的文件名就是 start_time，而实时对局的 run_id
                # 是 `run-<start_time>`（current_run.save 里没有 seed 字段，
                # 只能靠 start_time 认）。
                live_stem = str(cur_run.run_id).removeprefix("run-")
                same = (path.stem == live_stem
                        or str(info.get("run_id")) == live_stem)
                kind = "通关" if info["win"] else (
                    "放弃" if info["abandoned"] else f"败于 {info['killed_by'] or '未知'}")
                print(f"\n   ⏹ 检测到对局结束（{kind}）"
                      f" · {info['floors']} 层 · {info['run_time'] // 60} 分钟"
                      f"{' · 与本局匹配' if same else ''}")
                bus.emit(end_watcher.end_event(info))
                # 解析完整 .run 并落库：实时数据换成权威历史数据
                try:
                    run = hist_adapter.parse_file(path)
                    rdps_adapter.enrich(run)
                    if same and cur_run.players:
                        # 保留实时里认到的玩家身份（本机 Steam ID 等）
                        run.local_player = run.local_player or cur_run.local_player
                    ctx.store.save_run(run)
                    if same and live_stem:
                        # 实时那条是按 start_time 建的（run-1791089551），
                        # 而权威记录按种子（9FVA1LG1LN）。迁移过去并删掉实时那条，
                        # 否则同一局会留下两条、且实时那条永远停在 1 层。
                        moved = ctx.store.migrate_run(f"run-{live_stem}", run.run_id)
                        if moved:
                            print(f"   ↩ 实时记录已并入 {run.run_id}")
                    ctx.store.close_stale_live_runs(keep="")
                    ingested_seeds.add(path.stem)
                    print(f"   ✔ 已落库: {run.run_id}（种子）· {len(run.floors)} 层 · "
                          f"{len(run.combats)} 战")
                except Exception as exc:
                    print(f"   [!] 落库失败: {exc!r}")

            # ---- 逐局令牌：新局最早的信号（战斗状态文件可能还没出现）----
            tk = run_token.read()
            if tk is not None:
                start = str(tk.get("StartTime") or "")
                if last_token is not None and start and start != str(last_token.get("StartTime") or ""):
                    char = ctx.g.char(str(tk.get("CharacterId") or ""))
                    print(f"\n   ▶ 检测到新对局（token start={start}，"
                          f"角色={char}，人数={tk.get('PlayerCount')}）")
                    print("     等待 DamageMeter 写出战斗状态后接管…")
                    token_run = start
                    token_pending = True
                last_token = tk

            # ---- 真实 HP / 金币（进行中存档）----
            save_st = run_save.read()
            if save_st is None and not run_save.exists():
                # current_run.save 消失 = 没有进行中的对局。
                # 这是「监控在开局之后才启动、结果 .run 在启动前就写好了」时
                # 唯一能发现对局已结束的信号（prime 会把旧 .run 排除掉）。
                cur = ctx.live.tracker.run
                if cur.live and cur.floors:
                    print(f"\n   ⏹ 进行中存档已消失 → 对局 {cur.run_id} 已结束"
                          f"（{len(cur.floors)} 层）")
                    bus.emit(Event(kind=K.RUN_END, run_id=cur.run_id,
                                   ts=time.time(), source="current_run",
                                   data={"ended_reason": "current_run.save 消失"}))
                    flush()
                    closed = ctx.store.close_stale_live_runs(keep="")
                    if closed:
                        print(f"   ✔ 已收尾: {', '.join(closed)}")
            if save_st is not None:
                smt = run_save.mtime()
                if smt != save_mtime or prev_save is None:
                    save_mtime = smt
                    for ev in (run_save.initial_events(save_st) if prev_save is None
                               else run_save.diff(prev_save, save_st)):
                        bus.emit(ev)
                    prev_save = save_st
                # DamageMeter 不记录楼层、也不知道当前是第几幕、更没有权威 run_id，
                # 把存档的楼层/幕/run_id 注入给它，多个数据源才会落到
                # 同一个 tracker + 同一个地图节点（否则 HP 会丢）
                snap_save = run_save.snapshot(save_st)
                adapter.set_floor_hint(snap_save.get("floor"))
                adapter.set_act_hint(snap_save.get("act"))
                adapter.set_run_id_hint(run_save.run_id(save_st))

            st = adapter.read()
            # 楼层权威在存档：存档说已经走到第 N 层，而战斗状态文件还停在更早的
            # 那一层（离开战斗后它就不再更新了），那这份战斗数据就是**过时**的，
            # 不能采用 —— 否则事件房会被标成上一场的怪名（实测踩过）。
            floor_trusted = 0
            if save_st is not None:
                floor_trusted = int(run_save.snapshot(save_st).get("floor") or 0)
            if st is not None and floor_trusted:
                st_floor = adapter._floor_of(adapter._segment(st))
                if st_floor and st_floor < floor_trusted:
                    st = None
            if st is not None and prev is not None and floor_trusted:
                prev_floor = adapter._floor_of(adapter._segment(prev))
                if prev_floor and prev_floor < floor_trusted:
                    prev = None        # 基线也过时了，重新建立
            if st is not None:
                mt = adapter.mtime()
                if mt != last_mtime or prev is None:
                    last_mtime = mt
                    # 换局检测（run_seed 变化）——这是「开局即监测」的关键
                    new_run = (prev is not None
                               and st.get("run_seed") is not None
                               and prev.get("run_seed") != st.get("run_seed"))
                    if token_pending and not new_run and prev is None:
                        # 令牌说开了新局、状态文件也重新出现 → 按全新一局从头建立
                        new_run = True
                    events = adapter.diff(prev, st)
                    for ev in events:
                        bus.emit(ev)
                    if new_run:
                        if token_pending:
                            print(f"   ✔ 已接管新对局（seed={st.get('run_seed')}）\n")
                        else:
                            print(f"\n   ▶ 检测到新对局（seed={st.get('run_seed')}），"
                                  f"已切换监控目标\n")
                        token_pending = False
                    if events:
                        snap = ctx.live.snapshot()
                        stale = "  [数据已过期]" if snap.get("stale") else ""
                        print(f"   [T{snap['turn']:>2}] {snap['encounter_name']:<18} "
                              f"输出{snap['damage_dealt']:>6} 受伤{snap['damage_taken']:>4} "
                              f"HP{snap['hp']}/{snap['max_hp']} (+{len(events)}事件){stale}")
                    prev = st
                    idle_since = None
                else:
                    # 文件没变：游戏这一拍没动作（正常）
                    idle_since = idle_since or time.time()
            elif not warned and not adapter.exists():
                warned = True
                print("   [i] 还没看到战斗状态文件；游戏一写盘就会自动开始记录。")
                if run_token.exists():
                    print(f"   [i] 但已发现逐局令牌（{run_token.path.name}），"
                          f"新对局会被立刻识别。")
            # 定时落库：即使游戏这几秒没有新动作，也保证库里的实时状态不落后于 3 秒
            if ctx.live.last_change and time.time() - counter["last"] >= 3:
                counter["last"] = time.time()
                flush()
            # 每隔一阵清理「卡在进行中」的实时对局（进程被杀/换存档留下的）
            if time.time() - counter.get("last_sweep", 0) >= 30:
                counter["last_sweep"] = time.time()
                closed = ctx.store.close_stale_live_runs(
                    keep=ctx.live.tracker.run.run_id)
                if closed:
                    print(f"   [i] 已收尾停止更新的实时对局: {', '.join(closed)}")
            if duration and ctx.live.last_change and \
                    time.time() - ctx.live.last_change > duration:
                print(f"\n   {duration}s 内没有新数据，退出。")
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n   停止监控")
    finally:
        flush()
        bus.close()
        ctx.close()
    return 0


def cmd_demo(args) -> int:
    """用一场真实历史对局回放，模拟实时数据流（无需启动游戏）。"""
    ctx = Ctx(args.db)
    _banner(ctx, "回放演示（真实对局 → 模拟实时）")
    if not ctx.store.run_ids():
        print("   数据库为空，先导入历史对局…\n")
        cmd_ingest(argparse.Namespace(db=args.db, limit=None, verbose=False))

    rid = None
    if args.seed:
        for r in ctx.store.all_run_rows():
            if str(r["seed"]) == args.seed or r["run_id"] == args.seed:
                rid = r["run_id"]
                break
        if not rid:
            print(f"   [!] 找不到种子 {args.seed}")
    if not rid:
        rows = [r for r in ctx.store.all_run_rows() if (r["combats"] or 0) > 3]
        rows.sort(key=lambda r: (int(r["combats"] or 0), int(r["turns"] or 0)), reverse=True)
        if not rows:
            print("   [!] 没有可用对局（需要带战斗记录的对局）")
            ctx.close()
            return 1
        rid = rows[0]["run_id"]
    row = ctx.store.run_row(rid) or {}
    print(f"   回放对局 : {rid}  种子={row.get('seed')}  角色={row.get('character_name')}  "
          f"{row.get('combats')} 战 / {row.get('turns')} 回合")

    _start_web(ctx, args.host, args.port, args.sandbox)
    print(f"   回放速度 : {args.speed}x（真实模式 {args.realtime}）\n")

    import threading
    done = threading.Event()

    def run() -> None:
        try:
            res = start_replay(ctx.store, rid, ctx.live, args.sandbox,
                               speed=args.speed, realtime=args.realtime)
            print(f"\n   回放结束: {res['turns']} 个回合，用时约 {res['eta']}s")
        except Exception as exc:
            print(f"   [!] 回放异常: {exc!r}")
        finally:
            done.set()

    t = threading.Thread(target=run, name="demo-replay", daemon=True)
    t.start()

    try:
        last = 0
        while not done.is_set():
            snap = ctx.live.snapshot()
            if snap["turn"] != last and snap["available"]:
                last = snap["turn"]
                print(f"   [T{snap['turn']:>3}] {snap['encounter_name']:<20} "
                      f"输出{snap['damage_dealt']:>7} 受伤{snap['damage_taken']:>5} "
                      f"HP{snap['hp']}/{snap['max_hp']}  卡牌{len(snap['cards'])}")
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n   中断回放")
    finally:
        ctx.store.save_run(ctx.live.tracker.run)
        bus_events = [r for c in ctx.live.tracker.run.combats for r in c.events]
        ctx.store.add_events(ctx.live.tracker.run.run_id, bus_events)
        print(f"\n   已保存实时事件 {len(bus_events)} 条到数据库")
    if args.hold:
        print("\n   仪表盘仍在运行，按 Ctrl+C 退出…")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    ctx.close()
    return 0


# ------------------------------------------------------------------ argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sts2mon", description="杀戮尖塔2 数据监控程序（实时地图/路线、战斗数据、全局统计）")
    p.add_argument("--db", type=Path, default=None,
                   help="SQLite 路径（默认走 STS2MON_DATA，即 <项目根>/.sts2mon/sts2mon.db）")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="检查数据源与运行环境")
    d.add_argument("--port", type=int, default=8770)
    d.set_defaults(func=cmd_doctor)

    x = sub.add_parser("extract", help="从游戏文件提取文案与数值（公开版不含这些数据）")
    x.add_argument("--game", type=Path, default=None,
                   help=f"游戏安装目录（默认自动探测，可设 {paths.ENV_GAME}）")
    x.add_argument("--force", action="store_true", help="已有数据也重新提取")
    x.set_defaults(func=cmd_extract)

    i = sub.add_parser("ingest", help="导入历史对局（.run 存档）")
    i.add_argument("--limit", type=int, default=None, help="只导入最近 N 场")
    i.add_argument("--verbose", action="store_true")
    i.set_defaults(func=cmd_ingest)

    s = sub.add_parser("stats", help="打印全局统计")
    s.add_argument("--json", type=str, default=None, help="同时写出 JSON 报告到该路径")
    s.set_defaults(func=cmd_stats)

    e = sub.add_parser("export", help="导出统计结果")
    e.add_argument("--format", choices=["json", "csv"], default="json")
    e.add_argument("--out", type=str, default="sts2mon-export.json")
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("clean", help="清理实时对局数据（回放/监控产生的 live-*）")
    c.add_argument("--all", action="store_true", help="清空整库（含历史对局）")
    c.set_defaults(func=cmd_clean)

    def add_web_args(sp) -> None:
        sp.add_argument("--host", default="127.0.0.1")
        sp.add_argument("--port", type=int, default=8770)
        sp.add_argument("--sandbox", type=Path, default=None,
                        help="回放沙箱目录（不会碰游戏真实文件）")

    sv = sub.add_parser("serve", help="只启动仪表盘")
    add_web_args(sv)
    sv.add_argument("--watch", action="store_true", help="同时开启实时监控")
    sv.set_defaults(func=cmd_serve)

    w = sub.add_parser("watch", help="实时监控（+仪表盘）")
    add_web_args(w)
    w.add_argument("--interval", type=float, default=0.5, help="轮询间隔秒（默认 0.5）")
    w.add_argument("--jsonl", type=Path, default=None, help="事件流落盘路径")
    w.add_argument("--duration", type=int, default=None,
                   help="无新数据持续 N 秒后自动退出（便于脚本测试）")
    w.set_defaults(func=cmd_watch)

    dm = sub.add_parser("demo", help="用真实对局回放模拟实时（无需启动游戏）")
    add_web_args(dm)
    dm.add_argument("--seed", type=str, default=None, help="要回放的对局种子")
    dm.add_argument("--speed", type=float, default=6.0, help="回放倍速（默认 6x）")
    dm.add_argument("--realtime", action="store_true", help="按真实速度（2 秒/回合）")
    dm.add_argument("--hold", action="store_true", help="回放结束后保持服务运行")
    dm.set_defaults(func=cmd_demo)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())

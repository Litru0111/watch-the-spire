"""端到端自检：导入数据 -> 起服务 -> 打全部 API -> 回放真实对局 -> 校验实时链路。

用法:  python tools/selftest.py [--port 8799]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters import HistoryAdapter, RdpsAdapter, find_run_files  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState, start_replay  # noqa: E402
from sts2mon.model import Store  # noqa: E402
from sts2mon.web import Dashboard  # noqa: E402
from _guard import require_saves  # noqa: E402
from sts2mon import paths

PASS, FAIL = "✔", "✘"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> bool:
    results.append((bool(cond), label))
    print(f"  [{PASS if cond else FAIL}] {label}" + (f"  —— {detail}" if detail else ""))
    return bool(cond)


def get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = r.read()
        ctype = r.headers.get("Content-Type", "")
    return (json.loads(body.decode("utf-8")) if "json" in ctype else body.decode("utf-8")), r.status


def post(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8")), r.status


def _api_checks(base: str, store: Store, live: LiveState, g, row: dict, n: int, check,
                replay_seed: str | None = None) -> None:
    """⑥~⑨ 的全部 API / 实时链路断言。

    replay_seed：指定回放用哪一局（必须选战斗足够多的，才能观察到遭遇切换）。
    """
    body, status = get(base + "/api/health")
    check(status == 200 and body.get("ok"), "/api/health", json.dumps(body, ensure_ascii=False))

    html, _ = get(base + "/")
    # 标题已从「杀戮尖塔2 · 数据监控台」改为「Watch the Spire 监控台」，
    # 所以这里改查新标题（产品名）。
    check("Watch the Spire" in html and "api/live" in html, "/ 返回仪表盘页面", f"{len(html)} 字节")

    ov2, _ = get(base + "/api/overview")
    check(ov2["overview"]["runs"] == n, "/api/overview 场次一致")

    runs2, _ = get(base + "/api/runs")
    check(len(runs2["runs"]) == n, "/api/runs 列表长度一致", f"{len(runs2['runs'])}")

    d2, _ = get(base + "/api/run?id=" + urllib.parse.quote(row["run_id"]))
    check(len(d2["acts"]) > 0 and len(d2["combats"]) > 0, "/api/run 返回地图与战斗",
          f"{len(d2['acts'])} acts / {len(d2['combats'])} combats")
    has_turn = any(c.get("turn_stats") for c in d2["combats"])
    has_card = any(c.get("card_stats") for c in d2["combats"])
    check(has_turn or has_card, "/api/run 含战斗明细",
          f"turn_stats={has_turn} card_stats={has_card}")

    lv, _ = get(base + "/api/live")
    check(lv["available"] is False, "/api/live 未回放时 available=False（诚实标注）")

    gl, _ = get(base + "/api/glossary")
    check(gl["size"] > 1000, "/api/glossary 名称库规模", str(gl["size"]))

    print("\n⑦ 真实对局回放（模拟实时数据流）")
    seed_for_replay = replay_seed or row["seed"]
    with urllib.request.urlopen(
            urllib.request.Request(base + "/api/replay?seed=" + seed_for_replay + "&speed=60",
                                   method="POST"), timeout=30) as r:
        rep = json.loads(r.read().decode("utf-8"))
    check(rep.get("ok"), "回放已启动",
          f"seed={seed_for_replay} " + json.dumps(rep, ensure_ascii=False))

    seen_turns, seen_cards, seen_enc, snap = set(), 0, set(), {}
    t0 = time.time()
    while time.time() - t0 < 30:
        lv, _ = get(base + "/api/live")
        if lv.get("available"):
            seen_turns.add(lv.get("turn"))
            seen_cards = max(seen_cards, len(lv.get("cards") or []))
            if lv.get("encounter"):
                seen_enc.add(lv["encounter"])
            snap = lv
            if lv.get("damage_dealt", 0) > 0 and seen_cards > 0 and len(seen_enc) >= 2:
                break
        time.sleep(0.3)
    check(len(seen_turns) >= 2, "实时快照推进了多回合", f"观察到 {len(seen_turns)} 个回合")
    check(seen_cards > 0, "实时卡牌贡献非空", f"{seen_cards} 张")
    check(len(seen_enc) >= 2, "实时遭遇切换", f"{len(seen_enc)} 个：" + ",".join(list(seen_enc)[:3]))
    check(snap.get("damage_dealt", 0) > 0, "实时累计伤害 > 0", str(snap.get("damage_dealt")))
    check(bool(snap.get("encounter_name")) and not str(snap["encounter_name"]).isascii(),
          "实时遭遇名为中文", str(snap.get("encounter_name")))

    print("\n⑧ 实时事件流落库与查询")
    evs = [r for c in live.tracker.run.combats for r in c.events]
    check(len(evs) > 0, "回放产生了实时事件", f"{len(evs)} 条（去重后）")
    store.add_events(live.tracker.run.run_id, evs)
    got, _ = get(base + "/api/events?id=" + urllib.parse.quote(live.tracker.run.run_id))
    check(len(got["events"]) > 0, "事件流可通过 API 查询", f"{len(got['events'])} 条")
    kinds = {e["kind"] for e in got["events"]}
    check(len(kinds) >= 1, "事件类型", ",".join(sorted(kinds)))

    print("\n⑨ 实时状态持久化")
    live_run = live.tracker.run
    check(len(live_run.combats) > 0, "实时对局含战斗", f"{len(live_run.combats)} 场")
    store.save_run(live_run)
    got2 = store.run_row(live_run.run_id)
    check(got2 is not None, "实时对局已入库", live_run.run_id)
    check(len(store.nodes(live_run.run_id)) > 0, "实时对局的楼层已入库",
          f"{len(store.nodes(live_run.run_id))} 层")
    combats_db = store.combats(live_run.run_id)
    check(len(combats_db) > 0, "实时对局的战斗已入库", f"{len(combats_db)} 场")
    cards_db = store.card_stats(live_run.run_id)
    check(len(cards_db) > 0, "实时对局的卡牌统计已入库", f"{len(cards_db)} 张")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--db", type=Path, default=paths.data_dir() / "selftest.db")
    ap.add_argument("--replay-speed", type=float, default=40.0)
    args = ap.parse_args()

    if args.db.exists():
        args.db.unlink()
    g = default_glossary()
    store = Store(args.db)
    base = f"http://127.0.0.1:{args.port}"

    print("=" * 78)
    print("① 数据源与解析")
    files = find_run_files()
    check(len(files) > 0, "发现历史对局存档", f"{len(files)} 个")
    if not files:
        # 后面会 files[-1]，没存档直接 IndexError；给明确指引而不是 traceback
        require_saves()
    ad = HistoryAdapter(g)
    run = ad.parse_file(files[-1])
    check(run.seed != "", "解析出种子", run.seed)
    check(len(run.floors) > 0, "解析出楼层", f"{len(run.floors)} 层")
    check(len(run.combats) > 0, "解析出战斗", f"{len(run.combats)} 战")
    check(run.character_name not in ("", "?"), "角色名本地化", run.character_name)
    kinds = {n.point_type for n in run.floors}
    check(len(kinds) >= 2, "地图点类型多样", ",".join(sorted(kinds)))
    rdp = RdpsAdapter(glossary=g)
    n_rdps = rdp.enrich(run)
    check(True, "rdps 明细匹配", f"{n_rdps} 场")

    print("\n② 名称解析")
    # 遭遇名用**游戏本地化表里的原文**（自建名称库提供的值）。
    # 以前这里是「外骨骼虫（弱）」「帝皇蟹（Boss）」——那套「基础名+强度后缀」
    # 是名称库只能拿到单体怪物译名时的权宜之计。现在游戏自己给每个强度
    # 都写了独立文案，而且信息量更大（如 NIBBITS_NORMAL=「两只小啃兽」、
    # NIBBITS_WEAK=「一只落单的小啃兽」），所以改用原文。
    cases = {
        "CARD.SHIV": "小刀", "MONSTER.SEAPUNK": "海洋混混",
        "ENCOUNTER.EXOSKELETONS_WEAK": "外骨骼虫",
        "ENCOUNTER.KAISER_CRAB_BOSS": "帝皇蟹",   # 译名来自游戏内 UI 文本
        "RELIC.VAJRA": "金刚杵",
    }
    for raw, want in cases.items():
        got = g.name(raw) if not raw.startswith("ENCOUNTER") else g.encounter(raw)
        check(got == want, f"{raw} -> {want}", f"实际 {got}")
    check(g.canonical("小刀") == "SHIV", "本地化名归一化 小刀 -> SHIV",
          f"实际 {g.canonical('小刀')}")
    check(g.canonical("CARD.SHIV") == "SHIV", "带前缀 ID 归一化 CARD.SHIV -> SHIV",
          f"实际 {g.canonical('CARD.SHIV')}")

    print("\n③ 导入数据库")
    # 优先选用「有 rdps 明细且战斗最多」的对局：既覆盖逐卡数据，也保证回放足够长，
    # 能观察到多次遭遇切换（只按文件大小挑会选到刚打完的短对局）。
    rdps_seeds = set(rdp.available_runs())

    def _rank(f: Path):
        try:
            d = json.loads(f.read_text(encoding="utf-8-sig"))
        except Exception:
            return (1, 0, 0)
        seed = d.get("seed")
        combats = sum(1 for act in (d.get("map_point_history") or [])
                      for pt in act
                      if pt.get("map_point_type") in ("monster", "elite", "boss"))
        return (0 if seed in rdps_seeds else 1, -combats, -f.stat().st_size)

    ordered = sorted(files, key=_rank)
    n = 0
    replay_seed = ""
    for f in ordered[:40]:
        try:
            r = ad.parse_file(f)
            rdp.enrich(r)
            store.save_run(r)
            n += 1
            if not replay_seed and len(r.combats) >= 12:
                # 回放样本要用「遭遇足够多」的一局，否则观察不到切换
                replay_seed = r.seed
        except Exception as exc:
            print("     导入失败:", f.name, repr(exc))
    check(n > 0, "批量导入", f"{n} 场")
    check(bool(replay_seed), "找到适合回放的样本（≥12 场战斗）", replay_seed)
    row = store.all_run_rows()[0]
    nodes = store.nodes(row["run_id"])
    check(len(nodes) > 0, "节点已落库", f"{len(nodes)} 行")
    combats = store.combats(row["run_id"])
    check(len(combats) > 0, "战斗已落库", f"{len(combats)} 行")
    cs = store.card_stats(row["run_id"])
    check(True, "卡牌统计已落库", f"{len(cs)} 张")
    turns = store.turn_stats(row["run_id"])
    check(len(turns) > 0, "逐回合数据已落库", f"{len(turns)} 行")

    print("\n④ 统计聚合")
    from sts2mon.analytics import Analytics
    a = Analytics(store, g)
    ov = a.overview()
    check(ov["runs"] == n, "总览场次一致", f"{ov['runs']}")
    check(isinstance(ov["win_rate"], float), "胜率可算", f"{ov['win_rate']}%")
    fc = a.floor_curve()
    check(len(fc["floors"]) > 0, "楼层曲线有数据", f"{len(fc['floors'])} 个楼层点")
    check(len(a.card_performance(10)) > 0, "卡牌表现可用")
    check(len(a.encounter_stats(10)) > 0, "遭遇战绩可用")
    exp = a.export()
    check("overview" in exp and "data_sources" in exp, "导出包含全部区块")

    print("\n⑤ 图表数据（逐回合）")
    tc = a.turn_curve(row["run_id"])
    check(len(tc["turns"]) > 0, "逐回合曲线", f"{len(tc['turns'])} 回合")

    print("\n⑥ Web 服务与 API")
    live = LiveState(g)
    dash = Dashboard(store, live_provider=live.snapshot, glossary=g,
                     live_state=live, sandbox_dir=paths.data_dir() / "selftest-replay")
    # 后台线程跑服务，主线程跑断言：断言抛异常能被立即看到
    httpd = dash.serve("127.0.0.1", args.port, background=True)
    time.sleep(0.4)
    try:
        _api_checks(base, store, live, g, row, n, check, replay_seed=replay_seed)
    except BaseException as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(tb)
        results.append((False, f"API 断言过程未抛异常 —— {exc!r}"))
        try:
            (paths.data_dir() / "selftest-traceback.txt").write_text(tb, encoding="utf-8")
        except Exception:
            pass
    finally:
        httpd.shutdown()
        httpd.server_close()

    ok = sum(1 for c, _ in results if c)
    bad = [(c, l) for c, l in results if not c]
    print("\n" + "=" * 78)
    print(f"自检结果: {ok} / {len(results)} 项通过")
    for _, label in bad:
        print(f"   {FAIL} {label}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())

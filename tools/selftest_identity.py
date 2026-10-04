"""验证「本机玩家」判定。

背景：`.run` 存档里**没有** local_player_id 字段（实测恒为 None），
唯一可靠依据是存档路径里的 Steam ID（`.../steam/<id>/modded/profile2/...`）
与 `players[].id` 对齐。

踩过的坑：
  * history 适配器退化成 `players[0]` → 多人局**全部**把队友当成本机
  * current_run 适配器同样取 `players[0]` → 多人局的 HP/金币/牌组会看错人
  * 单人局仍应正常（此时 players 只有一位）

本测试**不含任何写死的 Steam ID**：本机身份从真实存档路径里推导，
所以换了账号、换台机器照样能跑。没有多人存档时会跳过依赖真实样本的断言
（并如实报告跳过，而不是假装通过）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.adapters.history import (HistoryAdapter, find_run_files,  # noqa: E402
                                      local_steam_id)
from sts2mon.glossary import default_glossary  # noqa: E402

G = default_glossary()
results: list[tuple[bool, str]] = []
skipped: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


def skip(label: str, why: str) -> None:
    skipped.append(label)
    print(f"  [·] 跳过 {label} —— {why}")


# ---------- ① 路径里提取 Steam ID（纯字符串，无需任何存档） ----------
print("① 从存档路径提取本机 Steam ID")
SAMPLE_ID = "76561198000000000"      # 明显的假 ID，避免把真实账号写进仓库
p = (f"C:\\Users\\x\\AppData\\Roaming\\SlayTheSpire2\\steam\\{SAMPLE_ID}\\modded\\"
     f"profile2\\saves\\history\\1791089551.run")
check(local_steam_id(p) == SAMPLE_ID, "标准路径能提取", local_steam_id(p))
check(local_steam_id(p.replace("\\modded", "")) == SAMPLE_ID,
      "非 modded 路径也能提取")
check(local_steam_id("C:\\nope\\saves\\history\\1.run") == "",
      "无 steam 段时返回空串")
check(local_steam_id("/home/u/.steam/steam/76561198000000000/modded/x.run") == SAMPLE_ID,
      "正斜杠路径也能提取")

# ---------- ② 真实存档统计 ----------
print()
print("② 真实存档：本机身份推导")
files = find_run_files()
check(bool(files), "找到历史存档", f"{len(files)} 个 .run")
if not files:
    print("\n结果: 没有存档，无法继续（这是环境问题，不是代码问题）")
    sys.exit(1)

# 本机 ID 由真实路径推导 —— 不写死
ME = local_steam_id(files[-1])
check(bool(ME), "从存档路径推导出本机 Steam ID", ME or "（该路径不含 steam 段）")

multi: list[tuple[Path, dict]] = []
single: tuple[Path, dict] | None = None
for f in files:
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    n = len(d.get("players") or [])
    if n > 1:
        multi.append((f, d))
    elif n == 1 and single is None:
        single = (f, d)
print(f"     多人存档 {len(multi)} 场 · 单人存档 {1 if single else 0} 场（取样）")

# ---------- ③ 多人局：本机不能取 players[0] ----------
print()
print("③ 真实多人存档：本机位置与判定")
if not ME:
    skip("多人局本机判定", "存档路径里没有 Steam ID，无法定位本机")
elif not multi:
    skip("多人局本机判定", "本机没有多人对局存档")
else:
    a = HistoryAdapter(G)
    all_ok = True
    first_is_me = 0
    for f, d in multi:
        ids = [str(x.get("id")) for x in d["players"]]
        if ids[0] == ME:
            first_is_me += 1
        run = a.parse_file(f)
        if run.local_player != ME:
            all_ok = False
            print(f"       ✘ {f.stem}: local_player={run.local_player!r}")
        lp = [x for x in run.players if x.is_local]
        if len(lp) != 1 or lp[0].player_id != ME:
            all_ok = False
    check(all_ok, f"全部 {len(multi)} 场都认出本机")
    check(first_is_me < len(multi),
          "样本里存在「本机不在第一位」的情形（否则测不出这个 bug）",
          f"{len(multi) - first_is_me}/{len(multi)} 场本机不在首位")

    f0, d0 = next(((f, d) for f, d in multi
                   if str(d["players"][0].get("id")) != ME), (None, None))
    if f0:
        run0 = a.parse_file(f0)
        check(run0.local_player != str(d0["players"][0].get("id")),
              "本机 != players[0] 时不会认错")
    else:
        skip("「本机 != players[0]」断言", "样本里本机恰好都在首位")

    # ---------- ④ current_run 的 me() ----------
    print()
    print("④ current_run.me() 选人（多人局不能取第一个）")
    f1, dd = multi[0]
    me = CurrentRunAdapter(glossary=G, path=f1).me(dd)
    check(str(me.get("id")) == ME, "me() 选中本机（不是 players[0]）",
          f"选中 id={str(me.get('id'))[-6:] if me.get('id') else None}")
    shuffled = dict(dd)
    shuffled["players"] = list(reversed(dd["players"]))
    me2 = CurrentRunAdapter(glossary=G, path=f1).me(shuffled)
    check(str(me2.get("id")) == ME, "玩家顺序颠倒后仍选中本机")

# ---------- ⑤ 单人局 ----------
print()
print("⑤ 单人存档不受影响")
if not single:
    skip("单人局判定", "本机没有单人存档")
else:
    f1, d1 = single
    run1 = HistoryAdapter(G).parse_file(f1)
    # 单人局的 players[0].id 是**整数 1**（不是 Steam ID），
    # 所以本机标识就是 "1"，而不是账号 ID —— 断言要按这个来。
    check(run1.local_player == str(d1["players"][0].get("id")),
          "单人局本机 = 唯一玩家", f"local_player={run1.local_player!r}")
    check(len([x for x in run1.players if x.is_local]) == 1,
          "单人局恰好一位标记为本机")
    ad1 = CurrentRunAdapter(glossary=G, path=f1)
    check(ad1.me(d1).get("id") == d1["players"][0].get("id"),
          "单人局 me() 返回唯一玩家")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过"
      + (f" · 跳过 {len(skipped)} 项" if skipped else ""))
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

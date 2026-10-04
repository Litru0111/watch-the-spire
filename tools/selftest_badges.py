"""验证「对局结束获得的称号」能被读取、翻译并落进对局记录。

数据链：
    存档 players[].badges  [{"id":"ELITE","rarity":"bronze"}]
      → history.py 解析进 RunState/PlayerStat
      → SQLite players.badges
      → summary 用 pck 文案翻成中文
      → 界面按稀有度上色显示

踩过的坑：
  * 称号的本地化键是**两个点**：`ELITE.bronzeTitle`（按 bronze/silver/gold 分档）。
    早先的提取正则只认 `KEY.title`，称号文案**一条都没提取到**
  * 正则里 `(?:bronze|silver|gold)?(?:title|...)` 这种写法会先吃掉 `bronze`，
    剩下的 `Title` 不再以小写 `title` 开头 → 永远匹配不上（实测踩过）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon import effects as E  # noqa: E402
from sts2mon.adapters.history import find_run_files  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.model import Store  # noqa: E402
from sts2mon import paths
from _guard import require_extracted  # noqa: E402

# 称号文案来自 card_effects.json（公开仓库不含，需本地 extract）
require_extracted("card_effects.json")

G = default_glossary()
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


print("① 称号文案已从 pck 提取（含分档）")
raw = json.loads((paths.data_dir() / "card_effects.json").read_text(encoding="utf-8"))["文案"]
tiered = {k: v for k, v in raw.items() if v.get("badge_tiers")}
check(len(tiered) >= 8, "带 bronze/silver/gold 分档的称号条目", f"{len(tiered)} 个")
for bid in ("ELITE", "SPEEDY", "MONEY_MONEY", "TINY_DECK", "BIG_DECK"):
    t = (raw.get(bid) or {}).get("badge_tiers") or {}
    check(bool(t.get("bronze", {}).get("title")),
          f"{bid} 有中文名", (t.get("bronze") or {}).get("title") or "无")

print()
print("② 按稀有度取对应档的名字（不能三档同名）")
e_b = E.badge_name("ELITE", "bronze")
e_s = E.badge_name("ELITE", "silver")
e_g = E.badge_name("ELITE", "gold")
check(len({e_b, e_s, e_g}) == 3, "精英三档名字各不相同",
      f"{e_b} / {e_s} / {e_g}")
check(e_g == "精英屠夫", "gold 档取到最高档名", e_g)
check(E.badge_name("ELITE", "") in (e_b, e_s, e_g), "不传稀有度也能取一个")

print()
print("③ 不分档的称号退回普通 title")
check(E.badge_name("CCCCOMBO") == "连连连连段攻击",
      "CCCCOMBO 用普通 title", E.badge_name("CCCCOMBO"))
check(E.badge_name("HIGHLANDER") == "宇宙", "宇宙（HIGHLANDER）", E.badge_name("HIGHLANDER"))

print()
print("④ 存档形态 → 带中文名的列表")
got = E.badges([{"id": "ELITE", "rarity": "silver"},
                {"id": "DEBUFFER", "rarity": "bronze"},
                {"id": "KACHING", "rarity": "bronze"}])
check(len(got) == 3, "三条都转出来了")
check(got[0]["name"] == "精英杀手", "稀有度对上了", got[0]["name"])
check(all(b["desc"] for b in got), "每条都带达成条件说明",
      " / ".join(b["desc"][:14] for b in got))
check(E.badges(None) == [] and E.badges([]) == [], "空值安全")

print()
print("⑤ 真实存档：能读出称号并识别稀有度")
seen: dict[str, int] = {}
total = 0
for f in find_run_files():
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    for p in (d.get("players") or []):
        for b in (p.get("badges") or []):
            if not isinstance(b, dict) or not b.get("id"):
                continue
            total += 1
            seen[str(b.get("id"))] = seen.get(str(b.get("id")), 0) + 1
check(total > 0, "历史存档里读到称号", f"{total} 条 · {len(seen)} 种")
missing = [k for k in seen if not E.badge_name(k)]
check(not missing, "读到的称号都有中文名",
      f"缺 {missing}" if missing else f"{len(seen)} 种全有")

print()
print("⑥ 落库 + 总结接口带出称号")
store = Store(paths.db_path())
try:
    rows = [r for r in store.all_run_rows() if r.get("badges")]
    check(bool(rows), "数据库里有带称号的对局", f"{len(rows)} 局")
    if rows:
        from sts2mon.summary import summarize
        d = summarize(store, rows[0]["run_id"], G)
        check(bool(d.get("badge_list")), "总结接口顶层带 badge_list",
              f"{len(d.get('badge_list') or [])} 个")
        for p in (d.get("per_player") or []):
            if p.get("badge_list"):
                check(True, "per_player 也各带一份",
                      f"{p.get('name') or p['player_id'][-6:]}: "
                      f"{[b['name'] for b in p['badge_list']]}")
                break
        else:
            check(True, "（该局 per_player 无称号，跳过）")
finally:
    store.close()

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

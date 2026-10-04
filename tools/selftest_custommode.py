"""验证「自定义对局」的识别与区分。

自定义对局在数据里的标记：
    game_mode = "custom"
    modifiers = [{"id": "MODIFIER.DRAFT"}]

踩过的坑：
  * `current_run.py` 与 `damage_meter.py` 把 game_mode **写死成 "standard"**，
    自定义对局被当成标准局
  * `modifiers` 虽然在 _META_KEYS 里，但**从来没被解析**，修正项信息全丢
  * 名称库里没有 MODIFIER 类别，`MODIFIER.DRAFT` 只能显示英文 ID
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.adapters.history import HistoryAdapter, _modifier_ids  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from _guard import require_extracted  # noqa: E402

# 修正项中文名（MODIFIER.DRAFT → 选牌）来自 card_effects.json，
# 这是**间接依赖**：静态扫描看不出，只有跑起来调用 G.name() 才会暴露。
require_extracted("card_effects.json")

G = default_glossary()
# 假的 Steam ID（只用来拼构造存档路径）。真实账号 ID 不该进公开仓库。
ME = "76561198000000000"
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


# ---------- ① 修正项 ID 解析 ----------
print("① 修正项 ID 解析（两种存档形态都要兼容）")
check(_modifier_ids([{"id": "MODIFIER.DRAFT"}]) == ["DRAFT"],
      "dict 形态剥掉 MODIFIER. 前缀", str(_modifier_ids([{"id": "MODIFIER.DRAFT"}])))
check(_modifier_ids(["MODIFIER.DRAFT"]) == ["DRAFT"], "字符串形态也支持")
check(_modifier_ids([]) == [] and _modifier_ids(None) == [], "空值安全")
check(_modifier_ids([{"id": "A"}, {"id": "A"}]) == ["A"], "重复项去重")
check(_modifier_ids([{"name": "MODIFIER.HEALING"}]) == ["HEALING"], "name 字段兜底")

# ---------- ② current_run 适配器读出 custom ----------
print()
print("② 进行中存档：game_mode / modifiers 必须按实际读，不能写死")
work = Path(tempfile.mkdtemp(prefix="sts2-custom-"))
# 造一个「自定义对局」的进行中存档，路径里带本机 Steam ID
save_dir = work / "steam" / ME / "modded" / "profile1" / "saves"
save_dir.mkdir(parents=True)
state = {
    "game_mode": "custom",
    "modifiers": [{"id": "MODIFIER.DRAFT"}],
    "current_act_index": 0,
    # 楼层数取自 visited_map_coords 的长度（不是 map_point_history）
    "visited_map_coords": [[0, 1]],
    "start_time": 1791118165,
    "players": [{"id": ME, "character_id": "CHARACTER.SILENT",
                 "current_hp": 56, "max_hp": 70, "gold": 99,
                 "deck": [], "relics": [], "potions": []}],
    # 楼层数取自 map_point_history（各 Act 累计）；
    # 最后一个点若 player_stats 为空/hp=0 会被算作「进行中」而不计入，
    # 所以这里给一个已结算的点（current_hp 非 0）。
    "map_point_history": [[{"map_point_type": "monster", "rooms": [],
                            "player_stats": [{"current_hp": 56, "max_hp": 70}]}]],
}
p = save_dir / "current_run.save"
p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

ad = CurrentRunAdapter(glossary=G, path=p)
snap = ad.snapshot(ad.read())
check(snap.get("game_mode") == "custom", "读出 game_mode=custom",
      repr(snap.get("game_mode")))
check(snap.get("modifiers") == ["DRAFT"], "读出 modifiers=[DRAFT]",
      str(snap.get("modifiers")))
check(snap.get("floor") == 1 and snap.get("hp") == 56, "HP/层数仍然正常",
      f"第 {snap.get('floor')} 层 HP {snap.get('hp')}/{snap.get('max_hp')}")

# 同一份存档改成 standard，应读出 standard（证明不是写死）
state["game_mode"] = "standard"
state["modifiers"] = []
p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
snap2 = CurrentRunAdapter(glossary=G, path=p).snapshot(ad.read())
check(snap2.get("game_mode") == "standard" and snap2.get("modifiers") == [],
      "标准局读出 standard（不是写死）", repr(snap2.get("game_mode")))

# ---------- ③ 修正项中文名 ----------
print()
print("③ 修正项 ID → 中文名（名称库没有 MODIFIER 类别，要走文案表）")
check(G.name("MODIFIER.DRAFT") == "选牌", "MODIFIER.DRAFT → 选牌",
      repr(G.name("MODIFIER.DRAFT")))
check(G.name("DRAFT") == "选牌", "裸 ID 也能查到", repr(G.name("DRAFT")))
check(G.name("POTION.ENERGY_POTION") == "能量药水",
      "药水仍正常（补充表没有破坏原查询）", repr(G.name("POTION.ENERGY_POTION")))

print()
print("④ 模式不能被「不掌握它」的数据源覆盖")
from sts2mon.events import Event, K  # noqa: E402
from sts2mon.tracker import Tracker  # noqa: E402


def _fresh(mode="custom", mods=("DRAFT",)):
    t = Tracker("r", G)
    t.apply(Event(kind=K.RUN_START, run_id="r",
                  data={"game_mode": mode, "modifiers": list(mods)}))
    return t


t = _fresh()
t.apply(Event(kind=K.RUN_START, run_id="r", source="damage_meter", data={}))
check(t.run.game_mode == "custom" and t.run.modifiers == ["DRAFT"],
      "DamageMeter 发空 RUN_START 不会清掉模式", f"{t.run.game_mode} {t.run.modifiers}")
t.apply(Event(kind=K.RUN_START, run_id="r", data={"game_mode": "standard"}))
check(t.run.game_mode == "custom",
      "别处发 standard 不会覆盖已确认的 custom", t.run.game_mode)

t2 = _fresh("standard", ())
check(t2.run.game_mode == "standard", "标准局正常识别为 standard")
t2.apply(Event(kind=K.RUN_START, run_id="r",
               data={"game_mode": "custom", "modifiers": ["DRAFT"]}))
check(t2.run.game_mode == "custom" and t2.run.modifiers == ["DRAFT"],
      "之后再收到 custom 能正确升级（方向不能反）")

# ---------- ⑤ 历史存档 ----------
print()
print("⑤ 历史存档：game_mode / modifiers 要落进 RunState")
customs = []
for f in __import__("sts2mon.adapters.history", fromlist=["find_run_files"]).find_run_files():
    try:
        run = HistoryAdapter(G).parse_file(f)
    except Exception:
        continue
    if run.game_mode == "custom":
        customs.append(run)
check(True, "扫描历史存档完成", f"找到 {len(customs)} 场自定义对局")
for run in customs[:3]:
    check(bool(run.modifiers), f"{run.run_id} 带修正项", str(run.modifiers))
    check(all(m.isupper() for m in run.modifiers), "修正项已剥前缀",
          str(run.modifiers))
if not customs:
    print("      （当前没有自定义历史存档，跳过细节断言）")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

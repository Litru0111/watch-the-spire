"""验证多人局的实时监控：认对玩家、数据各归各。

## 用户报的现象

多人局里实时监控看不到本机数据：角色显示 `?`、HP/金币为空。

## 实际挖出的 **6 个 bug**（都是多人局才暴露）

1. `_track_player` 用「第一个被登记的玩家就是本机」推断 `is_local`。
   多人局里 DamageMeter 先报一个合成键 `"1"`，它被标成本机并锁定
   `run.local_player`，真正的本机（Steam ID）永远标不上。

2. `_pkey` 不读 `data.player_id`。`current_run` 把权威本机 ID 放在那里
   （没设 `ev.key`），不读它 → 存档的 HP 落到「第一位玩家」身上。

3. 适配器每次只发**一个**玩家的数据（`self.player_key` 默认 `"1"`，
   多人局里该键不存在 → 退回第一个玩家 = 队友）。于是事件流里只有队友，
   本机的伤害/出牌全丢。必须**逐玩家**发。

4. `Tracker.__init__` 设了 `local_player_id` 却**没设** `run.local_player`，
   导致 `run_start` / `combat_start`（不带玩家键）落到合成键 `"1"` 上，
   界面多出一列不存在的玩家。

5. 换局与 `reset()` 新建 `Tracker` 时**没带上** `local_player_id` ——
   启动时设好了，一换局又变回「不知道谁在本机」。

6. `_on_combat_start` 无脑 `pop + 重建`：多人局里每个玩家各发一次
   `COMBAT_START`，于是反复重建、留下幻影战斗（3 人局显示 4 列）。

7. `_track_player` 只在「战斗尚不存在」时被调，战斗中的 PLAYER_STAT
   不再补 `char_id`/名字 → 本机一直显示占位符名字与空角色。

## 本测试覆盖的点

* 逐玩家发事件（每人一条，key 各自正确）
* 本机身份：权威 Steam ID > 「确定单人」；不靠出现顺序
* 后到的权威 ID 能纠正早先的猜测
* 无权威 ID 时不乱认
* 伤害/角色/名字各归各，不串人
* 不产生幻影玩家
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import Event, EventBus, K  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402
from sts2mon.tracker import Tracker  # noqa: E402

G = default_glossary()
ME = "76561198412198880"
OTHER = "76561199029071077"
SYNTH = "1"

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


def state(**players) -> dict:
    return {
        "tracking": True, "encounter": "SPINY_TOAD_NORMAL", "turn": 2,
        "run_seed": "12345", "events": [],
        "segments": [{"floor": 3, "point_type": "monster"}],
        "players": players,
    }


P_ME = {"key": ME, "char_id": "SILENT", "name": "本机乙",
        "dmg_dealt": 50, "dmg_taken": 5, "cards_played": 6,
        "cpc": {"STRIKE_SILENT": 4}, "dpt": {"1": 20, "2": 30}}
P_OTHER = {"key": OTHER, "char_id": "IRONCLAD", "name": "队友甲",
           "dmg_dealt": 30, "dmg_taken": 10, "cards_played": 4,
           "cpc": {"STRIKE_IRONCLAD": 3}, "dpt": {"1": 10, "2": 20}}


print("① 适配器逐玩家发事件（每人一条，key 各自正确）")
ad = CombatStateAdapter(glossary=G)
evs = ad.initial_events(state(**{ME: P_ME, OTHER: P_OTHER}))
stats = [e for e in evs if e.kind == K.PLAYER_STAT]
check(len(stats) == 2, "PLAYER_STAT 每人一条", f"{len(stats)} 条")
by_key = {str(e.key): e for e in stats}
check(ME in by_key and OTHER in by_key, "两条分别属于两个玩家",
      f"{[str(k)[-6:] for k in by_key]}")
check(by_key.get(ME, Event(K.RUN_START)).data.get("char_id") == "SILENT",
      "本机的 char_id 正确", by_key.get(ME, Event(K.RUN_START)).data.get("char_id"))
check(by_key.get(OTHER, Event(K.RUN_START)).data.get("char_id") == "IRONCLAD",
      "队友的 char_id 正确")

print()
print("② 端到端：本机被正确识别且数据各归各")
live = LiveState(G, local_player_id=ME)
bus = EventBus()
live.attach(bus)
for e in evs:
    bus.emit(e)
run = live.tracker.run
check(run.local_player_id == ME, "权威本机 ID 已确立", run.local_player_id)
check(run.local_player == ME, "run.local_player 指向本机", run.local_player)
mine = next((p for p in run.players if p.is_local), None)
check(mine is not None and mine.player_id == ME, "本机被标 is_local")
check(mine is not None and mine.character == "SILENT", "本机角色",
      mine.character if mine else None)
check(mine is not None and mine.name == "本机乙", "本机名字",
      mine.name if mine else None)
check(not any(p.player_id == SYNTH for p in run.players),
      "没有幻影玩家（合成键）", f"{[str(p.player_id)[-6:] for p in run.players]}")
other = next((p for p in run.players if p.player_id == OTHER), None)
check(other is not None and other.is_local is False, "队友未被误标为本机")
check(other is not None and other.character == "IRONCLAD", "队友角色独立")

node = run.floors[0] if run.floors else None
combats = {c.player_id: c for c in (node.combats if node else [])}
check(len(combats) == 2, "两人各有独立战斗记录，无多余",
      f"{[str(k)[-6:] for k in combats]}")
check((combats.get(ME) or Event(K.RUN_START)).player_damage_dealt == 50
      if combats.get(ME) else False, "本机伤害归本机",
      combats[ME].player_damage_dealt if ME in combats else None)
check(combats.get(OTHER) and combats[OTHER].player_damage_dealt == 30,
      "队友伤害归队友", combats[OTHER].player_damage_dealt if OTHER in combats else None)

print()
print("③ 本机身份不能靠出现顺序猜")
t = Tracker("r", G)
t.apply(Event(kind=K.PLAYER_STAT, run_id="r", floor=1, node_id="f001", key=SYNTH,
              data={"player_key": SYNTH, "player_damage_dealt": 10}))
check(t.run.local_player == "", "无权威 ID 时不乱认本机", repr(t.run.local_player))
t.apply(Event(kind=K.RUN_START, run_id="r",
              data={"local_player_id": ME, "player_count": 3}))
t.apply(Event(kind=K.PLAYER_STAT, run_id="r", floor=1, node_id="f001", key=OTHER,
              data={"player_key": OTHER, "char_id": "IRONCLAD", "player_name": "队友甲"}))
t.apply(Event(kind=K.PLAYER_STAT, run_id="r", floor=1, node_id="f001", key=ME,
              data={"player_key": ME, "char_id": "SILENT", "player_name": "本机乙"}))
flags = {p.player_id: p.is_local for p in t.run.players}
check(flags.get(SYNTH) is False, "合成键被纠正为非本机", str(flags))
check(flags.get(ME) is True, "本机被标上", str(flags))
check(t.run.local_player == ME, "run.local_player 正确")

print()
print("④ 换局后本机身份不丢（LiveState 会新建 Tracker）")
live2 = LiveState(G, local_player_id=ME)
bus2 = EventBus()
live2.attach(bus2)
bus2.emit(Event(kind=K.RUN_START, run_id="run-A", data={"player_count": 2}))
bus2.emit(Event(kind=K.PLAYER_STAT, run_id="run-A", floor=1, node_id="f001",
                key=ME, data={"player_key": ME, "char_id": "SILENT",
                              "player_name": "本机乙"}))
# 换局
bus2.emit(Event(kind=K.RUN_START, run_id="run-B", data={"player_count": 2}))
bus2.emit(Event(kind=K.PLAYER_STAT, run_id="run-B", floor=1, node_id="f001",
                key=ME, data={"player_key": ME, "char_id": "SILENT",
                              "player_name": "本机乙"}))
check(live2.tracker.run.local_player_id == ME, "换局后权威 ID 仍在",
      live2.tracker.run.local_player_id)
check(live2.tracker.run.local_player == ME, "换局后本机标记仍对",
      live2.tracker.run.local_player)

print()
print("⑤ 单人局仍正常")
live3 = LiveState(G, local_player_id="")
bus3 = EventBus()
live3.attach(bus3)
bus3.emit(Event(kind=K.RUN_START, run_id="solo", data={"player_count": 1}))
bus3.emit(Event(kind=K.PLAYER_STAT, run_id="solo", floor=1, node_id="f001", key="1",
                data={"player_key": "1", "hp": 60, "max_hp": 80}))
solo = live3.tracker.run
lp = [p for p in solo.players if p.is_local]
check(len(lp) == 1 and lp[0].player_id == "1", "单人局唯一玩家即本机",
      f"{[p.player_id for p in lp]}")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

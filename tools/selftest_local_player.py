"""验证「多人局实时监控认对当前视角玩家」。

## 用户报的现象

多人局里实时页看不到本机数据：角色显示 `?`、HP/金币全是空。

## 根因（两个 bug 叠加）

1. `_track_player` 用「第一个被登记的玩家就是本机」推断 `is_local`。
   多人局里 DamageMeter 先报一个**合成的 `"1"` 键**，它就被标成本机并
   锁定 `run.local_player`；而真正的本机（存档路径里的 Steam ID）
   永远不会被标上。实测那一局的 players 表：

       player_id='1'       is_local=1     ← 错
       player_id='071077'  is_local=0
       player_id='198880'  is_local=0     ← 这才是本机（存档路径里的 ID）

   而合成的 `"1"` 没有任何权威数据（存档的 HP 挂在真本机 ID 上），
   所以实时快照取到 hp/max_hp/gold = None。

2. `_pkey` 只认 `data.player_key` 与 `ev.key`，**不读 `data.player_id`**。
   而 `current_run` 适配器把权威本机 ID 放在 `data.player_id` 里
   （没设 `ev.key`），于是存档发的 HP/金币会被归到「第一位玩家」身上。

## 修复后的行为

* 本机身份只认两种可靠信号：权威 Steam ID，或「确定单人」
* 后到的权威 ID 能**纠正**早先的猜测（`_reconcile_local`）
* `_pkey` 会读 `data.player_id`
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.events import Event, K  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.tracker import Tracker  # noqa: E402

G = default_glossary()
ME = "76561198412198880"          # 权威本机（存档路径里的那个）
OTHER = "76561199029071077"
SYNTH = "1"                       # DamageMeter 在某些情形下报的合成键

results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


def stat(key: str = "", **data) -> Event:
    return Event(kind=K.PLAYER_STAT, run_id="r", floor=1, node_id="f001",
                 key=key, data=data)


print("① 多人局：合成键先出现，不能抢到本机身份")
t = Tracker("r", G)
# DamageMeter 先报合成键 "1"（此时还不知道人数、也不知道本机是谁）
t.apply(stat(SYNTH, player_damage_dealt=10))
check(t.run.local_player == "", "尚无权威 ID 时不乱认本机",
      f"local_player={t.run.local_player!r}")

# 存档到来：给出权威本机 ID 与人数
t.apply(Event(kind=K.RUN_START, run_id="r",
              data={"local_player_id": ME, "player_count": 3}))
# 另外两名玩家（含本机）随后被登记
t.apply(stat(OTHER, player_damage_dealt=20))
t.apply(stat(ME, player_damage_dealt=30))

ids = {p.player_id: p.is_local for p in t.run.players}
check(ids.get(ME) is True, "本机（权威 ID）被标为 is_local", str(ids))
check(ids.get(SYNTH) is False, "合成键被纠正为非本机", f"合成键 is_local={ids.get(SYNTH)}")
check(t.run.local_player == ME, "run.local_player 指向权威本机",
      t.run.local_player)

print()
print("② 权威 ID 后到也能纠正（顺序反过来）")
t2 = Tracker("r", G)
t2.apply(stat(SYNTH, player_damage_dealt=10))       # 先猜
t2.apply(stat(ME, player_damage_dealt=30))
before = next(p.is_local for p in t2.run.players if p.player_id == SYNTH)
t2.apply(Event(kind=K.RUN_START, run_id="r",
               data={"local_player_id": ME, "player_count": 3}))
after = {p.player_id: p.is_local for p in t2.run.players}
check(before is False, "拿到权威 ID 前不会误认（旧实现在这里就错了）",
      f"合成键 is_local={before}")
check(after.get(ME) is True and after.get(SYNTH) is False,
      "拿到权威 ID 后标记迁移到本机", str(after))

print()
print("③ 存档发的事件带 data.player_id → 必须归到那个玩家")
t3 = Tracker("r", G)
t3.apply(Event(kind=K.RUN_START, run_id="r",
               data={"local_player_id": ME, "player_count": 1}))
# current_run 的 HP 事件：key 为空，本机 ID 在 data.player_id 里
t3.apply(stat(**{"player_id": ME, "hp": 55, "max_hp": 70, "gold": 123,
                 "hp_authoritative": True}))
node = t3.run.floors[0] if t3.run.floors else None
ps = node.stat(ME) if node else None
check(ps is not None, "HP 事件落到了带 player_id 的那个玩家")
check(ps is not None and ps.hp == 55, "本机 HP 正确写入",
      f"hp={ps.hp if ps else None}")
check(ps is not None and ps.gold == 123, "本机金币正确写入",
      f"gold={ps.gold if ps else None}")

print()
print("④ 单人局仍要正常认本机")
t4 = Tracker("r", G)
t4.apply(Event(kind=K.RUN_START, run_id="r",
               data={"player_count": 1, "local_player_id": ""}))
t4.apply(stat("1", hp=60, max_hp=80))
lp = [p for p in t4.run.players if p.is_local]
check(len(lp) == 1 and lp[0].player_id == "1",
      "单人局唯一玩家即本机", f"local={[p.player_id for p in lp]}")

print()
print("⑤ 本机不在已知玩家列表里时不能瞎标")
t5 = Tracker("r", G)
t5.apply(Event(kind=K.RUN_START, run_id="r",
               data={"local_player_id": ME, "player_count": 3}))
t5.apply(stat(OTHER, player_damage_dealt=5))
check(all(not p.is_local for p in t5.run.players),
      "只有别人时没人被误标为本机",
      str({p.player_id[-6:]: p.is_local for p in t5.run.players}))

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

"""端到端：把「进行中存档」与「战斗状态」两个数据源合起来喂给 LiveState。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.adapters.damage_meter import CombatStateAdapter  # noqa: E402
from sts2mon.events import EventBus  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon.live import LiveState  # noqa: E402

g = default_glossary()
live = LiveState(g)
bus = EventBus()
live.attach(bus)

cr = CurrentRunAdapter()
st_save = cr.read()
snap_save = cr.snapshot(st_save or {})
print(f"存档: HP {snap_save.get('hp')}/{snap_save.get('max_hp')} "
      f"金币 {snap_save.get('gold')} 第 {snap_save.get('floor')} 层")

dm = CombatStateAdapter(glossary=g)
dm.set_floor_hint(snap_save.get("floor"))
dm.set_act_hint(snap_save.get("act"))
dm.set_run_id_hint(cr.run_id(st_save))
st_dm = dm.read()

print("\n喂事件（按监控循环的真实顺序）：")
for e in cr.initial_events(st_save):
    bus.emit(e)
    print(f"   [存档] {e.kind:<12} node={e.node_id!r}")
if st_dm:
    for e in dm.initial_events(st_dm):
        bus.emit(e)
        if e.kind in ("map_node", "combat_start", "player_stat"):
            print(f"   [战斗] {e.kind:<12} node={e.node_id!r}")

t = live.tracker
print("\n节点:")
for n in t.run.floors:
    stats = [(s.player_id, s.hp, s.max_hp, s.hp_source) for s in n.player_stats]
    print(f"   {n.node_id} ord={n.order} 类型={n.point_type} 玩家数值={stats}")

snap = live.snapshot()
print(f"\n实时快照: HP {snap['hp']}/{snap['max_hp']} 实测={snap.get('hp_authoritative')} "
      f"来源={snap.get('hp_source')!r}")
print(f"          金币={snap['gold']} 层={snap['floor']} 遭遇={snap['encounter_name']} "
      f"turn={snap['turn']}")
p = snap["prediction"]
print("\n预测:")
if p.get("available"):
    print(f"   est_hp={p['est_hp']}/{p['max_hp']} 实测={p.get('hp_authoritative')} "
          f"依据={p['hp_basis']}")
    print(f"   每层承伤={p['taken_per_floor']} 净损={p['net_loss_per_floor']} "
          f"还能撑={p['floors_left']}层 会死={p['will_die']} 置信={p['confidence']}")
else:
    print("   不可用:", p.get("reason"))

# 断言
ok = snap["hp"] == snap_save.get("hp")
print(f"\n{'✔' if ok else '✘'} 实时 HP 与存档一致（{snap['hp']} vs {snap_save.get('hp')}）")
sys.exit(0 if ok else 1)

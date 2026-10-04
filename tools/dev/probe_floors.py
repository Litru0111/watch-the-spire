"""看第 7 层之后各层的原始数据：房间类型、选择、以及升级相关字段。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402

ad = CurrentRunAdapter()
st = ad.read()
pts = [pt for a in (st.get("map_point_history") or []) for pt in (a or [])]
print(f"存档楼层数 = {len(pts)} · visited = {len(st.get('visited_map_coords') or [])}")
print()
for i, pt in enumerate(pts, 1):
    ps = (pt.get("player_stats") or [{}])[0]
    rooms = (pt.get("rooms") or [{}])[0]
    print(f"第 {i} 层  type={pt.get('map_point_type')!r}  "
          f"room_type={rooms.get('room_type')!r}  model_id={rooms.get('model_id')!r}")
    interesting = {k: v for k, v in ps.items()
                   if v not in (None, [], {}, 0, "", False)
                   and k not in ("player_id", "current_hp", "max_hp", "current_gold",
                                 "damage_taken", "hp_healed", "gold_gained",
                                 "gold_spent", "gold_lost", "gold_stolen",
                                 "max_hp_gained", "max_hp_lost", "stolen_loot")}
    for k, v in interesting.items():
        print(f"      {k}: {json.dumps(v, ensure_ascii=False)[:200]}")
    print()

# 牌组里的升级标记
me = (st.get("players") or [{}])[0]
deck = me.get("deck") or []
print("=== 牌组里的升级信息（前 10 张） ===")
for c in deck[:10]:
    if isinstance(c, dict):
        keys = {k: v for k, v in c.items() if k != "save_dict_List[BaseLib.Abstracts.CardModifier+ModifierSave]"}
        print("  ", json.dumps(keys, ensure_ascii=False)[:180])

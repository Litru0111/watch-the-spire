"""列出存档里每一层的所有「选择类」字段，确定要解析哪些。"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import newest_current_run  # noqa: E402

st = json.loads(newest_current_run().read_text(encoding="utf-8-sig"))
keys = Counter()
with_value = defaultdict(int)
samples: dict[str, object] = {}

for act in st.get("map_point_history") or []:
    for pt in act or []:
        for ps in pt.get("player_stats") or []:
            for k, v in ps.items():
                keys[k] += 1
                if v not in (None, [], {}, 0, "", False):
                    with_value[k] += 1
                    samples.setdefault(k, v)

print(f"player_stats 里出现过的字段（共 {len(keys)} 个）:")
print(f"  {'字段':<28} {'出现次数':>8} {'非空次数':>8}  样例")
for k, n in sorted(keys.items()):
    nv = with_value.get(k, 0)
    s = samples.get(k)
    text = json.dumps(s, ensure_ascii=False)[:60] if s is not None else "—"
    print(f"  {k:<28} {n:>8} {nv:>8}  {text}")

print()
print("=== 选择类字段的详细结构（取第一个有值的层）===")
for act in st.get("map_point_history") or []:
    for pt in act or []:
        for ps in pt.get("player_stats") or []:
            for k in ("ancient_choice", "event_choices", "card_choices",
                      "relic_choices", "potion_choices", "cards_transformed",
                      "cards_gained", "cards_removed"):
                if ps.get(k):
                    print(f"\n--- {k}（层类型 {pt.get('map_point_type')}）---")
                    print(json.dumps(ps[k], ensure_ascii=False, indent=2)[:700])
            break
        break
    break

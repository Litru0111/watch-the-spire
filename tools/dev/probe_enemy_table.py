"""实测：能否用「rdps 的每场总输出」+「.run 的 monster_ids」建出怪物 HP 表。

原理：rdps 里每场战斗记录了每个玩家的每卡伤害，求和即「这一场被打掉的总血量」。
      单体战斗打赢 → 这个值就是该怪物的最大 HP。
      多体战斗 → 是所有怪物 HP 之和（需按怪物数摊分或单独统计）。
      .run 的 rooms[].monster_ids 给出每场的怪物构成，用来对齐。
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.history import find_run_files  # noqa: E402
from sts2mon import paths

APPDATA = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
RDPS = APPDATA / "rdps_meter"

# ---- 1. 收集 .run 里「每场遭遇 → 怪物构成」 ----
enc_monsters: dict[str, Counter] = defaultdict(Counter)
run_files = {p.stem: p for p in find_run_files()}
for stem, p in run_files.items():
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    for act in d.get("map_point_history") or []:
        for pt in act or []:
            for r in (pt.get("rooms") or []):
                enc = str(r.get("model_id") or "").split(".")[-1]
                mons = r.get("monster_ids") or []
                if enc and mons:
                    key = tuple(str(m).split(".")[-1] for m in mons)
                    enc_monsters[enc][key] += 1
print(f"① 从 {len(run_files)} 个存档里收集到 {len(enc_monsters)} 个遭遇的怪物构成")
for enc, cnt in list(sorted(enc_monsters.items()))[:6]:
    print(f"   {enc[:32]:<34} {cnt.most_common(2)}")

# ---- 2. 收集 rdps 每场总输出，按 Label 对齐 ----
by_label: dict[str, Counter] = defaultdict(Counter)
n_combats = 0
for f in sorted(RDPS.glob("*.json")):
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    for c in d.get("Combats") or []:
        total = 0
        for pl in c.get("Players") or []:
            for x in pl.get("Dealt") or []:
                total += int(x.get("Amount") or 0)
        if total <= 0:
            continue
        n_combats += 1
        by_label[str(c.get("Label") or "?")][total] += 1
print(f"\n② rdps 里 {n_combats} 场战斗有输出数据，覆盖 {len(by_label)} 种遭遇名")

# ---- 3. 看同一个遭遇名的总输出是否稳定 ----
print("\n③ 同一遭遇的总输出分布（稳定 → 可当 HP 用）：")
stable = 0
for label, cnt in sorted(by_label.items(), key=lambda x: -sum(x[1].values()))[:20]:
    total = sum(cnt.values())
    top = cnt.most_common(3)
    agree = top[0][1] / total
    flag = "稳定" if agree >= 0.6 else ("较散" if agree >= 0.3 else "很散")
    if agree >= 0.6:
        stable += 1
    print(f"   {label[:26]:<28} n={total:<3} {flag:<4} {top}")
print(f"\n   其中『稳定』的遭遇 {stable} 个")

# ---- 4. 能不能把遭遇名映射到怪物 ID ----
print("\n④ 遭遇名 → 怪物（用 .run 的 Label 对齐测试）")
from sts2mon.glossary import default_glossary  # noqa: E402
G = default_glossary()
matched = 0
for enc, cnt in list(enc_monsters.items())[:40]:
    name = G.encounter(enc)
    mons = cnt.most_common(1)[0][0]
    hit = name in by_label
    if hit:
        matched += 1
    print(f"   {enc[:30]:<32} → {name[:16]:<18} 怪={mons} rdps有={'✔' if hit else '✘'}")
print(f"\n   可对齐 {matched} / 40")

out = paths.data_dir() / "enemy_hp_probe.json"
out.write_text(json.dumps({
    "enc_monsters": {k: {",".join(kk): vv for kk, vv in v.most_common(5)}
                     for k, v in enc_monsters.items()},
    "label_totals": {k: v.most_common(8) for k, v in by_label.items()},
}, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n明细写到 {out}")

"""把 t=0 / t=1 的来源名全部列出来，看哪些是卡牌、哪些是敌方/状态。"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.glossary import default_glossary  # noqa: E402

REAL = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2" / "DamageMeter_combat_state.json"
g = default_glossary()
if not REAL.is_file():
    print("真机文件不存在")
    sys.exit(0)

import json  # noqa: E402
st = json.loads(REAL.read_text(encoding="utf-8-sig"))
evs = st.get("events") or []
p1 = (st.get("players") or {}).get("1") or {}
print(f"战斗 {st.get('encounter')} · 第 {st.get('turn')} 回合 · "
      f"dmg_dealt={p1.get('dmg_dealt')} dmg_taken={p1.get('dmg_taken')}")
print(f"events 共 {len(evs)} 条")

for t, name in ((0, "t=0"), (1, "t=1")):
    sub = [e for e in evs if e.get("t") == t]
    total = sum(int(e.get("v") or 0) for e in sub)
    srcs = Counter()
    for e in sub:
        label = str(e.get("l") or "")
        src = label.split("→")[0].strip() if "→" in label else label
        srcs[src] += int(e.get("v") or 0)
    print(f"\n{name}：{len(sub)} 条，数值合计 {total}")
    for src, amt in srcs.most_common():
        canon = g.canonical(src)
        known = g.is_known(canon)
        tag = "卡牌" if known else "非卡牌"
        print(f"   {src[:24]:<26} {amt:>5}  {tag}")

# 卡牌名在 t=1 里出现过吗？
t1_cards = [s for s in (str(e.get("l") or "").split("→")[0].strip()
                        for e in evs if e.get("t") == 1)
            if g.is_known(g.canonical(s))]
print(f"\nt=1 里出现的卡牌名: {t1_cards or '无'}")
t0_notcard = [s for s in (str(e.get("l") or "").split("→")[0].strip()
                          for e in evs if e.get("t") == 0)
              if s and not g.is_known(g.canonical(s))]
print(f"t=0 里的非卡牌来源: {sorted(set(t0_notcard)) or '无'}")

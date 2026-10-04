"""查单人存档里 me() 为什么选不中本机。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.adapters.history import find_run_files, local_steam_id  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402

G = default_glossary()
for f in find_run_files():
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    ps = d.get("players") or []
    if len(ps) != 1:
        continue
    print(f"样本 {f.name}")
    print(f"  路径 Steam ID  = {local_steam_id(f)!r}")
    print(f"  players[0] 字段 = {sorted(ps[0].keys())}")
    print(f"  players[0].id  = {ps[0].get('id')!r}  (类型 {type(ps[0].get('id')).__name__})")
    print(f"  players[0].net_id = {ps[0].get('net_id')!r}")
    ad = CurrentRunAdapter(glossary=G, path=f)
    me = ad.me(d)
    print(f"  me() 选中 id   = {me.get('id')!r}")
    print(f"  me() is players[0]: {me is ps[0]}")
    print(f"  相等判定: {str(ps[0].get('id')) == local_steam_id(f)}")
    break

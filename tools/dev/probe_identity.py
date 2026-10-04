"""查「本机玩家」身份：数据里有哪些标识，监控程序目前认的是哪个。

涉及：
  * 多人对局的 players[].net_id / name
  * DamageMeter 状态文件里的 players 键与 NetId
  * rdps_meter 的 Roster（含 NetId）
  * godot.log 里出现的 Steam ID 与玩家名

**本机身份是运行时推导的**（存档目录名 / 日志），源码里不含任何真实账号 ID。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sts2mon import paths  # noqa: E402
from sts2mon.adapters.history import find_run_files  # noqa: E402

APPDATA = paths.sts2_appdata()
MY_STEAM = paths.local_steam_id_hint()

print("=" * 74)
print("① 日志里出现的所有玩家身份（谁在玩）")
LOG = APPDATA / "logs" / "godot.log"
name_of: dict[str, Counter] = {}
steam_seen: Counter = Counter()
my_name = ""
if LOG.is_file():
    with LOG.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            for sid in re.findall(r"\b(7656119\d{10})\b", line):
                steam_seen[sid] += 1
            # 「名字 (steamid)」形态
            for nm, sid in re.findall(
                    r"([A-Za-z0-9_\u4e00-\u9fff\.]{2,20})\s*[\(（](7656119\d{10})[\)）]", line):
                name_of.setdefault(sid, Counter())[nm] += 1
print("  出现过的 Steam ID:")
for sid, n in steam_seen.most_common(8):
    names = name_of.get(sid)
    tag = "  ← 本机（存档目录名）" if sid == MY_STEAM else ""
    print(f"    {sid}  {n:>6} 次  "
          f"名字: {[k for k, _ in names.most_common(3)] if names else '—'}{tag}")
if MY_STEAM and name_of.get(MY_STEAM):
    my_name = name_of[MY_STEAM].most_common(1)[0][0]

print()
print(f"② 日志里与本机名字（{my_name or '未识别'}）关联的 Steam ID")
if not my_name:
    print("   （日志里没找到「名字 (steamid)」形态，跳过）")
else:
    with LOG.open("r", encoding="utf-8", errors="ignore") as f:
        hits = Counter()
        sample = []
        for line in f:
            if my_name in line:
                for sid in re.findall(r"\b(7656119\d{10})\b", line):
                    hits[sid] += 1
                if len(sample) < 4:
                    sample.append(line.strip()[:150])
    print(f"  含「{my_name}」的行里出现的 Steam ID: {dict(hits.most_common(5))}")
    for s in sample:
        print(f"    {s}")

print()
print("③ 存档里多人对局的玩家标识")
for f in find_run_files():
    try:
        d = json.loads(f.read_text(encoding="utf-8-sig"))
    except Exception:
        continue
    ps = d.get("players") or []
    if len(ps) <= 1:
        continue
    ids = [str(p.get("id")) for p in ps]
    names = [p.get("name") for p in ps]
    mine = str(MY_STEAM) if MY_STEAM in ids else "（本机不在其中）"
    print(f"  {f.stem}")
    print(f"     ids={ids}")
    print(f"     names={names}")
    print(f"     本机={mine}")
    break

"""把模拟器「序列测试」中途写的存档原样打印出来，对照 floor() 的两种算法。

目的：确认 `map_point_history` 与 `visited_map_coords` 在序列中途
到底各是什么语义（真机 vs 桩），从而定出唯一正确的 floor()。
"""
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import simulate_runs as S  # noqa: E402
from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402

G = default_glossary()
ad = CurrentRunAdapter(glossary=G)

# 直接构造一局的真值，然后逐层看桩怎么写存档
import random
rng = random.Random(777)
truth = S.make_truth(rng, 3885000, acts=1)
print(f"真值：{len(truth.rooms)} 个房间")
print("前 8 个房间（floor / act）:")
for r in truth.rooms[:8]:
    print(f"   floor={r.floor} act={r.act} kind={r.kind}")

# 模拟器构造 current_run.save 的方法在哪个类里
gen = None
for name in dir(S):
    obj = getattr(S, name)
    if isinstance(obj, type) and hasattr(obj, "current_run_save"):
        gen = obj
        break
print()
print("生成器类:", gen)

# 用真值的第 N 个房间探测
probe = truth.rooms[3]
print(f"探测第 {probe.floor} 层（act={probe.act}）的存档:")
try:
    src = Path(S.__file__).read_text(encoding="utf-8")
    i = src.find("def current_run_save")
    print("\n".join("  " + x for x in src[i:i + 500].splitlines()[:14]))
except Exception as e:
    print("  ", e)

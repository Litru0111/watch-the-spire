"""验证模拟器的「杀伤力」：把已修复的 bug 临时放回去，看它是否报错。

做法：对源文件做一次临时替换（注入 bug）→ 跑模拟器 → 还原。
每个 bug 都必须被模拟器**发现**，否则说明断言不够强。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# (名称, 文件, 原始片段, 注入 bug 后的片段)
INJECTIONS = [
    (
        "楼层不建节点（非战斗层会丢）",
        "sts2mon/tracker.py",
        "        floor = ev.floor or (ev.data.get(\"floor\") if ev.data else 0)\n"
        "        if ev.node_id or floor:\n",
        "        floor = ev.floor or (ev.data.get(\"floor\") if ev.data else 0)\n"
        "        if False:\n",
    ),
    (
        "PLAYER_STAT 无条件建战斗（假战斗）",
        "sts2mon/tracker.py",
        "        if c is None and (has_combat_data or ev.encounter_id):\n"
        "            c = self._ensure_combat(ev)\n",
        "        if c is None:\n"
        "            c = self._ensure_combat(ev)\n",
    ),
    (
        "楼层提示失效（节点错位，HP 与战斗分家）",
        "sts2mon/adapters/damage_meter.py",
        "        except (TypeError, ValueError):\n"
        "            f = 0\n"
        "        return f or self.floor_hint\n",
        "        except (TypeError, ValueError):\n"
        "            f = 0\n"
        "        return f\n",
    ),
    (
        "首读历史事件不登记（同一份状态重发＝重复）",
        "sts2mon/adapters/damage_meter.py",
        "        if prev is None:\n"
        "            return self.initial_events(cur)\n\n"
        "        events: list[Event] = []\n",
        "        if prev is None:\n"
        "            return self.initial_events(cur)\n\n"
        "        cur = dict(cur)\n"
        "        cur[\"events\"] = (prev.get(\"events\") or []) + (cur.get(\"events\") or [])\n"
        "        events: list[Event] = []\n",
    ),
    (
        "落库不去重（flush 重复写入）",
        "sts2mon/live.py",
        "            total = len(c.events)\n"
        "            if total <= c.persisted_events:\n"
        "                continue\n",
        "            total = len(c.events)\n"
        "            if False:\n"
        "                continue\n",
    ),
    (
        "HP 不用实测值（药水/休息处会算错）",
        "sts2mon/live.py",
        "        if real_hp is not None:\n"
        "            est_hp = int(real_hp)\n"
        "        else:\n",
        "        if False:\n"
        "            est_hp = int(real_hp)\n"
        "        else:\n",
    ),
]


def run_sim(limit: int = 12) -> tuple[int, str]:
    r = subprocess.run(
        [PY, "-X", "utf8", "-W", "ignore", str(ROOT / "tools" / "simulate_runs.py"),
         "--runs", str(limit), "--seed", "3", "--sequences", "1", "--seq-runs", "2"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    print("=" * 78)
    print("基线：未注入 bug 时应全部通过")
    code, out = run_sim()
    tail = [line for line in out.splitlines() if "模拟完成" in line or "没有发现" in line]
    print("  " + ("\n  ".join(tail) if tail else out.strip()[-300:]))
    if code != 0:
        print("  ✘ 基线就是失败的，先修好再谈杀伤力")
        return 1

    results: list[tuple[str, bool, str]] = []
    for name, rel, orig, broken in INJECTIONS:
        path = ROOT / rel
        backup = path.read_text(encoding="utf-8")
        if orig not in backup:
            results.append((name, False, "注入点没找到（代码可能已改动）"))
            print(f"\n[{name}] ✘ 注入点没找到")
            continue
        path.write_text(backup.replace(orig, broken, 1), encoding="utf-8")
        try:
            code, out = run_sim()
        finally:
            path.write_text(backup, encoding="utf-8")
        caught = code != 0
        first = next((line.strip() for line in out.splitlines()
                      if "[BUG]" in line or "模拟过程抛异常" in line), "")
        detail = first or out.strip().splitlines()[-1][:120] if out.strip() else ""
        results.append((name, caught, detail))
        print(f"\n[{name}]")
        print(f"   {'✔ 被抓到' if caught else '✘ 没抓到（断言不够强）'}")
        if detail:
            print(f"   {detail[:140]}")

    print("\n" + "=" * 78)
    ok = sum(1 for _, c, _ in results if c)
    print(f"杀伤力验证: {ok}/{len(results)} 个历史 bug 能被模拟器抓到")
    for name, c, _ in results:
        print(f"  {'✔' if c else '✘'} {name}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

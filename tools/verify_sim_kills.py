"""验证模拟器的「杀伤力」：把已修复的 bug 临时放回去，看它是否报错。

做法：对源文件做一次临时替换（注入 bug）→ 跑模拟器 → 按字节原样还原。
每个 bug 都必须被模拟器**发现**，否则说明断言不够强。

## 两个必须遵守的约定（都是踩过的坑）

1. **注入与还原必须走字节，不能走 `read_text`/`write_text`。**
   Windows 上 `read_text` 会把 CRLF 压成 LF、`write_text` 又按平台写回，
   于是每跑一次就**悄悄改掉被注入文件的行尾**（实测 `damage_meter.py`
   被从 LF 改成 CRLF，而 `.gitattributes` 要求 `*.py eol=lf`）。
   这里用 `read_bytes` + 显式行尾还原，保证跑完文件逐字节不变。

2. **匹配片段要行尾无关。**
   文件里可能是 CRLF，而源码字面量写的是 `\n`，直接 `in` 判断会
   「注入点没找到」。所以先把内容归一成 LF 再匹配，
   写回时再换回该文件原本的行尾。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

# (名称, 文件, 原始片段, 注入 bug 后的片段)
#
# 每一条都**实测**能被模拟器抓到（改了断言后请重跑本脚本确认）。
#
# 注入片段必须落在**真会被执行**的代码上，否则是空操作、等于没测：
#   * `if False: continue` 这种写法对 `if total <= c.persisted_events: continue`
#     是**无效注入** —— 条件不成立时本来就会往下走，行为完全不变。
#     有效的写法是让水位线不前进（见下面第 4 条）。
#   * 同理，把 `if c is None and (...):` 改成 `if c is None:` 也是无效注入，
#     因为 `has_combat_data` 在有战斗数据时恒为真。
#
# 有两条历史 bug 这个模拟器天然复现不了，所以不列进来（别硬凑成"没抓到"）：
#
# * 「首读历史事件不登记去重集合」——真实事故是「下一回合把历史事件当新增重发」。
#   模拟器在每个房间边界都会把 diff 基线重置为 None（见 run_one 里
#   `if room.floor != last_room_floor: prev_dm = None`），所以它始终走
#   initial_events 首读路径，那条重发路径在这里不可达。实测把 `_seen_sigs`
#   清空、把 `record_only` 改成 False、甚至关掉 `_event_delta` 的去重判断，
#   模拟器都全绿 —— 这不是"注入点找错了"，是流程覆盖不到。
# * 「多人局适配器只发一个玩家」「_on_combat_start 无脑 pop+重建」——
#   前者由 selftest_multiplayer.py 覆盖，后者在这个模拟器的遭遇切换方式下
#   不会留下幻影战斗。
INJECTIONS = [
    (
        "楼层不建节点（非战斗层会丢）",
        "sts2mon/tracker.py",
        "    def _on_map_node(self, ev: Event) -> None:\n"
        "        \"\"\"记录地图点（可来自历史存档，也可来自实时地图快照）。\"\"\"\n",
        "    def _on_map_node(self, ev: Event) -> None:\n"
        "        \"\"\"记录地图点（可来自历史存档，也可来自实时地图快照）。\"\"\"\n"
        "        return\n",
    ),
    (
        "PLAYER_STAT 无条件建战斗（假战斗）",
        "sts2mon/tracker.py",
        "        if self._cur is None:\n"
        "            # 自动补一个地图点。",
        "        if True:\n"
        "            # 自动补一个地图点。",
    ),
    (
        "事件流的伤害翻倍（重复计数）",
        "sts2mon/adapters/damage_meter.py",
        "            ev = self._one_event(e, run_id, enc, node_id, fresh_sources=fresh_sources)\n"
        "            if ev is not None:\n"
        "                out.append(ev)\n",
        "            ev = self._one_event(e, run_id, enc, node_id, fresh_sources=fresh_sources)\n"
        "            if ev is not None:\n"
        "                if ev.kind == K.DAMAGE and ev.side == \"player\":\n"
        "                    ev.value = int(ev.value or 0) * 2\n"
        "                out.append(ev)\n",
    ),
    (
        "落库水位线不前进（每场战斗重写全量）",
        "sts2mon/live.py",
        "            store.add_events(rid, new)\n"
        "            c.persisted_events = total\n",
        "            store.add_events(rid, new)\n"
        "            c.persisted_events = 0\n",
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


def _eol_of(raw: bytes) -> str:
    return "\r\n" if b"\r\n" in raw else "\n"


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

    # 三种结局要分开报，别把「注入点失效」混进「模拟器抓不到」——
    # 前者是脚本自己过期了，后者才是断言不够强。
    caught_n = 0
    missing: list[str] = []
    missed: list[str] = []

    for name, rel, orig, broken in INJECTIONS:
        path = ROOT / rel
        raw = path.read_bytes()
        eol = _eol_of(raw)
        text = raw.decode("utf-8").replace("\r\n", "\n")
        if orig not in text:
            missing.append(name)
            print(f"\n[{name}]")
            print("   ⚠ 注入点没找到（代码已改动，脚本过期了）")
            continue

        path.write_bytes(text.replace(orig, broken, 1).replace("\n", eol).encode("utf-8"))
        damaged = False
        try:
            code, out = run_sim()
        finally:
            path.write_bytes(raw)
            if path.read_bytes() != raw:
                print(f"   !! 还原失败，{rel} 已损坏 !!", file=sys.stderr)
                damaged = True
        if damaged:
            # 还原失败必须立刻停手：继续跑只会把坏文件当基线。
            # 注意 return 不能写在 finally 里（会吞掉异常，Python 会告警）。
            return 2

        ok = code != 0
        if ok:
            caught_n += 1
        else:
            missed.append(name)
        first = next((line.strip() for line in out.splitlines()
                      if "[BUG]" in line or "模拟过程抛异常" in line), "")
        detail = first or (out.strip().splitlines()[-1][:120] if out.strip() else "")
        print(f"\n[{name}]")
        print(f"   {'✔ 被抓到' if ok else '✘ 没抓到（断言不够强）'}")
        if detail:
            print(f"   {detail[:140]}")

    print("\n" + "=" * 78)
    print(f"杀伤力验证: {caught_n}/{len(INJECTIONS)} 个历史 bug 能被模拟器抓到")
    if missed:
        print("  没抓到（要补断言）：")
        for n in missed:
            print(f"    ✘ {n}")
    if missing:
        print("  注入点失效（要更新脚本里的片段）：")
        for n in missing:
            print(f"    ⚠ {n}")
    return 0 if caught_n == len(INJECTIONS) else 1


if __name__ == "__main__":
    sys.exit(main())

"""验证 current_run 适配器的 floor()（跨 Act 累计楼层）。

踩过的坑：早先用 `len(visited_map_coords)` 当总层数，而它只记当前 Act，
多 Act 时严重少算（真机实测：实际 39 层却报 7 层），
于是回填只覆盖前 7 层，**后面的升级/选择全部丢失**（用户报的「升级记录不见了」）。

语义（真机实测）：
  * `map_point_history` 是权威来源，**包含当前 Act 的已结算部分**
    （Act1 恒 17 点、Act2 满 16 点、Act3 进行中就有 6 点 → 总共 39 层）
  * 历史存档里可见 `[17,16,7]` / `[17,5]` 这类「当前 Act 进行中」的形态
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon.adapters.current_run import CurrentRunAdapter  # noqa: E402
from sts2mon.glossary import default_glossary  # noqa: E402
from sts2mon import paths

G = default_glossary()
ad = CurrentRunAdapter(glossary=G, path=paths.data_dir() / "nonexistent.save")
results: list[tuple[bool, str]] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    results.append((bool(cond), label))
    print(f"  [{'✔' if cond else '✘'}] {label}" + (f"  —— {detail}" if detail else ""))


def pt(hp: int = 50) -> dict:
    return {"rooms": [], "player_stats": [{"current_hp": hp, "max_hp": 70}]}


def st(acts: list[int], visited: int = 0, *, last_hp: int = 50) -> dict:
    hist: list[list[dict]] = []
    for i, n in enumerate(acts):
        pts = [pt() for _ in range(n)]
        if i == len(acts) - 1 and pts:
            pts[-1] = pt(last_hp)
        hist.append(pts)
    return {"map_point_history": hist, "current_act_index": max(len(acts) - 1, 0),
            "visited_map_coords": list(range(visited))}


print("① 各 Act 点数之和就是总层数（含当前 Act 进行中的部分）")
check(ad.floor(st([17, 16, 6], 7)) == 39, "真机形态 17+16+6 = 39",
      str(ad.floor(st([17, 16, 6], 7))))
check(ad.floor(st([17])) == 17, "单 Act = 17", str(ad.floor(st([17]))))
check(ad.floor(st([17, 16])) == 33, "17+16 = 33", str(ad.floor(st([17, 16]))))
check(ad.floor(st([17, 7], 7)) == 24, "历史存档里的 [17,7] = 24",
      str(ad.floor(st([17, 7], 7))))

print()
print("② 末尾点是否结算，都不影响层数（真机历史里没有「未结算」这一说）")
check(ad.floor(st([17, 16, 6], last_hp=0)) == 39,
      "末点 hp=0（战斗中）仍是 39", str(ad.floor(st([17, 16, 6], last_hp=0))))
check(ad.floor(st([17, 16, 6], last_hp=50)) == 39,
      "末点已结算也是 39", str(ad.floor(st([17, 16, 6], last_hp=50))))

print()
print("③ 边界：空值 / 退化形态")
check(ad.floor(None) == 0, "None → 0")
check(ad.floor({}) == 0, "空 dict → 0")
check(ad.floor({"map_point_history": None}) == 0, "字段为 None → 0")
check(ad.floor({"map_point_history": [], "visited_map_coords": list(range(3))}) == 3,
      "无历史时退回坐标数 = 3")
check(ad.floor({"map_point_history": [[1, 2, 3]]}) == 3,
      "畸形点（非 dict）按点数计数，不炸", str(ad.floor({"map_point_history": [[1, 2, 3]]})))

print()
print("④ 关键回归：绝不能退回「只看 visited」的老算法")
old = 7
new = ad.floor(st([17, 16, 6], 7))
check(new == 39 and new != old, "多 Act 不再少算（老算法=7）", f"新={new}")

ok = sum(1 for c, _ in results if c)
print(f"\n结果: {ok}/{len(results)} 项通过")
for c, label in results:
    if not c:
        print(f"   ✘ {label}")
sys.exit(0 if ok == len(results) else 1)

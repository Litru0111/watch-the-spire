import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
from sts2mon import paths as _paths  # noqa: E402
"""扫描游戏与各模组的 .pck，找卡牌/遗物的「效果文本」。

思路：效果文本是本地化字符串，中文形态。在 pck 里定位中文串，
并结合附近的「键名」判断它属于哪张牌/哪个遗物。
先做可行性验证：能否稳定地拿到 (ID, 效果文本) 这类配对。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

APPDATA = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"
GAME = _paths.game_dir()
WORKSHOP = _paths.workshop_dir()

CJK = re.compile(r"[\u4e00-\u9fff]")
# 卡牌/遗物效果里常见的词，用来判定「这串是效果文本」
EFFECT_WORDS = ("造成", "伤害", "格挡", "抽", "消耗", "获得", "失去", "回复",
                "每", "所有", "敌人", "随机", "回合", "层", "点", "张", "能量")


def cjk_strings(blob: bytes, min_cjk: int = 4) -> list[str]:
    """从二进制里抽出「像中文文本」的串。

    pck 里的文本多以 UTF-8 连续存放，中间用 0 或非文本字节分隔。
    """
    out: list[str] = []
    for m in re.finditer(rb"(?:[\x20-\x7e]|[\xe4-\xe9][\x80-\xbf]{2}){4,400}", blob):
        s = m.group().decode("utf-8", "ignore")
        if len(CJK.findall(s)) >= min_cjk:
            out.append(s)
    return out


def looks_like_effect(s: str) -> bool:
    """是不是「效果描述」而不是标题/名字。"""
    if len(s) < 6 or len(s) > 200:
        return False
    hits = sum(1 for w in EFFECT_WORDS if w in s)
    return hits >= 1 and ("。" in s or "，" in s or hits >= 2)


def scan_pck(path: Path, label: str, limit: int = 8) -> dict:
    blob = path.read_bytes()
    strs = cjk_strings(blob)
    eff = [s for s in strs if looks_like_effect(s)]
    return {"label": label, "path": str(path), "mb": path.stat().st_size // 1048576,
            "cjk": len(strs), "effect_like": len(eff), "samples": eff[:limit]}


targets: list[tuple[str, Path]] = [("游戏本体", GAME / "SlayTheSpire2.pck")]
for p in sorted(WORKSHOP.glob("*/*.pck")):
    targets.append((p.parent.name, p))

print(f"扫描 {len(targets)} 个 pck（游戏本体 + 模组）…")
print()
results = []
for label, p in targets:
    if p.stat().st_size < 100_000:      # 跳过空壳
        continue
    try:
        r = scan_pck(p, label)
    except Exception as exc:
        print(f"  {label:<24} 读取失败: {exc!r}")
        continue
    results.append(r)
    print(f"  {label:<24} {r['mb']:>4} MB  中文串={r['cjk']:>6}  "
          f"疑似效果={r['effect_like']:>5}")

print()
print("=" * 78)
print("疑似效果文本样例")
for r in sorted(results, key=lambda x: -x["effect_like"])[:6]:
    print(f"\n--- {r['label']} ({r['path']}) ---")
    for s in r["samples"][:5]:
        print(f"   {s[:150]}")

out = Path("G:/DeepSeekHarness/sts2-monitor/.sts2mon/pck_text_scan.json")
out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\n写到 {out}")

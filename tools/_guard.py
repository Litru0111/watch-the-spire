"""测试用的前置数据检查。

公开仓库**不含**提取数据（`card_effects.json` / `card_values.json` /
`power_names.json`），它们是游戏版权内容，由用户本地跑 `sts2mon extract`
生成。所以依赖这些数据的测试必须**先检查再跑**，否则用户看到的是
一段 `FileNotFoundError` traceback，而不是「你还需要跑一步什么」。

用法：

    from _guard import require_extracted
    require_extracted("card_values.json")     # 缺了就友好退出（exit 2）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sts2mon import paths  # noqa: E402


def _banner(name: str) -> None:
    print("=" * 66)
    print(f" {name}")
    print("=" * 66)


def missing(*names: str) -> list[str]:
    """返回数据目录里缺失的文件名。"""
    d = paths.data_dir()
    return [n for n in names if not (d / n).is_file()]


def require_extracted(*names: str) -> None:
    """确保提取数据存在；缺失时给出指引并退出（码 2，与「断言失败」区分）。"""
    names = names or ("card_effects.json",)
    gone = missing(*names)
    if not gone:
        return
    _banner("缺少提取数据")
    print(f"  数据目录: {paths.data_dir()}")
    print(f"  缺失    : {', '.join(gone)}")
    print()
    print("  这些文件由游戏本地化文案与程序集常量提取而来，")
    print("  属于游戏版权内容，所以不随仓库分发。")
    print()
    print("  请在装有游戏本体的机器上运行：")
    print("      sts2mon.cmd extract")
    print("  或（在项目根目录）")
    print("      python -m sts2mon extract")
    g = paths.game_dir()
    if not g:
        print()
        print(f"  [!] 当前也没探测到游戏安装目录，需先设置 {paths.ENV_GAME}：")
        print(r"      set STS2MON_GAME=D:\SteamLibrary\steamapps\common\Slay the Spire 2")
    sys.exit(2)


def require_saves() -> None:
    """确保游戏历史存档可读；没有就友好退出。"""
    from sts2mon.adapters.history import find_run_files

    files = find_run_files()
    if files:
        return
    _banner("找不到游戏历史存档")
    print(f"  查找位置: {paths.sts2_appdata()}")
    print()
    print("  本测试需要至少一份真实对局存档（`.run` 文件）。")
    print("  它们由游戏在对局结束时写入，不随仓库分发。")
    print()
    print("  如果你确实玩过游戏，请确认存档路径；否则可跳过本测试，")
    print("  或运行不依赖存档的那些（见 docs/测试说明.md 的分层表）：")
    print("      python tools/selftest_eventscope.py")
    print("      python tools/simulate_runs.py --runs 25")
    sys.exit(2)

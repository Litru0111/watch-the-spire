"""路径解析：把「数据目录」「游戏目录」的定位集中到一处。

**为什么需要这个模块**

早先各处自己拼路径，出现了两套不一致的规则：

* `cli.py` 用 `Path.cwd() / ".sts2mon"` —— 于是从别的目录运行时，
  数据库会落到那个目录里（实测：从 `C:\\` 启动会生成 `C:\\.sts2mon\\sts2mon.db`），
  而且它是**在 import 时求值**的，进程内再也改不回来。
* `effects.py` / `glossary.py` 用 `Path(__file__).parent.parent / ".sts2mon"` ——
  锚定在包所在位置。

这里统一成「显式传入 > 环境变量 > 项目根 > 用户数据目录」的优先级，
并且**每次调用都重新求值**，不做 import 期缓存。

环境变量
--------
STS2MON_DATA
    数据目录（数据库、从游戏提取的文案、截图等）。默认 `<项目根>/.sts2mon`。
STS2MON_GAME
    游戏安装目录（含 `data_sts2_windows_x86_64/`）。留空则自动探测。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "ENV_DATA", "ENV_GAME",
    "data_dir", "db_path", "jsonl_path", "sandbox_dir", "asset",
    "game_dir", "dll_path", "pck_path", "workshop_dir",
    "sts2_appdata", "local_steam_id_hint", "explain",
]

ENV_DATA = "STS2MON_DATA"
ENV_GAME = "STS2MON_GAME"

# 包所在的**项目根**（`<root>/sts2mon/paths.py` → `<root>`）
_ROOT = Path(__file__).resolve().parent.parent


def _project_data_dir() -> Path:
    return _ROOT / ".sts2mon"


def data_dir(explicit: str | os.PathLike | None = None) -> Path:
    """数据目录。

    优先级：显式参数 > `STS2MON_DATA` > `<项目根>/.sts2mon`。
    项目根不可写时（例如装进只读的 site-packages）退回用户数据目录。
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_DATA)
    if env:
        return Path(env).expanduser()
    root = _project_data_dir()
    try:
        root.mkdir(parents=True, exist_ok=True)
        return root
    except OSError:
        fallback = _user_data_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _user_data_dir() -> Path:
    """只读安装时的兜底位置。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "WatchTheSpire"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "WatchTheSpire"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "WatchTheSpire"


def db_path(explicit: str | os.PathLike | None = None) -> Path:
    return Path(explicit).expanduser() if explicit else data_dir() / "sts2mon.db"


def jsonl_path(explicit: str | os.PathLike | None = None) -> Path:
    return Path(explicit).expanduser() if explicit else data_dir() / "events.jsonl"


def sandbox_dir(explicit: str | os.PathLike | None = None) -> Path:
    return Path(explicit).expanduser() if explicit else data_dir() / "replay"


def asset(*parts: str) -> Path:
    """包内静态资源（如 `assets/dashboard.html`）。"""
    return _ROOT.joinpath("sts2mon", *parts)


# ------------------------------------------------------------------ 游戏目录

# Steam 默认安装位置的候选（按常见程度排序）
_STEAM_HINTS = (
    r"C:\Program Files (x86)\Steam\steamapps\common",
    r"C:\Program Files\Steam\steamapps\common",
    r"D:\SteamLibrary\steamapps\common",
    r"E:\SteamLibrary\steamapps\common",
    r"F:\SteamLibrary\steamapps\common",
    r"G:\SteamLibrary\steamapps\common",
    r"D:\Steam\steamapps\common",
    r"E:\Steam\steamapps\common",
)
_GAME_FOLDER = "Slay the Spire 2"
_WORKSHOP_APPID = "2868840"


def game_dir(explicit: str | os.PathLike | None = None) -> Path | None:
    """游戏安装目录（含 `data_sts2_windows_x86_64/`）。找不到返回 None。

    优先级：显式参数 > `STS2MON_GAME` > Steam 库文件解析 > 常见路径探测。
    """
    for cand in (explicit, os.environ.get(ENV_GAME)):
        if cand:
            p = Path(cand).expanduser()
            if p.is_dir():
                return p
            # 允许传入上一级（steamapps/common）
            alt = p / _GAME_FOLDER
            if alt.is_dir():
                return alt
    for lib in _steam_libraries():
        p = lib / "steamapps" / "common" / _GAME_FOLDER
        if p.is_dir():
            return p
    for hint in _STEAM_HINTS:
        p = Path(hint) / _GAME_FOLDER
        if p.is_dir():
            return p
    return None


def _steam_libraries() -> list[Path]:
    """从 Steam 的 libraryfolders.vdf 里读出所有库位置。"""
    out: list[Path] = []
    roots: list[Path] = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        roots.append(Path(pf) / "Steam")
        roots.append(Path(pf).parent / "Steam")
    roots.append(Path.home() / ".steam" / "steam")
    roots.append(Path.home() / ".local" / "share" / "Steam")
    roots.append(Path.home() / "Library" / "Application Support" / "Steam")
    for r in roots:
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if '"path"' not in line:
                continue
            parts = line.split('"')
            if len(parts) >= 4:
                p = Path(parts[3].replace("\\\\", "\\"))
                if p.is_dir() and p not in out:
                    out.append(p)
    return out


def dll_path(explicit: str | os.PathLike | None = None) -> Path | None:
    """`sts2.dll`（.NET 程序集，卡牌数值与升级增量从这里提取）。"""
    if explicit:
        return Path(explicit).expanduser()
    g = game_dir()
    if not g:
        return None
    p = g / "data_sts2_windows_x86_64" / "sts2.dll"
    return p if p.is_file() else None


def pck_path(explicit: str | os.PathLike | None = None) -> Path | None:
    """`SlayTheSpire2.pck`（本地化文案从这里提取）。"""
    if explicit:
        return Path(explicit).expanduser()
    g = game_dir()
    if not g:
        return None
    for name in ("SlayTheSpire2.pck", "SlayTheSpire2.pck.bytes"):
        p = g / name
        if p.is_file():
            return p
    hits = sorted(g.glob("*.pck"))
    return hits[0] if hits else None


def workshop_dir() -> Path | None:
    """创意工坊内容目录（mod 的 pck 在这里）。"""
    for lib in _steam_libraries():
        p = lib / "steamapps" / "workshop" / "content" / _WORKSHOP_APPID
        if p.is_dir():
            return p
    for hint in _STEAM_HINTS:
        p = Path(hint).parent / "workshop" / "content" / _WORKSHOP_APPID
        if p.is_dir():
            return p
    return None


# ------------------------------------------------------------- 游戏写出的文件

def sts2_appdata(explicit: str | os.PathLike | None = None) -> Path:
    """游戏自己的数据目录（存档、mod 导出的状态文件都在这里）。

    Windows: `%APPDATA%/SlayTheSpire2`；其它平台按 Godot 的 `user://` 约定推测。
    """
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return Path(base) / "SlayTheSpire2"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SlayTheSpire2"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "SlayTheSpire2"


def local_steam_id_hint() -> str:
    """从 Steam 存档目录名推导本机 Steam ID（找不到返回空串）。

    给开发脚本用，免得把真实账号 ID 写进源码 —— 那既泄露隐私，
    也意味着只有作者本机能跑。
    """
    root = sts2_appdata() / "steam"
    if not root.is_dir():
        return ""
    for d in sorted(root.iterdir(), reverse=True):
        if d.is_dir() and d.name.isdigit() and len(d.name) >= 15:
            return d.name
    return ""


def explain() -> dict[str, str]:
    """把当前解析结果摊平，`doctor` 用来展示。"""
    g = game_dir()
    d = dll_path()
    p = pck_path()
    return {
        "项目根": str(_ROOT),
        "数据目录": str(data_dir()),
        "数据目录来源": ("环境变量 " + ENV_DATA) if os.environ.get(ENV_DATA)
                        else ("项目内 .sts2mon" if data_dir() == _project_data_dir()
                              else "用户数据目录（项目根只读）"),
        "数据库": str(db_path()),
        "游戏目录": str(g) if g else "未找到（可设 " + ENV_GAME + "）",
        "游戏目录来源": ("环境变量 " + ENV_GAME) if os.environ.get(ENV_GAME)
                        else ("Steam 库/常见路径探测" if g else "-"),
        "sts2.dll": str(d) if d else "未找到",
        "游戏 pck": str(p) if p else "未找到",
        "创意工坊": str(workshop_dir()) if workshop_dir() else "未找到（无 mod 时正常）",
        "游戏数据目录": str(sts2_appdata()),
    }

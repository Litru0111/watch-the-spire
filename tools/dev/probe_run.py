"""探针脚本：分析杀戮尖塔2 的 .run / .mcr 存档结构。

用法:
    python tools/probe_run.py <file> [...]      # 分析若干存档
    python tools/probe_run.py --list            # 列出所有可选存档
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROAMING = Path.home() / "AppData" / "Roaming" / "SlayTheSpire2"


def candidate_files() -> list[Path]:
    out: list[Path] = []
    for pat in ("**/saves/history/*.run", "**/saves/*.save", "**/replays/*.mcr",
                "**/saves/*.run", "**/*.run"):
        out.extend(sorted(ROAMING.glob(pat)))
    seen, uniq = set(), []
    for p in out:
        if p not in seen and p.is_file():
            seen.add(p)
            uniq.append(p)
    return uniq


def load(path: Path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return json.loads(raw.decode(enc)), raw
        except Exception:
            continue
    return None, raw


def shape(node, depth: int = 0, max_depth: int = 4):
    """返回结构的紧凑描述。"""
    if isinstance(node, dict):
        if depth >= max_depth:
            return "{...%d keys}" % len(node)
        return {"{...}": {}}
    if isinstance(node, list):
        if not node:
            return "[0]"
        return ["[%d]" % len(node), shape(node[0], depth, max_depth)]
    if isinstance(node, str):
        return "str"
    if isinstance(node, bool):
        return "bool"
    if isinstance(node, (int, float)):
        return "num"
    if node is None:
        return "null"
    return type(node).__name__


def walk(node, path: str = "", depth: int = 0, out: dict | None = None,
         max_depth: int = 6) -> dict:
    """把 JSON 展开成 {路径: 类型} 的扁平表，数组只走第一个元素。"""
    if out is None:
        out = {}
    if depth > max_depth:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            kind = type(v).__name__
            if isinstance(v, list):
                out.setdefault(f"{p}[]", f"list[{len(v)}]")
                if v:
                    walk(v[0], p + "[]", depth + 1, out, max_depth)
            elif isinstance(v, dict):
                out.setdefault(f"{p}", "{obj}")
                walk(v, p, depth + 1, out, max_depth)
            else:
                out.setdefault(f"{p}", kind)
    elif isinstance(node, list):
        for item in node[:1]:
            walk(item, path, depth + 1, out, max_depth)
    return out


def report(path: Path) -> None:
    data, raw = load(path)
    print("=" * 100)
    print(f"FILE : {path}")
    print(f"SIZE : {len(raw):,} bytes")
    if data is None:
        print("PARSE: 不是 JSON（可能是二进制存档）")
        print("HEAD :", raw[:120].hex(" "))
        return
    print(f"PARSE: JSON OK  (顶层 {len(data)} 个键)")
    top = {k: shape(v) for k, v in data.items()}
    print("TOP  :", json.dumps(top, ensure_ascii=False, indent=2)[:2000])

    flat = walk(data)
    print(f"PATHS: {len(flat)} 条（路径 -> 类型）")
    keys = sorted(flat)
    important = [k for k in keys if any(
        s in k.lower() for s in (
            "map_point", "player_stats", "monster", "damage", "turn", "card",
            "hp", "block", "energy", "relic", "potion", "enemy", "encounter",
            "combat", "battle", "damage_taken", "powers",
        ))]
    print(f"关键字段 {len(important)} 条:")
    for k in important:
        print(f"   {k:110s} {flat[k]}")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "--list":
        files = candidate_files()
        print(f"在 {ROAMING} 下发现 {len(files)} 个文件:")
        for p in files:
            print(f"   {p.stat().st_size:>10,}  {p.relative_to(ROAMING)}")
        if not args:
            return
        return
    for a in args:
        report(Path(a))


if __name__ == "__main__":
    main()

"""静态核对 model.py 里每条 INSERT 的列数 / 占位符数 / 实参个数是否一致。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "sts2mon" / "model.py"
tree = ast.parse(SRC.read_text(encoding="utf-8"))

def _definitions(ddl_source: str):
    """逐个取出 CREATE TABLE 的 (表名, 列清单)，正确处理括号嵌套。"""
    marker = "CREATE TABLE IF NOT EXISTS "
    pos = 0
    while True:
        start = ddl_source.find(marker, pos)
        if start < 0:
            return
        name_start = start + len(marker)
        paren = ddl_source.find("(", name_start)
        if paren < 0:
            return
        name = ddl_source[name_start:paren].strip()
        depth, i = 0, paren
        while i < len(ddl_source):
            ch = ddl_source[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield name, ddl_source[paren + 1:i]
        pos = i + 1


# 建表语句里的列清单
ddl_src = SRC.read_text(encoding="utf-8")
tables: dict[str, list[str]] = {}
for name, body in _definitions(ddl_src):
    cols = []
    depth = 0
    current = ""
    parts = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    for line in parts:
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        head = line.split()[0]
        if head.upper() in ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
            continue
        cols.append(head)
    tables[name] = cols

print("=== 建表列数 ===")
for t, cols in tables.items():
    print(f"   {t:12s} {len(cols):>2} 列")

problems = 0
for node in ast.walk(tree):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"):
        continue
    if not node.args:
        continue
    sql_node = node.args[0]
    if isinstance(sql_node, ast.Constant) and isinstance(sql_node.value, str):
        sql = sql_node.value
    elif isinstance(sql_node, ast.JoinedStr):
        continue
    else:
        continue
    m = re.search(r"INSERT\s+(?:OR\s+REPLACE\s+)?INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*\(([^)]*)\)",
                  sql, re.S | re.I)
    if not m:
        continue
    table, collist, values = m.group(1), m.group(2), m.group(3)
    cols = [c.strip() for c in collist.split(",") if c.strip()]
    ph = len(re.findall(r"\?", values))
    # SQL 里的字面量（如 VALUES('schema_version', ?)）也占一个列位
    literals = len(re.findall(r"'(?:[^']|'')*'|\bNULL\b|\b\d+(?:\.\d+)?\b", values))
    supplied = ph + literals
    # 实参：第二种参数形式是 tuple
    args = node.args[1] if len(node.args) > 1 else None
    n_args = None
    if isinstance(args, ast.Tuple):
        n_args = len(args.elts)
    schema_cols = tables.get(table, [])
    flag = "OK " if (len(cols) == supplied == (n_args + literals if n_args is not None else supplied)
                     and len(cols) == len(schema_cols)) else "BAD"
    if flag == "BAD":
        problems += 1
    print(f"[{flag}] line {node.lineno:>4} {table:12s} 列={len(cols):>2} "
          f"值={supplied:>2}(占位符{ph}+字面量{literals}) "
          f"实参={n_args if n_args is not None else '?':>2} 表定义={len(schema_cols):>2}")

print()
print("结论:", "全部一致 ✔" if problems == 0 else f"{problems} 处不一致 ✘")

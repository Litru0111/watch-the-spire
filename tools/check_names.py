"""静态检查：有没有「用到但没定义」的名字（NameError 的预检）。

**为什么需要这个**

`cli.py` 曾经只 `from .events import EventBus`，却在 `_run_watch` 里调用
`Event(kind=K.RUN_END, ...)` —— 这条路径只在「进行中存档消失 → 对局结束」
时才会走到。于是：

  * 所有测试都通过（没有测试覆盖这条冷路径）
  * 服务能正常启动、能正常采集
  * 只在**对局真的结束那一刻**抛 NameError 崩掉

实测后果：跑了一小时的服务在用户结束一局时崩了，仪表盘整个下线。
这种 bug 用运行时测试很难覆盖全（冷路径太多），但静态扫一遍
「Load 上下文里出现过、却没有任何绑定」的名字就能抓到。

局限
----
* 只做模块级的名字解析，不区分作用域嵌套 —— 宁可漏报也不误报，
  所以内层函数用到的全局名会被算作「已定义」。
* 动态赋值（setattr / globals()）识别不到，可能误报；
  误报时脚本会以退出码 1 结束，需要人工确认。
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 解释器/运行环境提供、AST 里看不到绑定的名字
IMPLICIT = {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "self", "cls",
    "WindowsError",
}


def defined_names(tree: ast.AST) -> set[str]:
    """模块里出现过「绑定」的名字（导入、定义、赋值、参数、with/except 目标）。"""
    out: set[str] = set(dir(builtins)) | IMPLICIT
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, (ast.Name, ast.Attribute)) and isinstance(n.ctx, ast.Store):
            if isinstance(n, ast.Name):
                out.add(n.id)
        elif isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.Global):
            out.update(n.names)
        elif isinstance(n, (ast.comprehension,)):
            if isinstance(n.target, ast.Name):
                out.add(n.target.id)
    return out


def annotation_names(tree: ast.AST) -> set[int]:
    """收集「纯注解」里的 Name 节点 id（这些不会在运行期求值）。

    有 `from __future__ import annotations`（PEP 563）时，注解不求值，
    所以 `def f(store: Store)` 里没导入的 `Store` 是**安全**的；
    而没有 PEP 563 就要真的能解析到 —— 必须区分开，否则：
      * 有 PEP 563：把安全的注解名字误报成 NameError 风险
      * 无 PEP 563：漏报真的会在导入期炸掉的名字
    """
    ids: set[int] = set()
    for n in ast.walk(tree):
        for ann in (getattr(n, "annotation", None),
                    getattr(n, "returns", None)):
            if ann is None:
                continue
            for sub in ast.walk(ann):
                if isinstance(sub, ast.Name):
                    ids.add(id(sub))
        # 变量注解：x: Foo = 1
        if isinstance(n, ast.AnnAssign) and n.annotation is not None:
            for sub in ast.walk(n.annotation):
                if isinstance(sub, ast.Name):
                    ids.add(id(sub))
    return ids


def has_pep563(tree: ast.AST) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "__future__":
            if any(a.name == "annotations" for a in n.names):
                return True
    return False


def check_file(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    """返回 (运行期未定义, 仅注解未定义)。

    第一个字典是**真的会抛 NameError** 的；第二个只在没有 PEP 563 时才危险。
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    known = defined_names(tree)
    lazy = has_pep563(tree)
    ann_ids = annotation_names(tree) if lazy else set()

    runtime: dict[str, int] = {}
    only_ann: dict[str, int] = {}
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
            continue
        if n.id in known:
            continue
        if id(n) in ann_ids:
            only_ann.setdefault(n.id, n.lineno)
        else:
            runtime.setdefault(n.id, n.lineno)
    return runtime, only_ann


def main() -> int:
    targets = sorted(list((ROOT / "sts2mon").rglob("*.py"))
                     + list((ROOT / "tools").rglob("*.py")))
    print(f"检查 {len(targets)} 个文件的未定义名字…")
    bad = 0
    for f in targets:
        rel = f.relative_to(ROOT)
        try:
            runtime, only_ann = check_file(f)
        except SyntaxError as exc:
            print(f"  ✘ {rel}: 语法错误 {exc}")
            bad += 1
            continue
        if runtime:
            # 这些会在运行期抛 NameError
            bad += 1
            print(f"  ✘ {rel}:")
            for name, line in sorted(runtime.items(), key=lambda kv: kv[1]):
                print(f"       L{line}: {name}   ← 运行期求值，会抛 NameError")
        elif only_ann:
            # 有 PEP 563 时注解不求值，安全；只是提示可以补导入
            print(f"  · {rel}: 仅注解未导入 {sorted(only_ann)}（PEP 563，安全）")
    print()
    if bad:
        print(f"结论: {bad} 个文件存在运行期未定义名字（会抛 NameError）")
        return 1
    print("结论: 没有运行期未定义名字 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

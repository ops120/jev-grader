"""参照系自身的完整性门禁：两份 S0 副本必须继续逐字相等。

为什么需要这一条
----------------
``_legacy`` 与 ``orchestrators`` 是同一份重构前实现的两个独立副本，
``golden_compose`` 拿它们对拍新的 spec／策略。可这道门禁有个前提：
参照系本身没被"顺手修一下"。如果哪天有人把 ``_legacy`` 改成与新策略一致，
等价证明就自我作废了——而两个副本必须同时改才能骗过这里。

只比对 AST，不比对文本：副本为了让三个同名函数共存必须改名（``compose`` →
``en_compose``），函数名之外的任何差异都不该存在。

跑法：python tests/test_judge_contract.py    退出码 0 = 参照系未被改动
"""
from __future__ import annotations

import ast
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
JUDGE = SRC / "judge"

# (副本 A 模块, 函数名) -> (副本 B 模块, 函数名)：同一份 S0 实现的两处抄本
TWINS = [("_legacy", "compose", "orchestrators", "en_compose"),
         ("_legacy", "math_compose", "orchestrators", "math_compose"),
         ("_legacy", "_kinds", "orchestrators", "_math_kinds")]

# 为了让三份同名实现共存于一个模块而做的改名。这些标识符比对前一律归一，
# 因为它们不同**不携带任何行为差异**；除名字外的任何一处不同都仍然是硬失败。
NORMALIZE = {"compose": "_fn", "en_compose": "_fn", "math_compose": "_fn",
             "_kinds": "_helper", "_math_kinds": "_helper"}


class _Renamer(ast.NodeTransformer):
    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.name = NORMALIZE.get(node.name, node.name)
        self.generic_visit(node)
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        node.id = NORMALIZE.get(node.id, node.id)
        return node


def _func(path: Path, name: str) -> ast.FunctionDef | None:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _dump(fn: ast.FunctionDef | None) -> str:
    """规范化 AST：只归一上表列出的改名，其余字段（含返回标注、常量、分支顺序）全部参与比对。"""
    if fn is None:
        return "<缺失>"
    return ast.dump(_Renamer().visit(deepcopy(fn)), annotate_fields=False)


# 允许 import 这两份 S0 副本的文件；生产代码 import 它们就是拿旧实现跑线上
ORACLE_FILES = {"_legacy.py", "orchestrators.py"}


def check_twins() -> list[str]:
    errs = []
    for amod, afn, bmod, bfn in TWINS:
        a = _dump(_func(JUDGE / f"{amod}.py", afn))
        b = _dump(_func(JUDGE / f"{bmod}.py", bfn))
        if a != b:
            errs.append(f"{amod}.{afn} 与 {bmod}.{bfn} 不再逐字相等——"
                        f"S0 参照系被改动了，golden_compose 的等价证明随之失效")
    return errs


def check_oracles_unused() -> list[str]:
    """生产代码不得 import 参照系：那等于线上悄悄跑着旧实现。"""
    errs = []
    for py in sorted(SRC.rglob("*.py")):
        if py.parent == JUDGE and py.name in ORACLE_FILES:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for m in mods:
                if m.split(".")[-1] in ("_legacy", "orchestrators") or \
                        m.endswith(("._legacy", ".orchestrators")):
                    rel = py.relative_to(ROOT).as_posix()
                    errs.append(f"{rel} 引用了 S0 参照系（{m}）：请改用 judge.engine / judge.strategies")
    return errs


def main() -> int:
    failures = check_twins() + check_oracles_unused()
    for f in failures:
        print("FAIL", f)
    if failures:
        return 1
    print(f"OK   {len(TWINS)} 对 S0 副本逐字相等；参照系未被生产代码引用")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(SRC))
    sys.exit(main())

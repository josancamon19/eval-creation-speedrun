"""Read and populate literal task settings without executing verifier code."""

import ast
from pathlib import Path
from pprint import pformat


def _assignment(source: str, name: str) -> ast.Assign:
    matches = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one literal assignment for {name}")
    return matches[0]


def read_literal(path: Path, name: str):
    return ast.literal_eval(_assignment(path.read_text(), name).value)


def write_cases(task: Path, cases: list[dict]) -> None:
    path = task / "tests/verify.py"
    source = path.read_text()
    node = _assignment(source, "CASES")
    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1 : node.end_lineno] = [
        f"CASES = {pformat(cases, width=88, sort_dicts=False, compact=True)}\n"
    ]
    path.write_text("".join(lines))


def task_spec(task: Path) -> dict:
    policy = read_literal(task / "tests/case_runner.py", "_PLATFORM_POLICY")
    return {
        "environment": policy["environment"],
        "cases": read_literal(task / "tests/verify.py", "CASES"),
    }

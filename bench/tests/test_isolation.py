"""Isolation lint (§7.5 / §9.3).

Static check that no file under ``bench/`` imports a private ``engine.*`` or
``plugins.*`` module. The membench harness reaches Minni only through the public
daemon socket; importing engine internals would be a fairness-isolation breach.
"""

import ast
from pathlib import Path

import pytest

_BENCH_DIR = Path(__file__).resolve().parents[1]
_FORBIDDEN_ROOTS = ("engine", "plugins")


def _python_files():
    for p in _BENCH_DIR.rglob("*.py"):
        # Skip nothing under bench/ — the rule applies to the whole tree.
        yield p


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("py_file", list(_python_files()), ids=lambda p: p.name)
def test_no_engine_or_plugins_imports(py_file):
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    roots = _imported_roots(tree)
    breaches = roots & set(_FORBIDDEN_ROOTS)
    assert not breaches, (
        f"{py_file.relative_to(_BENCH_DIR)} imports forbidden root(s) "
        f"{breaches} — bench/ must not reach into engine/ or plugins/ (§7.5)"
    )

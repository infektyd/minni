"""#258 — Make-driven pytest must import minni from *this* tree.

Mechanical gate denies edits to ``tests/conftest.py``, so the load-bearing
contract for ``make test-engine`` / ``make check`` / ``make coverage-engine`` is
``PYTHONPATH=src`` in the Makefile recipes. This module pins that contract
(fail-closed on Makefile text) and proves a competing ``minni`` on PYTHONPATH
loses when this tree's ``src`` is prepended the same way Make does.

Bare ``python -m pytest`` without Make (or without PYTHONPATH=src) can still
resolve an editable install from another checkout — that is outside this PR's
non-conftest surface; agents should use the Make targets above.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_SRC = (REPO_ROOT / "src").resolve()
MAKEFILE = REPO_ROOT / "Makefile"

# Targets whose recipes must prepend this tree's src (#258).
_PYTHONPATH_PINNED_TARGETS = ("test-engine", "coverage-engine", "check")


def _recipe_block(makefile_text: str, target: str) -> str:
    """Return tab-indented recipe lines for a simple Make target.

    Matches ``target:`` (with optional prerequisites) then consecutive recipe
    lines (tab-prefixed). Stops at the next non-recipe line.
    """
    pattern = re.compile(
        rf"(?m)^{re.escape(target)}:[^\n]*\n((?:[ \t].*\n|\n)*)"
    )
    match = pattern.search(makefile_text)
    assert match is not None, f"Makefile missing target {target!r}"
    return match.group(1)


def test_makefile_pythonpath_src_contract():
    """Fail closed if Make drops PYTHONPATH=src from the #258 test targets.

    Does **not** skip when the current process lacks PYTHONPATH — that fails
    open and lets the suite stay green after the contract is removed. The pin
    is the Makefile recipe text itself.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    for target in _PYTHONPATH_PINNED_TARGETS:
        recipe = _recipe_block(text, target)
        assert "PYTHONPATH=src" in recipe, (
            f"Makefile target {target!r} must set PYTHONPATH=src in its recipe "
            f"(#258 worktree import contract). Recipe was:\n{recipe!r}"
        )


def test_pythonpath_src_wins_over_competing_minni(tmp_path):
    """Simulated wrong-tree: fake minni loses when this tree's src is first.

    Self-supplies PYTHONPATH the same way Make does; does not read the Makefile
    (that pin is ``test_makefile_pythonpath_src_contract``).
    """
    fake = tmp_path / "fake_site"
    (fake / "minni").mkdir(parents=True)
    (fake / "minni" / "__init__.py").write_text(
        'raise RuntimeError("wrong-tree minni imported")\n', encoding="utf-8"
    )
    # Prepend order matches Makefile: this tree's src first, then junk.
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_SRC), str(fake)])
    env.pop("MINNI_HOME", None)
    script = textwrap.dedent(
        f"""
        import minni, os
        path = os.path.realpath(minni.__file__)
        expected = os.path.realpath({str(REPO_SRC)!r}) + os.sep
        assert path.startswith(expected), (path, expected)
        print("ok", path)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout

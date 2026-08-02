"""#258 — pytest must import minni from *this* tree, not an editable main.

Mechanical gate denies edits to ``tests/conftest.py``, so the load-bearing
contract for CI / ``make check`` is ``PYTHONPATH=src`` (Makefile). This module
pins that env contract and proves a competing ``minni`` on PYTHONPATH loses when
this tree's ``src`` is prepended the same way Make does.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_SRC = (REPO_ROOT / "src").resolve()


def test_makefile_pythonpath_contract_in_this_process():
    """When Make exports PYTHONPATH=src, import resolves under this tree.

    Bare pytest without PYTHONPATH may still hit an editable install — that is
    outside this PR's non-conftest surface; agents should use ``make test-engine``.
    """
    pythonpath = os.environ.get("PYTHONPATH", "")
    parts = [Path(p).resolve() for p in pythonpath.split(os.pathsep) if p]
    if REPO_SRC not in parts:
        pytest.skip(
            "PYTHONPATH does not include this tree's src/ — run via "
            "`make test-engine` / `make check` (the #258 contract)"
        )
    import minni

    package_file = Path(minni.__file__).resolve()
    assert str(package_file).startswith(str(REPO_SRC) + os.sep), (
        f"minni.__file__={package_file} is not under {REPO_SRC} despite "
        f"PYTHONPATH containing it (PYTHONPATH={pythonpath!r})"
    )


def test_pythonpath_src_wins_over_competing_minni(tmp_path):
    """Simulated wrong-tree: fake minni ahead of empty path still loses when
    this tree's src is first on PYTHONPATH (Make's prepend order).
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

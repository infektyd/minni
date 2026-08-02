"""#258 — Make-driven pytest must import minni from *this* tree.

Mechanical gate denies edits to ``tests/conftest.py``, so the load-bearing
contract for ``make test-engine`` / ``make check`` / ``make coverage-engine`` is
``PYTHONPATH=src`` in the Makefile recipes. This module:

1. Pins that Makefile recipe text (fail-closed, active assignment only)
2. Runtime-pins that *this* process imported ``minni`` from this worktree's
   ``src/`` (fail-closed for worktree + foreign-editable)
3. Proves ``PYTHONPATH=src`` (Make shape) beats a site-packages/.pth-style
   editable competitor — the real #258 failure mode

Bare ``python -m pytest`` without Make (or without ``PYTHONPATH=src``) can still
resolve an editable install from another checkout when the pin below is not
enforced by the process env — agents should use the Make targets above.
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

# Active recipe assignment (not a comment): tab + PYTHONPATH=src then :/$$/space
_ACTIVE_PYTHONPATH_SRC = re.compile(r"(?m)^\tPYTHONPATH=src(?::|\$\$| )")


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
    """Fail closed if Make drops active PYTHONPATH=src from #258 test targets.

    Requires a tab-indented assignment line (not a comment substring) and that
    the same recipe block invokes pytest.
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    for target in _PYTHONPATH_PINNED_TARGETS:
        recipe = _recipe_block(text, target)
        assert _ACTIVE_PYTHONPATH_SRC.search(recipe), (
            f"Makefile target {target!r} must have an active recipe line "
            f"assigning PYTHONPATH=src (not merely a comment). Recipe was:\n"
            f"{recipe!r}"
        )
        assert "pytest" in recipe, (
            f"Makefile target {target!r} must invoke pytest in the same "
            f"recipe block as PYTHONPATH=src. Recipe was:\n{recipe!r}"
        )


def test_import_minni_resolves_under_this_repo_src():
    """Process-local pin: suite must have imported *this* worktree's minni.

    On a correct single-checkout editable install this stays green. On the
    worktree + foreign-editable case (primary ``pip install -e`` reused from
    another checkout) it fails closed — the silent wrong-tree bug from #258.
    """
    import minni

    got = os.path.realpath(minni.__file__)
    root = str(REPO_SRC.resolve()) + os.sep
    assert got.startswith(root), (
        f"import minni resolved outside this worktree: {got!r}; "
        f"expected under {root!r}. Use make test-engine/check/coverage-engine "
        f"or PYTHONPATH=src (#258)."
    )


def test_pythonpath_src_wins_over_site_packages_editable(tmp_path):
    """Make-shape PYTHONPATH=src beats site-packages/.pth editable competitor.

    Models the real #258 competitor: foreign ``minni`` reachable via a ``.pth``
    processed by ``site.addsitedir`` (pip editable), *not* a second PYTHONPATH
    entry. Path ordering between two PYTHONPATH entries would pass even if Make
    never set ``PYTHONPATH=src``.

    Subprocess env only sets relative ``PYTHONPATH=src`` with ``cwd=REPO_ROOT``
    (Make recipe shape). The competitor is injected via ``addsitedir``, so the
    only win condition is this tree's ``src`` beating site/editable resolution.
    """
    expected_root = str(REPO_SRC.resolve()) + os.sep

    foreign_src = tmp_path / "foreign_src"
    (foreign_src / "minni").mkdir(parents=True)
    (foreign_src / "minni" / "__init__.py").write_text(
        'TREE = "foreign"\n', encoding="utf-8"
    )
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    # Same mechanism as pip's __editable__*.pth / easy-install.pth.
    (site_packages / "minni_foreign.pth").write_text(
        str(foreign_src.resolve()) + "\n", encoding="utf-8"
    )
    foreign_root = str(foreign_src.resolve()) + os.sep
    site_packages_s = str(site_packages.resolve())

    # Make shape: relative PYTHONPATH=src only (no second PYTHONPATH competitor).
    env_make = os.environ.copy()
    env_make["PYTHONPATH"] = "src"
    env_make.pop("MINNI_HOME", None)
    make_shape = textwrap.dedent(
        f"""
        import os, site
        site.addsitedir({site_packages_s!r})
        import minni
        path = os.path.realpath(minni.__file__)
        expected = {expected_root!r}
        foreign = {foreign_root!r}
        assert path.startswith(expected), (path, expected)
        assert not path.startswith(foreign), path
        assert getattr(minni, "TREE", None) != "foreign"
        print("ok", path)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", make_shape],
        env=env_make,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout

    # Document failure mode: without this tree's src on PYTHONPATH, a foreign
    # path entry ahead of site resolution loads the competitor marker.
    # Uses a single non-this-src path entry (not dual-PYTHONPATH ordering).
    env_bare = os.environ.copy()
    env_bare.pop("PYTHONPATH", None)
    env_bare.pop("MINNI_HOME", None)
    this_src = str(REPO_SRC.resolve())
    foreign_src_s = str(foreign_src.resolve())
    baseline = textwrap.dedent(
        f"""
        import os, sys
        this_src = {this_src!r}
        foreign_src = {foreign_src_s!r}
        # Competitor first; strip this worktree src so it cannot win by accident.
        sys.path = [foreign_src] + [
            p for p in sys.path
            if p and os.path.realpath(p) != os.path.realpath(this_src)
        ]
        import minni
        path = os.path.realpath(minni.__file__)
        foreign = {foreign_root!r}
        assert path.startswith(foreign), (path, foreign)
        assert getattr(minni, "TREE", None) == "foreign"
        print("foreign-ok", path)
        """
    )
    proc_f = subprocess.run(
        [sys.executable, "-c", baseline],
        env=env_bare,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_f.returncode == 0, proc_f.stdout + proc_f.stderr
    assert "foreign-ok" in proc_f.stdout

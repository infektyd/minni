"""#258 — Make-driven pytest must import minni from *this* tree.

Landing a ``tests/conftest.py`` edit forfeits the mechanical gate's automatic
approval (``.github/scripts/grok_approve_gate.py`` path-denies it, along with
``pyproject.toml`` and friends, since collection config can silently disable
every tripwire below) — it needs operator/manual review instead, which is why
the original #258 fix here avoided it. This module:

1. Pins that Makefile recipe text (fail-closed, active assignment on the same
   line as pytest)
2. Runtime-pins that *this* process imported ``minni`` from this worktree's
   ``src/`` (fail-closed for worktree + foreign-editable)
3. Proves ``PYTHONPATH=src`` (Make shape) is the reason this tree wins over a
   site-packages/.pth-style editable competitor — the real #258 failure mode

A ``pytest_configure`` hook in ``tests/conftest.py`` now closes the residual
gap this module's own docstring used to describe: it runs unconditionally for
every pytest invocation under this rootdir (unlike a collected test, it can't
be skipped by scoping to one file or a ``-k`` filter) and fails the session
before collection if ``import minni`` did not resolve under this rootdir's
``src/``. Bare ``python -m pytest`` from a worktree, with no PYTHONPATH set,
now fails loud instead of silently validating another checkout.
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

# Same recipe line: tab + PYTHONPATH=src[optional :$$PYTHONPATH] + $(VENV_PY)
# -m pytest (Make applies env only to that command). Rejects decoys like
# ``PYTHONPATH=src true; $(VENV_PY) -m pytest`` (``;`` before -m pytest) and
# ``PYTHONPATH=src echo pytest`` (no ``$(VENV_PY) -m pytest``).
_SAME_LINE_PYTHONPATH_PYTEST = re.compile(
    r"(?m)^\tPYTHONPATH=src(?::\$\$PYTHONPATH)?\s+\$\(VENV_PY\)\s+-m\s+pytest\b"
)


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
    """Fail closed if Make drops same-line PYTHONPATH=src + pytest (#258).

    Requires a single tab-indented line that both assigns PYTHONPATH=src and
    invokes pytest (not a comment, not a split-line assignment that only
    covers ``true``).
    """
    text = MAKEFILE.read_text(encoding="utf-8")
    for target in _PYTHONPATH_PINNED_TARGETS:
        recipe = _recipe_block(text, target)
        assert _SAME_LINE_PYTHONPATH_PYTEST.search(recipe), (
            f"Makefile target {target!r} must have a single recipe line that "
            f"both assigns PYTHONPATH=src and invokes pytest (Make applies "
            f"assignment only to that command). Recipe was:\n{recipe!r}"
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


def _competitor_probe_script(
    *,
    this_src: str,
    foreign_src: str,
    site_packages: str,
    expected_root: str,
    foreign_root: str,
    expect_tree: str,
) -> str:
    """Child script: force editable-competitor shape after site init.

    CI/same-tree editable already maps ``minni`` → this tree before the child
    runs. A late ``addsitedir`` alone does not prove PYTHONPATH won. After site
    init we:

    1. Drop this tree's ``src`` from ``sys.path`` (same-tree editable path)
    2. Put foreign first as a foreign editable would
    3. Re-introduce this tree *only* via env ``PYTHONPATH`` entries

    Then ``import minni``: with ``PYTHONPATH=src`` this tree must win; with
    PYTHONPATH absent foreign must win.
    """
    return textwrap.dedent(
        f"""
        import os, site, sys

        this_src = {this_src!r}
        foreign_src = {foreign_src!r}
        site_packages = {site_packages!r}
        expected_root = {expected_root!r}
        foreign_root = {foreign_root!r}
        expect_tree = {expect_tree!r}

        # Site-packages/.pth editable competitor (pip editable shape).
        site.addsitedir(site_packages)

        def _real(p):
            return os.path.realpath(p) if p else p

        this_real = _real(this_src)
        foreign_real = _real(foreign_src)

        # 1. Drop this tree's src wherever the editable / prior path put it.
        sys.path = [
            p for p in sys.path
            if p and _real(p) != this_real
        ]
        # 2. Foreign first — real competitor when PYTHONPATH lacks this tree.
        sys.path = [p for p in sys.path if p and _real(p) != foreign_real]
        sys.path.insert(0, foreign_src)

        # 3. Re-introduce this tree only via env PYTHONPATH (Make shape).
        #    Python prepends PYTHONPATH at startup; we re-apply after the forced
        #    competitor reshape so a missing env cannot false-green on editable.
        pp = os.environ.get("PYTHONPATH", "") or ""
        entries = []
        for raw in pp.split(os.pathsep):
            if not raw:
                continue
            abs_entry = raw if os.path.isabs(raw) else os.path.realpath(
                os.path.join(os.getcwd(), raw)
            )
            entries.append(abs_entry)
        for path in reversed(entries):
            sys.path = [p for p in sys.path if p and _real(p) != _real(path)]
            sys.path.insert(0, path)

        import minni
        path = os.path.realpath(minni.__file__)
        tree = getattr(minni, "TREE", None)

        if expect_tree == "this":
            assert path.startswith(expected_root), (path, expected_root, sys.path[:8])
            assert not path.startswith(foreign_root), path
            assert tree != "foreign", tree
            print("ok", path)
        elif expect_tree == "foreign":
            assert path.startswith(foreign_root), (path, foreign_root, sys.path[:8])
            assert tree == "foreign", tree
            print("foreign-ok", path)
        else:
            raise SystemExit(f"bad expect_tree={{expect_tree!r}}")
        """
    )


def test_pythonpath_src_wins_over_site_packages_editable(tmp_path):
    """Make-shape PYTHONPATH=src beats site-packages/.pth editable competitor.

    Models the real #258 competitor: foreign ``minni`` via a ``.pth`` processed
    by ``site.addsitedir`` (pip editable). Under same-tree editable (CI after
    ``make setup``) a late addsitedir alone is not enough — the child drops
    this tree's src from ``sys.path`` and only re-adds it via env PYTHONPATH,
    so deleting ``PYTHONPATH=src`` makes foreign win (no false-green).
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
    this_src_s = str(REPO_SRC.resolve())
    foreign_src_s = str(foreign_src.resolve())
    site_packages_s = str(site_packages.resolve())

    common = dict(
        this_src=this_src_s,
        foreign_src=foreign_src_s,
        site_packages=site_packages_s,
        expected_root=expected_root,
        foreign_root=foreign_root,
    )

    # Make shape: relative PYTHONPATH=src only, cwd=REPO_ROOT.
    env_make = os.environ.copy()
    env_make["PYTHONPATH"] = "src"
    env_make.pop("MINNI_HOME", None)
    make_shape = _competitor_probe_script(**common, expect_tree="this")
    proc = subprocess.run(
        [sys.executable, "-c", make_shape],
        env=env_make,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # "ok" is a substring of "foreign-ok"; require the this-arm token only.
    assert "foreign-ok" not in proc.stdout and "ok" in proc.stdout

    # Without PYTHONPATH, the forced competitor must win (proves the win arm
    # actually needed PYTHONPATH — not a same-tree editable false-green).
    env_bare = os.environ.copy()
    env_bare.pop("PYTHONPATH", None)
    env_bare.pop("MINNI_HOME", None)
    baseline = _competitor_probe_script(**common, expect_tree="foreign")
    proc_f = subprocess.run(
        [sys.executable, "-c", baseline],
        env=env_bare,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_f.returncode == 0, proc_f.stdout + proc_f.stderr
    assert "foreign-ok" in proc_f.stdout

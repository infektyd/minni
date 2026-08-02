"""#258 — pytest must import minni from *this* tree, not an editable main.

When agents run pytest from a git worktree whose venv is the primary checkout's
editable install, ``import minni`` used to resolve to main. That made a broken
branch report green. conftest.py prepends this tree's ``src/`` and asserts;
this test is the explicit, greppable regression for that contract.
"""

from __future__ import annotations

import os
from pathlib import Path

import minni


def test_minni_package_lives_under_this_repo_src():
    repo_root = Path(__file__).resolve().parent.parent
    expected = (repo_root / "src").resolve()
    package_file = Path(minni.__file__).resolve()
    assert str(package_file).startswith(str(expected) + os.sep), (
        f"minni.__file__={package_file} is not under {expected} — "
        "worktree import resolution is broken (#258)"
    )


def test_this_tree_src_precedes_other_src_entries():
    """This tree's ``src/`` must appear on ``sys.path`` ahead of any other
    checkout's ``src/minni`` — pytest may put ``tests/`` at index 0; that is
    fine as long as it does not supply a competing ``minni`` package.
    """
    import sys

    repo_src = os.path.realpath(Path(__file__).resolve().parent.parent / "src")
    resolved = [os.path.realpath(p) if p else p for p in sys.path]
    assert repo_src in resolved, f"this tree's src/ missing from sys.path: {sys.path[:8]!r}"
    our_idx = resolved.index(repo_src)
    # Any earlier entry that itself contains minni/ would win over us.
    for earlier in resolved[:our_idx]:
        if not earlier or not os.path.isdir(earlier):
            continue
        competitor = os.path.join(earlier, "minni", "__init__.py")
        assert not os.path.isfile(competitor), (
            f"sys.path entry {earlier!r} (before this tree's src/) also has "
            "minni/ and would steal the import"
        )

"""Private-path write guard tests (§5.1, s2(a)).

The snapshot freezer and raw-label writers MUST refuse to write outside the
designated private/gitignored area unless an explicit allow_public flag is set.
Also verifies the _private/ tree is actually gitignored (machine-checked).
"""

import subprocess

import pytest

from membench import paths
from membench.paths import (
    PRIVATE_ROOT,
    PrivatePathError,
    assert_private_path,
    is_within,
)
from membench.snapshot import freeze_snapshot


def test_assert_private_rejects_outside(tmp_path):
    with pytest.raises(PrivatePathError):
        assert_private_path(tmp_path / "x")


def test_assert_private_allows_inside():
    p = assert_private_path(PRIVATE_ROOT / "corpus_real" / "snap")
    assert is_within(p, PRIVATE_ROOT)


def test_allow_public_bypasses_guard(tmp_path):
    # allow_public is the ONLY escape hatch; returns the path without raising.
    assert assert_private_path(tmp_path / "x", allow_public=True) == tmp_path / "x"


def test_symlink_cannot_smuggle_private_writer_out(tmp_path, monkeypatch):
    """A symlink whose realpath escapes PRIVATE_ROOT is NOT treated as private."""
    fake_private = tmp_path / "_private" / "membench"
    fake_private.mkdir(parents=True)
    monkeypatch.setattr(paths, "PRIVATE_ROOT", fake_private)
    outside = tmp_path / "public_dir"
    outside.mkdir()
    # A link inside private that points OUTSIDE it must be rejected.
    escape = fake_private / "escape"
    escape.symlink_to(outside)
    with pytest.raises(PrivatePathError):
        assert_private_path(escape / "leak.md")


def test_freezer_refuses_non_private(tmp_path, fixture_dir):
    with pytest.raises(PrivatePathError):
        freeze_snapshot(fixture_dir, tmp_path / "public_snap")


def test_private_tree_is_gitignored():
    """git check-ignore must report representative _private/ paths as ignored."""
    rel_paths = [
        "_private/membench/corpus_real/x.md",
        "_private/membench/gold_real.jsonl",
        "_private/membench/gold_drafts.jsonl",
    ]
    result = subprocess.run(
        ["git", "check-ignore", *rel_paths],
        cwd=str(paths.REPO_ROOT),
        capture_output=True,
        text=True,
    )
    ignored = set(result.stdout.split())
    for rp in rel_paths:
        assert rp in ignored, f"{rp} is NOT gitignored (private data could leak)"

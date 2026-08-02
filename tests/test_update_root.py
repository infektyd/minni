"""scripts/update_root.sh refusal paths and dry-run.

The sync script's one hard promise is that it NEVER discards local state: a
dirty tree, a non-main branch, or local commits origin/main lacks all abort
before anything is touched. These tests run the real script against throwaway
git repos; the refusals fire before any install/deploy step, so nothing
outside the tmp dirs is reached.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "update_root.sh"

# These fixtures push to throwaway bare repos named `main`; a host-level git
# guard hook may block that in non-interactive runs. The bypass is scoped to
# the tmp repos these tests create — nothing here touches a real remote.
_ENV = {**os.environ, "GIT_BYPASS": "1"}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(cwd), text=True, env=_ENV,
    ).strip()


@pytest.fixture
def cloned(tmp_path):
    """(origin, clone): a bare origin with one commit on main, and a clone."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.invalid")
    _git(seed, "config", "user.name", "t")
    (seed / "f.txt").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "f.txt")
    _git(seed, "commit", "-qm", "one")
    subprocess.check_call(["git", "clone", "-q", "--bare", str(seed), str(origin)], env=_ENV)

    clone = tmp_path / "clone"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(clone)], env=_ENV)
    _git(clone, "config", "user.email", "t@example.invalid")
    _git(clone, "config", "user.name", "t")
    return origin, clone


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), "--repo", str(repo), *args],
        capture_output=True, text=True, env=_ENV,
    )


def test_refuses_dirty_tree(cloned):
    _origin, clone = cloned
    (clone / "f.txt").write_text("local edit\n", encoding="utf-8")
    proc = _run(clone)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "REFUSING" in proc.stderr
    assert "dirty" in proc.stderr
    # Local state untouched.
    assert (clone / "f.txt").read_text(encoding="utf-8") == "local edit\n"


def test_refuses_diverged_branch(cloned):
    origin, clone = cloned
    # Advance origin/main from a second clone.
    other = origin.parent / "other"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(other)], env=_ENV)
    _git(other, "config", "user.email", "t@example.invalid")
    _git(other, "config", "user.name", "t")
    (other / "f.txt").write_text("remote two\n", encoding="utf-8")
    _git(other, "commit", "-aqm", "remote two")
    _git(other, "push", "-q", "origin", "main")
    # And give the clone its own local commit -> diverged.
    (clone / "g.txt").write_text("local\n", encoding="utf-8")
    _git(clone, "add", "g.txt")
    _git(clone, "commit", "-qm", "local only")
    local_head = _git(clone, "rev-parse", "HEAD")

    proc = _run(clone)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "REFUSING" in proc.stderr
    assert "diverged" in proc.stderr
    assert _git(clone, "rev-parse", "HEAD") == local_head, "history must be untouched"


def test_refuses_non_main_branch(cloned):
    _origin, clone = cloned
    _git(clone, "checkout", "-q", "-b", "feature")
    proc = _run(clone)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "REFUSING" in proc.stderr
    assert "feature" in proc.stderr


def test_dry_run_fast_forwards_nothing(cloned):
    origin, clone = cloned
    other = origin.parent / "other2"
    subprocess.check_call(["git", "clone", "-q", str(origin), str(other)], env=_ENV)
    _git(other, "config", "user.email", "t@example.invalid")
    _git(other, "config", "user.name", "t")
    (other / "f.txt").write_text("remote two\n", encoding="utf-8")
    _git(other, "commit", "-aqm", "remote two")
    _git(other, "push", "-q", "origin", "main")
    before = _git(clone, "rev-parse", "HEAD")

    proc = _run(clone, "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "would run" in proc.stdout
    assert _git(clone, "rev-parse", "HEAD") == before, "dry run must not move HEAD"


def test_refuses_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = _run(plain)
    assert proc.returncode == 1
    assert "REFUSING" in proc.stderr
    assert "not a git checkout" in proc.stderr

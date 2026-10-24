"""Tests for scripts.recall_replay.private_root (Rev 5 §3, §8.1)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.recall_replay.private_root import (
    PrivateRootError,
    assert_under,
    resolve_private_root,
)


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    (repo / ".gitignore").write_text("_private/\nignored_dir/\n", encoding="utf-8")
    tracked = repo / "tracked_dir"
    tracked.mkdir()
    (tracked / "tracked.txt").write_text("tracked content\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".gitignore", "tracked_dir/tracked.txt"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"], check=True)


def test_refuses_relative_empty_or_control_char_env_root() -> None:
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": ""})
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": "   "})
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": "relative/private/root"})
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": "/tmp/bad\x07root"})


def test_assert_under_enforces_containment(tmp_path: Path) -> None:
    priv = tmp_path / "priv_root"
    priv.mkdir()

    target = assert_under("corpus/events.jsonl", root=priv)
    assert target == Path(os.path.realpath(priv)) / "corpus" / "events.jsonl"

    with pytest.raises(PrivateRootError):
        assert_under("../escape.jsonl", root=priv)

    with pytest.raises(PrivateRootError):
        assert_under("corpus/../../escape.jsonl", root=priv)

    with pytest.raises(PrivateRootError):
        assert_under(tmp_path / "outside.jsonl", root=priv)

    with pytest.raises(PrivateRootError):
        assert_under("", root=priv)


def test_refuses_symlink_root_and_symlink_children(tmp_path: Path) -> None:
    real_dir = tmp_path / "real_priv"
    real_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.json"
    outside_file.write_text("{}", encoding="utf-8")

    # Root itself is a symlink -> refused
    symlink_root = tmp_path / "symlink_priv"
    symlink_root.symlink_to(real_dir)
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": str(symlink_root)})

    # Ancestor component under tmp_path is a symlink -> refused
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": str(symlink_root / "nested")})

    # Child symlink pointing outside -> refused
    escape_link = real_dir / "escape.json"
    escape_link.symlink_to(outside_file)
    with pytest.raises(PrivateRootError):
        assert_under("escape.json", root=real_dir)

    # Intermediate directory symlink pointing outside -> refused
    dir_link = real_dir / "linked_sub"
    dir_link.symlink_to(outside_dir)
    with pytest.raises(PrivateRootError):
        assert_under("linked_sub/file.json", root=real_dir)

    # Child symlink pointing inside -> still refused (no symlinks under private root)
    inside_file = real_dir / "inside.json"
    inside_file.write_text("{}", encoding="utf-8")
    inside_link = real_dir / "inside_link.json"
    inside_link.symlink_to(inside_file)
    with pytest.raises(PrivateRootError):
        assert_under("inside_link.json", root=real_dir)


def test_refuses_non_directory_root_and_fifo_child(tmp_path: Path) -> None:
    file_as_root = tmp_path / "not_a_dir"
    file_as_root.write_text("data", encoding="utf-8")
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": str(file_as_root)})

    priv = tmp_path / "priv"
    priv.mkdir()
    fifo_path = priv / "pipe.fifo"
    os.mkfifo(fifo_path)
    with pytest.raises(PrivateRootError):
        assert_under("pipe.fifo", root=priv)


def test_refuses_tracked_directory_repo_root_and_unignored_subdir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)

    # Repo root itself -> refused
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": str(repo)})

    # Inside a tracked directory -> refused
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": str(repo / "tracked_dir")})
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": str(repo / "tracked_dir" / "sub")})

    # Inside an unignored directory in the repo -> refused
    with pytest.raises(PrivateRootError):
        resolve_private_root(env={"MINNI_PRIVATE_ROOT": str(repo / "unignored_sub" / "priv")})

    # Inside _private/recall-to-use -> allowed
    resolved = resolve_private_root(env={"MINNI_PRIVATE_ROOT": str(repo / "_private" / "recall-to-use")})
    assert resolved == Path(os.path.realpath(repo)) / "_private" / "recall-to-use"


def test_resolves_identical_main_worktree_from_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "main_repo"
    _init_git_repo(repo)

    wt = tmp_path / "linked_wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "slice-wt", str(wt)],
        check=True,
    )

    from_main = resolve_private_root(cwd=repo, env={})
    from_wt = resolve_private_root(cwd=wt, env={})
    expected = Path(os.path.realpath(repo)) / "_private" / "recall-to-use"
    assert from_main == expected
    assert from_wt == expected


def test_cli_verify_exit_codes(tmp_path: Path) -> None:
    priv = tmp_path / "priv_cli"
    priv.mkdir()
    other = tmp_path / "other_cli"
    other.mkdir()

    env = {**os.environ, "MINNI_PRIVATE_ROOT": str(priv)}
    ok_proc = subprocess.run(
        [sys.executable, "-m", "scripts.recall_replay.private_root", "--verify", str(priv)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ok_proc.returncode == 0

    bad_proc = subprocess.run(
        [sys.executable, "-m", "scripts.recall_replay.private_root", "--verify", str(other)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_proc.returncode == 2

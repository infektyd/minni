"""Private-root resolution and containment guard (Rev 5 §3, §8.1).

All campaign transcripts, corpus rows, labels, probe notes, and replay reports
live under one private root outside every slice worktree:
  - ``MINNI_PRIVATE_ROOT`` if set in the environment, else
  - ``<main worktree>/_private/recall-to-use/``

Invariants enforced here:
  - No empty, relative, or control-character paths in ``MINNI_PRIVATE_ROOT``.
  - Symlink components in the private root or beneath it are refused via ``lstat``.
  - A private root at a git repository root, inside a tracked directory, or
    inside an unignored repository directory is refused.
  - Both the main checkout and any linked ``git worktree`` resolve the exact
    same ``<main worktree>/_private/recall-to-use`` root via ``--git-common-dir``.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import os
from pathlib import Path
import stat
import subprocess
import sys


class PrivateRootError(ValueError):
    """Raised when a private root or target path violates containment or safety rules."""


_SYSTEM_SYMLINK_PREFIXES = frozenset({Path("/tmp"), Path("/var"), Path("/etc")})


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _find_main_worktree(start_dir: Path) -> Path:
    try:
        start_real = Path(os.path.realpath(start_dir))
    except OSError as exc:
        raise PrivateRootError(f"cannot resolve cwd: {exc}") from exc

    proc = subprocess.run(
        [
            "git",
            "-C",
            str(start_real),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise PrivateRootError("cannot determine git common directory from cwd")

    common_git = Path(os.path.realpath(proc.stdout.strip()))
    if common_git.name == ".git":
        return Path(os.path.realpath(common_git.parent))
    return Path(os.path.realpath(common_git.parent))


def _check_no_symlinks_on_root(candidate: Path) -> None:
    prefixes = [*reversed(candidate.parents), candidate]
    for prefix in prefixes:
        if prefix in _SYSTEM_SYMLINK_PREFIXES:
            continue
        try:
            st = os.lstat(prefix)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PrivateRootError(f"cannot stat private root component {prefix}: {exc}") from exc

        if stat.S_ISLNK(st.st_mode):
            raise PrivateRootError(f"symlink component refused in private root: {prefix}")
        if not stat.S_ISDIR(st.st_mode):
            raise PrivateRootError(f"private root component is not a directory: {prefix}")


def _check_not_tracked_in_git(real_root: Path) -> None:
    existing = real_root
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent

    if not existing.exists():
        return

    top_proc = subprocess.run(
        ["git", "-C", str(existing), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if top_proc.returncode != 0 or not top_proc.stdout.strip():
        return

    wt_root = Path(os.path.realpath(top_proc.stdout.strip()))
    if real_root == wt_root:
        raise PrivateRootError("private root cannot be the repository root")

    if wt_root not in real_root.parents:
        return

    rel = real_root.relative_to(wt_root)
    for depth in range(1, len(rel.parts) + 1):
        prefix_rel = Path(*rel.parts[:depth]).as_posix()
        ls_proc = subprocess.run(
            ["git", "-C", str(wt_root), "ls-files", "--", prefix_rel],
            capture_output=True,
            text=True,
            check=False,
        )
        if ls_proc.returncode == 0 and ls_proc.stdout.strip():
            raise PrivateRootError(
                f"private root is inside or contains a tracked directory: {prefix_rel}"
            )

    if rel.parts and rel.parts[0] != "_private":
        ign_proc = subprocess.run(
            ["git", "-C", str(wt_root), "check-ignore", "-q", "--no-index", rel.as_posix()],
            capture_output=True,
            text=True,
            check=False,
        )
        if ign_proc.returncode != 0:
            raise PrivateRootError(
                f"private root inside repository is not gitignored: {rel.as_posix()}"
            )


def resolve_private_root(
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Resolve and validate the canonical private root directory."""
    eff_env = os.environ if env is None else env
    if "MINNI_PRIVATE_ROOT" in eff_env:
        raw = eff_env["MINNI_PRIVATE_ROOT"]
        if not isinstance(raw, str) or not raw.strip() or _has_control_chars(raw):
            raise PrivateRootError("MINNI_PRIVATE_ROOT must be a non-empty path without control characters")
        candidate = Path(raw)
        if not candidate.is_absolute():
            raise PrivateRootError("MINNI_PRIVATE_ROOT must be an absolute path")
    else:
        start_dir = Path.cwd() if cwd is None else Path(cwd)
        main_wt = _find_main_worktree(start_dir)
        candidate = main_wt / "_private" / "recall-to-use"

    _check_no_symlinks_on_root(candidate)
    real_root = Path(os.path.realpath(candidate))
    _check_no_symlinks_on_root(real_root)
    _check_not_tracked_in_git(real_root)
    return real_root


def assert_under(
    path: str | Path,
    root: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Validate that ``path`` resolves strictly inside the private root without symlinks."""
    raw_str = str(path) if path is not None else ""
    if not raw_str or not raw_str.strip() or _has_control_chars(raw_str):
        raise PrivateRootError("target path must be non-empty and free of control characters")

    if root is not None:
        base_env = dict(os.environ if env is None else env)
        base_env["MINNI_PRIVATE_ROOT"] = str(root)
        root_real = resolve_private_root(cwd=cwd, env=base_env)
    else:
        root_real = resolve_private_root(cwd=cwd, env=env)

    p = Path(raw_str)
    if p.is_absolute():
        # Handle macOS /tmp or /var prefix when matching root_real under /private
        p_str = str(p)
        for sys_prefix in ("/tmp/", "/var/", "/etc/"):
            priv_prefix = f"/private{sys_prefix}"
            if p_str.startswith(sys_prefix) and str(root_real).startswith(priv_prefix):
                p = Path(f"/private{p_str}")
                break
        candidate = Path(os.path.normpath(str(p)))
    else:
        candidate = Path(os.path.normpath(str(root_real / p)))

    if candidate != root_real and root_real not in candidate.parents:
        raise PrivateRootError(f"target path escapes private root: {path}")

    rel_parts = candidate.relative_to(root_real).parts
    cur = root_real
    for idx, part in enumerate(rel_parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PrivateRootError(f"cannot stat {cur}: {exc}") from exc

        if stat.S_ISLNK(st.st_mode):
            raise PrivateRootError(f"symlink escape under private root: {cur}")

        is_last = idx == len(rel_parts) - 1
        if is_last:
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                raise PrivateRootError(f"non-regular file under private root: {cur}")
        elif not stat.S_ISDIR(st.st_mode):
            raise PrivateRootError(f"non-directory parent under private root: {cur}")

    real_target = Path(os.path.realpath(candidate))
    if real_target != root_real and root_real not in real_target.parents:
        raise PrivateRootError(f"realpath of target escapes private root: {path}")
    return real_target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve or verify Minni private root.")
    parser.add_argument("--verify", metavar="DIR", help="Verify DIR matches the canonical private root.")
    parser.add_argument("--assert-under", metavar="PATH", help="Verify PATH is strictly under the private root.")
    args = parser.parse_args(argv)

    try:
        root = resolve_private_root()
        if args.verify is not None:
            verify_real = Path(os.path.realpath(args.verify))
            if verify_real != root:
                sys.stderr.write(f"private root mismatch: {verify_real} != {root}\n")
                return 2
            print(str(root))
            return 0
        if args.assert_under is not None:
            resolved = assert_under(args.assert_under, root=root)
            print(str(resolved))
            return 0
        print(str(root))
        return 0
    except PrivateRootError as exc:
        sys.stderr.write(f"PrivateRootError: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

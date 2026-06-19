"""Private-path write guard + designated private/gitignored locations (§5.1).

The frozen real-vault snapshot and the operator's raw approved labels MUST stay
OUT of public git: they live under the already-gitignored ``_private/`` tree.
This module is the single chokepoint every PRIVATE writer (snapshot freezer,
raw-label writer) routes through:

- :data:`PRIVATE_ROOT` — the repo-root ``_private/membench/`` tree (gitignored).
- :func:`assert_private_path` — RAISES :class:`PrivatePathError` if asked to
  write a path that does not resolve under :data:`PRIVATE_ROOT`, unless the
  caller passes an explicit ``allow_public=True`` (the only escape hatch, used by
  the PUBLIC synthetic-fixture tooling).

The guard uses realpath-containment so a symlink or ``..`` cannot smuggle a
private writer's output to a public location.
"""

from __future__ import annotations

import os
from pathlib import Path

# Repo root = three parents up from this file: bench/membench/paths.py -> repo.
REPO_ROOT = Path(__file__).resolve().parents[2]
# The designated private, gitignored area (root .gitignore has `_private/`).
PRIVATE_ROOT = REPO_ROOT / "_private" / "membench"

# Canonical private sub-locations (spec §11 layout).
PRIVATE_CORPUS_SNAPSHOT = PRIVATE_ROOT / "corpus_real"
PRIVATE_GOLD_LABELS = PRIVATE_ROOT / "gold_real.jsonl"
PRIVATE_DRAFTS = PRIVATE_ROOT / "gold_drafts.jsonl"


class PrivatePathError(RuntimeError):
    """Raised when a private writer is pointed outside the private area (§5.1)."""


def is_within(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """True iff ``realpath(path)`` is ``root`` or sits under it (containment).

    Resolves both via realpath so a symlink/`..` cannot escape the containment.
    Does not require either path to exist.
    """
    target = os.path.realpath(path)
    base = os.path.realpath(root)
    return target == base or target.startswith(base + os.sep)


def assert_private_path(
    path: str | os.PathLike[str], *, allow_public: bool = False
) -> Path:
    """Guard a private write target. RAISE unless private or explicitly public.

    Returns the path (as :class:`pathlib.Path`) when allowed, so callers can
    write inline: ``p = assert_private_path(dest)``.

    ``allow_public=True`` is the ONLY way to write outside the private area; it
    is reserved for the public synthetic-fixture tooling and is never the default
    for real-data writers. The guard is checked BEFORE any byte is written.
    """
    path = Path(path)
    if allow_public:
        return path
    if not is_within(path, PRIVATE_ROOT):
        raise PrivatePathError(
            "refusing to write outside the private area without allow_public=True\n"
            f"  target:       {os.path.realpath(path)}\n"
            f"  private_root: {os.path.realpath(PRIVATE_ROOT)}\n"
            "  (private vault/label bytes must never reach a public-git path; "
            "pass allow_public=True only for PUBLIC synthetic fixtures)"
        )
    return path

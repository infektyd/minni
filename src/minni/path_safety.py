"""Path containment helpers for vault/wiki indexing (symlink escape guard)."""

from __future__ import annotations

from pathlib import Path


def path_within_root(path: Path | str, root: Path | str) -> bool:
    """Return True when resolved ``path`` stays under resolved ``root``."""
    try:
        resolved = Path(path).resolve()
        root_resolved = Path(root).resolve()
        return resolved.is_relative_to(root_resolved)
    except Exception:
        return False

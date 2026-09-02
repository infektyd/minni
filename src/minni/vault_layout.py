"""Seed the human vault contract under an existing agent vault root.

Per-agent vaults hold inbox/identity (wiki dirs, log.md, index.md), not
the learning corpus. Live: 1075 hermes-stamped daemon learnings sit in
shared ``~/.minni/learnings`` and AFM wiki ``~/.minni/vault``; hermes-vault
being ``.index``-only is a missing inbox/identity layout, not a hole those
learnings should fill. The plugin's ``ensureVault`` already creates this
layout on first hook; CLI/socket principals never took that path. Seed
only when the root already exists — do not invent a vault from a missing
path. Do not route learnings into hermes-vault.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Union

_DIR_MODE = 0o700
_FILE_MODE = 0o600

CONTRACT_DIRS = (
    "wiki",
    "wiki/entities",
    "wiki/concepts",
    "wiki/decisions",
    "wiki/syntheses",
    "wiki/sessions",
    "wiki/procedures",
    "wiki/artifacts",
    "wiki/handoffs",
    "schema",
    "logs",
    "inbox",
    "outbox",
)

_LOG_HEADER = "# Minni Log\n\n"
_INDEX_HEADER = "# Minni Index\n\n"


def ensure_agent_vault(vault: Union[str, Path]) -> List[str]:
    """Create missing contract dirs/files under an existing vault directory.

    Returns relative paths that were created. Empty list if ``vault`` does
    not exist, or if everything was already present. Raises
    ``NotADirectoryError`` / ``IsADirectoryError`` / ``OSError`` when the
    root or a contract path exists as the wrong type. A file (or
    symlink) at the vault root is a conflict, not a missing vault.
    A directory symlink is refused so wiki/inbox/log.md are not planted
    in a shop/peer tree (``is_dir()`` follows; ``resolve()`` would then
    become the containment base).
    """
    root = Path(vault).expanduser()
    if root.is_symlink():
        raise OSError(f"refusing symlinked vault root: {root}")
    if root.exists() and not root.is_dir():
        raise NotADirectoryError(
            f"vault root exists and is not a directory: {root}"
        )
    if not root.is_dir():
        return []
    try:
        # Unresolved vault path is the containment base. After refusing a
        # last-component symlink, resolve() does not retarget into shop/peer.
        root_real = root.resolve()
    except OSError as exc:
        raise OSError(f"vault root is not resolvable: {root}") from exc
    created: List[str] = []
    for rel in CONTRACT_DIRS:
        dest = root / rel
        _reject_symlink_or_escape(dest, root_real, rel)
        if dest.exists():
            if not dest.is_dir():
                raise NotADirectoryError(
                    f"vault contract {rel!r} exists and is not a directory: {dest}"
                )
            continue
        dest.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        os.chmod(dest, _DIR_MODE)
        created.append(rel)
    for rel, header in (("log.md", _LOG_HEADER), ("index.md", _INDEX_HEADER)):
        dest = root / rel
        _reject_symlink_or_escape(dest, root_real, rel)
        if dest.exists():
            _require_regular_contract_file(dest, rel)
            continue
        if _seed_exclusive_file(dest, header):
            created.append(rel)
        else:
            _require_regular_contract_file(dest, rel)
    return created


def _reject_symlink_or_escape(dest: Path, root_real: Path, rel: str) -> None:
    if dest.is_symlink():
        raise OSError(
            f"vault contract {rel!r} is a symlink; refusing to seed through it: {dest}"
        )
    try:
        dest_real = dest.resolve()
    except (OSError, RuntimeError) as exc:
        raise OSError(f"vault contract {rel!r} is not resolvable: {dest}") from exc
    if not dest_real.is_relative_to(root_real):
        raise OSError(
            f"vault contract {rel!r} resolves outside vault root: {dest}"
        )


def _seed_exclusive_file(dest: Path, header: str) -> bool:
    """O_EXCL create at 0600. Do not write at offset 0 if another writer appended."""
    if dest.is_symlink():
        raise OSError(f"refusing to seed through symlink: {dest}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(dest, flags, _FILE_MODE)
    except FileExistsError:
        return False
    try:
        os.fchmod(fd, _FILE_MODE)
        if os.fstat(fd).st_size > 0:
            return False
        os.write(fd, header.encode("utf-8"))
        return True
    finally:
        os.close(fd)


def _append_regular_file(dest: Path, text: str) -> None:
    if dest.is_symlink():
        raise OSError(f"refusing to append through symlink: {dest}")
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(text)


def _resolved_vault_root(root: Path) -> Path:
    if root.is_symlink():
        raise OSError(f"refusing symlinked vault root: {root}")
    try:
        return root.resolve()
    except OSError as exc:
        raise OSError(f"vault root is not resolvable: {root}") from exc


def _require_regular_contract_file(dest: Path, rel: str) -> None:
    if dest.is_symlink():
        raise OSError(
            f"vault contract {rel!r} is a symlink; refusing to seed through it: {dest}"
        )
    if dest.is_dir():
        raise IsADirectoryError(
            f"vault contract {rel!r} exists and is a directory: {dest}"
        )
    if not dest.is_file():
        raise OSError(
            f"vault contract {rel!r} exists and is not a regular file: {dest}"
        )

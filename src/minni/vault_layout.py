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

from pathlib import Path
from typing import List, Union

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
    symlink-to-file) at the vault root is a conflict, not a missing vault.
    """
    root = Path(vault).expanduser()
    if root.exists() and not root.is_dir():
        raise NotADirectoryError(
            f"vault root exists and is not a directory: {root}"
        )
    if not root.is_dir():
        return []
    created: List[str] = []
    for rel in CONTRACT_DIRS:
        dest = root / rel
        if dest.exists():
            if not dest.is_dir():
                raise NotADirectoryError(
                    f"vault contract {rel!r} exists and is not a directory: {dest}"
                )
            continue
        dest.mkdir(parents=True, exist_ok=True)
        created.append(rel)
    for rel, header in (("log.md", _LOG_HEADER), ("index.md", _INDEX_HEADER)):
        dest = root / rel
        if dest.exists():
            _require_regular_contract_file(dest, rel)
            continue
        try:
            # Exclusive create: exists()+write_text races plugin ensureVault/
            # recordAudit and can truncate append-only log.md / index.md.
            with dest.open("x", encoding="utf-8") as fh:
                fh.write(header)
        except FileExistsError:
            _require_regular_contract_file(dest, rel)
            continue
        created.append(rel)
    return created


def _require_regular_contract_file(dest: Path, rel: str) -> None:
    if dest.is_dir():
        raise IsADirectoryError(
            f"vault contract {rel!r} exists and is a directory: {dest}"
        )
    if not dest.is_file():
        raise OSError(
            f"vault contract {rel!r} exists and is not a regular file: {dest}"
        )

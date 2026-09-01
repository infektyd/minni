"""Seed the human vault contract under an existing agent vault root.

Learnings can land in SQLite with no wiki/log.md (live: hermes-vault is
``.index`` only, 1075 daemon learnings, zero documents). The plugin's
``ensureVault`` already creates this layout on first hook; CLI/socket
principals never took that path. Seed only when the root already exists —
do not invent a vault from a missing path.
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

    Returns relative paths that were created. Empty list if ``vault`` is not
    already a directory, or if everything was already present.
    """
    root = Path(vault)
    if not root.is_dir():
        return []
    created: List[str] = []
    for rel in CONTRACT_DIRS:
        dest = root / rel
        if not dest.exists():
            dest.mkdir(parents=True, exist_ok=True)
            created.append(rel)
    for rel, header in (("log.md", _LOG_HEADER), ("index.md", _INDEX_HEADER)):
        dest = root / rel
        if not dest.exists():
            dest.write_text(header, encoding="utf-8")
            created.append(rel)
    return created

"""Hermes vault/wire slice (usage-audit R2).

Live machine: 1075 learnings with agent_id=hermes (2026-04-13..2026-05-17)
live in shared ``~/.minni/learnings`` and AFM wiki ``~/.minni/vault``, not
hermes-vault wiki. hermes-vault being `.index`-only is a missing
inbox/identity layout (wiki dirs, log.md, index.md). Principal JSON names
the vault; docs/contracts/VAULT.md documents it. hook-platform.wireFor
used to silently render the Claude Code shape for hermes; it now keeps
the id and refuses inject/note.

This slice:
- seeds inbox/identity contract files when a principal's vault root
  already exists as a directory
- does not invent a vault from nothing
- does not route learnings into hermes-vault
"""

from __future__ import annotations

import json
from pathlib import Path

from minni.principal import _principal_from_raw
from minni.vault_layout import ensure_agent_vault


def test_ensure_agent_vault_seeds_empty_index_only_dir(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / ".index").mkdir()
    created = ensure_agent_vault(vault)
    assert (vault / "log.md").is_file()
    assert (vault / "index.md").is_file()
    assert (vault / "wiki" / "sessions").is_dir()
    assert "log.md" in created
    # idempotent
    assert ensure_agent_vault(vault) == []


def test_ensure_agent_vault_does_not_create_missing_root(tmp_path):
    missing = tmp_path / "no-such-vault"
    assert ensure_agent_vault(missing) == []
    assert not missing.exists()


def test_principal_load_seeds_existing_vault_root(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    raw = {
        "agent_id": "hermes",
        "workspace_id": "workspace-hermes",
        "capabilities": ["search", "learn"],
        "allowed_vault_roots": [str(vault)],
    }
    p = _principal_from_raw(raw, transport="uds", principals_dir=tmp_path)
    assert p.agent_id == "hermes"
    assert (vault / "log.md").is_file()
    assert (vault / "wiki").is_dir()

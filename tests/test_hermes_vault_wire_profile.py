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

import pytest

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


def _acl_principal(agent_id, roots, capabilities=("search", "learn")):
    return {
        "agent_id": agent_id,
        "workspace_id": "default",
        "capabilities": list(capabilities),
        "allowed_vault_roots": [str(r) for r in roots],
    }


def test_principal_load_does_not_seed_shared_or_foreign_acl_roots(tmp_path):
    """allowed_vault_roots is a read ACL. Seed only <id>-vault / dashless alias."""
    own = tmp_path / "claudecode-vault"
    shared = tmp_path / "shared"
    foreign = tmp_path / "codex-vault"
    own.mkdir()
    shared.mkdir()
    foreign.mkdir()
    (shared / "shop.md").write_text("existing shop file\n", encoding="utf-8")

    p = _principal_from_raw(
        _acl_principal("claude-code", [own, shared, foreign]),
        transport="uds",
        principals_dir=tmp_path,
    )
    assert p.agent_id == "claude-code"
    assert (own / "log.md").is_file()
    assert (own / "wiki" / "sessions").is_dir()
    assert not (shared / "log.md").exists()
    assert not (shared / "index.md").exists()
    assert not (shared / "wiki").exists()
    assert not (shared / "inbox").exists()
    assert not (shared / "outbox").exists()
    assert (shared / "shop.md").read_text(encoding="utf-8") == "existing shop file\n"
    assert not (foreign / "log.md").exists()
    assert not (foreign / "wiki").exists()
    assert not (foreign / "inbox").exists()


def test_principal_load_does_not_seed_broad_home_acl_root(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _principal_from_raw(
        _acl_principal("main", [home], capabilities=["*"]),
        transport="uds",
        principals_dir=tmp_path,
    )
    assert not (home / "wiki").exists()
    assert not (home / "log.md").exists()
    assert not (home / "inbox").exists()
    assert list(home.iterdir()) == []


def test_ensure_agent_vault_raises_on_log_md_directory(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / "log.md").mkdir()
    with pytest.raises(OSError):
        ensure_agent_vault(vault)
    assert not (vault / "index.md").exists()


def test_ensure_agent_vault_raises_on_wiki_file(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / "wiki").write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(OSError):
        ensure_agent_vault(vault)


def test_principal_load_does_not_swallow_vault_seed_failure(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / "wiki").write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(OSError):
        _principal_from_raw(
            _acl_principal("hermes", [vault]),
            transport="uds",
            principals_dir=tmp_path,
        )

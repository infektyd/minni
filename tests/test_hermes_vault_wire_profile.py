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
import os
from pathlib import Path

import pytest

from minni.principal import (
    IdentityMismatchError,
    _principal_from_raw,
    resolve_effective_principal,
)
from minni.tools.author_principals import AGENT_VAULT_DIRS
from minni.vault_layout import ensure_agent_vault


def _write_principal(principals: Path, name: str, raw: dict) -> Path:
    principals.mkdir(parents=True, exist_ok=True)
    path = principals / f"{name}.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


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
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "hermes",
        {
            "agent_id": "hermes",
            "workspace_id": "workspace-hermes",
            "capabilities": ["search", "learn"],
            "allowed_vault_roots": [str(vault)],
        },
    )
    p = resolve_effective_principal(
        supplied_agent_id="hermes",
        transport="uds",
        principals_dir=principals,
    )
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
    """allowed_vault_roots is a read ACL. Seed only the canonical vault slug."""
    own = tmp_path / "claudecode-vault"
    shared = tmp_path / "shared"
    foreign = tmp_path / "codex-vault"
    own.mkdir()
    shared.mkdir()
    foreign.mkdir()
    (shared / "shop.md").write_text("existing shop file\n", encoding="utf-8")

    principals = tmp_path / "principals"
    _write_principal(
        principals, "claude-code", _acl_principal("claude-code", [own, shared, foreign])
    )
    p = resolve_effective_principal(
        supplied_agent_id="claude-code",
        transport="uds",
        principals_dir=principals,
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
    principals = tmp_path / "principals"
    _write_principal(
        principals, "local", _acl_principal("main", [home], capabilities=["*"])
    )
    resolve_effective_principal(
        supplied_agent_id=None,
        transport="uds",
        principals_dir=principals,
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
    principals = tmp_path / "principals"
    _write_principal(principals, "hermes", _acl_principal("hermes", [vault]))
    with pytest.raises(OSError):
        resolve_effective_principal(
            supplied_agent_id="hermes",
            transport="uds",
            principals_dir=principals,
        )


def test_vault_dirname_for_uses_canonical_agent_vault_dirs():
    from minni.tools.author_principals import vault_dirname_for

    assert vault_dirname_for("claude-code") == "claudecode-vault"
    assert vault_dirname_for("grok-build") == "grok-build-vault"
    assert vault_dirname_for("grok-build") != "grokbuild-vault"
    assert vault_dirname_for("hermes") == "hermes-vault"
    for agent_id, vault_dir in AGENT_VAULT_DIRS.items():
        assert vault_dirname_for(agent_id) == vault_dir


def test_principal_resolve_seeds_canonical_grok_build_vault_not_dashless_alias(tmp_path):
    canonical = tmp_path / "grok-build-vault"
    dashless = tmp_path / "grokbuild-vault"
    shared = tmp_path / "shared"
    canonical.mkdir()
    dashless.mkdir()
    shared.mkdir()
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "grok-build",
        _acl_principal("grok-build", [canonical, dashless, shared]),
    )
    p = resolve_effective_principal(
        supplied_agent_id="grok-build",
        transport="uds",
        principals_dir=principals,
    )
    assert p.agent_id == "grok-build"
    assert (canonical / "log.md").is_file()
    assert (canonical / "wiki" / "sessions").is_dir()
    assert not (dashless / "log.md").exists()
    assert not (dashless / "wiki").exists()
    assert not (dashless / "inbox").exists()
    assert not (shared / "log.md").exists()
    assert not (shared / "wiki").exists()


def test_principal_from_raw_does_not_seed_on_construction(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    p = _principal_from_raw(
        _acl_principal("hermes", [vault]),
        transport="uds",
        principals_dir=tmp_path,
    )
    assert p.agent_id == "hermes"
    assert not (vault / "log.md").exists()
    assert not (vault / "wiki").exists()
    assert not (vault / "inbox").exists()


def test_resolve_unknown_identity_does_not_seed_operator_vault(tmp_path):
    vault = tmp_path / "main-vault"
    vault.mkdir()
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "local",
        _acl_principal("main", [vault], capabilities=["*"]),
    )
    p = resolve_effective_principal(
        supplied_agent_id="codex",
        transport="uds",
        principals_dir=principals,
    )
    assert p.capabilities == []
    assert p.deny_reason == "unknown_identity"
    assert not (vault / "log.md").exists()
    assert not (vault / "wiki").exists()
    assert not (vault / "inbox").exists()


def test_resolve_identity_mismatch_does_not_seed_operator_vault(tmp_path):
    vault = tmp_path / "claudecode-vault"
    vault.mkdir()
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "local",
        _acl_principal("claude-code", [vault], capabilities=["*"]),
    )
    with pytest.raises(IdentityMismatchError):
        resolve_effective_principal(
            supplied_agent_id="codex",
            transport="uds",
            principals_dir=principals,
            operator_context=True,
        )
    assert not (vault / "log.md").exists()
    assert not (vault / "wiki").exists()


def test_resolve_search_only_operator_does_not_seed_own_vault(tmp_path):
    vault = tmp_path / "claudecode-vault"
    vault.mkdir()
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "local",
        _acl_principal("claude-code", [vault], capabilities=["search"]),
    )
    p = resolve_effective_principal(
        supplied_agent_id=None,
        transport="uds",
        principals_dir=principals,
    )
    assert p.agent_id == "claude-code"
    assert p.can("search")
    assert not p.can("learn")
    assert not (vault / "log.md").exists()
    assert not (vault / "wiki").exists()


def test_resolve_platform_agent_does_not_seed_operator_vault(tmp_path, monkeypatch):
    minni_home = tmp_path / "minni-home"
    hermes = minni_home / "hermes-vault"
    hermes.mkdir(parents=True)
    operator_vault = tmp_path / "claudecode-vault"
    operator_vault.mkdir()
    monkeypatch.setenv("MINNI_HOME", str(minni_home))
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "local",
        {
            "agent_id": "claude-code",
            "capabilities": ["*"],
            "allowed_vault_roots": [str(operator_vault)],
            "platform_agent_ids": ["hermes"],
            "platform_agent_capabilities": {"hermes": ["search", "learn"]},
        },
    )
    p = resolve_effective_principal(
        supplied_agent_id="hermes",
        transport="uds",
        principals_dir=principals,
    )
    assert p.agent_id == "hermes"
    assert not (operator_vault / "log.md").exists()
    assert not (operator_vault / "wiki").exists()
    assert not (operator_vault / "inbox").exists()


def test_platform_agent_resolve_seeds_canonical_hermes_vault(tmp_path, monkeypatch):
    minni_home = tmp_path / "minni-home"
    hermes = minni_home / "hermes-vault"
    dashless = minni_home / "grokbuild-vault"
    hermes.mkdir(parents=True)
    dashless.mkdir()
    (hermes / ".index").mkdir()
    operator_vault = tmp_path / "operator-vault"
    operator_vault.mkdir()
    monkeypatch.setenv("MINNI_HOME", str(minni_home))
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "local",
        {
            "agent_id": "main",
            "capabilities": ["*"],
            "allowed_vault_roots": [str(operator_vault)],
            "platform_agent_ids": ["hermes"],
            "platform_agent_capabilities": {"hermes": ["search", "learn"]},
        },
    )
    p = resolve_effective_principal(
        supplied_agent_id="hermes",
        transport="uds",
        principals_dir=principals,
    )
    assert p.agent_id == "hermes"
    assert (hermes / "log.md").is_file()
    assert (hermes / "index.md").is_file()
    assert (hermes / "wiki" / "sessions").is_dir()
    assert not (operator_vault / "log.md").exists()
    assert not (dashless / "log.md").exists()


def test_platform_agent_resolve_does_not_seed_dashless_grokbuild_alias(
    tmp_path, monkeypatch
):
    minni_home = tmp_path / "minni-home"
    canonical = minni_home / "grok-build-vault"
    dashless = minni_home / "grokbuild-vault"
    canonical.mkdir(parents=True)
    dashless.mkdir()
    monkeypatch.setenv("MINNI_HOME", str(minni_home))
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "local",
        {
            "agent_id": "main",
            "capabilities": ["*"],
            "allowed_vault_roots": [str(tmp_path / "operator-vault")],
            "platform_agent_ids": ["grok-build"],
            "platform_agent_capabilities": {"grok-build": ["search", "learn"]},
        },
    )
    p = resolve_effective_principal(
        supplied_agent_id="grok-build",
        transport="uds",
        principals_dir=principals,
    )
    assert p.agent_id == "grok-build"
    assert (canonical / "log.md").is_file()
    assert not (dashless / "log.md").exists()
    assert not (dashless / "wiki").exists()


def test_ensure_agent_vault_raises_on_file_at_root(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.write_text("not a directory\n", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        ensure_agent_vault(vault)
    assert vault.is_file()


def test_ensure_agent_vault_raises_on_symlink_to_file_at_root(tmp_path):
    target = tmp_path / "payload"
    target.write_text("not a directory\n", encoding="utf-8")
    vault = tmp_path / "hermes-vault"
    vault.symlink_to(target)
    with pytest.raises(NotADirectoryError):
        ensure_agent_vault(vault)


def test_principal_resolve_fail_closed_on_file_at_vault_root(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.write_text("not a directory\n", encoding="utf-8")
    principals = tmp_path / "principals"
    _write_principal(principals, "hermes", _acl_principal("hermes", [vault]))
    with pytest.raises(NotADirectoryError):
        resolve_effective_principal(
            supplied_agent_id="hermes",
            transport="uds",
            principals_dir=principals,
        )


def test_principal_resolve_expands_user_vault_root_and_seeds(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    vault = fake_home / ".minni" / "hermes-vault"
    vault.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    principals = tmp_path / "principals"
    _write_principal(
        principals,
        "hermes",
        _acl_principal("hermes", ["~/.minni/hermes-vault"]),
    )
    p = resolve_effective_principal(
        supplied_agent_id="hermes",
        transport="uds",
        principals_dir=principals,
    )
    assert Path(p.allowed_vault_roots[0]) == vault.resolve()
    assert (vault / "log.md").is_file()
    assert (vault / "wiki").is_dir()

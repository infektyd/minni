"""Vault layout seed: exclusive create, never truncate append-only files."""

from __future__ import annotations

from pathlib import Path

from minni.vault_layout import ensure_agent_vault

_PLUGIN_LOG = "# Minni Log\n\n## [2026-09-01T00:00:00Z] plugin | audit\n\n"
_PLUGIN_INDEX = "# Minni Index\n\n- [[wiki/entities/peer]]\n"


def test_ensure_agent_vault_preserves_existing_log_and_index(tmp_path):
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / "log.md").write_text(_PLUGIN_LOG, encoding="utf-8")
    (vault / "index.md").write_text(_PLUGIN_INDEX, encoding="utf-8")
    created = ensure_agent_vault(vault)
    assert "log.md" not in created
    assert "index.md" not in created
    assert (vault / "log.md").read_text(encoding="utf-8") == _PLUGIN_LOG
    assert (vault / "index.md").read_text(encoding="utf-8") == _PLUGIN_INDEX


def test_ensure_agent_vault_exclusive_create_does_not_clobber_raced_append(
    tmp_path, monkeypatch
):
    """VAULT.md: log.md never truncated; index.md never rewritten in full.

    Identity load calls ensure_agent_vault on every principal JSON read, racing
    the plugin's ensureVault/recordAudit. exists() then write_text is a
    truncating replace: if the plugin creates and appends after the exists()
    check, a stub header wipes the audit. Exclusive create (mode 'x') must
    merge-not-wipe.
    """
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    orig_exists = Path.exists

    def racing_exists(self: Path) -> bool:
        if self == vault / "log.md" or self == vault / "index.md":
            if not orig_exists(self):
                body = _PLUGIN_LOG if self.name == "log.md" else _PLUGIN_INDEX
                self.write_text(body, encoding="utf-8")
            return False
        return orig_exists(self)

    monkeypatch.setattr(Path, "exists", racing_exists)
    created = ensure_agent_vault(vault)
    assert (vault / "log.md").read_text(encoding="utf-8") == _PLUGIN_LOG
    assert (vault / "index.md").read_text(encoding="utf-8") == _PLUGIN_INDEX
    assert "log.md" not in created
    assert "index.md" not in created

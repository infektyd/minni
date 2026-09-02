"""Vault layout seed: exclusive create, never truncate append-only files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

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


_RACE_AUDIT = (
    "## [2026-09-01T00:00:00Z] plugin | audit | unique-payload-do-not-clobber\n\n"
)
_RACE_INDEX = "- [[wiki/entities/peer]] unique-index-do-not-clobber\n"


def test_ensure_agent_vault_does_not_clobber_append_after_exclusive_create(
    tmp_path, monkeypatch
):
    """open('x') only excludes a pre-existing path; the fd is then written at 0.

    Plugin recordAudit can append after O_EXCL create and before the stub
    header write. Writing the header at offset 0 would overwrite the start of
    the audit entry. Treat a non-empty exclusive fd as already owned.
    """
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    orig_os_open = os.open
    orig_path_open = Path.open

    def append_after_create(path) -> None:
        try:
            name = Path(os.fsdecode(path)).name
        except (TypeError, ValueError, OSError):
            return
        if name not in {"log.md", "index.md"}:
            return
        payload = _RACE_AUDIT if name == "log.md" else _RACE_INDEX
        extra = orig_os_open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(extra, payload.encode("utf-8"))
        finally:
            os.close(extra)

    def racing_os_open(path, flags, *args, **kwargs):
        fd = orig_os_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            append_after_create(path)
        return fd

    def racing_path_open(self, *args, **kwargs):
        fh = orig_path_open(self, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if "x" in str(mode):
            append_after_create(self)
        return fh

    monkeypatch.setattr(os, "open", racing_os_open)
    monkeypatch.setattr(Path, "open", racing_path_open)
    created = ensure_agent_vault(vault)
    log_text = (vault / "log.md").read_text(encoding="utf-8")
    index_text = (vault / "index.md").read_text(encoding="utf-8")
    assert _RACE_AUDIT in log_text
    assert _RACE_INDEX in index_text
    assert "log.md" not in created
    assert "index.md" not in created


def test_ensure_agent_vault_created_paths_are_owner_only(tmp_path):
    """Seeded wiki/inbox/log.md must be 0700/0600; umask is not the boundary."""
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    old_umask = os.umask(0)
    try:
        ensure_agent_vault(vault)
    finally:
        os.umask(old_umask)
    for rel in ("wiki", "wiki/sessions", "inbox", "outbox", "logs"):
        mode = stat.S_IMODE((vault / rel).stat().st_mode)
        assert mode == 0o700, f"{rel} mode {oct(mode)}"
    for rel in ("log.md", "index.md"):
        mode = stat.S_IMODE((vault / rel).stat().st_mode)
        assert mode == 0o600, f"{rel} mode {oct(mode)}"


def test_ensure_agent_vault_refuses_symlink_wiki_into_shop_restore(tmp_path):
    """Contract-dir mkdir must not follow wiki → shop/backup/peer trees."""
    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "wiki").symlink_to(shop)
    with pytest.raises(OSError):
        ensure_agent_vault(vault)
    assert not (shop / "entities").exists()
    assert not (shop / "sessions").exists()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_ensure_agent_vault_refuses_symlink_inbox_into_peer_vault(tmp_path):
    vault = tmp_path / "hermes-vault"
    peer = tmp_path / "codex-vault"
    vault.mkdir()
    peer.mkdir()
    (vault / "inbox").symlink_to(peer)
    with pytest.raises(OSError):
        ensure_agent_vault(vault)
    assert list(peer.iterdir()) == []


def test_ensure_agent_vault_refuses_symlink_root_into_shop_restore(tmp_path):
    """hermes-vault → shop-restore must not plant wiki/inbox in the target."""
    shop = tmp_path / "shop-restore"
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    vault = tmp_path / "hermes-vault"
    vault.symlink_to(shop)
    with pytest.raises(OSError):
        ensure_agent_vault(vault)
    assert not (shop / "wiki").exists()
    assert not (shop / "inbox").exists()
    assert not (shop / "log.md").exists()
    assert not (shop / "index.md").exists()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def _plant_stamp_then_claim_missing(orig_exists, vault: Path):
    def racing_exists(self: Path) -> bool:
        if self == vault / "log.md" or self == vault / "index.md":
            if not orig_exists(self):
                body = _PLUGIN_LOG if self.name == "log.md" else _PLUGIN_INDEX
                self.write_text(body, encoding="utf-8")
            return False
        return orig_exists(self)

    return racing_exists


def test_afm_ensure_vault_does_not_wipe_raced_log_md(tmp_path, monkeypatch):
    """Peer AFM seeder must not exists()+write_text over a raced stamp."""
    from minni.afm_writer import _ensure_vault

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    monkeypatch.setattr(Path, "exists", _plant_stamp_then_claim_missing(Path.exists, vault))
    _ensure_vault(vault)
    assert (vault / "log.md").read_text(encoding="utf-8") == _PLUGIN_LOG
    assert (vault / "index.md").read_text(encoding="utf-8") == _PLUGIN_INDEX


def test_handoff_ensure_vault_does_not_wipe_raced_log_md(tmp_path, monkeypatch):
    """Peer handoff seeder must not exists()+write_text over a raced stamp."""
    from minni.minnid_runtime.handoff import ensure_handoff_vault

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    monkeypatch.setattr(Path, "exists", _plant_stamp_then_claim_missing(Path.exists, vault))
    ensure_handoff_vault(vault)
    assert (vault / "log.md").read_text(encoding="utf-8") == _PLUGIN_LOG
    assert (vault / "index.md").read_text(encoding="utf-8") == _PLUGIN_INDEX


def test_afm_ensure_vault_does_not_clobber_append_after_exclusive_create(
    tmp_path, monkeypatch
):
    """Peer AFM seeder must skip a non-empty exclusive fd, not write at 0."""
    from minni.afm_writer import _ensure_vault

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    orig_os_open = os.open

    def append_after_create(path) -> None:
        try:
            name = Path(os.fsdecode(path)).name
        except (TypeError, ValueError, OSError):
            return
        if name not in {"log.md", "index.md"}:
            return
        payload = _RACE_AUDIT if name == "log.md" else _RACE_INDEX
        extra = orig_os_open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(extra, payload.encode("utf-8"))
        finally:
            os.close(extra)

    def racing_os_open(path, flags, *args, **kwargs):
        fd = orig_os_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            append_after_create(path)
        return fd

    monkeypatch.setattr(os, "open", racing_os_open)
    _ensure_vault(vault)
    log_text = (vault / "log.md").read_text(encoding="utf-8")
    index_text = (vault / "index.md").read_text(encoding="utf-8")
    assert _RACE_AUDIT in log_text
    assert _RACE_INDEX in index_text


def _shop_restore_symlink_root(tmp_path: Path) -> tuple[Path, Path]:
    shop = tmp_path / "shop-restore"
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    vault = tmp_path / "hermes-vault"
    vault.symlink_to(shop)
    return vault, shop


def _assert_shop_unplanted(shop: Path) -> None:
    assert not (shop / "wiki").exists()
    assert not (shop / "inbox").exists()
    assert not (shop / "log.md").exists()
    assert not (shop / "index.md").exists()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_afm_ensure_vault_refuses_symlink_root_into_shop_restore(tmp_path):
    """Peer AFM mkdir must not follow hermes-vault → shop-restore."""
    from minni.afm_writer import _ensure_vault

    vault, shop = _shop_restore_symlink_root(tmp_path)
    with pytest.raises(OSError, match="symlinked vault root"):
        _ensure_vault(vault)
    _assert_shop_unplanted(shop)


def test_handoff_ensure_vault_refuses_symlink_root_into_shop_restore(tmp_path):
    """Peer handoff mkdir must not follow hermes-vault → shop-restore."""
    from minni.minnid_runtime.handoff import ensure_handoff_vault

    vault, shop = _shop_restore_symlink_root(tmp_path)
    with pytest.raises(OSError, match="symlinked vault root"):
        ensure_handoff_vault(vault)
    _assert_shop_unplanted(shop)


def test_afm_append_audit_refuses_symlink_root_into_shop_restore(tmp_path):
    from minni.afm_writer import _append_audit

    vault, shop = _shop_restore_symlink_root(tmp_path)
    with pytest.raises(OSError, match="symlinked vault root"):
        _append_audit(vault, "afm_loop", "wrote draft", {"k": "v"})
    _assert_shop_unplanted(shop)


def test_afm_append_audit_does_not_truncate_when_exists_lies(tmp_path, monkeypatch):
    """Leftover exists()+write_text after exclusive ensure must not wipe log.md."""
    from minni.afm_writer import _append_audit

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / "log.md").write_text(_PLUGIN_LOG, encoding="utf-8")
    orig_exists = Path.exists

    def lying_exists(self: Path) -> bool:
        if self == vault / "log.md":
            return False
        return orig_exists(self)

    monkeypatch.setattr(Path, "exists", lying_exists)
    _append_audit(vault, "afm_loop", "wrote draft", {"k": "v"})
    text = (vault / "log.md").read_text(encoding="utf-8")
    assert _PLUGIN_LOG in text
    assert "wrote draft" in text


def test_handoff_append_audit_does_not_truncate_when_exists_lies(tmp_path, monkeypatch):
    """Leftover exists()+write_text after exclusive ensure must not wipe log.md."""
    from minni.minnid_runtime.handoff import append_handoff_audit

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / "log.md").write_text(_PLUGIN_LOG, encoding="utf-8")
    orig_exists = Path.exists

    def lying_exists(self: Path) -> bool:
        if self == vault / "log.md":
            return False
        return orig_exists(self)

    monkeypatch.setattr(Path, "exists", lying_exists)
    append_handoff_audit(vault, "handoff_sent", "wrote draft", {"k": "v"})
    text = (vault / "log.md").read_text(encoding="utf-8")
    assert _PLUGIN_LOG in text
    assert "wrote draft" in text


def test_wire_bootstrap_vault_preserves_existing_log_and_index(tmp_path, monkeypatch):
    from minni.wire.writers import bootstrap_vault

    monkeypatch.setenv("HOME", str(tmp_path))
    vault = tmp_path / ".minni" / "hermes-vault"
    vault.mkdir(parents=True)
    (vault / "log.md").write_text(_PLUGIN_LOG, encoding="utf-8")
    (vault / "index.md").write_text(_PLUGIN_INDEX, encoding="utf-8")
    bootstrap_vault("hermes")
    assert (vault / "log.md").read_text(encoding="utf-8") == _PLUGIN_LOG
    assert (vault / "index.md").read_text(encoding="utf-8") == _PLUGIN_INDEX


def test_wire_bootstrap_vault_does_not_clobber_append_after_exclusive_create(
    tmp_path, monkeypatch
):
    from minni.wire.writers import bootstrap_vault

    monkeypatch.setenv("HOME", str(tmp_path))
    vault = tmp_path / ".minni" / "hermes-vault"
    vault.mkdir(parents=True)
    orig_os_open = os.open

    def append_after_create(path) -> None:
        try:
            name = Path(os.fsdecode(path)).name
        except (TypeError, ValueError, OSError):
            return
        if name not in {"log.md", "index.md"}:
            return
        payload = _RACE_AUDIT if name == "log.md" else _RACE_INDEX
        extra = orig_os_open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(extra, payload.encode("utf-8"))
        finally:
            os.close(extra)

    def racing_os_open(path, flags, *args, **kwargs):
        fd = orig_os_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            append_after_create(path)
        return fd

    monkeypatch.setattr(os, "open", racing_os_open)
    bootstrap_vault("hermes")
    log_text = (vault / "log.md").read_text(encoding="utf-8")
    index_text = (vault / "index.md").read_text(encoding="utf-8")
    assert _RACE_AUDIT in log_text
    assert _RACE_INDEX in index_text


def _wiki_symlink_to_shop(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "wiki").symlink_to(shop)
    return vault, shop


def _inbox_symlink_to_shop(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "inbox").symlink_to(shop)
    return vault, shop


def _file_symlink_to_shop(tmp_path: Path, rel: str) -> tuple[Path, Path]:
    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    dest = vault / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(shop / "keep.md")
    return vault, shop


def _assert_shop_identity_unplanted(shop: Path) -> None:
    assert not (shop / "sessions").exists()
    assert not (shop / "entities").exists()
    assert not (shop / "concepts").exists()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_afm_ensure_vault_refuses_symlink_wiki_into_shop_restore(tmp_path):
    """Peer AFM mkdir must not plant wiki/sessions through wiki → shop-restore."""
    from minni.afm_writer import _ensure_vault

    vault, shop = _wiki_symlink_to_shop(tmp_path)
    with pytest.raises(OSError):
        _ensure_vault(vault)
    _assert_shop_identity_unplanted(shop)


def test_handoff_ensure_vault_refuses_symlink_wiki_into_shop_restore(tmp_path):
    """Peer handoff mkdir must not plant wiki/handoffs through wiki → shop-restore."""
    from minni.minnid_runtime.handoff import ensure_handoff_vault

    vault, shop = _wiki_symlink_to_shop(tmp_path)
    with pytest.raises(OSError):
        ensure_handoff_vault(vault)
    _assert_shop_identity_unplanted(shop)
    assert not (shop / "handoffs").exists()


def test_afm_ensure_vault_refuses_symlink_inbox_into_shop_restore(tmp_path):
    from minni.afm_writer import _ensure_vault

    vault, shop = _inbox_symlink_to_shop(tmp_path)
    with pytest.raises(OSError):
        _ensure_vault(vault)
    _assert_shop_identity_unplanted(shop)
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_handoff_ensure_vault_refuses_symlink_inbox_into_shop_restore(tmp_path):
    from minni.minnid_runtime.handoff import ensure_handoff_vault

    vault, shop = _inbox_symlink_to_shop(tmp_path)
    with pytest.raises(OSError):
        ensure_handoff_vault(vault)
    _assert_shop_identity_unplanted(shop)
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_afm_ensure_vault_refuses_log_md_symlink_into_shop(tmp_path):
    """dest.exists() follows log.md → shop; skip-or-append must lstat first."""
    from minni.afm_writer import _ensure_vault

    vault, shop = _file_symlink_to_shop(tmp_path, "log.md")
    with pytest.raises(OSError):
        _ensure_vault(vault)
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_afm_ensure_vault_refuses_index_md_symlink_into_shop(tmp_path):
    from minni.afm_writer import _ensure_vault

    vault, shop = _file_symlink_to_shop(tmp_path, "index.md")
    with pytest.raises(OSError):
        _ensure_vault(vault)
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_handoff_ensure_vault_refuses_log_md_symlink_into_shop(tmp_path):
    from minni.minnid_runtime.handoff import ensure_handoff_vault

    vault, shop = _file_symlink_to_shop(tmp_path, "log.md")
    with pytest.raises(OSError):
        ensure_handoff_vault(vault)
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_handoff_ensure_vault_refuses_index_md_symlink_into_shop(tmp_path):
    from minni.minnid_runtime.handoff import ensure_handoff_vault

    vault, shop = _file_symlink_to_shop(tmp_path, "index.md")
    with pytest.raises(OSError):
        ensure_handoff_vault(vault)
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_afm_append_audit_refuses_log_md_symlink_into_shop(tmp_path):
    from minni.afm_writer import _append_audit

    vault, shop = _file_symlink_to_shop(tmp_path, "log.md")
    with pytest.raises(OSError):
        _append_audit(vault, "afm_loop", "wrote draft", {"k": "v"})
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_handoff_append_audit_refuses_log_md_symlink_into_shop(tmp_path):
    from minni.minnid_runtime.handoff import append_handoff_audit

    vault, shop = _file_symlink_to_shop(tmp_path, "log.md")
    with pytest.raises(OSError):
        append_handoff_audit(vault, "handoff_sent", "wrote draft", {"k": "v"})
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_afm_append_audit_refuses_daily_log_symlink_into_shop(tmp_path):
    import time

    from minni.afm_writer import _append_audit

    day = time.strftime("%Y-%m-%d", time.gmtime())
    vault, shop = _file_symlink_to_shop(tmp_path, f"logs/{day}.md")
    with pytest.raises(OSError):
        _append_audit(vault, "afm_loop", "wrote draft", {"k": "v"})
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def _afm_draft(*, section: str, kind: str = "concept") -> dict:
    return {
        "title": "Shop plant probe",
        "body": "A body long enough to chunk. " * 20,
        "page_id": "page-abc123",
        "trace_id": "trace-shop",
        "kind": kind,
        "section": section,
        "sources": ["`probe`"],
    }


def _wiki_section_symlink_to_shop(tmp_path: Path, section: str) -> tuple[Path, Path]:
    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "wiki").mkdir()
    (vault / "wiki" / section).symlink_to(shop)
    return vault, shop


@pytest.mark.parametrize(
    "section",
    ("decisions", "syntheses", "procedures", "artifacts"),
)
def test_afm_write_one_refuses_wiki_section_symlink_into_shop_restore(tmp_path, section):
    """mkdir(parents=True) on wiki/<section> must not plant through a dir symlink."""
    from minni.afm_writer import _write_one

    vault, shop = _wiki_section_symlink_to_shop(tmp_path, section)
    with pytest.raises(OSError):
        _write_one(vault, _afm_draft(section=section))
    assert list(shop.iterdir()) == [shop / "keep.md"]
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_afm_write_one_refuses_wiki_dir_symlink_into_shop_restore(tmp_path):
    from minni.afm_writer import _write_one

    vault, shop = _wiki_symlink_to_shop(tmp_path)
    with pytest.raises(OSError):
        _write_one(vault, _afm_draft(section="decisions"))
    _assert_shop_identity_unplanted(shop)
    assert not (shop / "decisions").exists()


def test_afm_write_batch_refuses_inbox_drafts_symlink_into_shop(tmp_path):
    """exists()+write_text follows inbox/afm-drafts-*.json into shop-restore."""
    import time

    from minni.afm_writer import _write_batch

    day = time.strftime("%Y-%m-%d", time.gmtime())
    vault, shop = _file_symlink_to_shop(tmp_path, f"inbox/afm-drafts-{day}.json")
    with pytest.raises(OSError):
        _write_batch(
            {
                "vault_path": str(vault),
                "pass_name": "probe",
                "drafts": [],
            }
        )
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_afm_write_batch_merges_inbox_runs_after_exclusive_seed(tmp_path):
    import json

    from minni.afm_writer import _write_batch

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    job = {"vault_path": str(vault), "pass_name": "probe", "drafts": []}
    first = _write_batch(job)
    second = _write_batch(job)
    inbox = Path(first["inbox_path"])
    assert inbox == Path(second["inbox_path"])
    payload = json.loads(inbox.read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 2
    assert not inbox.is_symlink()


def test_wire_bootstrap_vault_refuses_schema_agents_symlink_into_shop(
    tmp_path, monkeypatch
):
    from minni.wire.writers import bootstrap_vault

    monkeypatch.setenv("HOME", str(tmp_path))
    vault = tmp_path / ".minni" / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir(parents=True)
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "schema").mkdir()
    (vault / "schema" / "AGENTS.md").symlink_to(shop / "keep.md")
    with pytest.raises(OSError):
        bootstrap_vault("hermes")
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_wire_bootstrap_vault_refuses_schema_dir_symlink_into_shop(
    tmp_path, monkeypatch
):
    from minni.wire.writers import bootstrap_vault

    monkeypatch.setenv("HOME", str(tmp_path))
    vault = tmp_path / ".minni" / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir(parents=True)
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "schema").symlink_to(shop)
    with pytest.raises(OSError):
        bootstrap_vault("hermes")
    assert not (shop / "AGENTS.md").exists()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_wire_bootstrap_vault_refuses_symlink_wiki_into_shop_restore(
    tmp_path, monkeypatch
):
    from minni.wire.writers import bootstrap_vault

    monkeypatch.setenv("HOME", str(tmp_path))
    vault = tmp_path / ".minni" / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir(parents=True)
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "wiki").symlink_to(shop)
    with pytest.raises(OSError):
        bootstrap_vault("hermes")
    _assert_shop_identity_unplanted(shop)


def test_wire_bootstrap_vault_refuses_symlink_inbox_into_shop_restore(
    tmp_path, monkeypatch
):
    from minni.wire.writers import bootstrap_vault

    monkeypatch.setenv("HOME", str(tmp_path))
    vault = tmp_path / ".minni" / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir(parents=True)
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    (vault / "inbox").symlink_to(shop)
    with pytest.raises(OSError):
        bootstrap_vault("hermes")
    assert list(shop.iterdir()) == [shop / "keep.md"]

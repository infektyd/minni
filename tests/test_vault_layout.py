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


def test_afm_write_batch_does_not_plant_inbox_drafts_tmp_sidecar_symlink(tmp_path):
    """Predictable afm-drafts-DATE.json.tmp must not follow into shop-restore."""
    import json
    import time

    from minni.afm_writer import _write_batch

    day = time.strftime("%Y-%m-%d", time.gmtime())
    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    inbox = vault / "inbox"
    inbox.mkdir()
    sidecar = inbox / f"afm-drafts-{day}.json.tmp"
    sidecar.symlink_to(shop / "keep.md")
    result = _write_batch(
        {
            "vault_path": str(vault),
            "pass_name": "probe",
            "drafts": [],
        }
    )
    dest = Path(result["inbox_path"])
    assert dest == inbox / f"afm-drafts-{day}.json"
    assert dest.is_file()
    assert not dest.is_symlink()
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert len(payload["runs"]) == 1
    assert sidecar.is_symlink()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_afm_write_one_does_not_plant_wiki_page_tmp_sidecar_symlink(tmp_path):
    """Predictable wiki/<section>/<page>.md.tmp must not follow into shop-restore."""
    import time

    from minni.afm_writer import _slugify, _write_one

    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    draft = _afm_draft(section="decisions")
    now = 1_777_000_000.0
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    name = (
        f"{created[:10].replace('-', '')}-"
        f"{_slugify(draft['title'])}-{draft['page_id'][-6:]}.md"
    )
    page_dir = vault / "wiki" / "decisions"
    page_dir.mkdir(parents=True)
    sidecar = page_dir / f"{name}.tmp"
    sidecar.symlink_to(shop / "keep.md")
    written = _write_one(vault, draft, now=now)
    dest = vault / written["path"]
    assert dest == page_dir / name
    assert dest.is_file()
    assert not dest.is_symlink()
    assert sidecar.is_symlink()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_atomic_write_text_refuses_unique_tmp_symlink_into_shop(tmp_path, monkeypatch):
    """O_EXCL unique tmp must lstat-refuse a planted sidecar, not follow it."""
    from minni.afm_writer import _atomic_write_text

    monkeypatch.setattr(os, "getpid", lambda: 4242)
    monkeypatch.setattr(os, "urandom", lambda n: b"\xab" * n)
    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    dest = vault / "page.md"
    tmp = vault / f"page.md.4242.{(b'\xab' * 8).hex()}.tmp"
    tmp.symlink_to(shop / "keep.md")
    with pytest.raises(OSError):
        _atomic_write_text(dest, "new\n")
    assert not dest.exists()
    assert tmp.is_symlink()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def _pending_lifecycle() -> dict:
    return {
        "promote_candidate_ids": [3],
        "dedup_candidate_ids": [],
        "review_candidate_ids": [7, 8],
    }


def test_persist_pending_lifecycle_does_not_plant_pid_sidecar_symlink(
    tmp_path, monkeypatch
):
    """Pid-only .afm-pending-lifecycle.json.{pid}.tmp must not follow into shop."""
    from minni.afm_writer import _persist_pending_lifecycle

    monkeypatch.setattr(os, "getpid", lambda: 4242)
    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    inbox = vault / "inbox"
    inbox.mkdir()
    sidecar = inbox / ".afm-pending-lifecycle.json.4242.tmp"
    sidecar.symlink_to(shop / "keep.md")
    _persist_pending_lifecycle("consolidation", _pending_lifecycle(), str(vault))
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]
    assert sidecar.is_symlink()


def _submit_drafts_wet_enqueue_spy(monkeypatch, vault: Path):
    """Cold-start submit_drafts: return (result, put_calls)."""
    import queue as queue_mod

    import minni.afm_writer as afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))
    put_calls = []

    def _spy_put(item):
        put_calls.append(item)
        raise AssertionError("must not enqueue a wet batch")

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy_put)
    result = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "vault_path": str(vault),
            "drafts": [
                {"title": "dup", "page_id": "consolidation-review-1-t2"},
            ],
        },
        timeout=0.05,
    )
    return result, put_calls


def test_persist_pending_lifecycle_refuses_inbox_dir_symlink_into_shop(tmp_path):
    """mkdir + write of afm-pending-lifecycle.json must not follow inbox → shop."""
    from minni.afm_writer import _persist_pending_lifecycle

    vault, shop = _inbox_symlink_to_shop(tmp_path)
    _persist_pending_lifecycle("consolidation", _pending_lifecycle(), str(vault))
    assert (vault / "inbox").is_symlink()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_persist_inbox_symlink_restart_hydrate_refuses_wet_enqueue(
    tmp_path, monkeypatch
):
    """Swallowed persist through planted inbox must not remint after restart."""
    from minni.afm_writer import _persist_pending_lifecycle

    vault, shop = _inbox_symlink_to_shop(tmp_path)
    _persist_pending_lifecycle("consolidation", _pending_lifecycle(), str(vault))
    assert list(shop.iterdir()) == [shop / "keep.md"]
    result, put_calls = _submit_drafts_wet_enqueue_spy(monkeypatch, vault)
    assert result.get("status") == "write_in_flight", result
    assert result.get("lifecycle_pending") is True
    assert result.get("drafts_written") == []
    assert put_calls == []
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"


def test_persist_exception_restart_hydrate_refuses_wet_enqueue(
    tmp_path, monkeypatch
):
    """Persist Exception with a missing sidecar must not hydrate-empty into a remint."""
    import minni.afm_writer as afm_writer

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / "inbox").mkdir()

    def boom(*_args, **_kwargs):
        raise OSError("injected persist failure")

    monkeypatch.setattr(afm_writer, "_atomic_write_json", boom)
    afm_writer._persist_pending_lifecycle(
        "consolidation", _pending_lifecycle(), str(vault)
    )
    result, put_calls = _submit_drafts_wet_enqueue_spy(monkeypatch, vault)
    assert result.get("status") == "write_in_flight", result
    assert result.get("lifecycle_pending") is True
    assert result.get("drafts_written") == []
    assert put_calls == []


def test_clear_persisted_pending_lifecycle_refuses_inbox_dir_symlink_into_shop(
    tmp_path,
):
    """Clear rewrite of leftover passes must not mkdir-through inbox → shop."""
    import json

    from minni.afm_writer import _clear_persisted_pending_lifecycle

    vault, shop = _inbox_symlink_to_shop(tmp_path)
    bait = shop / "afm-pending-lifecycle.json"
    bait_body = (
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-09-01T00:00:00Z",
                "passes": {
                    "consolidation": _pending_lifecycle(),
                    "probe": {"promote_candidate_ids": [1]},
                },
            }
        )
        + "\n"
    )
    bait.write_text(bait_body, encoding="utf-8")
    _clear_persisted_pending_lifecycle("consolidation", str(vault))
    assert (vault / "inbox").is_symlink()
    assert bait.read_text(encoding="utf-8") == bait_body
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert set(shop.iterdir()) == {shop / "keep.md", bait}


_TORN_PENDING_SIDECAR = (
    '{"version": 1, "updated_at": "2026-09-01T00:00:00Z",'
    ' "passes": {"probe": {"promote_candidate_ids": [9]}'
)


def _torn_pending_sidecar(vault: Path) -> Path:
    inbox = vault / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / "afm-pending-lifecycle.json"
    path.write_text(_TORN_PENDING_SIDECAR, encoding="utf-8")
    return path


def test_read_pending_lifecycle_file_raises_on_torn_sidecar(tmp_path):
    """Parse/IO errors are not empty — empty would let RMW wipe sibling passes."""
    from minni.afm_writer import _read_pending_lifecycle_file

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    _torn_pending_sidecar(vault)
    with pytest.raises(OSError, match="unreadable pending-lifecycle"):
        _read_pending_lifecycle_file(str(vault))


def test_clear_pending_lifecycle_keeps_torn_sidecar(tmp_path):
    """Unreadable sidecar must not be treated as “no other passes” and unlinked."""
    from minni.afm_writer import _clear_persisted_pending_lifecycle

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    path = _torn_pending_sidecar(vault)
    _clear_persisted_pending_lifecycle("consolidation", str(vault))
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == _TORN_PENDING_SIDECAR


def test_persist_pending_lifecycle_does_not_replace_torn_sidecar(tmp_path):
    """RMW must not rewrite a torn sidecar as only the current pass."""
    from minni.afm_writer import _persist_pending_lifecycle

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    path = _torn_pending_sidecar(vault)
    _persist_pending_lifecycle("consolidation", _pending_lifecycle(), str(vault))
    assert path.read_text(encoding="utf-8") == _TORN_PENDING_SIDECAR


def test_hydrate_pending_lifecycle_from_torn_sidecar_is_noop(tmp_path):
    from minni.afm_writer import (
        _PENDING_LIFECYCLE,
        _hydrate_pending_lifecycle_from_vault,
        reset_pass_counters,
    )

    reset_pass_counters()
    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    path = _torn_pending_sidecar(vault)
    assert _hydrate_pending_lifecycle_from_vault(str(vault)) is False
    assert "probe" not in _PENDING_LIFECYCLE
    assert path.read_text(encoding="utf-8") == _TORN_PENDING_SIDECAR


def test_submit_drafts_refuses_wet_enqueue_on_torn_pending_sidecar(
    tmp_path, monkeypatch
):
    """Torn sticky sidecar must not hydrate-empty into a second wet mint."""
    import queue as queue_mod

    import minni.afm_writer as afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    path = _torn_pending_sidecar(vault)
    put_calls = []

    def _spy_put(item):
        put_calls.append(item)
        raise AssertionError("torn pending-lifecycle must not enqueue a wet batch")

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy_put)
    result = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "vault_path": str(vault),
            "drafts": [
                {"title": "dup", "page_id": "consolidation-review-1-t2"},
            ],
        },
        timeout=0.05,
    )
    assert result.get("status") == "write_in_flight", result
    assert result.get("lifecycle_pending") is True
    assert result.get("drafts_written") == []
    assert put_calls == []
    assert path.read_text(encoding="utf-8") == _TORN_PENDING_SIDECAR
    afm_writer.reset_pass_counters()


def test_afm_write_batch_does_not_wipe_runs_on_truncated_inbox(tmp_path):
    """Second merge must not replace the day's ledger when the read is torn."""
    import json
    import time

    from minni.afm_writer import _write_batch

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    job = {"vault_path": str(vault), "pass_name": "probe", "drafts": []}
    first = _write_batch(job)
    inbox = Path(first["inbox_path"])
    original = inbox.read_text(encoding="utf-8")
    assert len(json.loads(original)["runs"]) == 1
    torn = original[: max(12, original.find("runs") + 6)]
    assert torn != original
    inbox.write_text(torn, encoding="utf-8")
    with pytest.raises(OSError, match="unreadable AFM inbox ledger"):
        _write_batch({**job, "pass_name": "probe-2"})
    assert inbox.read_text(encoding="utf-8") == torn
    day = time.strftime("%Y-%m-%d", time.gmtime())
    assert inbox == vault / f"inbox/afm-drafts-{day}.json"


def test_afm_write_batch_does_not_wipe_runs_on_non_object_inbox(tmp_path):
    from minni.afm_writer import _write_batch

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    job = {"vault_path": str(vault), "pass_name": "probe", "drafts": []}
    first = _write_batch(job)
    inbox = Path(first["inbox_path"])
    inbox.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(OSError, match="not a JSON object"):
        _write_batch({**job, "pass_name": "probe-2"})
    assert inbox.read_text(encoding="utf-8") == "[1, 2]\n"


def test_afm_write_batch_does_not_mint_wiki_when_inbox_torn(tmp_path):
    """Inbox fail-close must happen before wiki writes, or retry dual-mints."""
    import json

    from minni.afm_writer import _write_batch

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    first_draft = _afm_draft(section="concepts")
    first = _write_batch(
        {"vault_path": str(vault), "pass_name": "probe", "drafts": [first_draft]}
    )
    inbox = Path(first["inbox_path"])
    wiki_before = {p.relative_to(vault) for p in (vault / "wiki").rglob("*.md")}
    assert wiki_before
    original = inbox.read_text(encoding="utf-8")
    torn = original[: max(12, original.find("runs") + 6)]
    assert torn != original
    inbox.write_text(torn, encoding="utf-8")
    second_draft = {
        **_afm_draft(section="concepts"),
        "page_id": "page-zzz999",
        "title": "Second mint",
    }
    with pytest.raises(OSError, match="unreadable AFM inbox ledger"):
        _write_batch(
            {
                "vault_path": str(vault),
                "pass_name": "probe-2",
                "drafts": [second_draft],
            }
        )
    wiki_after = {p.relative_to(vault) for p in (vault / "wiki").rglob("*.md")}
    assert wiki_after == wiki_before
    assert inbox.read_text(encoding="utf-8") == torn
    assert "page-zzz999" not in json.dumps(
        [p.read_text(encoding="utf-8") for p in (vault / "wiki").rglob("*.md")]
    )


def test_afm_write_batch_inbox_commit_fail_does_not_dual_mint_wiki(
    tmp_path, monkeypatch
):
    """Wiki-before-inbox left durable pages when ledger commit raised.

    Next compile mints new page_ids. Commit the inbox first; retry must not
    land a second wiki set.
    """
    from minni import afm_writer

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    orig = afm_writer._atomic_write_text

    def fail_inbox(path, data):
        if path.name.startswith("afm-drafts-") and path.suffix == ".json":
            raise OSError("inbox commit failed")
        return orig(path, data)

    monkeypatch.setattr(afm_writer, "_atomic_write_text", fail_inbox)
    first_draft = {**_afm_draft(section="concepts"), "page_id": "page-aaa111"}
    with pytest.raises(OSError, match="inbox commit failed"):
        afm_writer._write_batch(
            {
                "vault_path": str(vault),
                "pass_name": "probe",
                "drafts": [first_draft],
            }
        )
    wiki_after_fail = list((vault / "wiki").rglob("*.md")) if (vault / "wiki").exists() else []
    assert wiki_after_fail == []

    monkeypatch.setattr(afm_writer, "_atomic_write_text", orig)
    second_draft = {
        **_afm_draft(section="concepts"),
        "page_id": "page-bbb222",
        "title": "Second mint",
    }
    result = afm_writer._write_batch(
        {
            "vault_path": str(vault),
            "pass_name": "probe-2",
            "drafts": [second_draft],
        }
    )
    assert result["drafts_written"]
    texts = [p.read_text(encoding="utf-8") for p in (vault / "wiki").rglob("*.md")]
    assert any("page-bbb222" in text for text in texts)
    assert all("page-aaa111" not in text for text in texts)


def test_handoff_write_json_refuses_dest_symlink_into_shop(tmp_path):
    """Path.write_text follows inbox/packet.json → shop/keep.md."""
    from minni.minnid_runtime.handoff import write_json

    vault, shop = _file_symlink_to_shop(tmp_path, "inbox/packet.json")
    with pytest.raises(OSError):
        write_json(vault / "inbox" / "packet.json", {"k": "v"})
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]
    assert (vault / "inbox" / "packet.json").is_symlink()


def test_compile_handoff_page_refuses_dest_symlink_into_shop(tmp_path):
    from minni.minnid_runtime.handoff import (
        _handoff_wiki_filename,
        compile_handoff_page,
    )

    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    (shop / "keep.md").write_text("restore\n", encoding="utf-8")
    dest_dir = vault / "wiki" / "handoffs"
    dest_dir.mkdir(parents=True)
    stamp = "20260901T000000Z"
    packet = {
        "kind": "handoff",
        "task": "task",
        "from_agent": "hermes",
        "to_agent": "grok",
        "trace_id": "trace-1",
        "lease_id": "handoff-lease1",
        "created_at": "2026-09-01T00:00:00Z",
        "wikilink_refs": [],
        "envelope": "<e/>",
    }
    dest = dest_dir / _handoff_wiki_filename(packet, stamp)
    dest.symlink_to(shop / "keep.md")
    with pytest.raises(OSError):
        compile_handoff_page(vault, packet, stamp)
    assert dest.is_symlink()
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_compile_handoff_page_keeps_same_day_packets(tmp_path):
    """Date+slug alone would atomically replace the first same-day handoff."""
    from minni.minnid_runtime.handoff import compile_handoff_page

    vault = tmp_path / "hermes-vault"
    (vault / "wiki" / "handoffs").mkdir(parents=True)
    stamp = "20260901T000000Z"
    first = {
        "kind": "handoff",
        "task": "task",
        "from_agent": "hermes",
        "to_agent": "grok",
        "trace_id": "trace-aaa",
        "lease_id": "handoff-lease-aaa",
        "created_at": "2026-09-01T00:00:00Z",
        "wikilink_refs": [],
        "envelope": "<e>first</e>",
    }
    second = {
        **first,
        "trace_id": "trace-bbb",
        "lease_id": "handoff-lease-bbb",
        "envelope": "<e>second</e>",
    }
    path1 = compile_handoff_page(vault, first, stamp)
    path2 = compile_handoff_page(vault, second, stamp)
    assert path1 is not None and path2 is not None
    assert path1 != path2
    assert path1.is_file() and path2.is_file()
    assert "handoff-lease-aaa" in path1.name
    assert "handoff-lease-bbb" in path2.name
    assert "<e>first</e>" in path1.read_text(encoding="utf-8")
    assert "<e>second</e>" in path2.read_text(encoding="utf-8")


def test_compile_handoff_page_lease_id_case_collision(tmp_path):
    """slugify lowercases; handoff-AAA and handoff-aaa must not share a page."""
    from minni.minnid_runtime.handoff import compile_handoff_page

    vault = tmp_path / "hermes-vault"
    (vault / "wiki" / "handoffs").mkdir(parents=True)
    stamp = "20260901T000000Z"
    upper = {
        "kind": "handoff",
        "task": "task",
        "from_agent": "hermes",
        "to_agent": "grok",
        "trace_id": "trace-upper",
        "lease_id": "handoff-AAA",
        "created_at": "2026-09-01T00:00:00Z",
        "wikilink_refs": [],
        "envelope": "<e>upper</e>",
    }
    lower = {
        **upper,
        "trace_id": "trace-lower",
        "lease_id": "handoff-aaa",
        "envelope": "<e>lower</e>",
    }
    path1 = compile_handoff_page(vault, upper, stamp)
    path2 = compile_handoff_page(vault, lower, stamp)
    assert path1 is not None and path2 is not None
    assert path1 != path2
    assert path1.is_file() and path2.is_file()
    assert "<e>upper</e>" in path1.read_text(encoding="utf-8")
    assert "<e>lower</e>" in path2.read_text(encoding="utf-8")


def _pruning_proposal(trace_id: str = "trace-prune") -> dict:
    return {
        "proposal_id": "afm-pruning-transition-probe",
        "proposal_type": "status_transition",
        "status": "draft",
        "agent": "afm-loop",
        "trace_id": trace_id,
        "path": "wiki/concepts/expired.md",
        "from_status": "accepted",
        "to_status": "expired",
        "reason": "probe",
        "lifecycle": "requires_endorsement; original_page_unchanged",
    }


def test_pruning_write_inbox_does_not_wipe_runs_on_truncated_inbox(tmp_path):
    """Torn pruning ledger must not be treated as empty then Path.write_text."""
    import json
    import time

    from minni.afm_passes.pruning import _write_inbox

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    first = _write_inbox(str(vault), "trace-prune-1", [_pruning_proposal("trace-prune-1")])
    inbox = vault / first["path"]
    original = inbox.read_text(encoding="utf-8")
    assert len(json.loads(original)["runs"]) == 1
    torn = original[: max(12, original.find("runs") + 6)]
    assert torn != original
    inbox.write_text(torn, encoding="utf-8")
    with pytest.raises(OSError, match="unreadable AFM pruning inbox"):
        _write_inbox(str(vault), "trace-prune-2", [_pruning_proposal("trace-prune-2")])
    assert inbox.read_text(encoding="utf-8") == torn
    day = time.strftime("%Y-%m-%d", time.gmtime())
    assert inbox == vault / f"inbox/afm-pruning-{day}.json"


def test_pruning_write_inbox_refuses_dest_symlink_into_shop(tmp_path):
    import time

    from minni.afm_passes.pruning import _write_inbox

    day = time.strftime("%Y-%m-%d", time.gmtime())
    vault, shop = _file_symlink_to_shop(tmp_path, f"inbox/afm-pruning-{day}.json")
    with pytest.raises(OSError):
        _write_inbox(str(vault), "trace-prune", [_pruning_proposal()])
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]
    assert (vault / "inbox" / f"afm-pruning-{day}.json").is_symlink()


def test_pruning_append_audit_does_not_truncate_when_exists_lies(
    tmp_path, monkeypatch
):
    """exists()+write_text after a peer stamp must not wipe log.md."""
    from minni.afm_passes.pruning import _append_audit

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    (vault / "log.md").write_text(_PLUGIN_LOG, encoding="utf-8")
    orig_exists = Path.exists

    def lying_exists(self: Path) -> bool:
        if self == vault / "log.md":
            return False
        return orig_exists(self)

    monkeypatch.setattr(Path, "exists", lying_exists)
    _append_audit(vault, "trace-prune", 1, "inbox/afm-pruning-probe.json")
    text = (vault / "log.md").read_text(encoding="utf-8")
    assert _PLUGIN_LOG in text
    assert "pruning proposed" in text


def test_pruning_append_audit_does_not_clobber_append_after_exclusive_create(
    tmp_path, monkeypatch
):
    """O_EXCL seed must not write the header at offset 0 after a raced stamp."""
    from minni.afm_passes.pruning import _append_audit

    vault = tmp_path / "hermes-vault"
    vault.mkdir()
    orig_os_open = os.open

    def append_after_create(path) -> None:
        try:
            name = Path(os.fsdecode(path)).name
        except (TypeError, ValueError, OSError):
            return
        if name != "log.md":
            return
        extra = orig_os_open(path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(extra, _RACE_AUDIT.encode("utf-8"))
        finally:
            os.close(extra)

    def racing_os_open(path, flags, *args, **kwargs):
        fd = orig_os_open(path, flags, *args, **kwargs)
        if flags & os.O_EXCL:
            append_after_create(path)
        return fd

    monkeypatch.setattr(os, "open", racing_os_open)
    _append_audit(vault, "trace-prune", 1, "inbox/afm-pruning-probe.json")
    text = (vault / "log.md").read_text(encoding="utf-8")
    assert _RACE_AUDIT in text
    assert "pruning proposed" in text


def _plant_log_md_symlink_before_append(vault: Path, keep: Path, monkeypatch):
    """Swap log.md for a shop symlink between exclusive seed and append open."""
    orig_os_open = os.open

    def racing_os_open(path, flags, *args, **kwargs):
        try:
            dest = Path(os.fsdecode(path))
        except (TypeError, ValueError, OSError):
            return orig_os_open(path, flags, *args, **kwargs)
        if (
            dest.name == "log.md"
            and flags & os.O_APPEND
            and not flags & os.O_EXCL
        ):
            if not dest.is_symlink():
                try:
                    dest.unlink()
                except FileNotFoundError:
                    pass
                dest.symlink_to(keep)
        return orig_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_os_open)


def test_afm_append_audit_does_not_follow_raced_log_md_symlink_into_shop(
    tmp_path, monkeypatch
):
    from minni.afm_writer import _append_audit

    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    keep = shop / "keep.md"
    keep.write_text("restore\n", encoding="utf-8")
    _plant_log_md_symlink_before_append(vault, keep, monkeypatch)
    with pytest.raises(OSError):
        _append_audit(vault, "afm_loop", "wrote draft", {"k": "v"})
    assert keep.read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [keep]


def test_handoff_append_audit_does_not_follow_raced_log_md_symlink_into_shop(
    tmp_path, monkeypatch
):
    from minni.minnid_runtime.handoff import append_handoff_audit

    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    keep = shop / "keep.md"
    keep.write_text("restore\n", encoding="utf-8")
    _plant_log_md_symlink_before_append(vault, keep, monkeypatch)
    with pytest.raises(OSError):
        append_handoff_audit(vault, "handoff_sent", "wrote draft", {"k": "v"})
    assert keep.read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [keep]


def test_pruning_append_audit_refuses_log_md_symlink_into_shop(tmp_path):
    from minni.afm_passes.pruning import _append_audit

    vault, shop = _file_symlink_to_shop(tmp_path, "log.md")
    with pytest.raises(OSError):
        _append_audit(vault, "trace-prune", 1, "inbox/afm-pruning-probe.json")
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_pruning_append_audit_does_not_follow_raced_log_md_symlink_into_shop(
    tmp_path, monkeypatch
):
    from minni.afm_passes.pruning import _append_audit

    vault = tmp_path / "hermes-vault"
    shop = tmp_path / "shop-restore"
    vault.mkdir()
    shop.mkdir()
    keep = shop / "keep.md"
    keep.write_text("restore\n", encoding="utf-8")
    _plant_log_md_symlink_before_append(vault, keep, monkeypatch)
    with pytest.raises(OSError):
        _append_audit(vault, "trace-prune", 1, "inbox/afm-pruning-probe.json")
    assert keep.read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [keep]


def test_pruning_append_audit_refuses_daily_log_symlink_into_shop(tmp_path):
    import time

    from minni.afm_passes.pruning import _append_audit

    day = time.strftime("%Y-%m-%d", time.gmtime())
    vault, shop = _file_symlink_to_shop(tmp_path, f"logs/{day}.md")
    with pytest.raises(OSError):
        _append_audit(vault, "trace-prune", 1, "inbox/afm-pruning-probe.json")
    assert (shop / "keep.md").read_text(encoding="utf-8") == "restore\n"
    assert list(shop.iterdir()) == [shop / "keep.md"]


def test_atomic_write_text_preserves_dest_0600(tmp_path):
    from minni.afm_writer import _atomic_write_text

    dest = tmp_path / "secret.json"
    dest.write_text("{}\n", encoding="utf-8")
    dest.chmod(0o600)
    _atomic_write_text(dest, '{"ok": true}\n')
    assert dest.stat().st_mode & 0o777 == 0o600
    assert dest.read_text(encoding="utf-8") == '{"ok": true}\n'


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

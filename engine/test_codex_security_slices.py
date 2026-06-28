"""Security slice regression tests (plan plan-0f3f87e260f2e382)."""

from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minnid
from principal import EffectivePrincipal


def _patch_db(monkeypatch, tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "sec.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    import config as cfg_mod
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"))
    return db_obj, cfg


def _stamp(monkeypatch, agent_id, capabilities, roots=None):
    principal = EffectivePrincipal(
        agent_id=agent_id,
        capabilities=list(capabilities),
        allowed_vault_roots=list(roots or []),
    )
    monkeypatch.setattr(minnid, "resolve_effective_principal", lambda **_kw: principal)
    return principal


def test_vault_index_doc_rejects_out_of_root_path(monkeypatch, tmp_path):
    vault = tmp_path / "codex-vault"
    vault.mkdir()
    _patch_db(monkeypatch, tmp_path)
    _stamp(monkeypatch, "codex", ["learn"], [str(vault)])
    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "vault_index_doc",
            "params": {
                "agent_id": "codex",
                "content": "# note",
                "path": "../outside.md",
                "vault_path": str(vault),
            },
        }
    )
    assert resp.get("error", {}).get("code") == -32602, resp


def test_durable_index_ignores_forged_frontmatter_agent(monkeypatch, tmp_path):
    db_obj, _ = _patch_db(monkeypatch, tmp_path)
    content = "---\nagent: victim\nprivacy: safe\n---\n# forged owner\nbody text here.\n"
    monkeypatch.setattr(minnid, "_lazy_retrieval", lambda: types.SimpleNamespace(
        config=types.SimpleNamespace(vault_path=str(tmp_path / "vault")),
        index_durable_document=lambda **kwargs: {"status": "ok", "doc_id": 1, "chunks": 1},
    ))
    minnid._index_durable_learning("codex", content, key="learning:42", db=db_obj)
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT agent FROM documents WHERE path LIKE '%_durable%' ORDER BY doc_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["agent"] == "codex"


def test_inbox_ingest_rejects_forged_agent_id(monkeypatch, tmp_path):
    from afm_passes.inbox_ingest import _scan_inbox

    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    doc = {
        "kind": "stop_candidates",
        "candidates": ["lesson from inbox"],
        "agent_id": "victim",
    }
    (inbox / "bad.json").write_text(json.dumps(doc), encoding="utf-8")
    candidates, skipped = _scan_inbox(inbox, "codex")
    assert candidates == []
    assert skipped.get("_agent_mismatch") == 1


def test_ax_snapshot_ttl_expires(monkeypatch, tmp_path):
    from ax_memory import AXMemory

    db_obj, _ = _patch_db(monkeypatch, tmp_path)
    ax = AXMemory(db_obj)
    ax.add_snapshot("codex", "Finder", "{}", ttl_seconds=1)
    time.sleep(1.1)
    assert ax.get_latest_snapshot("codex", "Finder") is None


def test_path_within_root_blocks_symlink_escape(tmp_path):
    from path_safety import path_within_root

    root = tmp_path / "vault"
    wiki = root / "wiki"
    wiki.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = wiki / "escape.md"
    link.symlink_to(outside)
    assert not path_within_root(link, root)


def test_graph_accepted_pages_skips_private(tmp_path):
    from afm_passes._graph_utils import VaultPage, accepted_pages

    pages = [
        VaultPage("wiki/safe.md", tmp_path / "safe.md", "Safe", "concept", "accepted", privacy="safe"),
        VaultPage("wiki/private.md", tmp_path / "private.md", "Private", "concept", "accepted", privacy="private"),
    ]
    safe = accepted_pages(pages)
    assert len(safe) == 1
    assert safe[0].rel_path == "wiki/safe.md"


def test_graph_accepted_pages_allowlist_rejects_unknown_privacy(tmp_path):
    """PR91-5: an UNRECOGNIZED privacy label must fail closed (allowlist), not
    slip past a known-bad blocklist into AFM graph synthesis."""
    from afm_passes._graph_utils import VaultPage, accepted_pages

    pages = [
        VaultPage("wiki/safe.md", tmp_path / "safe.md", "Safe", "concept", "accepted", privacy="safe"),
        VaultPage("wiki/pub.md", tmp_path / "pub.md", "Pub", "concept", "accepted", privacy="public"),
        VaultPage("wiki/unset.md", tmp_path / "unset.md", "Unset", "concept", "accepted", privacy=""),
        VaultPage("wiki/sensitive.md", tmp_path / "sensitive.md", "Sensitive", "concept", "accepted", privacy="sensitive"),
        VaultPage("wiki/internal.md", tmp_path / "internal.md", "Internal", "concept", "accepted", privacy="internal"),
    ]
    accepted = {p.rel_path for p in accepted_pages(pages)}
    assert accepted == {"wiki/safe.md", "wiki/pub.md", "wiki/unset.md"}
    assert "wiki/sensitive.md" not in accepted
    assert "wiki/internal.md" not in accepted

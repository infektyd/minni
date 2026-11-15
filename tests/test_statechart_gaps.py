"""Regression tests for Statechart Gaps (Charts 1, 2, 5, 6, 7, 8, and 9)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

import minni.config as cfg_mod
import minni.db as db_mod
import minni.minnid as minnid
import minni.minnid_runtime.provenance as provenance
import minni.principal as principal_mod
from minni.config import MinniConfig
from minni.indexer import VaultIndexer
from minni.minnid_runtime.handoff import pending_handoff_leases, store_handoff_lease
from minni.minnid_runtime.vault_index import VaultIndexContext, handle_vault_index_doc
from minni.principal import (
    EffectivePrincipal,
    agent_scope_for,
    canonicalize_agent_id,
    clear_principal_caches,
    resolve_effective_principal,
)
from minni.thread_relay import (
    MAX_RELAY_CURSORS,
    active_plan_ids_from_vault,
    empty_relay_store,
    live_session_subscribers_for_plan,
    load_relay_store,
    save_relay_store,
    sweep_dead_relay_cursors,
)
from minni.vector_backend import VectorBackend, VectorHit, VectorItem
from minni.vector_sync import get_backend_state, should_sync, sync_backend


def _make_test_db(tmp_path: Path, vault_dir: Path | None = None) -> tuple[db_mod.MinniDB, MinniConfig]:
    vdir = vault_dir or (tmp_path / "vault")
    vdir.mkdir(parents=True, exist_ok=True)
    cfg = MinniConfig(
        db_path=str(tmp_path / "minni.db"),
        vault_path=str(vdir),
    )
    old_flag = db_mod._migrations_run
    try:
        db_mod._migrations_run = False
        db_obj = db_mod.MinniDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _stamp_principal(monkeypatch, agent_id: str, capabilities=("*",), *, auto_accept_own: bool = False):
    principal = EffectivePrincipal(
        agent_id=agent_id,
        capabilities=list(capabilities),
        auto_accept_own=auto_accept_own,
    )
    monkeypatch.setattr(provenance, "resolve_effective_principal", lambda **_kw: principal)
    return principal


def _patch_minnid_db(monkeypatch, tmp_path: Path) -> db_mod.MinniDB:
    db_obj, cfg = _make_test_db(tmp_path)
    wb_ns = types.SimpleNamespace(
        db=db_obj,
        config=types.SimpleNamespace(
            db_path=cfg.db_path,
            vault_path=cfg.vault_path,
            writeback_enabled=False,
        ),
        model=None,
    )
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: wb_ns)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", cfg.vault_path)
    return db_obj


# ---------------------------------------------------------------------------
# Chart 7 + Chart 1: Non-canonical principal files & canonicalize_agent_id
# ---------------------------------------------------------------------------


def test_chart7_noncanonical_principal_file_resolves_legacy_and_scope(monkeypatch, tmp_path: Path):
    pdir = tmp_path / "principals"
    pdir.mkdir(mode=0o700)

    local_file = pdir / "local.json"
    local_file.write_text(
        json.dumps({"agent_id": "main", "capabilities": ["*", "govern"]}),
        encoding="utf-8",
    )
    local_file.chmod(0o600)

    cc_file = pdir / "claude-code.json"
    cc_file.write_text(
        json.dumps(
            {
                "agent_id": "claude-code",
                "capabilities": ["*", "govern", "resolve_candidate"],
                "legacy_agent_ids": ["claudecode", "cc-legacy"],
                "legacy_agent_capabilities": {
                    "cc-legacy": ["read", "recall"],
                },
            }
        ),
        encoding="utf-8",
    )
    cc_file.chmod(0o600)
    clear_principal_caches()

    # supplied_agent_id=None still binds strictly to local.json -> 'main'
    local_eff = resolve_effective_principal(supplied_agent_id=None, principals_dir=pdir)
    assert local_eff.agent_id == "main"
    assert "govern" in local_eff.capabilities

    # Legacy alias in claude-code.json resolves as a non-denied mapped principal
    # with operator capabilities stripped (H7: NamedAgentNeverOperator)
    unmapped_legacy = resolve_effective_principal(supplied_agent_id="claudecode", principals_dir=pdir)
    assert unmapped_legacy.agent_id == "claudecode"
    assert unmapped_legacy.deny_reason is None
    assert "*" not in unmapped_legacy.capabilities
    assert "govern" not in unmapped_legacy.capabilities
    assert "resolve_candidate" not in unmapped_legacy.capabilities

    mapped_legacy = resolve_effective_principal(supplied_agent_id="cc-legacy", principals_dir=pdir)
    assert mapped_legacy.agent_id == "cc-legacy"
    assert mapped_legacy.deny_reason is None
    assert set(mapped_legacy.capabilities) == {"read", "recall"}

    # agent_scope_for unions non-canonical principal file aliases bidirectionally
    scope_cc = agent_scope_for("claude-code", principals_dir=pdir)
    assert {"claude-code", "claudecode", "cc-legacy"} <= set(scope_cc)
    scope_alias = agent_scope_for("claudecode", principals_dir=pdir)
    assert {"claude-code", "claudecode", "cc-legacy"} <= set(scope_alias)

    # canonicalize_agent_id maps aliases to canonical agent_id and passes unknown valid IDs through
    assert canonicalize_agent_id("claudecode", principals_dir=pdir) == "claude-code"
    assert canonicalize_agent_id("cc-legacy", principals_dir=pdir) == "claude-code"
    assert canonicalize_agent_id("antigravity", principals_dir=pdir) == "gemini"
    assert canonicalize_agent_id("worker-b", principals_dir=pdir) == "worker-b"

    # Multi-tenant local.json isolation: legacy_agent_ids & platform_agent_ids in local.json
    # must NEVER grant a legacy agent "main" scope or collapse peer platform_agent_ids
    local_file.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*", "govern"],
                "platform_agent_ids": ["gemini", "codex"],
                "legacy_agent_ids": ["old-codex", "mapped-alias"],
            }
        ),
        encoding="utf-8",
    )
    local_file.chmod(0o600)
    clear_principal_caches()
    assert "main" not in agent_scope_for("old-codex", principals_dir=pdir)
    assert "mapped-alias" not in agent_scope_for("old-codex", principals_dir=pdir)
    assert canonicalize_agent_id("codex", principals_dir=pdir) == "codex"
    assert "old-codex" not in principal_mod.handoff_aliases_for("gemini", principals_dir=pdir)
    assert "main" not in principal_mod.handoff_aliases_for("old-codex", principals_dir=pdir)

    # strict=True still rejects world/group-readable non-canonical principal files
    cc_file.chmod(0o644)
    clear_principal_caches()
    with pytest.raises(RuntimeError, match="must be 0600"):
        resolve_effective_principal(supplied_agent_id="claudecode", principals_dir=pdir)


# ---------------------------------------------------------------------------
# Chart 1: Handoff lease party hijack protection & alias canonicalization
# ---------------------------------------------------------------------------


def test_chart1_handoff_party_hijack_rejected_and_alias_canonicalized(monkeypatch, tmp_path: Path):
    db_obj = _patch_minnid_db(monkeypatch, tmp_path)
    pdir = tmp_path / "principals"
    pdir.mkdir(mode=0o700)
    monkeypatch.setattr(principal_mod, "PRINCIPALS_DIR", pdir)
    clear_principal_caches()

    vaults = {
        "codex": str(tmp_path / "codex-vault"),
        "claude-code": str(tmp_path / "claudecode-vault"),
        "claudecode": str(tmp_path / "claudecode-vault"),
        "worker-b": str(tmp_path / "worker-b-vault"),
    }
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps(vaults))

    # Send to alias "claudecode": canonicalizes to "claude-code" on send
    _stamp_principal(monkeypatch, "codex", capabilities=["*"])
    lease_id = "lease-chart1-0001"
    send_resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "claudecode",
                "packet": {
                    "lease_id": lease_id,
                    "from_agent": "codex",
                    "to_agent": "claudecode",
                    "kind": "handoff",
                    "task": "original codex -> claude-code task",
                    "envelope": "<sovereign:context event=\"Handoff\">orig</sovereign:context>",
                    "wikilink_refs": [],
                    "trace_id": "trace-c1",
                },
            },
        }
    )
    res = send_resp["result"]
    assert res["from_agent"] == "codex"
    assert res["to_agent"] == "claude-code"
    inbox_path = Path(res["inbox_path"])
    outbox_path = Path(res["outbox_path"])
    orig_inbox_bytes = inbox_path.read_bytes()
    orig_outbox_bytes = outbox_path.read_bytes()
    assert json.loads(orig_inbox_bytes.decode("utf-8"))["to_agent"] == "claude-code"

    # Cross-party hijack attempt with same lease_id MUST fail (-32004) without touching files or DB
    _stamp_principal(monkeypatch, "worker-b", capabilities=["*"])
    hijack_resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "worker-b",
                "to_agent": "claude-code",
                "packet": {
                    "lease_id": lease_id,
                    "from_agent": "worker-b",
                    "to_agent": "claude-code",
                    "kind": "handoff",
                    "task": "forged hijack task",
                    "envelope": "<sovereign:context event=\"Handoff\">forged</sovereign:context>",
                    "wikilink_refs": [],
                    "trace_id": "trace-hijack",
                },
            },
        }
    )
    assert hijack_resp["error"]["code"] == -32004
    assert "principal_mismatch" in hijack_resp["error"]["message"]
    assert inbox_path.read_bytes() == orig_inbox_bytes
    assert outbox_path.read_bytes() == orig_outbox_bytes
    assert not (Path(vaults["worker-b"]) / "_handoff" / "outbox" / inbox_path.name).exists()

    # Direct store_handoff_lease call with different parties also refuses overwrite
    handoff_ctx = minnid._handoff_context()
    wrote = store_handoff_lease(
        {
            "lease_id": lease_id,
            "from_agent": "worker-b",
            "to_agent": "claude-code",
            "task": "forged direct call",
            "requires_ack": True,
            "created_at": "2026-11-12T12:00:00Z",
            "expires_at": "2026-11-13T12:00:00Z",
        },
        inbox_path,
        outbox_path,
        handoff_ctx,
    )
    assert wrote is False
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT from_agent, to_agent, status FROM handoff_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
    assert (row["from_agent"], row["to_agent"], row["status"]) == ("codex", "claude-code", "pending")

    # Canonical recipient "claude-code" can ack it; same-party resend by "codex" re-arms it
    _stamp_principal(monkeypatch, "claude-code", capabilities=["*"])
    ack1 = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "minni_ack_handoff",
            "params": {"lease_id": lease_id, "agent_id": "claude-code", "status": "accepted"},
        }
    )
    assert ack1["result"]["status"] == "accepted"

    _stamp_principal(monkeypatch, "codex", capabilities=["*"])
    resend = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "daemon.handoff",
            "params": {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "packet": {
                    "lease_id": lease_id,
                    "from_agent": "codex",
                    "to_agent": "claude-code",
                    "kind": "handoff",
                    "task": "same-party re-send",
                    "envelope": "<sovereign:context event=\"Handoff\">re-armed</sovereign:context>",
                    "wikilink_refs": [],
                    "trace_id": "trace-c1-resend",
                },
            },
        }
    )
    assert resend["result"]["lease_persisted"] is True
    with db_obj.cursor() as c:
        row_rearmed = c.execute(
            "SELECT status FROM handoff_leases WHERE lease_id = ?",
            (lease_id,),
        ).fetchone()
    assert row_rearmed["status"] == "pending"

    # Legacy DB row addressed to raw "claudecode" is visible and ackable by "claude-code", not "worker-b"
    legacy_lid = "lease-legacy-claudecode"
    now = time.time()
    legacy_inbox = Path(vaults["claude-code"]) / "_handoff" / "inbox" / f"1_{legacy_lid}.json"
    legacy_inbox.parent.mkdir(parents=True, exist_ok=True)
    legacy_inbox.write_text(
        json.dumps(
            {
                "lease_id": legacy_lid,
                "from_agent": "codex",
                "to_agent": "claudecode",
                "task": "legacy alias row",
                "created_at": now,
                "expires_at": now + 3600,
                "lease_status": "pending",
            }
        ),
        encoding="utf-8",
    )
    with db_obj.transaction() as c:
        c.execute(
            """INSERT INTO handoff_leases
               (lease_id, from_agent, to_agent, status, inbox_path, outbox_path, created_at, expires_at)
               VALUES (?, 'codex', 'claudecode', 'pending', ?, '', ?, ?)""",
            (legacy_lid, str(legacy_inbox), now, now + 3600),
        )

    pending_for_cc = pending_handoff_leases("claude-code", context=handoff_ctx)
    assert any(item["lease_id"] == legacy_lid for item in pending_for_cc)

    _stamp_principal(monkeypatch, "worker-b", capabilities=["*"])
    wrong_ack = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "minni_ack_handoff",
            "params": {"lease_id": legacy_lid, "agent_id": "worker-b", "status": "accepted"},
        }
    )
    assert wrong_ack["error"]["code"] == -32004

    _stamp_principal(monkeypatch, "claude-code", capabilities=["*"])
    legacy_ack = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "minni_ack_handoff",
            "params": {"lease_id": legacy_lid, "agent_id": "claude-code", "status": "accepted"},
        }
    )
    assert legacy_ack["result"]["status"] == "accepted"
    db_obj.close()


# ---------------------------------------------------------------------------
# Chart 2: Contradiction review gate & intake guards
# ---------------------------------------------------------------------------


def test_chart2_resolve_contradiction_intake_guards_and_proposal_promotion(monkeypatch, tmp_path: Path):
    db_obj = _patch_minnid_db(monkeypatch, tmp_path)
    # Exercise real production writeback_enabled=True path (catches FK + contradiction_events regressions)
    db_obj.config.writeback_enabled = True
    minnid._lazy_writeback().config.writeback_enabled = True
    minnid._lazy_writeback()._write_to_disk = lambda *a, **kw: None
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)

    now = time.time()
    with db_obj.transaction() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status, workspace_id)
               VALUES ('codex', 'architecture', 'old architecture note', 1.0, ?, 'active', 'workspace-alpha')""",
            (now,),
        )
        old_lid = c.lastrowid
        # Attach a reader record + contradicts_id FK reference to old_lid
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status, workspace_id, contradicts_id)
               VALUES ('gemini', 'architecture', 'conflicting observation', 1.0, ?, 'active', 'workspace-alpha', ?)""",
            (now, old_lid),
        )
        c.execute(
            """INSERT INTO learning_reads (learning_id, agent_id, read_at)
               VALUES (?, 'gemini', ?)""",
            (old_lid, now),
        )

    # 1. Wire-supplied auto_accept_own rejected (-32004)
    _stamp_principal(monkeypatch, "codex", capabilities=["learn"], auto_accept_own=False)
    knob_resp = minnid._handle_resolve_contradiction(
        {
            "new_content": "updated architecture note",
            "supersede_ids": [old_lid],
            "auto_accept_own": True,
        },
        10,
    )
    assert knob_resp["error"]["code"] == -32004
    assert "auto_accept_own" in knob_resp["error"]["message"]

    # 2. Wrapper leak rejected (-32602) even for auto_accept_own=True
    _stamp_principal(monkeypatch, "codex", capabilities=["learn"], auto_accept_own=True)
    leak_resp = minnid._handle_resolve_contradiction(
        {
            "new_content": "<MINNI_ENVELOPE>leaked runtime wrapper</MINNI_ENVELOPE>",
            "supersede_ids": [old_lid],
        },
        11,
    )
    assert leak_resp["error"]["code"] == -32602
    assert "wrapper_leak" in leak_resp["error"]["message"]

    # 3. Non-operator owner with auto_accept_own=False stages candidate_packets ('proposed')
    _stamp_principal(monkeypatch, "codex", capabilities=["learn"], auto_accept_own=False)
    staged_resp = minnid._handle_resolve_contradiction(
        {
            "new_content": "reviewed architecture replacement",
            "supersede_ids": [old_lid],
            "category": "architecture",
            "assertion": "uses new transport",
            "applies_when": "daemon startup",
            "evidence_doc_ids": [42],
        },
        12,
    )
    staged = staged_resp["result"]
    assert staged["status"] == "proposed"
    assert staged["workspace_id"] == "workspace-alpha"
    assert staged["supersede_ids"] == [old_lid]
    assert staged["superseded"] == []
    cid = staged["candidate_id"]

    with db_obj.cursor() as c:
        old_row = c.execute(
            "SELECT status, superseded_by FROM learnings WHERE learning_id = ?",
            (old_lid,),
        ).fetchone()
        total_learnings = c.execute("SELECT COUNT(*) AS n FROM learnings").fetchone()["n"]
    assert old_row["status"] == "active"
    assert old_row["superseded_by"] is None
    assert total_learnings == 2

    # 4. Operator resolve_candidate(decision='accept') promotes new learning & supersedes old_lid
    _stamp_principal(monkeypatch, "main", capabilities=["*", "govern"])
    accept_resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 13)
    assert accept_resp["result"]["new_status"] == "accepted"
    new_lid = accept_resp["result"]["learning_id"]

    with db_obj.cursor() as c:
        old_after = c.execute(
            "SELECT status, superseded_by FROM learnings WHERE learning_id = ?",
            (old_lid,),
        ).fetchone()
        new_after = c.execute(
            """SELECT agent_id, category, content, status, workspace_id,
                      assertion, applies_when, evidence_doc_ids
               FROM learnings WHERE learning_id = ?""",
            (new_lid,),
        ).fetchone()
    assert old_after is not None
    assert old_after["status"] == "superseded"
    assert old_after["superseded_by"] == new_lid
    with db_obj.cursor() as c:
        ce_row = c.execute(
            """SELECT superseded_learning_id, new_learning_id, originating_agent
               FROM contradiction_events
               WHERE superseded_learning_id = ? AND new_learning_id = ?""",
            (old_lid, new_lid),
        ).fetchone()
    assert ce_row is not None
    assert ce_row["originating_agent"] == "codex"
    _stamp_principal(monkeypatch, "gemini", capabilities=["read", "recall"])
    sub_resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "minni_subscribe_contradictions",
            "params": {"agent_id": "gemini", "since": 0.0},
        }
    )
    events = sub_resp["result"]["events"]
    assert any(
        ev["superseded_learning_id"] == old_lid and ev["new_learning_id"] == new_lid
        for ev in events
    )
    assert new_after["agent_id"] == "codex"
    assert new_after["category"] == "architecture"
    assert new_after["content"] == "reviewed architecture replacement"
    assert new_after["status"] == "active"
    assert new_after["workspace_id"] == "workspace-alpha"
    assert new_after["assertion"] == "uses new transport"
    assert new_after["applies_when"] == "daemon startup"
    assert json.loads(new_after["evidence_doc_ids"]) == [42]
    db_obj.close()


# ---------------------------------------------------------------------------
# Charts 5 & 6: Vector sync SQLite dirty triggers, remove-error cursor, race seq
# ---------------------------------------------------------------------------


class _MemoryBackend(VectorBackend):
    name = "mem-vec"
    dim = 384

    def __init__(self) -> None:
        self.store: dict[int, VectorItem] = {}
        self.fail_remove_once = False
        self.on_remove = None

    def upsert(self, items: list[VectorItem]) -> None:
        for it in items:
            self.store[it.chunk_id] = it

    def remove(self, chunk_ids: list[int]) -> None:
        if self.on_remove is not None:
            cb = self.on_remove
            self.on_remove = None
            cb()
        if self.fail_remove_once:
            self.fail_remove_once = False
            raise RuntimeError("simulated remove failure")
        for cid in chunk_ids:
            self.store.pop(cid, None)

    def query(self, vector: np.ndarray, top_k: int = 10, filters=None) -> list[VectorHit]:
        return []

    def stats(self) -> dict:
        return {"vector_count": len(self.store)}


def test_charts5_and_6_sqlite_triggers_remove_failure_cursor_and_concurrent_delete(tmp_path: Path):
    db_obj, _ = _make_test_db(tmp_path)
    vec = np.ones(384, dtype=np.float32).tobytes()
    now = time.time()

    with db_obj.transaction() as c:
        c.execute("INSERT INTO documents (path, indexed_at) VALUES ('wiki/a.md', ?)", (now,))
        doc_a = c.lastrowid
        c.execute("INSERT INTO documents (path, indexed_at) VALUES ('wiki/b.md', ?)", (now,))
        doc_b = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding, computed_at)
               VALUES (?, 0, 'chunk a', ?, ?)""",
            (doc_a, vec, now),
        )
        cid_a = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding, computed_at)
               VALUES (?, 0, 'chunk b', ?, ?)""",
            (doc_b, vec, now),
        )
        cid_b = c.lastrowid

    backend = _MemoryBackend()
    res0 = sync_backend(backend, db_obj)
    assert res0["status"] == "ok"
    st0 = get_backend_state(backend.name, db_obj)
    assert st0["status"] == "ok"
    assert st0["dirty_seq"] == st0["synced_dirty_seq"]
    assert set(backend.store) == {cid_a, cid_b}

    # Automatic SQLite trigger on DELETE FROM documents (ON DELETE CASCADE -> chunk_embeddings)
    # with ZERO manual mark_dirty() call!
    with db_obj.transaction() as c:
        c.execute("DELETE FROM documents WHERE doc_id = ?", (doc_a,))

    st_after_cascade = get_backend_state(backend.name, db_obj)
    assert st_after_cascade["status"] == "dirty"
    assert st_after_cascade["dirty_seq"] > st0["dirty_seq"]
    assert should_sync(backend.name, db_obj) is True

    # Chart 5: Insert chunk c, upsert succeeds (uploading cid_c), then backend.remove fails.
    # Delete cid_c in SQLite before retry -> retry MUST still know last_synced_chunk_rowid >= cid_c
    # and remove cid_c from backend!
    with db_obj.transaction() as c:
        c.execute("INSERT INTO documents (path, indexed_at) VALUES ('wiki/c.md', ?)", (now,))
        doc_c = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding, computed_at)
               VALUES (?, 0, 'chunk c', ?, ?)""",
            (doc_c, vec, now),
        )
        cid_c = c.lastrowid

    backend.fail_remove_once = True
    res_err = sync_backend(backend, db_obj)
    assert res_err["status"] == "error"
    assert cid_c in backend.store
    st_err = get_backend_state(backend.name, db_obj)
    assert st_err["last_synced_chunk_rowid"] >= cid_c

    # Delete newly uploaded cid_c before retry
    with db_obj.transaction() as c:
        c.execute("DELETE FROM chunk_embeddings WHERE chunk_id = ?", (cid_c,))

    res_retry = sync_backend(backend, db_obj)
    assert res_retry["status"] == "ok"
    assert set(backend.store) == {cid_b}, f"orphaned vectors remained: {set(backend.store)}"

    # Chart 6: Concurrent SQLite DELETE while status == 'syncing' (inside backend.remove)
    # MUST keep status == 'dirty' and dirty_seq > synced_dirty_seq!
    with db_obj.transaction() as c:
        c.execute("INSERT INTO documents (path, indexed_at) VALUES ('wiki/d.md', ?)", (now,))
        doc_d = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding, computed_at)
               VALUES (?, 0, 'chunk d', ?, ?)""",
            (doc_d, vec, now),
        )
        cid_d = c.lastrowid

    sync_backend(backend, db_obj)
    assert set(backend.store) == {cid_b, cid_d}

    # Delete cid_b first to trigger dirty reconcile; during remove(), delete cid_d concurrently
    with db_obj.transaction() as c:
        c.execute("DELETE FROM chunk_embeddings WHERE chunk_id = ?", (cid_b,))

    def _delete_d_during_syncing():
        assert get_backend_state(backend.name, db_obj)["status"] == "syncing"
        with db_obj.transaction() as c2:
            c2.execute("DELETE FROM chunk_embeddings WHERE chunk_id = ?", (cid_d,))

    backend.on_remove = _delete_d_during_syncing
    res_race = sync_backend(backend, db_obj)
    st_race = get_backend_state(backend.name, db_obj)
    assert res_race["status"] == "dirty"
    assert st_race["status"] == "dirty"
    assert st_race["dirty_seq"] > st_race["synced_dirty_seq"]
    assert should_sync(backend.name, db_obj) is True

    # Next sync pass cleans up cid_d and settles to 'ok'
    res_final = sync_backend(backend, db_obj)
    assert res_final["status"] == "ok"
    assert set(backend.store) == set()
    db_obj.close()


# ---------------------------------------------------------------------------
# Chart 8: Vault index legacy rel_path cleanup & indexer rerank invalidation
# ---------------------------------------------------------------------------


def test_chart8_vault_index_doc_legacy_rel_path_cleanup_and_mtime_bump(tmp_path: Path):
    vault_dir = tmp_path / "codex-vault"
    (vault_dir / "wiki").mkdir(parents=True)
    db_obj, _ = _make_test_db(tmp_path, vault_dir=vault_dir)

    rel_path = "wiki/legacy_note.md"
    full_path = (vault_dir / rel_path).resolve()
    full_path.write_text("# Updated Note\n", encoding="utf-8")

    vec = np.ones(384, dtype=np.float32).tobytes()
    now = time.time()
    with db_obj.transaction() as c:
        c.execute("INSERT INTO documents (path, agent, indexed_at) VALUES (?, 'codex', ?)", (rel_path, now))
        legacy_doc_id = c.lastrowid
        c.execute("INSERT INTO documents (path, agent, indexed_at) VALUES ('wiki/peer.md', 'codex', ?)", (now,))
        peer_doc_id = c.lastrowid
        c.execute("INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, 'old', 'codex', '❓')", (legacy_doc_id, rel_path))
        c.execute(
            """INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding, computed_at)
               VALUES (?, 0, 'legacy chunk', ?, ?)""",
            (legacy_doc_id, vec, now),
        )
        legacy_cid = c.lastrowid
        c.execute(
            """INSERT INTO memory_links (source_doc_id, target_doc_id, link_type, weight, created_at)
               VALUES (?, ?, 'related_to', 1.0, ?)""",
            (legacy_doc_id, peer_doc_id, now),
        )

    invalidated_rerank: list[list[int]] = []
    removed_faiss: list[list[int]] = []
    trie_invalidations: list[int] = []

    engine_ns = types.SimpleNamespace(
        db=db_obj,
        _invalidate_durable_rerank=lambda cids: invalidated_rerank.append(list(cids)),
        _remove_live_faiss=lambda cids: removed_faiss.append(list(cids)),
        _invalidate_trie=lambda: trie_invalidations.append(1),
        index_durable_document=lambda **kw: {"status": "ok", "doc_id": 999, "chunks": 1},
    )

    principal = EffectivePrincipal(agent_id="codex", capabilities=["*"])
    ctx = VaultIndexContext(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}, "id": rid},
        make_response=lambda res, rid: {"result": res, "id": rid},
        make_mismatch_error=lambda w, s, rid: {"error": {"code": -32004, "message": f"{w}!={s}"}, "id": rid},
        handler_principal=lambda _p, _r: (principal, None),
        guard_vault_root=lambda *_a, **_k: None,
        lazy_retrieval=lambda: engine_ns,
        agent_vault=lambda _a: (vault_dir, True),
        record_latency=lambda *_a: None,
    )

    resp = handle_vault_index_doc(
        {"content": "# Updated Note\n", "path": rel_path, "agent": "codex"},
        1,
        ctx,
    )
    assert resp["result"]["status"] == "ok"
    assert invalidated_rerank == [[legacy_cid]]
    assert removed_faiss == [[legacy_cid]]
    assert trie_invalidations == [1]

    with db_obj.cursor() as c:
        rem_links = c.execute(
            "SELECT COUNT(*) AS n FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
            (legacy_doc_id, legacy_doc_id),
        ).fetchone()["n"]
        rem_docs = c.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE doc_id = ?",
            (legacy_doc_id,),
        ).fetchone()["n"]
    assert rem_links == 0
    assert rem_docs == 0

    # Retract failure returns -32000 AND bumps file mtime so next sweep won't fast-skip
    old_mtime = 1_700_000_000.0
    os.utime(full_path, (old_mtime, old_mtime))
    failing_ctx = VaultIndexContext(
        make_error=ctx.make_error,
        make_response=ctx.make_response,
        make_mismatch_error=ctx.make_mismatch_error,
        handler_principal=ctx.handler_principal,
        guard_vault_root=ctx.guard_vault_root,
        lazy_retrieval=lambda: (_ for _ in ()).throw(RuntimeError("simulated retract boom")),
        agent_vault=ctx.agent_vault,
        record_latency=ctx.record_latency,
    )
    err_resp = handle_vault_index_doc(
        {"content": "---\nstatus: draft\n---\n# Draft\n", "path": rel_path, "agent": "codex"},
        2,
        failing_ctx,
    )
    assert err_resp["error"]["code"] == -32000
    assert full_path.stat().st_mtime > old_mtime
    db_obj.close()


def test_chart8_vault_indexer_invalidates_rerank_on_retract_and_blocked(monkeypatch, tmp_path: Path):
    vault_dir = tmp_path / "codex-vault"
    wiki_dir = vault_dir / "wiki"
    wiki_dir.mkdir(parents=True)
    db_obj, cfg = _make_test_db(tmp_path, vault_dir=vault_dir)

    p_superseded = wiki_dir / "sup.md"
    p_blocked = wiki_dir / "blk.md"
    p_superseded.write_text("---\nstatus: superseded\n---\nbody\n", encoding="utf-8")
    p_blocked.write_text("---\nprivacy: blocked\n---\nsecret\n", encoding="utf-8")

    vec = np.ones(384, dtype=np.float32).tobytes()
    past = 1_000.0
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, agent, last_modified, indexed_at, page_status, privacy_level) VALUES (?, 'codex', ?, ?, 'accepted', 'safe')",
            (str(p_superseded.resolve()), past, past),
        )
        sup_doc_id = c.lastrowid
        c.execute(
            "INSERT INTO documents (path, agent, last_modified, indexed_at, page_status, privacy_level) VALUES (?, 'codex', ?, ?, 'accepted', 'safe')",
            (str(p_blocked.resolve()), past, past),
        )
        blk_doc_id = c.lastrowid
        c.execute(
            "INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding, computed_at) VALUES (?, 0, 'sup', ?, ?)",
            (sup_doc_id, vec, past),
        )
        sup_cid = c.lastrowid
        c.execute(
            "INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding, computed_at) VALUES (?, 0, 'blk', ?, ?)",
            (blk_doc_id, vec, past),
        )
        blk_cid = c.lastrowid
        c.execute(
            "INSERT INTO memory_links (source_doc_id, target_doc_id, link_type, weight, created_at) VALUES (?, ?, 'related_to', 1.0, ?)",
            (blk_doc_id, sup_doc_id, past),
        )

    invalidated: list[int] = []
    indexer = VaultIndexer(db=db_obj, config=cfg, embed=False)
    monkeypatch.setattr(indexer, "_invalidate_rerank_chunks", lambda cids: invalidated.extend(cids))
    monkeypatch.setattr(indexer, "_rebuild_faiss_index", lambda: None)
    monkeypatch.setattr(indexer, "_sync_vector_backends", lambda: None)

    res = indexer.index_vault()
    assert res["status"] == "success"
    assert set(invalidated) == {sup_cid, blk_cid}

    with db_obj.cursor() as c:
        rem_links = c.execute(
            "SELECT COUNT(*) AS n FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
            (blk_doc_id, blk_doc_id),
        ).fetchone()["n"]
    assert rem_links == 0
    db_obj.close()


# ---------------------------------------------------------------------------
# Chart 9: Python thread_relay live-PID protection past 24h / 7d
# ---------------------------------------------------------------------------


def test_chart9_live_pid_protected_past_8_days_while_dead_and_pidless_age_out(tmp_path: Path):
    vault = tmp_path / "claudecode-vault"
    active_thread_dir = vault / ".runtime" / "active-thread.d"
    artifacts_dir = vault / "wiki" / "artifacts"
    active_thread_dir.mkdir(parents=True)
    artifacts_dir.mkdir(parents=True)

    # Obtain a guaranteed-dead PID by spawning and reaping a trivial child
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=5)
    dead_pid = proc.pid

    now_dt = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
    now_ms = now_dt.timestamp() * 1000.0
    eight_days_ago_iso = (now_dt - timedelta(days=8)).isoformat().replace("+00:00", "Z")
    recent_iso = (now_dt - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")

    live_sk = "1111222233334444"
    hint_live_sk = "aaaabbbbccccdddd"
    dead_sk = "5555666677778888"
    pidless_old_sk = "99990000aaaabbbb"
    plan_id = "plan-chart9-long"

    # 1. Live host_pid (os.getpid()) with 8-day-old updated_at -> MUST stay live!
    (active_thread_dir / f"{live_sk}.json").write_text(
        json.dumps(
            {
                "plan_id": plan_id,
                "agent_id": "claude-code",
                "host_pid": os.getpid(),
                "updated_at": eight_days_ago_iso,
            }
        ),
        encoding="utf-8",
    )
    # 2. active-session-<pid>.json hint with live host_pid and 8-day-old updated_at -> MUST stay live!
    (vault / ".runtime" / f"active-session-{os.getpid()}.json").write_text(
        json.dumps(
            {
                "session_key": hint_live_sk,
                "host_pid": os.getpid(),
                "updated_at": eight_days_ago_iso,
            }
        ),
        encoding="utf-8",
    )
    # 3. Dead host_pid even with recent updated_at -> MUST be skipped/pruned!
    (active_thread_dir / f"{dead_sk}.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-dead-only",
                "agent_id": "claude-code",
                "host_pid": dead_pid,
                "updated_at": recent_iso,
            }
        ),
        encoding="utf-8",
    )
    # 4. PID-less entry with 8-day-old updated_at -> MUST age out!
    (active_thread_dir / f"{pidless_old_sk}.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-pidless-old",
                "agent_id": "claude-code",
                "updated_at": eight_days_ago_iso,
            }
        ),
        encoding="utf-8",
    )

    plans = active_plan_ids_from_vault(vault, now_ms=now_ms)
    assert plan_id in plans
    assert "plan-dead-only" not in plans
    assert "plan-pidless-old" not in plans

    subs = live_session_subscribers_for_plan(vault, plan_id, now_ms=now_ms)
    assert f"claude-code:{live_sk}" in subs
    assert f"claude-code:{dead_sk}" not in subs
    assert f"claude-code:{pidless_old_sk}" not in subs

    store = empty_relay_store()
    store["cursors"] = [
        {
            "subscriber_id": f"claude-code:{live_sk}",
            "plan_id": plan_id,
            "last_delivered_seq": 3,
            "updated_at": eight_days_ago_iso,
        },
        {
            "subscriber_id": f"claude-code:{hint_live_sk}",
            "plan_id": plan_id,
            "last_delivered_seq": 3,
            "updated_at": eight_days_ago_iso,
        },
        {
            "subscriber_id": f"claude-code:{dead_sk}",
            "plan_id": plan_id,
            "last_delivered_seq": 3,
            "updated_at": eight_days_ago_iso,
        },
        {
            "subscriber_id": f"claude-code:{pidless_old_sk}",
            "plan_id": plan_id,
            "last_delivered_seq": 3,
            "updated_at": eight_days_ago_iso,
        },
    ]
    save_relay_store(vault, store)

    sweep_res = sweep_dead_relay_cursors(vault, now_ms=now_ms, max_cursors=MAX_RELAY_CURSORS)
    assert sweep_res["pruned_cursors"] == 2
    remaining = {c["subscriber_id"] for c in load_relay_store(vault)["cursors"]}
    assert remaining == {f"claude-code:{live_sk}", f"claude-code:{hint_live_sk}"}

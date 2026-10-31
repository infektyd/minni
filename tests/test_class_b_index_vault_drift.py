"""Class B regression tests: index/vault drift, ghost <system-reminder> hits, trie/FAISS invalidation, and vault_index_doc canonical path."""

from __future__ import annotations

from pathlib import Path
import shutil
from types import SimpleNamespace

from minni.config import MinniConfig as Config
from minni.db import MinniDB
from minni.durability import classify_durability
from minni.durable_projection import durable_doc_path, evict_learning_to_cold_archive
from minni.graph_commit import ensure_canonical_learning_node
from minni.indexer import VaultIndexer
from minni.minnid_runtime.vault_index import VaultIndexContext, handle_vault_index_doc
from minni.retrieval import RetrievalEngine


def _make_engine(tmp_path: Path) -> tuple[Config, MinniDB, RetrievalEngine]:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "minni.db"
    cfg = Config(vault_path=str(vault_dir), db_path=str(db_path))
    db = MinniDB(cfg)
    engine = RetrievalEngine(db, cfg)
    return cfg, db, engine


def test_class_b_durability_classifies_wrapped_and_truncated_system_reminder_as_noise():
    wrapped = (
        "---\n"
        "title: Captured system reminder\n"
        "status: accepted\n"
        "category: architecture\n"
        "---\n\n"
        "# Captured system reminder\n\n"
        "<system-reminder>\n"
        "The following skills are available for use with the Skill tool.\n"
        "</system-reminder>\n"
    )
    res_wrapped = classify_durability({"content": wrapped})
    assert res_wrapped["kind"] == "noise"
    assert res_wrapped["rule_id"] == "noise.envelope_system_reminder"

    truncated = (
        "---\n"
        "title: Truncated reminder\n"
        "---\n\n"
        "# Truncated reminder\n\n"
        "<system-reminder>\n"
        "SessionStart.startup hook success: <EVIDENCE truncated without closing tag"
    )
    res_truncated = classify_durability({"content": truncated})
    assert res_truncated["kind"] == "noise"
    assert res_truncated["rule_id"] == "noise.envelope_system_reminder"


def test_class_b_ghost_system_reminder_and_purged_trie_cache_not_served(tmp_path: Path):
    cfg, db, engine = _make_engine(tmp_path)

    ghost_body = (
        "<system-reminder>\n"
        "praxisghosttoken The following skills are available for use with the Skill tool.\n"
        "</system-reminder>"
    )
    ghost_path = durable_doc_path(
        "claude-code", "learning:ghost", cfg.vault_path, content=ghost_body
    )
    engine.index_durable_document(
        content=ghost_body,
        path=ghost_path,
        agent="claude-code",
        sigil="claude-code",
        page_status="accepted",
    )

    real_body = "Always verify disk headroom with df -h / before running praxisghosttoken builds."
    with db.transaction() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, status, created_at) "
            "VALUES (?, ?, ?, 'active', '2026-10-30T00:00:00Z')",
            ("claude-code", "procedure", real_body),
        )
    real_path = durable_doc_path(
        "claude-code", "learning:real", cfg.vault_path, content=real_body
    )
    engine.index_durable_document(
        content=real_body,
        path=real_path,
        agent="claude-code",
        sigil="claude-code",
        page_status="accepted",
    )

    hits = engine.retrieve("praxisghosttoken", limit=5)
    hit_paths = [h.get("source") or h.get("path") for h in hits]
    assert ghost_path not in hit_paths, "captured <system-reminder> ghost note must never be served by retrieve()"
    assert real_path in hit_paths

    # Warm the in-memory Trie cache, then purge real_path and verify Trie does not serve the ghost
    trie_before = engine.trie_search("praxisghosttoken", limit=5)
    assert any((r.get("path") == real_path) for r in trie_before)

    engine.purge_durable_document(real_path)
    trie_after = engine.trie_search("praxisghosttoken", limit=5)
    assert not any((r.get("path") == real_path) for r in trie_after), (
        "purge_durable_document must invalidate the in-memory Trie cache immediately"
    )


def test_class_b_evict_learning_invalidates_trie_and_purges_by_learning_id(tmp_path: Path):
    cfg, db, engine = _make_engine(tmp_path)
    content = "Unique eviction token xylophonequasar for durable projection purge verification."
    with db.transaction() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, status, created_at) "
            "VALUES (?, ?, ?, 'active', '2026-10-30T00:00:00Z')",
            ("claude-code", "procedure", content),
        )
        lid = int(c.lastrowid)
        doc_id = ensure_canonical_learning_node(
            c,
            learning_id=lid,
            agent_id="claude-code",
            content=content,
            vault_path=cfg.vault_path,
            created_at="2026-10-30T00:00:00Z",
        )
    doc_path = durable_doc_path(
        "claude-code", f"learning:{lid}", cfg.vault_path, content=content
    )
    engine.index_durable_document(
        content=content,
        path=doc_path,
        agent="claude-code",
        sigil="claude-code",
        page_status="accepted",
    )
    assert any(r.get("doc_id") == doc_id for r in engine.trie_search("xylophonequasar", limit=5))

    with db.transaction() as c:
        evict_learning_to_cold_archive(
            c,
            engine,
            lid,
            reason="superseded_by:999",
            db_path=cfg.db_path,
            vault_path=cfg.vault_path,
        )

    assert not any(
        r.get("doc_id") == doc_id for r in engine.trie_search("xylophonequasar", limit=5)
    ), "evict_learning_to_cold_archive must invalidate engine Trie cache"


def test_class_b_vault_index_doc_uses_canonical_full_path_for_clean_pruning(tmp_path: Path):
    cfg, db, engine = _make_engine(tmp_path)
    note_rel = "wiki/notes/drift-target.md"
    note_full = Path(cfg.vault_path) / note_rel
    note_full.parent.mkdir(parents=True, exist_ok=True)
    note_text = "---\ntitle: Drift Target\nstatus: accepted\n---\n\n# Drift Target\n\nCanonical path drift check token.\n"
    note_full.write_text(note_text, encoding="utf-8")

    ctx = VaultIndexContext(
        make_error=lambda code, msg, req_id, data=None: {"error": {"code": code, "message": msg}, "id": req_id},
        make_response=lambda data, req_id: {"result": data, "id": req_id},
        make_mismatch_error=lambda w, s, r: {"error": "mismatch", "id": r},
        handler_principal=lambda p, r: (SimpleNamespace(agent_id="claude-code"), None),
        guard_vault_root=lambda *a, **kw: None,
        lazy_retrieval=lambda: engine,
        agent_vault=lambda aid: (Path(cfg.vault_path), True),
        record_latency=lambda *_a, **_kw: None,
        increment_request_count=lambda: None,
    )
    resp = handle_vault_index_doc(
        {
            "path": note_rel,
            "content": note_text,
            "agent": "claude-code",
            "page_status": "accepted",
            "privacy_level": "safe",
            "layer": "knowledge",
        },
        request_id=1,
        context=ctx,
    )
    assert resp.get("result", {}).get("status") == "ok"

    with db.cursor() as c:
        rows = c.execute("SELECT path FROM documents").fetchall()
    paths = [r["path"] for r in rows]
    assert str(note_full.resolve()) in paths, (
        f"vault_index_doc must store canonical absolute path, got {paths}"
    )
    assert note_rel not in paths

    # Now remove the file on disk and run index_vault() — row must be pruned!
    note_full.unlink()
    indexer = VaultIndexer(db, cfg)
    indexer.index_vault()
    with db.cursor() as c:
        remaining = [r["path"] for r in c.execute("SELECT path FROM documents").fetchall()]
    assert str(note_full.resolve()) not in remaining


def test_class_b_retrieve_drops_deleted_vault_note_without_reindex(tmp_path: Path):
    cfg, db, engine = _make_engine(tmp_path)
    note_path = Path(cfg.vault_path) / "wiki" / "notes" / "ephemeral-note.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_content = (
        "---\n"
        "title: Ephemeral Vault Note\n"
        "status: accepted\n"
        "privacy: safe\n"
        "---\n\n"
        "# Ephemeral Vault Note\n\n"
        "Recall token quasarghostdeletednote exists while the backing file is on disk.\n"
    )
    note_path.write_text(note_content, encoding="utf-8")

    indexer = VaultIndexer(db, cfg)
    indexer.index_vault()

    hits_before = engine.retrieve("quasarghostdeletednote", limit=5)
    sources_before = [h.get("source") or h.get("path") for h in hits_before]
    assert str(note_path.resolve()) in sources_before

    # Delete backing file WITHOUT reindexing — retrieve() must drop the ghost hit
    note_path.unlink()
    hits_after = engine.retrieve("quasarghostdeletednote", limit=5)
    sources_after = [h.get("source") or h.get("path") for h in hits_after]
    assert str(note_path.resolve()) not in sources_after, (
        f"retrieve() must drop hit whose backing file was deleted on disk, got {sources_after}"
    )


def test_class_b_retrieve_drops_note_when_whole_directory_deleted_without_reindex(tmp_path: Path):
    cfg, db, engine = _make_engine(tmp_path)
    rel_path = "wiki/deleted-subdir/dir-ghost-note.md"
    note_path = Path(cfg.vault_path) / rel_path
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_content = (
        "---\n"
        "title: Directory Ghost Note\n"
        "status: accepted\n"
        "privacy: safe\n"
        "---\n\n"
        "# Directory Ghost Note\n\n"
        "Recall token quasardirdeletedtoken exists while directory is on disk.\n"
    )
    note_path.write_text(note_content, encoding="utf-8")
    engine.index_durable_document(
        content=note_content,
        path=rel_path,
        agent="claude-code",
        sigil="📄",
        page_status="accepted",
    )

    hits_before = engine.retrieve("quasardirdeletedtoken", limit=5)
    sources_before = [h.get("source") or h.get("path") for h in hits_before]
    assert rel_path in sources_before

    # Delete the note's entire parent directory WITHOUT reindexing — retrieve() must drop it
    shutil.rmtree(note_path.parent)
    hits_after = engine.retrieve("quasardirdeletedtoken", limit=5)
    sources_after = [h.get("source") or h.get("path") for h in hits_after]
    assert rel_path not in sources_after, (
        f"retrieve() must drop hit when its entire parent directory was deleted, got {sources_after}"
    )


def test_class_b_retrieve_drops_superseded_or_missing_learning_durable_doc(tmp_path: Path):
    cfg, db, engine = _make_engine(tmp_path)
    content = "Durable rule nebularsupersededtoken for retrieve-time learnings status check."
    with db.transaction() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, status, created_at) "
            "VALUES (?, ?, ?, 'active', '2026-10-30T00:00:00Z')",
            ("claude-code", "procedure", content),
        )
        lid = int(c.lastrowid)
        ensure_canonical_learning_node(
            c,
            learning_id=lid,
            agent_id="claude-code",
            content=content,
            vault_path=cfg.vault_path,
            created_at="2026-10-30T00:00:00Z",
        )
    doc_path = durable_doc_path("claude-code", f"learning:{lid}", cfg.vault_path, content=content)
    engine.index_durable_document(
        content=content,
        path=doc_path,
        agent="claude-code",
        sigil="claude-code",
        page_status="accepted",
    )

    hits_active = engine.retrieve("nebularsupersededtoken", limit=5)
    assert any((h.get("source") or h.get("path")) == doc_path for h in hits_active)

    # Mark learning superseded in learnings table WITHOUT purging documents table
    with db.transaction() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, status, created_at) "
            "VALUES (?, ?, ?, 'active', '2026-10-30T00:01:00Z')",
            ("claude-code", "procedure", "Successor rule replacing nebularsupersededtoken."),
        )
        successor_lid = int(c.lastrowid)
        c.execute(
            "UPDATE learnings SET status = 'superseded', superseded_by = ? WHERE learning_id = ?",
            (successor_lid, lid),
        )

    hits_superseded = engine.retrieve("nebularsupersededtoken", limit=5)
    assert not any((h.get("source") or h.get("path")) == doc_path for h in hits_superseded), (
        f"retrieve() must drop _durable doc whose learnings row is superseded, got {hits_superseded}"
    )


def test_class_b_index_vault_migrates_and_prunes_legacy_relative_path_rows(tmp_path: Path):
    cfg, db, _engine = _make_engine(tmp_path)
    live_rel = "wiki/notes/legacy-live.md"
    gone_rel = "wiki/notes/legacy-gone.md"
    live_full = Path(cfg.vault_path) / live_rel
    live_full.parent.mkdir(parents=True, exist_ok=True)
    live_full.write_text(
        "---\ntitle: Legacy Live\nstatus: accepted\n---\n\n# Legacy Live\n\nStill on disk.\n",
        encoding="utf-8",
    )

    with db.transaction() as c:
        for rel in (live_rel, gone_rel):
            c.execute(
                "INSERT INTO documents (path, agent, sigil, last_modified, indexed_at, page_status, privacy_level, layer) "
                "VALUES (?, 'claude-code', '📄', 1.0, 1.0, 'accepted', 'safe', 'knowledge')",
                (rel,),
            )
            did = int(c.lastrowid)
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, 'legacy body', 'claude-code', '📄')",
                (did, rel),
            )

    indexer = VaultIndexer(db, cfg)
    indexer.index_vault()

    with db.cursor() as c:
        paths = [r["path"] for r in c.execute("SELECT path FROM documents").fetchall()]
    assert gone_rel not in paths, f"legacy relative row for deleted file must be pruned, got {paths}"
    assert live_rel not in paths, f"legacy relative row for live file must be migrated, got {paths}"
    assert str(live_full.resolve()) in paths, f"canonical resolved path must exist after index_vault, got {paths}"


def test_h11_vault_index_doc_retract_surfaces_db_error_instead_of_swallowing(
    tmp_path: Path, monkeypatch
):
    import sqlite3

    cfg, db, engine = _make_engine(tmp_path)
    note_rel = "wiki/notes/retract-ghost.md"
    note_full = Path(cfg.vault_path) / note_rel
    note_full.parent.mkdir(parents=True, exist_ok=True)
    accepted_text = "---\ntitle: Retract Ghost\nstatus: accepted\n---\n\n# Retract Ghost\n\nToken retractghosttoken123.\n"
    note_full.write_text(accepted_text, encoding="utf-8")

    ctx = VaultIndexContext(
        make_error=lambda code, msg, req_id, data=None: {
            "error": {"code": code, "message": msg},
            "id": req_id,
        },
        make_response=lambda data, req_id: {"result": data, "id": req_id},
        make_mismatch_error=lambda w, s, r: {"error": "mismatch", "id": r},
        handler_principal=lambda p, r: (SimpleNamespace(agent_id="claude-code"), None),
        guard_vault_root=lambda *a, **kw: None,
        lazy_retrieval=lambda: engine,
        agent_vault=lambda aid: (Path(cfg.vault_path), True),
        record_latency=lambda *_a, **_kw: None,
        increment_request_count=lambda: None,
    )
    resp1 = handle_vault_index_doc(
        {
            "path": note_rel,
            "content": accepted_text,
            "agent": "claude-code",
            "page_status": "accepted",
        },
        request_id=1,
        context=ctx,
    )
    assert resp1.get("result", {}).get("status") == "ok"

    def broken_transaction():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "transaction", broken_transaction)

    archived_text = "---\ntitle: Retract Ghost\nstatus: archived\n---\n\n# Retract Ghost\n\nToken retractghosttoken123.\n"
    note_full.write_text(archived_text, encoding="utf-8")
    resp2 = handle_vault_index_doc(
        {
            "path": note_rel,
            "content": archived_text,
            "agent": "claude-code",
            "page_status": "archived",
        },
        request_id=2,
        context=ctx,
    )
    assert "error" in resp2, f"retract failure was swallowed as success: {resp2}"


def test_h11_vault_index_doc_retract_invalidates_faiss_trie_and_rerank(
    tmp_path: Path, monkeypatch
):
    cfg, db, engine = _make_engine(tmp_path)
    note_rel = "wiki/notes/retract-cache.md"
    note_full = Path(cfg.vault_path) / note_rel
    note_full.parent.mkdir(parents=True, exist_ok=True)
    accepted_text = "---\ntitle: Retract Cache\nstatus: accepted\n---\n\n# Retract Cache\n\nToken retractcachetoken987.\n"
    note_full.write_text(accepted_text, encoding="utf-8")

    ctx = VaultIndexContext(
        make_error=lambda code, msg, req_id, data=None: {
            "error": {"code": code, "message": msg},
            "id": req_id,
        },
        make_response=lambda data, req_id: {"result": data, "id": req_id},
        make_mismatch_error=lambda w, s, r: {"error": "mismatch", "id": r},
        handler_principal=lambda p, r: (SimpleNamespace(agent_id="claude-code"), None),
        guard_vault_root=lambda *a, **kw: None,
        lazy_retrieval=lambda: engine,
        agent_vault=lambda aid: (Path(cfg.vault_path), True),
        record_latency=lambda *_a, **_kw: None,
        increment_request_count=lambda: None,
    )
    resp1 = handle_vault_index_doc(
        {
            "path": note_rel,
            "content": accepted_text,
            "agent": "claude-code",
            "page_status": "accepted",
        },
        request_id=1,
        context=ctx,
    )
    assert resp1.get("result", {}).get("status") == "ok"

    # Warm the in-memory Trie cache
    trie_before = engine.trie_search("retractcachetoken987", limit=5)
    assert any(r.get("path") == str(note_full.resolve()) for r in trie_before)

    with db.cursor() as c:
        expected_chunks = [
            int(r["chunk_id"])
            for r in c.execute("SELECT chunk_id FROM chunk_embeddings").fetchall()
        ]
    assert expected_chunks, "expected chunk_embeddings rows after indexing"

    faiss_removed: list[int] = []
    rerank_invalidated: list[int] = []
    orig_faiss = engine._remove_live_faiss
    orig_rerank = engine._invalidate_durable_rerank

    def spy_faiss(cids):
        faiss_removed.extend(cids)
        return orig_faiss(cids)

    def spy_rerank(cids):
        rerank_invalidated.extend(cids)
        return orig_rerank(cids)

    monkeypatch.setattr(engine, "_remove_live_faiss", spy_faiss)
    monkeypatch.setattr(engine, "_invalidate_durable_rerank", spy_rerank)

    archived_text = "---\ntitle: Retract Cache\nstatus: archived\n---\n\n# Retract Cache\n\nToken retractcachetoken987.\n"
    note_full.write_text(archived_text, encoding="utf-8")
    resp2 = handle_vault_index_doc(
        {
            "path": note_rel,
            "content": archived_text,
            "agent": "claude-code",
            "page_status": "archived",
        },
        request_id=2,
        context=ctx,
    )
    assert resp2.get("result", {}).get("retracted") is True

    trie_after = engine.trie_search("retractcachetoken987", limit=5)
    assert not any(r.get("path") == str(note_full.resolve()) for r in trie_after), (
        "retract must invalidate in-memory Trie cache"
    )
    assert sorted(faiss_removed) == sorted(expected_chunks), (
        f"retract must remove chunks from live FAISS: got {faiss_removed}, expected {expected_chunks}"
    )
    assert sorted(rerank_invalidated) == sorted(expected_chunks), (
        f"retract must invalidate durable rerank cache: got {rerank_invalidated}, expected {expected_chunks}"
    )


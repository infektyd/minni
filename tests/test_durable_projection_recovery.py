"""Temporary-store recovery of committed learnings missing document projections."""
import time

import numpy as np
import pytest

from minni.backfill import backfill_learning_projections, run_backfill
from minni.config import SovereignConfig
from minni.db import SovereignDB
from minni.durable_projection import durable_doc_path
from minni.retrieval import RetrievalEngine


class Encoder:
    def __init__(self, dim):
        self.dim = dim
        self.before_encode = None

    def encode(self, text, **kwargs):
        if self.before_encode:
            callback, self.before_encode = self.before_encode, None
            callback()
        vector = np.zeros(self.dim, dtype=np.float32)
        vector[0] = 1
        return vector if isinstance(text, str) else np.tile(vector, (len(text), 1))


@pytest.fixture
def store(tmp_path, monkeypatch):
    from minni import models

    config = SovereignConfig(
        db_path=str(tmp_path / "memory.db"), vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "notes"), faiss_index_path=str(tmp_path / "index.faiss"),
        reranker_enabled=False, attribution_enabled=False,
    )
    model = Encoder(config.embedding_dim)
    monkeypatch.setattr(models, "get_embedder", lambda: model)
    db = SovereignDB(config)
    yield db, config, model
    db.close()


def learning(db, content="Recovery specimen", agent="codex", status="active"):
    with db.cursor() as c:
        c.execute("INSERT INTO learnings (agent_id, category, content, created_at, status) "
                  "VALUES (?, 'general', ?, ?, ?)", (agent, content, time.time(), status))
        return c.lastrowid


def rows(db, table):
    with db.cursor() as c:
        return [dict(r) for r in c.execute("SELECT * FROM " + table).fetchall()]


def test_missing_projection_recovers_once_and_refreshes_warm_index(store):
    db, config, _ = store
    lid = learning(db)
    warm = RetrievalEngine(db, config)
    warm.index_durable_document(content="Warm seed", path=str(config.vault_path) + "/seed.md",
                                agent="codex")
    warm._set_current_deadline(None)
    warm._ensure_faiss_loaded()
    assert warm.faiss_index.count == 1
    result = run_backfill(db, config, on_vectors=warm._refresh_live_faiss)
    assert result["projections"]["repaired"] == 1
    assert warm.faiss_index.count == 2
    projected = [r for r in rows(db, "documents") if "_durable/" in r["path"]]
    assert len(projected) == 1
    assert projected[0]["page_type"] == "learning"
    assert projected[0]["agent"] == "codex"
    from pathlib import Path
    assert not Path(projected[0]["path"]).exists()
    ids = [r["chunk_id"] for r in rows(db, "chunk_embeddings")]
    assert run_backfill(db, config)["projections"]["repaired"] == 0
    assert [r["chunk_id"] for r in rows(db, "chunk_embeddings")] == ids
    assert [r["learning_id"] for r in warm.search_learnings(
        "specimen", agent_id="codex", update_access=False)] == [lid]
    assert projected[0]["doc_id"] in [r["doc_id"] for r in warm._semantic_search("specimen", 5)]


def test_existing_lexical_projection_uses_existing_vector_repair(store, monkeypatch):
    from minni import models
    db, config, model = store
    content = "Existing lexical-only specimen"
    learning(db, content)
    engine = RetrievalEngine(db, config)
    with monkeypatch.context() as patch:
        patch.setattr(models, "get_embedder", lambda: None)
        engine.index_durable_document(content=content, agent="codex", page_type="learning",
            path=durable_doc_path("codex", "", config.vault_path, content))
    before = rows(db, "documents")[0]["doc_id"]
    result = run_backfill(db, config)
    assert result["projections"]["repaired"] == 0
    assert result["documents"]["documents"] == 1
    assert rows(db, "documents")[0]["doc_id"] == before
    assert len(rows(db, "chunk_embeddings")) == 1


@pytest.mark.parametrize("mutation", ["supersede", "reject", "change_content", "change_owner"])
def test_publication_rechecks_after_concurrent_lifecycle_change(store, mutation):
    db, config, model = store
    lid = learning(db)
    def change():
        # Independent connection commits while encode runs outside the publish lock.
        other = SovereignDB(config)
        try:
            with other.cursor() as c:
                if mutation == "supersede":
                    c.execute("UPDATE learnings SET superseded_by=? WHERE learning_id=?", (lid, lid))
                elif mutation == "reject":
                    c.execute("UPDATE learnings SET status='rejected' WHERE learning_id=?", (lid,))
                elif mutation == "change_content":
                    c.execute("UPDATE learnings SET content='Different content' WHERE learning_id=?", (lid,))
                else:
                    c.execute("UPDATE learnings SET agent_id='claude' WHERE learning_id=?", (lid,))
        finally:
            other.close()
    model.before_encode = change
    result = backfill_learning_projections(db, config)
    assert result["skipped"] == 1
    assert not rows(db, "documents")
    assert not rows(db, "chunk_embeddings")


def test_identical_content_requires_one_active_exact_owner_row(store):
    db, config, _ = store
    learning(db, status="rejected")
    learning(db)
    learning(db, agent="claude", status="superseded")
    assert backfill_learning_projections(db, config)["repaired"] == 1
    documents = rows(db, "documents")
    assert len(documents) == 1
    assert documents[0]["agent"] == "codex"
    assert backfill_learning_projections(db, config)["repaired"] == 0


def test_ineligible_and_private_metadata_preserved(store):
    db, config, _ = store
    for status in ("expired", "rejected", "superseded"):
        learning(db, content=status + " learning", status=status)
    learning(db, "---\nprivacy: blocked\n---\nBlocked specimen")
    learning(db, "---\nstatus: expired\n---\nExpired specimen")
    learning(db, "---\nprivacy: private\nagent: foreign\ntype: wiki\n---\nPrivate specimen")
    result = backfill_learning_projections(db, config)
    assert result["repaired"] == 1
    document = rows(db, "documents")[0]
    assert document["privacy_level"] == "private"
    assert document["agent"] == "codex"
    assert document["page_type"] == "learning"
    from minni.principal import EffectivePrincipal, can_read_document
    assert not can_read_document(EffectivePrincipal(agent_id="claude", capabilities=["read"]),
                                 "default", document)


def test_failed_head_does_not_starve_next_batch(store, monkeypatch):
    db, config, _ = store
    learning(db, "bad head")
    learning(db, "good tail")
    original = RetrievalEngine.index_durable_document
    def fail_head(self, **kwargs):
        if kwargs["content"] == "bad head":
            return {"status": "degraded"}
        return original(self, **kwargs)
    monkeypatch.setattr(RetrievalEngine, "index_durable_document", fail_head)
    assert backfill_learning_projections(db, config, limit=1)["failed"] == 1
    assert backfill_learning_projections(db, config, limit=1)["repaired"] == 1
    assert backfill_learning_projections(db, config, limit=1)["failed"] == 1


def test_explicit_db_binding_rejects_other_config(store, tmp_path):
    db, config, _ = store
    from dataclasses import replace
    other = replace(config, db_path=str(tmp_path / "other.db"))
    with pytest.raises(ValueError, match="source database"):
        backfill_learning_projections(db, other)
    assert not (tmp_path / "other.db").exists()


def test_governed_accept_failure_then_real_scheduled_sweep(store, monkeypatch):
    import minni.minnid as daemon
    import minni.index_all as indexing
    from minni.principal import EffectivePrincipal

    db, config, _ = store
    monkeypatch.setattr(daemon, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(indexing, "discover_agent_vaults", lambda *args: [])
    warm = RetrievalEngine(db, config)
    monkeypatch.setattr(daemon, "_lazy_retrieval", lambda: warm)
    # Context DB factory must bind to this test store even though the function's
    # default config was imported before the fixture existed.
    from dataclasses import replace
    from minni.writeback import WriteBackMemory
    context = replace(daemon._governance_context(), sovereign_db=lambda: SovereignDB(config),
                      lazy_writeback=lambda: WriteBackMemory(db, config),
                      maybe_archive_inbox_source=lambda *args: None)
    monkeypatch.setattr(daemon, "_governance_context", lambda: context)
    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    staged = daemon._stage_candidate({"content": "Accepted recovery specimen", "_principal": op}, 1)
    cid = staged["result"]["candidate_id"]
    with monkeypatch.context() as patch:
        patch.setattr(daemon, "_lazy_retrieval", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
        accepted = daemon._resolve_candidate(
            {"candidate_id": cid, "decision": "accept", "_principal": op}, 2)
    assert "result" in accepted, accepted
    assert accepted["result"]["indexed"] is False
    # Promotion now commits its canonical identity even when the later
    # semantic projection fails. Repair must fill that same node, not replace it.
    canonical = rows(db, "documents")
    assert len(canonical) == 1
    canonical_id = canonical[0]["doc_id"]
    assert canonical[0]["memory_kind"] == "learning"
    assert len(rows(db, "learning_documents")) == 1
    assert not rows(db, "chunk_embeddings")
    result = daemon._backfill_sweep_once()
    assert result["shared"]["projections"]["repaired"] == 1
    assert rows(db, "documents")[0]["doc_id"] == canonical_id
    assert rows(db, "chunk_embeddings")
    assert len(rows(db, "learnings")) == 1
    assert len(rows(db, "documents")) == 1
    assert rows(db, "candidate_packets")[0]["status"] == "accepted"
    assert daemon._backfill_sweep_once()["shared"]["projections"]["repaired"] == 0


@pytest.mark.parametrize("failure", ["lazy", "callback", "false_result"])
@pytest.mark.parametrize("purge_before_retry", [False, True])
def test_next_sweep_retries_live_refresh_from_current_database(
    store, monkeypatch, failure, purge_before_retry,
):
    import minni.minnid as daemon
    import minni.index_all as indexing

    db, config, _ = store
    monkeypatch.setattr(daemon, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(indexing, "discover_agent_vaults", lambda *args: [])
    monkeypatch.setattr(daemon, "_backfill_shared_refresh_pending", {})
    warm = RetrievalEngine(db, config)
    warm.index_durable_document(content="Warm seed", path=config.vault_path + "/seed.md",
                                agent="codex")
    warm._set_current_deadline(None)
    warm._ensure_faiss_loaded()
    assert warm.faiss_index.ready and warm.faiss_index.count == 1
    monkeypatch.setattr(daemon, "_lazy_retrieval", lambda: warm)
    lid = learning(db)
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise RuntimeError("transient callback unavailable")
        if failure == "lazy":
            patch.setattr(daemon, "_lazy_retrieval", unavailable)
        elif failure == "callback":
            patch.setattr(warm, "_refresh_live_faiss", unavailable)
        else:
            patch.setattr(warm, "_refresh_live_faiss", lambda *args: False)
        daemon._backfill_sweep_once()
    assert len(rows(db, "documents")) == 2  # commit survived lost notification
    assert warm.faiss_index.count == 1
    assert daemon._backfill_shared_refresh_pending
    projection_path = durable_doc_path("codex", "", config.vault_path, "Recovery specimen")
    if purge_before_retry:
        with db.cursor() as c:
            c.execute("UPDATE learnings SET status='superseded' WHERE learning_id=?", (lid,))
        warm.purge_durable_document(projection_path)
    chunks_before = [r["chunk_id"] for r in rows(db, "chunk_embeddings")]
    # Failure of the retry itself must also remain pending across sweeps.
    with monkeypatch.context() as patch:
        patch.setattr(daemon, "_lazy_retrieval", unavailable)
        assert daemon._backfill_sweep_once()["shared_live_refresh"]["recovered"] is False
    retry = daemon._backfill_sweep_once()
    assert retry["shared_live_refresh"]["recovered"] is True
    assert retry["shared"]["projections"]["repaired"] == 0
    assert retry["shared"]["documents"]["documents"] == 0
    assert not daemon._backfill_shared_refresh_pending
    assert [r["chunk_id"] for r in rows(db, "chunk_embeddings")] == chunks_before
    assert warm.faiss_index.ready
    assert warm.faiss_index.count == (1 if purge_before_retry else 2)
    hits = warm._semantic_search("specimen", 5)
    assert any(r["path"] == projection_path for r in hits) is (not purge_before_retry)


def _placeholder(store, content, agent="codex"):
    """A committed canonical node + join with no FTS/chunk rows."""
    from minni.graph_commit import ensure_canonical_learning_node

    db, config, _ = store
    lid = learning(db, content, agent=agent)
    with db.transaction() as c:
        doc_id = ensure_canonical_learning_node(
            c, learning_id=lid, agent_id=agent, content=content,
            vault_path=config.vault_path, created_at=0.0,
        )
    assert doc_id is not None
    assert not rows(db, "chunk_embeddings")
    return lid, doc_id


@pytest.mark.parametrize("privacy,status", [("blocked", "accepted"), ("safe", "rejected")])
def test_closed_placeholder_is_never_resurrected(store, privacy, status):
    """A lifecycle-closed/restricted placeholder stays untouched: no repair
    count, no rewritten metadata, no vectors — through the actual backfill."""
    db, config, _ = store
    content = "Restricted repair specimen"
    lid, doc_id = _placeholder(store, content)
    with db.cursor() as c:
        c.execute("UPDATE documents SET privacy_level=?, page_status=? WHERE doc_id=?",
                  (privacy, status, doc_id))
    result = backfill_learning_projections(db, config)
    assert result["repaired"] == 0
    assert result["skipped"] == 1
    doc = rows(db, "documents")[0]
    assert (doc["doc_id"], doc["privacy_level"], doc["page_status"]) == (doc_id, privacy, status)
    assert len(rows(db, "documents")) == 1
    assert not rows(db, "chunk_embeddings")
    assert len(rows(db, "learning_documents")) == 1
    assert backfill_learning_projections(db, config)["repaired"] == 0


def test_engine_repair_gate_holds_inside_write_transaction(store):
    """The in-transaction stored-row gate refuses a closed placeholder even
    when the backfill pre-check is bypassed (concurrent lifecycle change)."""
    db, config, _ = store
    content = "Racy repair specimen"
    _, doc_id = _placeholder(store, content)
    engine = RetrievalEngine(db, config)
    path = durable_doc_path("codex", "", config.vault_path, content)
    result = engine.index_durable_document(
        content=content, path=path, agent="codex", page_type="learning",
        repair_projection=True,
    )
    assert result["status"] == "ok"
    assert result["doc_id"] == doc_id
    with db.cursor() as c:
        c.execute("UPDATE documents SET privacy_level='blocked', page_status='rejected'"
                  " WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM vault_fts WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM chunk_embeddings WHERE doc_id=?", (doc_id,))
    rerun = engine.index_durable_document(
        content=content, path=path, agent="codex", page_type="learning",
        repair_projection=True,
    )
    assert rerun["status"] == "skipped"
    assert rerun["reason"] == "projection_closed"
    doc = rows(db, "documents")[0]
    assert (doc["privacy_level"], doc["page_status"]) == ("blocked", "rejected")
    assert not rows(db, "chunk_embeddings")


def _shared_projected_node(store):
    from minni.graph_commit import ensure_canonical_learning_node

    db, config, _ = store
    content = "Shared historical recovery specimen"
    first, doc_id = _placeholder(store, content)
    second = learning(db, content)
    with db.transaction() as c:
        assert ensure_canonical_learning_node(
            c, learning_id=second, agent_id="codex", content=content,
            vault_path=config.vault_path, created_at=0.0,
        ) == doc_id
    engine = RetrievalEngine(db, config)
    path = durable_doc_path("codex", "", config.vault_path, content)
    assert engine.index_durable_document(
        content=content, path=path, agent="codex", page_type="learning",
        repair_projection=True,
    )["status"] == "ok"
    engine._set_current_deadline(None)
    engine._ensure_faiss_loaded()
    return first, second, doc_id, path, engine


@pytest.mark.parametrize("first_status", ["active", "superseded", "rejected", "expired"])
def test_shared_purge_preserves_live_projection_and_every_historical_join(store, first_status):
    db, config, _ = store
    first, second, doc_id, path, engine = _shared_projected_node(store)
    joins = rows(db, "learning_documents")
    chunks = rows(db, "chunk_embeddings")
    with db.cursor() as c:
        c.execute("UPDATE learnings SET status=? WHERE learning_id=?", (first_status, first))
    assert engine.purge_durable_document(path)["status"] == "shared_kept"
    assert rows(db, "learning_documents") == joins
    assert rows(db, "chunk_embeddings") == chunks
    assert rows(db, "documents")[0]["page_status"] == "accepted"
    assert engine.faiss_index.count == 1
    assert doc_id in [hit["doc_id"] for hit in engine._semantic_search("specimen", 5)]
    assert backfill_learning_projections(db, config)["repaired"] == 0


def test_final_purge_retires_identity_but_preserves_all_historical_joins(store):
    db, config, _ = store
    first, second, doc_id, path, engine = _shared_projected_node(store)
    joins = rows(db, "learning_documents")
    with db.cursor() as c:
        c.execute("UPDATE learnings SET status='superseded' WHERE learning_id IN (?, ?)",
                  (first, second))
        c.execute("UPDATE documents SET privacy_level='private' WHERE doc_id=?", (doc_id,))
    assert engine.purge_durable_document(path)["status"] == "ok"
    doc = rows(db, "documents")[0]
    assert (doc["doc_id"], doc["page_status"], doc["privacy_level"]) == (doc_id, "superseded", "private")
    assert rows(db, "learning_documents") == joins
    assert not rows(db, "vault_fts")
    assert not rows(db, "chunk_embeddings")
    # FAISS retains physical slots; removal tombstones them from live search.
    assert not engine.faiss_index.search(store[2].encode("specimen"), top_k=5)
    assert not engine._semantic_search("specimen", 5)
    assert backfill_learning_projections(db, config)["repaired"] == 0
    assert engine.purge_durable_document(path)["status"] == "ok"
    assert rows(db, "learning_documents") == joins


def test_governed_contradiction_preserves_shared_node_until_last_learning(store, monkeypatch):
    import minni.minnid as daemon
    from dataclasses import replace
    from minni.minnid_runtime.governance import handle_resolve_contradiction
    from minni.principal import EffectivePrincipal
    from minni.writeback import WriteBackMemory

    db, config, _ = store
    first, second, doc_id, path, engine = _shared_projected_node(store)
    monkeypatch.setattr(daemon, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(daemon, "_lazy_retrieval", lambda: engine)
    monkeypatch.setattr(config, "writeback_enabled", False)
    context = replace(daemon._governance_context(),
                      lazy_writeback=lambda: WriteBackMemory(db, config))
    principal = EffectivePrincipal(agent_id="codex", capabilities=["*"])
    joins = rows(db, "learning_documents")
    for index, lid in enumerate((first, second)):
        reply = handle_resolve_contradiction({
            "new_content": f"Corrected recovery specimen version {index}",
            "supersede_ids": [lid], "agent_id": "codex", "_principal": principal,
        }, index, context)
        assert "result" in reply, reply
        assert reply["result"]["superseded"] == [lid]
        assert rows(db, "learning_documents") == joins
        doc = next(row for row in rows(db, "documents") if row["doc_id"] == doc_id)
        assert doc["page_status"] == ("accepted" if index == 0 else "superseded")
        assert bool(rows(db, "chunk_embeddings")) is (index == 0)
    assert not engine._semantic_search("specimen", 5)


@pytest.mark.parametrize("statuses,expected", [
    (("expired", "rejected"), "expired"),
    (("rejected", "rejected"), "rejected"),
    (("superseded", "superseded"), "superseded"),
])
def test_canonical_purge_inactive_status_precedence(store, statuses, expected):
    db, _, _ = store
    first, second, doc_id, path, engine = _shared_projected_node(store)
    joins = rows(db, "learning_documents")
    with db.cursor() as c:
        for lid, status in zip((first, second), statuses):
            c.execute("UPDATE learnings SET status=? WHERE learning_id=?", (status, lid))
    assert engine.purge_durable_document(path)["status"] == "ok"
    doc = rows(db, "documents")[0]
    assert (doc["page_status"], doc["superseded_by"]) == (expected, None)
    assert rows(db, "learning_documents") == joins
    assert not rows(db, "chunk_embeddings")


def test_canonical_purge_chooses_largest_successor_learning(store):
    from minni.graph_commit import ensure_canonical_learning_node

    db, config, _ = store
    first, second, doc_id, path, engine = _shared_projected_node(store)
    successors = [learning(db, "Successor one"), learning(db, "Successor two")]
    with db.transaction() as c:
        for lid, content in zip(successors, ("Successor one", "Successor two")):
            successor_doc = ensure_canonical_learning_node(
                c, learning_id=lid, agent_id="codex", content=content,
                vault_path=config.vault_path, created_at=0.0,
            )
        for old, new in zip((first, second), reversed(successors)):
            c.execute("UPDATE learnings SET status='superseded', superseded_by=? WHERE learning_id=?",
                      (new, old))
    joins = rows(db, "learning_documents")
    assert engine.purge_durable_document(path)["status"] == "ok"
    doc = next(row for row in rows(db, "documents") if row["doc_id"] == doc_id)
    assert (doc["page_status"], doc["superseded_by"]) == ("superseded", successor_doc)
    assert rows(db, "learning_documents") == joins


@pytest.mark.parametrize("privacy,status", [("blocked", "accepted"), ("safe", "rejected")])
def test_canonical_purge_preserves_explicit_restriction_even_with_live_learning(store, privacy, status):
    db, config, _ = store
    _, _, doc_id, path, engine = _shared_projected_node(store)
    joins = rows(db, "learning_documents")
    with db.cursor() as c:
        c.execute("UPDATE documents SET privacy_level=?, page_status=? WHERE doc_id=?",
                  (privacy, status, doc_id))
    assert engine.purge_durable_document(path)["status"] == "ok"
    doc = rows(db, "documents")[0]
    assert (doc["privacy_level"], doc["page_status"]) == (privacy, status)
    assert rows(db, "learning_documents") == joins
    assert not rows(db, "vault_fts")
    assert not rows(db, "chunk_embeddings")
    assert not engine._semantic_search("specimen", 5)
    assert backfill_learning_projections(db, config)["repaired"] == 0


def test_unmapped_canonical_purge_preserves_existing_retirement(store):
    db, _, _ = store
    _, _, doc_id, path, engine = _shared_projected_node(store)
    with db.cursor() as c:
        c.execute("DELETE FROM learning_documents WHERE doc_id=?", (doc_id,))
        c.execute("UPDATE documents SET page_status='superseded', superseded_by=42 WHERE doc_id=?", (doc_id,))
    before = rows(db, "documents")
    assert engine.purge_durable_document(path)["status"] == "unmapped_kept"
    assert rows(db, "documents") == before


@pytest.mark.parametrize("privacy,status", [("blocked", "accepted"), ("safe", "rejected")])
def test_placeholder_repair_rechecks_restriction_changed_during_encoding(store, privacy, status):
    db, config, model = store
    _, doc_id = _placeholder(store, "Concurrent restricted repair specimen")
    def restrict():
        other = SovereignDB(config)
        try:
            with other.cursor() as c:
                c.execute("UPDATE documents SET privacy_level=?, page_status=? WHERE doc_id=?",
                          (privacy, status, doc_id))
        finally:
            other.close()
    model.before_encode = restrict
    assert backfill_learning_projections(db, config)["repaired"] == 0
    doc = rows(db, "documents")[0]
    assert (doc["privacy_level"], doc["page_status"]) == (privacy, status)
    assert not rows(db, "vault_fts")
    assert not rows(db, "chunk_embeddings")


def test_new_active_mapping_reactivates_aggregate_retirement_for_scheduled_repair(store, monkeypatch):
    from minni.writeback import WriteBackMemory

    db, config, _ = store
    first, second, doc_id, path, engine = _shared_projected_node(store)
    with db.cursor() as c:
        c.execute("UPDATE learnings SET status='superseded' WHERE learning_id IN (?, ?)",
                  (first, second))
    assert engine.purge_durable_document(path)["status"] == "ok"
    assert rows(db, "documents")[0]["page_status"] == "superseded"
    monkeypatch.setattr(config, "writeback_enabled", False)
    # store_learning commits canonical state without doing the later optional
    # normal document projection. The scheduled repair must recover this case.
    new_lid = WriteBackMemory(db, config).store_learning(
        agent_id="codex", content="Shared historical recovery specimen",
    )
    assert len(rows(db, "learning_documents")) == 3
    assert rows(db, "documents")[0]["doc_id"] == doc_id
    assert rows(db, "documents")[0]["page_status"] == "accepted"
    assert not rows(db, "chunk_embeddings")
    assert backfill_learning_projections(db, config)["repaired"] == 1
    assert rows(db, "documents")[0]["doc_id"] == doc_id
    assert rows(db, "chunk_embeddings")
    assert new_lid in [row["learning_id"] for row in rows(db, "learning_documents")]
    assert backfill_learning_projections(db, config)["repaired"] == 0


@pytest.mark.parametrize("restriction", ["blocked", "rejected", "draft", "expired",
                                          "aggregate_status", "successor", "active_sibling", "file"])
def test_repromotion_does_not_override_unproven_or_explicit_retirement(store, monkeypatch, restriction):
    from pathlib import Path
    from minni.writeback import WriteBackMemory

    db, config, _ = store
    first, second, doc_id, path, engine = _shared_projected_node(store)
    with db.cursor() as c:
        c.execute("UPDATE learnings SET status='superseded' WHERE learning_id IN (?, ?)", (first, second))
    engine.purge_durable_document(path)
    with db.cursor() as c:
        if restriction == "blocked":
            c.execute("UPDATE documents SET privacy_level='blocked' WHERE doc_id=?", (doc_id,))
        elif restriction in {"rejected", "draft", "expired"}:
            c.execute("UPDATE documents SET page_status=? WHERE doc_id=?", (restriction, doc_id))
        elif restriction == "successor":
            c.execute("UPDATE documents SET superseded_by=42 WHERE doc_id=?", (doc_id,))
        elif restriction == "aggregate_status":
            c.execute("UPDATE learnings SET status='expired' WHERE learning_id IN (?, ?)", (first, second))
        elif restriction == "active_sibling":
            c.execute("UPDATE learnings SET status='active' WHERE learning_id=?", (first,))
    if restriction == "file":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text("---\nstatus: superseded\n---\nExplicit file lifecycle")
    before = rows(db, "documents")[0]
    monkeypatch.setattr(config, "writeback_enabled", False)
    WriteBackMemory(db, config).store_learning(agent_id="codex", content="Shared historical recovery specimen")
    after = rows(db, "documents")[0]
    assert (after["page_status"], after["privacy_level"], after["superseded_by"]) == (
        before["page_status"], before["privacy_level"], before["superseded_by"])
    assert backfill_learning_projections(db, config)["repaired"] == 0


def test_repromotion_clears_matching_canonical_successor_pointer(store, monkeypatch):
    from minni.graph_commit import ensure_canonical_learning_node
    from minni.writeback import WriteBackMemory

    db, config, _ = store
    first, second, doc_id, path, engine = _shared_projected_node(store)
    successor = learning(db, "Intermediate successor")
    with db.transaction() as c:
        successor_doc = ensure_canonical_learning_node(c, learning_id=successor, agent_id="codex",
            content="Intermediate successor", vault_path=config.vault_path, created_at=0.0)
        c.execute("UPDATE learnings SET status='superseded', superseded_by=? WHERE learning_id IN (?, ?)",
                  (successor, first, second))
    engine.purge_durable_document(path)
    assert rows(db, "documents")[0]["superseded_by"] == successor_doc
    monkeypatch.setattr(config, "writeback_enabled", False)
    WriteBackMemory(db, config).store_learning(agent_id="codex", content="Shared historical recovery specimen")
    doc = rows(db, "documents")[0]
    assert (doc["page_status"], doc["superseded_by"]) == ("accepted", None)
    assert backfill_learning_projections(db, config)["repaired"] >= 1
    assert any(chunk["doc_id"] == doc_id for chunk in rows(db, "chunk_embeddings"))

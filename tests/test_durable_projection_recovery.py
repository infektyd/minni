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
    context = replace(daemon._governance_context(), sovereign_db=lambda: SovereignDB(config),
                      maybe_archive_inbox_source=lambda *args: None)
    monkeypatch.setattr(daemon, "_governance_context", lambda: context)
    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    staged = daemon._stage_candidate({"content": "Accepted recovery specimen", "_principal": op}, 1)
    cid = staged["result"]["candidate_id"]
    with monkeypatch.context() as patch:
        patch.setattr(daemon, "_lazy_retrieval", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
        accepted = daemon._resolve_candidate(
            {"candidate_id": cid, "decision": "accept", "_principal": op}, 2)
    assert accepted["result"]["indexed"] is False
    assert not rows(db, "documents")
    result = daemon._backfill_sweep_once()
    assert result["shared"]["projections"]["repaired"] == 1
    assert len(rows(db, "learnings")) == 1
    assert len(rows(db, "documents")) == 1
    assert rows(db, "candidate_packets")[0]["status"] == "accepted"
    assert daemon._backfill_sweep_once()["shared"]["projections"]["repaired"] == 0

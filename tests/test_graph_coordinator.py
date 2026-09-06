"""P1.3 coordinator core: synthetic tests on disposable SQLite.

Deterministic histogram embedder + brute-force chunk search + canned
classifier only. No live vault, model, network, or daemon. Covers: ok commit
with edge, no-candidates commit, fail-loud zero-write aborts (classifier
failure, stale target, embed failure, unknown pair), threshold/none drops,
self-edge drop on re-promotion, contradicts log row, disabled baseline,
repair complete/lexical/deferred, and learnings-row immutability in repair.
"""

import hashlib
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.graph_coordinator import (
    commit_learning_with_graph,
    repair_learning_projection,
)
from minni.principal import EffectivePrincipal

DIM = 8


def _store_id(db):
    import os

    return os.path.realpath(os.path.abspath(str(db.config.db_path)))


def _encode(text):
    vec = np.zeros(DIM, dtype=np.float32)
    for index, ch in enumerate(text):
        vec[(index * 31 + ord(ch)) % DIM] += 1.0
    norm = float(np.linalg.norm(vec)) or 1.0
    return (vec / norm).astype(np.float32).tobytes()


def _decode(blob):
    return np.frombuffer(blob, dtype=np.float32)


class _Fakes:
    """Deterministic prepare-phase collaborators bound to one store DB."""

    def __init__(self, db, fail_embed=False, classifier=None):
        self.db = db
        self.store_id = _store_id(db)
        self.fail_embed = fail_embed
        self.classifier = classifier

    def embed_text(self, text):
        if self.fail_embed:
            raise RuntimeError("embedder offline")
        return _encode(text)

    def chunk_texts(self, text):
        return [text]

    def search_chunks(self, vector, top_k):
        if not vector:
            return []
        query = _decode(vector)
        scored = []
        with self.db.cursor() as c:
            for row in c.execute(
                "SELECT chunk_id, doc_id, embedding FROM chunk_embeddings"
            ).fetchall():
                vec = _decode(row["embedding"])
                denom = float(np.linalg.norm(query) * np.linalg.norm(vec))
                cosine = float(np.dot(query, vec) / denom) if denom else 0.0
                scored.append({
                    "doc_id": row["doc_id"],
                    "chunk_id": row["chunk_id"], "cosine": cosine,
                })
        for hit in scored:
            hit["store_id"] = self.store_id
        scored.sort(key=lambda h: (-h["cosine"], h["doc_id"], h["chunk_id"]))
        return scored[:top_k]

    def get_metadata(self, store_id, doc_id):
        assert store_id == self.store_id, (store_id, self.store_id)
        with self.db.cursor() as c:
            doc = c.execute(
                "SELECT doc_id, path, agent, privacy_level, page_status,"
                " page_type, memory_kind FROM documents WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if doc is None:
                return None
            fts = c.execute(
                "SELECT content FROM vault_fts WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            content = fts["content"] if fts else ""
        return {
            "store_id": self.store_id, "doc_id": doc["doc_id"],
            "path": doc["path"], "agent": doc["agent"],
            "privacy_level": doc["privacy_level"],
            "page_status": doc["page_status"], "page_type": doc["page_type"],
            "memory_kind": doc["memory_kind"], "title": doc["path"],
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        }

    def get_content(self, store_id, doc_id):
        assert store_id == self.store_id, (store_id, self.store_id)
        with self.db.cursor() as c:
            fts = c.execute(
                "SELECT content FROM vault_fts WHERE doc_id = ?", (doc_id,)
            ).fetchone()
        if fts is None:
            return None
        return {"store_id": self.store_id, "doc_id": doc_id,
                "content": fts["content"]}

    def classify(self, source, candidates):
        assert self.classifier is not None
        return self.classifier(source, candidates)


class _FakeBatch:
    """Classifier result with provenance consistent with the SHARED renderer.

    Renders the received inputs itself so prompt/source/candidate hashes and
    pair accounting match what the coordinator independently recomputes —
    exactly like the production adapter. Edges are real InferredEdges.
    """

    def __init__(self, source, candidates, edges=(), ok=True, status="ok",
                 error=None):
        from types import SimpleNamespace

        from minni.edge_classifier import compute_canonical_hash
        from minni.edge_inference import _pair_id_of, render_edge_inference_prompt

        render = render_edge_inference_prompt(
            source=source, candidates=list(candidates))
        sent = [_pair_id_of(c, i) for i, c in enumerate(candidates)]
        rendered = list(render.pair_ids)
        self.inner = SimpleNamespace(
            edges=list(edges), ok=ok, status=status, error=error,
            classified_pair_ids=tuple(rendered) if ok else (),
            unclassified_pair_ids=tuple(
                p for p in sent if p not in set(rendered)) if ok else tuple(sent),
            evidence_hash="",
            prompt_hash=hashlib.sha256(
                render.prompt_text.encode()).hexdigest(),
            source_hash=compute_canonical_hash(source),
            candidates_hash=compute_canonical_hash(list(candidates)),
            batch_candidates_hash=compute_canonical_hash(
                [c for i, c in enumerate(candidates)
                 if _pair_id_of(c, i) in set(rendered)]),
            output_hash=compute_canonical_hash(
                [e.to_dict() for e in edges]),
            model_id="test-model", prompt_version="edge_inference_v1",
            raw_response=None,
        )
        self.inner.evidence_hash = hashlib.sha256(
            f"{self.inner.source_hash}:{self.inner.batch_candidates_hash}:"
            f"{self.inner.prompt_hash}:{self.inner.output_hash}:"
            f"{self.inner.prompt_version}:{self.inner.model_id}".encode()
        ).hexdigest()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _edge(pair_id, label, direction, confidence, evidence=(1,),
          rationale="test rationale"):
    from minni.edge_inference import InferredEdge

    return InferredEdge(pair_id=pair_id, label=label, direction=direction,
                        confidence=confidence,
                        supporting_evidence_indices=list(evidence),
                        rationale=rationale)


def _ok_classifier(*specs):
    """Canned edges: each spec is (pair_index, label, direction, confidence)."""

    def classify(source, candidates):
        edges = [_edge(candidates[i]["pair_id"], label, direction, conf)
                 for i, label, direction, conf in specs]
        return _FakeBatch(source, candidates, edges=edges)

    return classify


def _fail_classifier(status="provider_unavailable", error="offline"):
    def classify(source, candidates):
        return _FakeBatch(source, candidates, ok=False, status=status,
                          error=error)

    return classify


@pytest.fixture
def store(tmp_path):
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.migrations import run_migrations

    config = SovereignConfig(
        db_path=str(tmp_path / "graph.db"),
        vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "notes"),
        faiss_index_path=str(tmp_path / "index.faiss"),
        reranker_enabled=False, attribution_enabled=False,
    )
    db = SovereignDB(config)
    run_migrations(db._get_conn())
    yield db, config
    db.close()


def _principal():
    return EffectivePrincipal(agent_id="codex", capabilities=["learn"])


def _commit(db, config, content, fakes, **over):
    params = dict(
        db=db, store_id=fakes.store_id, principal=_principal(),
        content=content,
        vault_path=config.vault_path, embedding_model="test-model",
        embed_text=fakes.embed_text, chunk_texts=fakes.chunk_texts,
        search_chunks=fakes.search_chunks, get_metadata=fakes.get_metadata,
        get_content=fakes.get_content, classify=fakes.classify,
    )
    params.update(over)
    return commit_learning_with_graph(**params)


def _repair(db, config, learning_id, fakes, **over):
    params = dict(
        db=db, store_id=fakes.store_id, learning_id=learning_id,
        vault_path=config.vault_path, embedding_model="test-model",
        embed_text=fakes.embed_text, chunk_texts=fakes.chunk_texts,
        search_chunks=fakes.search_chunks, get_metadata=fakes.get_metadata,
        get_content=fakes.get_content, classify=fakes.classify,
        principal=_principal(),
    )
    params.update(over)
    return repair_learning_projection(**params)


def _rows(db, sql, args=()):
    with db.cursor() as c:
        return [tuple(r) for r in c.execute(sql, args).fetchall()]


def _counts(db):
    return {
        table: _rows(db, f"SELECT COUNT(*) FROM {table}")[0][0]
        for table in ("learnings", "documents", "learning_documents",
                      "vault_fts", "chunk_embeddings", "memory_links",
                      "contradiction_log")
    }


CONTENT_A = "The staging deploy requires a signed release checklist."
CONTENT_B = "The staging deploy requires a signed release checklist! Please review."


def test_first_commit_no_candidates_ok(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    result = _commit(db, config, CONTENT_A, fakes)
    assert result.status == "ok", result.error
    assert result.no_candidates is True
    assert result.edges == () and result.new_chunk_ids != ()
    assert len(result.new_chunk_ids) == len(result.new_chunk_vectors) == 1
    counts = _counts(db)
    assert counts == {"learnings": 1, "documents": 1, "learning_documents": 1,
                      "vault_fts": 1, "chunk_embeddings": 1, "memory_links": 0,
                      "contradiction_log": 0}
    doc = _rows(db, "SELECT memory_kind, page_type FROM documents")[0]
    assert doc == ("learning", "learning")


def test_second_commit_writes_edge(store):
    db, config = store
    seed = _Fakes(db, classifier=_ok_classifier())
    first = _commit(db, config, CONTENT_A, seed)
    assert first.status == "ok", first.error

    fakes = _Fakes(db, classifier=_ok_classifier(
        (0, "extends", "forward", 0.85)))
    result = _commit(db, config, CONTENT_B, fakes)
    assert result.status == "ok", result.error
    assert result.no_candidates is False
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert (edge.source_doc_id, edge.target_doc_id) == (
        result.doc_id, first.doc_id)
    assert edge.link_type == "extends" and edge.confidence == 0.85
    rows = _rows(db, "SELECT source_doc_id, target_doc_id, link_type,"
                     " confidence, inference_method, edge_status"
                     " FROM memory_links")
    assert rows == [(result.doc_id, first.doc_id, "extends", 0.85,
                     "local_classifier", "active")]
    assert _counts(db)["contradiction_log"] == 0


def test_classifier_failure_leaves_zero_writes(store):
    db, config = store
    seed = _Fakes(db, classifier=_ok_classifier())
    assert _commit(db, config, CONTENT_A, seed).status == "ok"
    before = _counts(db)
    fakes = _Fakes(db, classifier=_fail_classifier())
    result = _commit(db, config, CONTENT_B, fakes)
    assert result.status == "error"
    assert result.error_code == "edge_inference_failed"
    assert _counts(db) == before


def test_unknown_pair_fails_whole_batch(store):
    db, config = store
    seed = _Fakes(db, classifier=_ok_classifier())
    assert _commit(db, config, CONTENT_A, seed).status == "ok"
    before = _counts(db)
    def classifier(source, candidates):
        return _FakeBatch(source, candidates,
                          edges=[_edge("nope", "extends", "forward", 0.9)])
    fakes = _Fakes(db, classifier=classifier)
    result = _commit(db, config, CONTENT_B, fakes)
    assert result.status == "error"
    assert result.error_code == "edge_inference_failed"
    assert _counts(db) == before


def test_threshold_and_none_edges_dropped(store):
    db, config = store
    seed = _Fakes(db, classifier=_ok_classifier())
    assert _commit(db, config, CONTENT_A, seed).status == "ok"

    def below_threshold(source, candidates):
        # Every rendered pair gets a sub-threshold edge: valid batch,
        # zero persists.
        return _FakeBatch(source, candidates, edges=[
            _edge(c["pair_id"], "relates", "mutual", 0.50) for c in candidates
        ])
    low = _commit(db, config, CONTENT_B,
                  _Fakes(db, classifier=below_threshold))
    assert low.status == "ok", low.error
    assert low.edges == ()

    def none_label(source, candidates):
        return _FakeBatch(source, candidates, edges=[
            _edge(c["pair_id"], "none", "none", 0.99) for c in candidates
        ])
    nothing = _commit(db, config, CONTENT_B + " More detail here.",
                      _Fakes(db, classifier=none_label))
    assert nothing.status == "ok", nothing.error
    assert nothing.edges == ()
    assert _counts(db)["memory_links"] == 0
    assert _rows(db, "SELECT COUNT(*) FROM memory_links"
                     " WHERE link_type='none'")[0][0] == 0


def test_contradicts_persists_log_row(store):
    db, config = store
    seed = _Fakes(db, classifier=_ok_classifier())
    assert _commit(db, config, CONTENT_A, seed).status == "ok"

    def classifier(source, candidates):
        pid = candidates[0]["pair_id"]
        return _FakeBatch(source, candidates,
                          edges=[_edge(pid, "contradicts", "mutual", 0.91)])
    result = _commit(db, config, CONTENT_B, _Fakes(db, classifier=classifier))
    assert result.status == "ok", result.error
    assert result.contradiction_logged is True
    # mutual binds both directions.
    assert len(result.edges) == 2
    log = _rows(db, "SELECT memory_a_id, resolution_status, confidence"
                    " FROM contradiction_log")
    assert len(log) == 1
    assert log[0][0] == result.learning_id and log[0][1] == "unresolved"
    assert log[0][2] == 0.91


def test_repromotion_shares_node_and_drops_self_edge(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    first = _commit(db, config, CONTENT_A, fakes)
    assert first.status == "ok"

    seen = {}

    def classifier(source, candidates):
        seen["n"] = len(candidates)
        assert len(candidates) == 1
        pid = candidates[0]["pair_id"]
        return _FakeBatch(source, candidates,
                          edges=[_edge(pid, "extends", "forward", 0.9)])
    second = _commit(db, config, CONTENT_A,
                     _Fakes(db, classifier=classifier))
    assert second.status == "ok", second.error
    assert seen["n"] == 1  # own node shortlisted...
    assert second.doc_id == first.doc_id  # ...shared, never duplicated...
    assert second.edges == ()  # ...and the self-edge never persists.
    assert _counts(db)["memory_links"] == 0
    assert _counts(db)["documents"] == 1
    joins = _rows(db, "SELECT learning_id, doc_id FROM learning_documents")
    assert sorted(joins) == sorted([
        (first.learning_id, first.doc_id),
        (second.learning_id, second.doc_id),
    ])


def test_stale_target_aborts_with_zero_writes(store):
    db, config = store
    seed = _Fakes(db, classifier=_ok_classifier())
    first = _commit(db, config, CONTENT_A, seed)
    assert first.status == "ok"
    # Target learning dies (superseded) while its doc row stays accepted:
    # edges must bind live memory only.
    with db.cursor() as c:
        c.execute("INSERT INTO learnings (agent_id, category, content,"
                  " confidence, created_at) VALUES ('codex', 'general',"
                  " 'Successor scratch.', 1.0, 0.0)")
        successor = c.lastrowid
        c.execute("UPDATE learnings SET superseded_by=?, status='superseded'"
                  " WHERE learning_id=?", (successor, first.learning_id))

    def classifier(source, candidates):
        pid = candidates[0]["pair_id"]
        return _FakeBatch(source, candidates,
                          edges=[_edge(pid, "extends", "forward", 0.9)])
    before = _counts(db)
    result = _commit(db, config, CONTENT_B, _Fakes(db, classifier=classifier))
    assert result.status == "error"
    assert result.error_code == "stale_candidate"
    assert _counts(db) == before


def test_embed_failure_fail_loud_and_baseline_tolerates(store):
    db, config = store
    loud = _Fakes(db, fail_embed=True,
                  classifier=_ok_classifier())
    result = _commit(db, config, CONTENT_A, loud)
    assert result.status == "error"
    assert result.error_code == "embed_failed"
    assert _counts(db)["learnings"] == 0
    calm = _Fakes(db, fail_embed=True,
                  classifier=_ok_classifier())
    baseline = _commit(db, config, CONTENT_A, calm, graph_enabled=False)
    assert baseline.status == "ok", baseline.error
    assert baseline.edges == () and baseline.edges_deferred == "disabled"
    counts = _counts(db)
    assert counts["learnings"] == 1 and counts["vault_fts"] == 1
    assert counts["chunk_embeddings"] == 0 and counts["memory_links"] == 0
    emb = _rows(db, "SELECT embedding FROM learnings")[0][0]
    assert emb is None


def test_repair_complete_preserves_learning_row(store):
    db, config = store
    with db.cursor() as c:
        c.execute("INSERT INTO learnings (agent_id, category, content,"
                  " confidence, created_at) VALUES ('codex', 'general', ?,"
                  " 1.0, 0.0)", (CONTENT_A,))
        lid = c.lastrowid
    before = _rows(db, "SELECT * FROM learnings WHERE learning_id=?", (lid,))
    fakes = _Fakes(db, classifier=_ok_classifier())
    result = _repair(db, config, lid, fakes)
    assert result.status == "complete", result.error
    assert result.doc_id is not None and result.new_chunk_ids != ()
    assert _rows(db, "SELECT * FROM learnings WHERE learning_id=?", (lid,)) == before
    assert _counts(db)["learning_documents"] == 1
    assert _counts(db)["chunk_embeddings"] == 1


def test_repair_lexical_only_and_edges_deferred(store):
    db, config = store
    with db.cursor() as c:
        c.execute("INSERT INTO learnings (agent_id, category, content,"
                  " confidence, created_at) VALUES ('codex', 'general', ?,"
                  " 1.0, 0.0)", (CONTENT_A,))
        lid = c.lastrowid
    fakes = _Fakes(db, fail_embed=True, classifier=_fail_classifier())
    result = _repair(db, config, lid, fakes)
    assert result.status == "incomplete_lexical_only", result.error
    assert _counts(db)["vault_fts"] == 1
    assert _counts(db)["chunk_embeddings"] == 0
    assert _counts(db)["memory_links"] == 0


def test_repair_missing_learning_failed(store):
    db, config = store
    fakes = _Fakes(db)
    result = _repair(db, config, 4242, fakes)
    assert result.status == "failed"
    assert result.error_code == "learning_missing"


def _seed_raw_learning(db, content, agent="codex"):
    with db.cursor() as c:
        c.execute("INSERT INTO learnings (agent_id, category, content,"
                  " confidence, created_at) VALUES (?, 'general', ?,"
                  " 1.0, 0.0)", (agent, content))
        return c.lastrowid


def test_empty_ok_and_unevidenced_batches_fail_loud(store):
    """R1: a lying ok batch (empty edges, missing evidence/rationale,
    incompatible direction) never commits."""
    db, config = store
    assert _commit(db, config, CONTENT_A,
                   _Fakes(db, classifier=_ok_classifier())).status == "ok"

    def empty_ok(source, candidates):
        assert len(candidates) >= 1
        return _FakeBatch(source, candidates, edges=[])
    before = _counts(db)
    r1 = _commit(db, config, CONTENT_B, _Fakes(db, classifier=empty_ok))
    assert r1.status == "error" and r1.error_code == "edge_inference_failed"
    assert _counts(db) == before

    def bare(source, candidates):
        return _FakeBatch(source, candidates, edges=[
            _edge(c["pair_id"], "extends", "forward", 0.99,
                  evidence=(), rationale="")
            for c in candidates
        ])
    r2 = _commit(db, config, CONTENT_B, _Fakes(db, classifier=bare))
    assert r2.status == "error" and r2.error_code == "edge_inference_failed"
    assert _counts(db) == before

    def incompatible(source, candidates):
        return _FakeBatch(source, candidates, edges=[
            _edge(c["pair_id"], "relates", "forward", 0.99)
            for c in candidates
        ])
    r3 = _commit(db, config, CONTENT_B,
                 _Fakes(db, classifier=incompatible))
    assert r3.status == "error" and r3.error_code == "edge_inference_failed"
    assert _counts(db) == before


def test_repair_aborts_when_source_retired_mid_prepare(store):
    """R2: embed callback retires the learning; repair must fail, not heal."""
    db, config = store
    lid = _seed_raw_learning(db, CONTENT_A)
    fakes = _Fakes(db, classifier=_ok_classifier())

    def treacherous_embed(text):
        with db.cursor() as c:
            c.execute("UPDATE learnings SET status='rejected'"
                      " WHERE learning_id=?", (lid,))
        return _encode(text)
    fakes.embed_text = treacherous_embed
    result = _repair(db, config, lid, fakes)
    assert result.status == "failed"
    assert result.error_code == "repair_stale_source"
    assert _counts(db)["documents"] == 0
    assert _counts(db)["vault_fts"] == 0
    assert _counts(db)["chunk_embeddings"] == 0
    assert _counts(db)["learning_documents"] == 0


def test_repair_preserves_restricted_canonical(store):
    """R3: blocked/rejected canonical without projection gains nothing."""
    from minni.durable_projection import durable_doc_path

    db, config = store
    lid = _seed_raw_learning(db, CONTENT_A)
    path = durable_doc_path("codex", "", config.vault_path, CONTENT_A)
    with db.cursor() as c:
        c.execute("INSERT INTO documents (path, agent, privacy_level,"
                  " page_status, page_type, memory_kind) VALUES (?, 'codex',"
                  " 'blocked', 'rejected', 'learning', 'learning')", (path,))
        doc_id = c.lastrowid
    result = _repair(db, config, lid, _Fakes(db, classifier=_ok_classifier()))
    assert result.status == "failed"
    assert result.error_code == "repair_projection_restricted"
    row = _rows(db, "SELECT privacy_level, page_status FROM documents"
                    " WHERE doc_id=?", (doc_id,))[0]
    assert row == ("blocked", "rejected")
    assert _counts(db)["vault_fts"] == 0
    assert _counts(db)["chunk_embeddings"] == 0
    assert _counts(db)["learning_documents"] == 0


def test_cross_store_prepare_never_commits(store, tmp_path):
    """R4: evidence prepared against store B cannot commit into store A,
    even with colliding doc ids — store_id must be the canonical db path."""
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.migrations import run_migrations

    db_a, config_a = store
    config_b = SovereignConfig(
        db_path=str(tmp_path / "other.db"),
        vault_path=str(tmp_path / "vault_b"),
        writeback_path=str(tmp_path / "notes_b"),
        faiss_index_path=str(tmp_path / "index_b.faiss"),
        reranker_enabled=False, attribution_enabled=False,
    )
    db_b = SovereignDB(config_b)
    try:
        run_migrations(db_b._get_conn())
        seed_b = _Fakes(db_b, classifier=_ok_classifier())
        assert _commit(db_b, config_b, CONTENT_A, seed_b).status == "ok"
        fakes_b = _Fakes(db_b, classifier=_ok_classifier(
            (0, "extends", "forward", 0.9)))
        # Same content, same doc ids, different store: must refuse.
        result = commit_learning_with_graph(
            db=db_a, store_id=fakes_b.store_id, principal=_principal(),
            content=CONTENT_B, vault_path=config_a.vault_path,
            embedding_model="test-model", embed_text=fakes_b.embed_text,
            chunk_texts=fakes_b.chunk_texts,
            search_chunks=fakes_b.search_chunks,
            get_metadata=fakes_b.get_metadata,
            get_content=fakes_b.get_content, classify=fakes_b.classify)
        assert result.status == "error"
        assert result.error_code == "store_binding_mismatch"
        assert _counts(db_a)["learnings"] == 0
        assert _counts(db_a)["documents"] == 0
    finally:
        db_b.close()


def test_metadata_drift_in_target_aborts(store):
    """R4b: same ids, relocated-path metadata → full revalidation aborts.

    The tampered path still passes shortlist auth (same owner, safe
    privacy) so the pair reaches Phase B, where the authoritative row
    comparison catches the drift."""
    db, config = store
    assert _commit(db, config, CONTENT_A,
                   _Fakes(db, classifier=_ok_classifier())).status == "ok"
    fakes = _Fakes(db, classifier=_ok_classifier((0, "extends", "forward",
                                                  0.9)))
    real_metadata = fakes.get_metadata

    def tampered(store_id, doc_id):
        meta = real_metadata(store_id, doc_id)
        if meta is None:
            return None
        return dict(meta, path="/vault/_durable/relocated.md")
    fakes.get_metadata = tampered
    before = _counts(db)
    result = _commit(db, config, CONTENT_B, fakes)
    assert result.status == "error"
    assert result.error_code == "stale_candidate"
    assert _counts(db) == before


def test_lexical_repair_upgrades_vectors_truthfully(store):
    """R5: offline repair lands FTS-only; healthy re-repair fills vectors
    with a truthful complete status and post-commit vectors."""
    db, config = store
    lid = _seed_raw_learning(db, CONTENT_A)
    offline = _Fakes(db, fail_embed=True)
    first = _repair(db, config, lid, offline)
    assert first.status == "incomplete_lexical_only", first.error
    assert _counts(db)["vault_fts"] == 1
    assert _counts(db)["chunk_embeddings"] == 0
    assert first.new_chunk_ids == () and first.new_chunk_vectors == ()

    online = _Fakes(db, classifier=_ok_classifier())
    second = _repair(db, config, lid, online)
    assert second.status == "complete", second.error
    assert _counts(db)["chunk_embeddings"] == 1
    assert len(second.new_chunk_ids) == 1
    stored = _rows(db, "SELECT chunk_id, embedding FROM chunk_embeddings")
    assert stored[0][0] == second.new_chunk_ids[0]
    assert stored[0][1] == second.new_chunk_vectors[0]
    # Third repair is idempotent: complete, nothing new.
    third = _repair(db, config, lid, online)
    assert third.status == "complete"
    assert third.new_chunk_ids == ()


@pytest.mark.parametrize("field", ["output_hash", "evidence_hash"])
def test_forged_output_or_evidence_hash_aborts_without_writes(store, field):
    db, config = store
    assert _commit(db, config, CONTENT_A, _Fakes(db, classifier=_ok_classifier())).status == "ok"
    def forged(source, candidates):
        batch = _ok_classifier((0, "extends", "forward", 0.9))(source, candidates)
        setattr(batch.inner, field, "wrong")
        return batch
    before = _counts(db)
    result = _commit(db, config, CONTENT_B, _Fakes(db, classifier=forged))
    assert result.status == "error" and result.error_code == "edge_inference_failed"
    assert _counts(db) == before


@pytest.mark.parametrize("when", ["content_read", "classifier"])
def test_original_shortlist_metadata_is_retained_across_drift(store, when):
    db, config = store
    assert _commit(db, config, CONTENT_A, _Fakes(db, classifier=_ok_classifier())).status == "ok"
    target_id = _rows(db, "SELECT doc_id FROM documents")[0][0]
    fakes = _Fakes(db, classifier=_ok_classifier((0, "extends", "forward", 0.9)))
    metadata_calls = []
    original_metadata, original_content = fakes.get_metadata, fakes.get_content
    def metadata(store_id, doc_id):
        metadata_calls.append(doc_id)
        return original_metadata(store_id, doc_id)
    def relocate():
        with db.transaction() as c:
            c.execute("UPDATE documents SET path=? WHERE doc_id=?", ("/changed-during-prepare.md", target_id))
    def content(store_id, doc_id):
        result = original_content(store_id, doc_id)
        if when == "content_read":
            relocate()
        return result
    def classify(source, candidates):
        result = _ok_classifier((0, "extends", "forward", 0.9))(source, candidates)
        if when == "classifier":
            relocate()
        return result
    fakes.get_metadata, fakes.get_content, fakes.classifier = metadata, content, classify
    before = _counts(db)
    result = _commit(db, config, CONTENT_B, fakes)
    assert result.status == "error" and result.error_code == "stale_candidate"
    assert metadata_calls == [target_id]
    assert _counts(db) == before


@pytest.mark.parametrize("target", ["source", "candidates"])
def test_classifier_cannot_rewrite_retained_hash_inputs(store, target):
    db, config = store
    assert _commit(db, config, CONTENT_A, _Fakes(db, classifier=_ok_classifier())).status == "ok"
    def mutate(source, candidates):
        # Non-rendered provenance field: prompt remains unchanged, but the
        # batch must still bind the original coordinator input snapshot.
        if target == "source":
            source["content_sha256"] = "f" * 64
        else:
            candidates[0]["content_sha256"] = "f" * 64
        return _ok_classifier((0, "extends", "forward", 0.9))(source, candidates)
    before = _counts(db)
    result = _commit(db, config, CONTENT_B, _Fakes(db, classifier=mutate))
    assert result.status == "error" and result.error_code == "edge_inference_failed"
    assert _counts(db) == before


@pytest.mark.parametrize("failure", ["missing", "exception"])
def test_unavailable_candidate_content_aborts_new_promotion(store, failure):
    db, config = store
    assert _commit(db, config, CONTENT_A, _Fakes(db, classifier=_ok_classifier())).status == "ok"
    calls = []
    def classify(*args):
        calls.append(True)
        raise AssertionError("unprepared candidates must not reach classifier")
    def unavailable(*args):
        if failure == "exception":
            raise OSError("temporary content failure")
        return None
    before = _counts(db)
    result = _commit(db, config, CONTENT_B, _Fakes(db, classifier=classify), get_content=unavailable)
    assert result.status == "error"
    assert result.error_code == "candidate_preparation_failed"
    assert _counts(db) == before
    assert calls == []


def test_unavailable_candidate_content_marks_repair_edges_degraded(store):
    db, config = store
    assert _commit(db, config, CONTENT_A, _Fakes(db, classifier=_ok_classifier())).status == "ok"
    lid = _seed_raw_learning(db, CONTENT_B)
    result = _repair(db, config, lid, _Fakes(db, classifier=_ok_classifier()), get_content=lambda *args: None)
    assert result.status == "complete"
    assert result.edges_deferred == "degraded"
    assert result.edges == ()
    assert _rows(db, "SELECT COUNT(*) FROM learning_documents WHERE learning_id=?", (lid,)) == [(1,)]


@pytest.mark.parametrize("missing_first", [False, True])
def test_pair_cap_is_allowed_but_missing_content_before_eight_valid_pairs_is_not(store, missing_first):
    db, config = store
    for i in range(12):
        result = _commit(db, config, f"Stored graph candidate number {i}.",
                         _Fakes(db), graph_enabled=False)
        assert result.status == "ok"
    fakes = _Fakes(db)
    hits = [{"store_id": fakes.store_id, "doc_id": doc_id, "chunk_id": chunk_id, "cosine": 0.9}
            for chunk_id, doc_id in _rows(db, "SELECT chunk_id, doc_id FROM chunk_embeddings ORDER BY doc_id")]
    reads, classified = [], []
    original_content = fakes.get_content
    def content(store_id, doc_id):
        reads.append(doc_id)
        if missing_first and doc_id == hits[0]["doc_id"]:
            return None
        return original_content(store_id, doc_id)
    def classify(source, candidates):
        classified.append(len(candidates))
        return _FakeBatch(source, candidates, edges=[
            _edge(c["pair_id"], "none", "none", 0.1, evidence=()) for c in candidates])
    before = _counts(db)
    result = _commit(db, config, CONTENT_B, fakes, search_chunks=lambda *args: hits,
                     get_content=content, classify=classify)
    if missing_first:
        assert result.status == "error" and result.error_code == "candidate_preparation_failed"
        assert len(reads) == 9  # one unavailable + eight successful, still a failure
        assert classified == []
        assert _counts(db) == before
    else:
        assert result.status == "ok" and result.edges_deferred is None
        assert result.no_candidates is False
        assert len(reads) == 8 and classified == [8]
    assert all(hit["doc_id"] not in reads for hit in hits[9:])

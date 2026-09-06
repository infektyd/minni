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

from dataclasses import replace

from minni.graph_coordinator import (
    GraphCommitAborted,
    LearningFields,
    commit_learning_with_graph,
    commit_prepared_learning,
    prepare_learning_with_graph,
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


def _prepare(db, config, content, fakes, **over):
    params = dict(
        db=db, store_id=fakes.store_id, principal=_principal(),
        content=content,
        vault_path=config.vault_path, embedding_model="test-model",
        embed_text=fakes.embed_text, chunk_texts=fakes.chunk_texts,
        search_chunks=fakes.search_chunks, get_metadata=fakes.get_metadata,
        get_content=fakes.get_content, classify=fakes.classify,
    )
    params.update(over)
    return prepare_learning_with_graph(**params)


def _seed_candidate(db, content, principal="codex", workspace="default"):
    with db.cursor() as c:
        c.execute(
            "INSERT INTO candidate_packets"
            " (principal, workspace_id, content, status, proposed_at)"
            " VALUES (?, ?, ?, 'proposed', 0.0)",
            (principal, workspace, content),
        )
        return int(c.lastrowid)


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


class _Rollback(Exception):
    """Caller-owned abort used to roll back an outer SQLite transaction."""


def test_outer_rollback_after_phase_b_leaves_zero_durable_rows(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    prepared = _prepare(db, config, CONTENT_A, fakes)
    assert prepared.status == "ok" and prepared.payload is not None
    try:
        with db.transaction() as c:
            staged = commit_prepared_learning(
                c, prepared.payload, db=db, principal=_principal(),
            )
            assert staged.status == "staged"
            assert staged.learning_id is not None
            assert c.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 1
            raise _Rollback()
    except _Rollback:
        pass
    assert _counts(db) == {
        "learnings": 0, "documents": 0, "learning_documents": 0,
        "vault_fts": 0, "chunk_embeddings": 0, "memory_links": 0,
        "contradiction_log": 0,
    }


def test_candidate_accept_is_atomic_with_learning_and_projection(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    cid = _seed_candidate(db, CONTENT_A)
    prepared = _prepare(db, config, CONTENT_A, fakes)
    assert prepared.status == "ok" and prepared.payload is not None
    try:
        with db.transaction() as c:
            c.execute("UPDATE candidate_packets SET status='accepted' WHERE candidate_id=?", (cid,))
            commit_prepared_learning(
                c, prepared.payload, db=db, principal=_principal(),
            )
            raise _Rollback()
    except _Rollback:
        pass
    assert _counts(db)["learnings"] == 0
    assert _counts(db)["documents"] == 0
    status = _rows(db, "SELECT status FROM candidate_packets WHERE candidate_id=?",
                   (cid,))
    assert status == [("proposed",)]

    with db.transaction() as c:
        c.execute("UPDATE candidate_packets SET status='accepted', resolved_by='codex', resolution_reason='accepted' WHERE candidate_id=?", (cid,))
        staged = commit_prepared_learning(
            c, prepared.payload, db=db, principal=_principal(),
        )
    assert staged.status == "staged"
    assert staged.learning_id is not None and staged.doc_id is not None
    packet = _rows(db, "SELECT status, resolved_by, resolution_reason"
                       " FROM candidate_packets WHERE candidate_id=?", (cid,))
    assert packet == [("accepted", "codex", "accepted")]
    assert _counts(db)["learnings"] == 1
    assert _counts(db)["documents"] == 1
    assert _counts(db)["learning_documents"] == 1
    assert _counts(db)["vault_fts"] == 1
    assert _counts(db)["chunk_embeddings"] == 1
    join = _rows(db, "SELECT learning_id, doc_id FROM learning_documents")
    assert join == [(staged.learning_id, staged.doc_id)]


def test_embed_and_classify_assert_not_in_transaction(store):
    db, config = store
    seed = _Fakes(db, classifier=_ok_classifier())
    assert _commit(db, config, CONTENT_A, seed).status == "ok"
    seen = []
    canned = _ok_classifier((0, "extends", "forward", 0.85))
    fakes = _Fakes(db, classifier=canned)
    real_embed = fakes.embed_text

    def embed(text):
        seen.append(("embed", bool(db._get_conn().in_transaction)))
        return real_embed(text)

    def classify(source, candidates):
        seen.append(("classify", bool(db._get_conn().in_transaction)))
        return canned(source, candidates)

    result = _commit(db, config, CONTENT_B, fakes,
                     embed_text=embed, classify=classify)
    assert result.status == "ok", result.error
    assert ("embed", False) in seen
    assert ("classify", False) in seen
    assert all(locked is False for _, locked in seen)


def test_prepare_rejects_open_write_transaction(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    with db.transaction() as _c:
        prepared = _prepare(db, config, CONTENT_A, fakes)
    assert prepared.status == "error"
    assert prepared.error_code == "model_in_transaction"
    assert prepared.payload is None
    assert _counts(db)["learnings"] == 0


def test_commit_prepared_does_not_invoke_phase_c_or_models(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    calls = []
    real_embed, real_search = fakes.embed_text, fakes.search_chunks
    canned = fakes.classifier

    def embed(text):
        calls.append("embed")
        return real_embed(text)

    def search(vector, top_k):
        calls.append("search")
        return real_search(vector, top_k)

    def classify(source, candidates):
        calls.append("classify")
        return canned(source, candidates)

    fakes.embed_text, fakes.search_chunks, fakes.classifier = (
        embed, search, classify)
    prepared = _prepare(db, config, CONTENT_A, fakes)
    assert prepared.status == "ok"
    assert "embed" in calls
    before = list(calls)
    with db.transaction() as c:
        assert c.connection.in_transaction
        staged = commit_prepared_learning(
            c, prepared.payload, db=db, principal=_principal(),
        )
        assert c.connection.in_transaction
        assert calls == before
    assert staged.status == "staged"
    assert staged.new_chunk_ids != ()
    assert len(staged.new_chunk_ids) == len(staged.new_chunk_vectors)
    assert calls == before


def test_commit_prepared_requires_open_transaction(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    prepared = _prepare(db, config, CONTENT_A, fakes)
    assert prepared.status == "ok"
    with db.cursor() as c:
        with pytest.raises(GraphCommitAborted) as caught:
            commit_prepared_learning(
                c, prepared.payload, db=db, principal=_principal(),
            )
    assert caught.value.code == "write_requires_transaction"
    assert _counts(db)["learnings"] == 0


def test_prepared_payload_rejects_relabel_and_digest_tamper(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    prepared = _prepare(db, config, CONTENT_A, fakes)
    payload = prepared.payload
    foreign = EffectivePrincipal(agent_id="intruder", capabilities=["learn"])
    cases = [
        replace(payload, store_id="/tmp/other-store.db"),
        replace(payload, content=CONTENT_B, content_sha256=payload.content_sha256),
        replace(payload, principal=foreign),
        replace(payload, digest="0" * 64),
    ]
    for tampered in cases:
        with pytest.raises(GraphCommitAborted) as caught:
            with db.transaction() as c:
                commit_prepared_learning(
                    c, tampered, db=db, principal=_principal(),
                )
        assert caught.value.code == "payload_tampered"
    assert _counts(db)["learnings"] == 0

    mutated = _prepare(db, config, CONTENT_A, fakes).payload
    mutated.principal.capabilities.append("steal")
    with pytest.raises(GraphCommitAborted) as caught:
        with db.transaction() as c:
            commit_prepared_learning(
                c, mutated, db=db, principal=_principal(),
            )
    assert caught.value.code == "payload_tampered"


def test_commit_rejects_cross_store_cursor_and_principal(store, tmp_path):
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.migrations import run_migrations

    db_a, config_a = store
    fakes_a = _Fakes(db_a, classifier=_ok_classifier())
    prepared = _prepare(db_a, config_a, CONTENT_A, fakes_a)
    assert prepared.status == "ok"

    other = EffectivePrincipal(agent_id="intruder", capabilities=["learn"])
    with pytest.raises(GraphCommitAborted) as caught:
        with db_a.transaction() as c:
            commit_prepared_learning(
                c, prepared.payload, db=db_a, principal=other,
            )
    assert caught.value.code == "principal_mismatch"
    assert _counts(db_a)["learnings"] == 0

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
        with pytest.raises(GraphCommitAborted) as caught:
            with db_b.transaction() as c:
                commit_prepared_learning(
                    c, prepared.payload, db=db_a, principal=_principal(),
                )
        assert caught.value.code == "store_binding_mismatch"
        assert _counts(db_a)["learnings"] == 0
        assert _counts(db_b)["learnings"] == 0
        with pytest.raises(GraphCommitAborted) as caught:
            with db_a.transaction() as c:
                commit_prepared_learning(
                    c, prepared.payload, db=db_b, principal=_principal(),
                )
        assert caught.value.code == "store_binding_mismatch"
        assert _counts(db_a)["learnings"] == 0
        assert _counts(db_b)["learnings"] == 0
    finally:
        db_b.close()


def test_learning_fields_persist_and_content_hash_is_fail_loud(store):
    db, config = store
    prior = _seed_raw_learning(db, "An older learning about staging.")
    fields = LearningFields(
        source_query="how do we ship staging?",
        source_doc_ids=[11, 2],
        evidence_doc_ids=[7],
        confidence=0.7,
        assertion="signed checklist",
        applies_when="staging deploys",
        contradicts_id=prior,
        supersedes=prior,
        status="active",
    )
    fakes = _Fakes(db, classifier=_ok_classifier())
    result = _commit(db, config, CONTENT_A, fakes, learning_fields=fields)
    assert result.status == "ok", result.error
    row = _rows(
        db,
        "SELECT source_query, source_doc_ids, evidence_doc_ids, confidence,"
        " assertion, applies_when, contradicts_id, status, superseded_by"
        " FROM learnings WHERE learning_id=?",
        (result.learning_id,),
    )[0]
    assert row[0] == "how do we ship staging?"
    assert "11" in row[1] and "2" in row[1]
    assert "7" in row[2]
    assert row[3] == 0.7
    assert row[4] == "signed checklist"
    assert row[5] == "staging deploys"
    assert row[6] == prior
    assert row[7] == "active"
    superseded = _rows(db, "SELECT superseded_by FROM learnings WHERE learning_id=?",
                       (prior,))[0][0]
    assert superseded == result.learning_id

    hashed = _prepare(
        db, config, CONTENT_B, _Fakes(db, classifier=_ok_classifier()),
        learning_fields=LearningFields(content_hash="abc123"),
        graph_enabled=False,
    )
    assert hashed.status == "ok" and hashed.payload is not None
    present = {
        str(r[1]) for r in db._get_conn().execute(
            "PRAGMA table_info(learnings)").fetchall()
    }
    assert "content_hash" not in present
    before = _counts(db)
    with pytest.raises(GraphCommitAborted) as caught:
        with db.transaction() as c:
            commit_prepared_learning(
                c, hashed.payload, db=db, principal=_principal(),
            )
    assert caught.value.code == "learning_field_unavailable"
    assert _counts(db) == before


def test_wrapper_maps_staged_to_ok_only_after_commit(store):
    db, config = store
    fakes = _Fakes(db, classifier=_ok_classifier())
    result = _commit(db, config, CONTENT_A, fakes)
    assert result.status == "ok"
    assert result.learning_id is not None
    assert _counts(db)["learnings"] == 1


# --- P2.1 high-confidence auto-supersession --------------------------------
# Fake classifier + disposable SQLite only. Covers: forward updates >= 0.96
# retires the same-agent active target atomically with edge + node flip;
# the 0.96 floor boundary; lower-confidence accepted updates persist with
# no lifecycle action; backward/mutual downgrade to one graph_update_review
# (would-be cycles); N:1 mixed statuses and cross-agent siblings; repair
# never retires (review once via dedup); lifecycle-write failure rolls
# back everything; restricted-target race aborts with zero writes;
# non-updates labels never retire; unit-level plan downgrades unreachable
# via shortlist auth (cross-agent/wiki/already-superseded).

CONTENT_C = ("The staging deploy requires a signed release checklist, "
             "countersigned by the release captain.")


def _learning_state(db, learning_id):
    return _rows(db, "SELECT status, superseded_by FROM learnings"
                     " WHERE learning_id=?", (learning_id,))[0]


def _doc_state(db, doc_id):
    return _rows(db, "SELECT page_status, superseded_by FROM documents"
                     " WHERE doc_id=?", (doc_id,))[0]


def _reviews(db):
    return _rows(db, "SELECT target_learning_id, superseded_learning_id,"
                     " confidence, status, detail FROM consolidation_actions"
                     " WHERE action_type='graph_update_review'"
                     " ORDER BY action_id")


def _seed_updates_target(db, config):
    seed = _Fakes(db, classifier=_ok_classifier())
    first = _commit(db, config, CONTENT_A, seed)
    assert first.status == "ok", first.error
    return first


def _targeted_classifier(target_doc, label, direction, confidence):
    """Canned classifier covering EVERY sent pair: the target doc (by id)
    gets the requested edge, all other pairs get none.

    Pair indexes shift as the store gains docs, so targeting by document
    id — not pair index — is required; an unclassified pair fails the
    whole batch by contract.
    """

    def classify(source, candidates):
        return _FakeBatch(source, candidates, edges=[
            _edge(c["pair_id"], label, direction, confidence)
            if c["doc_id"] == target_doc
            else _edge(c["pair_id"], "none", "none", 0.99)
            for c in candidates
        ])

    return classify


def _commit_updates(db, config, content, confidence, direction="forward",
                    label="updates", target_doc=None, principal=None):
    fakes = _Fakes(
        db, classifier=_targeted_classifier(target_doc, label, direction,
                                            confidence))
    over = {} if principal is None else {"principal": principal}
    return _commit(db, config, content, fakes, **over)


def test_forward_updates_at_floor_retires_target_and_flips_node(store):
    db, config = store
    first = _seed_updates_target(db, config)
    result = _commit_updates(db, config, CONTENT_B, 0.99, target_doc=first.doc_id)
    assert result.status == "ok", result.error
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert (edge.source_doc_id, edge.target_doc_id) == (
        result.doc_id, first.doc_id)
    assert result.superseded_learning_ids == (first.learning_id,)
    assert result.update_reviews_queued == 0
    assert _reviews(db) == []
    status, successor = _learning_state(db, first.learning_id)
    assert status == "superseded" and successor == result.learning_id
    page_status, node_successor = _doc_state(db, first.doc_id)
    assert page_status == "superseded" and node_successor == result.doc_id


@pytest.mark.parametrize("confidence,retires",
                         [(0.88, False), (0.90, False), (0.9599, False),
                          (0.96, True)])
def test_supersede_floor_boundary(store, confidence, retires):
    db, config = store
    first = _seed_updates_target(db, config)
    result = _commit_updates(db, config, CONTENT_B, confidence, target_doc=first.doc_id)
    assert result.status == "ok", result.error
    assert len(result.edges) == 1  # persist floor 0.88 kept in every band
    if retires:
        assert result.superseded_learning_ids == (first.learning_id,)
        assert result.update_reviews_queued == 0
        assert _reviews(db) == []
        assert _learning_state(db, first.learning_id)[0] == "superseded"
        assert _doc_state(db, first.doc_id)[0] == "superseded"
    else:
        # Locked 8.1.1: lower band persists the edge AND queues a pending
        # review — never retires.
        assert result.superseded_learning_ids == ()
        assert result.update_reviews_queued == 1
        reviews = _reviews(db)
        assert len(reviews) == 1
        assert reviews[0][0] == result.learning_id
        assert reviews[0][1] == first.learning_id
        assert reviews[0][2] == confidence and reviews[0][3] == "pending"
        assert reviews[0][4] == "below_supersede_floor"
        assert _learning_state(db, first.learning_id) == ("active", None)
        assert _doc_state(db, first.doc_id)[0] == "accepted"


def test_lower_confidence_updates_persists_with_review_not_retire(store):
    db, config = store
    first = _seed_updates_target(db, config)
    result = _commit_updates(db, config, CONTENT_B, 0.90, target_doc=first.doc_id)
    assert result.status == "ok", result.error
    assert len(result.edges) == 1
    assert result.superseded_learning_ids == ()
    assert result.update_reviews_queued == 1
    assert len(_reviews(db)) == 1
    assert _reviews(db)[0][4] == "below_supersede_floor"
    assert _learning_state(db, first.learning_id) == ("active", None)
    assert _doc_state(db, first.doc_id)[0] == "accepted"


def test_backward_updates_queues_review_without_retire(store):
    db, config = store
    first = _seed_updates_target(db, config)
    result = _commit_updates(db, config, CONTENT_B, 0.99,
                             direction="backward", target_doc=first.doc_id)
    assert result.status == "ok", result.error
    # Wrong direction: the edge binds old -> new, nothing retires.
    assert [(e.source_doc_id, e.target_doc_id) for e in result.edges] == [
        (first.doc_id, result.doc_id)]
    assert result.superseded_learning_ids == ()
    assert result.update_reviews_queued == 1
    reviews = _reviews(db)
    assert len(reviews) == 1
    assert reviews[0][0] == result.learning_id
    assert reviews[0][1] == first.learning_id
    assert reviews[0][2] == 0.99 and reviews[0][3] == "pending"
    assert reviews[0][4] == "non_forward_updates:backward"
    assert _learning_state(db, first.learning_id) == ("active", None)
    assert _doc_state(db, first.doc_id)[0] == "accepted"


def test_mutual_updates_rejected_by_validation_with_zero_writes(store):
    # `updates` is new -> old only: the classifier contract rejects the
    # mutual direction, so mutual updates is a validation failure (zero
    # writes), never a supported supersession direction.
    db, config = store
    first = _seed_updates_target(db, config)
    before = _counts(db)
    result = _commit_updates(db, config, CONTENT_B, 0.99, direction="mutual", target_doc=first.doc_id)
    assert result.status == "error"
    assert result.error_code == "edge_inference_failed"
    assert _counts(db) == before
    assert _rows(db, "SELECT COUNT(*) FROM consolidation_actions")[0][0] == 0
    assert _learning_state(db, first.learning_id) == ("active", None)
    assert _doc_state(db, first.doc_id)[0] == "accepted"


def test_non_updates_labels_never_retire(store):
    db, config = store
    first = _seed_updates_target(db, config)
    for label, direction, confidence in (("extends", "forward", 0.99),
                                         ("contradicts", "mutual", 0.99),
                                         ("relates", "mutual", 0.99)):
        result = _commit_updates(db, config, CONTENT_B + label, confidence,
                                 direction=direction, label=label,
                                 target_doc=first.doc_id)
        assert result.status == "ok", result.error
        assert result.superseded_learning_ids == ()
        assert result.update_reviews_queued == 0
    assert _reviews(db) == []
    assert _learning_state(db, first.learning_id) == ("active", None)


def test_n1_second_mapping_retires_all_and_flips_node(store):
    db, config = store
    first = _seed_updates_target(db, config)
    # Re-promotion of identical content shares the canonical node: two
    # active same-agent learnings on one doc.
    repeat = _commit(db, config, CONTENT_A,
                     _Fakes(db, classifier=_targeted_classifier(
                         first.doc_id, "extends", "forward", 0.9)))
    assert repeat.status == "ok", repeat.error
    assert repeat.doc_id == first.doc_id
    result = _commit_updates(db, config, CONTENT_C, 0.99, target_doc=first.doc_id)
    assert result.status == "ok", result.error
    assert set(result.superseded_learning_ids) == {
        first.learning_id, repeat.learning_id}
    assert result.update_reviews_queued == 0
    assert _learning_state(db, first.learning_id)[0] == "superseded"
    assert _learning_state(db, repeat.learning_id)[1] == result.learning_id
    assert _doc_state(db, first.doc_id)[0] == "superseded"


def test_n1_mixed_statuses_retires_remainder_without_review(store):
    db, config = store
    first = _seed_updates_target(db, config)
    repeat = _commit(db, config, CONTENT_A,
                     _Fakes(db, classifier=_targeted_classifier(
                         first.doc_id, "extends", "forward", 0.9)))
    assert repeat.status == "ok", repeat.error
    assert repeat.doc_id == first.doc_id
    # One sibling already superseded by another durable learning: direct,
    # real successor id — no synthetic row.
    with db.transaction() as c:
        c.execute("UPDATE learnings SET status='superseded', superseded_by=?"
                  " WHERE learning_id=?", (repeat.learning_id,
                                           first.learning_id))
    result = _commit_updates(db, config, CONTENT_C, 0.99, target_doc=first.doc_id)
    assert result.status == "ok", result.error
    assert result.superseded_learning_ids == (repeat.learning_id,)
    assert result.update_reviews_queued == 0
    assert _reviews(db) == []
    assert _doc_state(db, first.doc_id)[0] == "superseded"


def test_n1_other_agent_sibling_kept_doc_stays_accepted(store):
    db, config = store
    first = _seed_updates_target(db, config)
    with db.transaction() as c:
        c.execute("INSERT INTO learnings"
                  " (agent_id, category, content, confidence, created_at)"
                  " VALUES ('mallory', 'general', ?, 1.0, 0.0)",
                  (CONTENT_A + " mallory note",))
        sibling = int(c.lastrowid)
        c.execute("INSERT INTO learning_documents (learning_id, doc_id,"
                  " created_at) VALUES (?, ?, 0.0)", (sibling, first.doc_id))
    result = _commit_updates(db, config, CONTENT_B, 0.99, target_doc=first.doc_id)
    assert result.status == "ok", result.error
    assert result.superseded_learning_ids == (first.learning_id,)
    assert _learning_state(db, sibling) == ("active", None)
    assert _doc_state(db, first.doc_id)[0] == "accepted"
    assert result.update_reviews_queued == 1
    reviews = _reviews(db)
    assert len(reviews) == 1
    assert reviews[0][4] == "other_agent_active"


def test_repair_never_retires_and_dedups_review(store):
    db, config = store
    first = _seed_updates_target(db, config)
    # A non-updates edge keeps the target active — and the queue empty —
    # for the repair pass.
    plain = _Fakes(db, classifier=_targeted_classifier(
        first.doc_id, "extends", "forward", 0.85))
    second = _commit(db, config, CONTENT_B, plain)
    assert second.status == "ok", second.error
    assert _reviews(db) == []
    hot = _Fakes(db, classifier=_targeted_classifier(
        first.doc_id, "updates", "forward", 0.99))
    repaired = _repair(db, config, second.learning_id, hot)
    assert repaired.status == "complete", repaired.error
    assert repaired.update_reviews_queued == 1
    assert len(_reviews(db)) == 1
    assert _reviews(db)[0][4] == "repair_no_retire"
    assert _learning_state(db, first.learning_id) == ("active", None)
    assert _doc_state(db, first.doc_id)[0] == "accepted"
    again = _repair(db, config, second.learning_id, hot)
    assert again.status == "complete", again.error
    assert again.update_reviews_queued == 0
    assert len(_reviews(db)) == 1


def test_repair_lower_band_queues_and_dedups_review(store):
    # Locked 8.1.1 applies to repair too: a lower-band updates edge queues
    # a pending review (never retires), and a repeated repair dedups it.
    db, config = store
    first = _seed_updates_target(db, config)
    plain = _Fakes(db, classifier=_targeted_classifier(
        first.doc_id, "extends", "forward", 0.85))
    second = _commit(db, config, CONTENT_B, plain)
    assert second.status == "ok", second.error
    assert _reviews(db) == []
    hot = _Fakes(db, classifier=_targeted_classifier(
        first.doc_id, "updates", "forward", 0.90))
    repaired = _repair(db, config, second.learning_id, hot)
    assert repaired.status == "complete", repaired.error
    assert repaired.update_reviews_queued == 1
    assert len(_reviews(db)) == 1
    assert _reviews(db)[0][4] == "below_supersede_floor"
    assert _learning_state(db, first.learning_id) == ("active", None)
    assert _doc_state(db, first.doc_id)[0] == "accepted"
    again = _repair(db, config, second.learning_id, hot)
    assert again.status == "complete", again.error
    assert again.update_reviews_queued == 0
    assert len(_reviews(db)) == 1


def test_lifecycle_write_failure_rolls_back_everything(store, monkeypatch):
    import minni.graph_coordinator as coordinator

    db, config = store
    first = _seed_updates_target(db, config)
    before = _counts(db)
    real = coordinator.retire_superseded_members

    def _boom(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("synthetic lifecycle outage")

    monkeypatch.setattr(coordinator, "retire_superseded_members", _boom)
    result = _commit_updates(db, config, CONTENT_B, 0.99, target_doc=first.doc_id)
    assert result.status == "error"
    assert _counts(db) == before
    assert _rows(db, "SELECT COUNT(*) FROM consolidation_actions")[0][0] == 0
    assert _learning_state(db, first.learning_id) == ("active", None)
    assert _doc_state(db, first.doc_id)[0] == "accepted"


def test_restricted_target_race_aborts_with_zero_writes(store):
    db, config = store
    first = _seed_updates_target(db, config)
    before = _counts(db)
    prepared = _prepare(db, config, CONTENT_B, _Fakes(
        db, classifier=_ok_classifier((0, "updates", "forward", 0.99))))
    assert prepared.status == "ok" and prepared.payload is not None
    with db.transaction() as c:
        c.execute("UPDATE documents SET privacy_level='blocked' WHERE doc_id=?",
                  (first.doc_id,))
    with pytest.raises(GraphCommitAborted):
        with db.transaction() as c:
            commit_prepared_learning(
                c, prepared.payload, db=db, principal=_principal(),
            )
    assert _counts(db) == before
    assert _rows(db, "SELECT COUNT(*) FROM consolidation_actions")[0][0] == 0
    assert _learning_state(db, first.learning_id) == ("active", None)


def test_plan_downgrades_unreachable_via_shortlist():
    from minni.graph_coordinator import _plan_updates_action

    def state(agent, status=None, by=None, kind="learning", doc_status="accepted",
              privacy="safe"):
        return {
            "doc": {"memory_kind": kind, "page_type": kind,
                    "page_status": doc_status, "privacy_level": privacy},
            "members": [{"learning_id": 7, "agent_id": agent, "status": status,
                         "superseded_by": by}],
        }

    # Cross-agent active target: never auto-retired, routed to review.
    assert _plan_updates_action(
        label="updates", direction="forward", confidence=0.99,
        new_agent_id="codex", new_learning_id=9,
        target_state=state("mallory")) == ((), 7, "other_agent_active")
    # Wiki-kind target downgrades even though shortlist never emits one.
    assert _plan_updates_action(
        label="updates", direction="forward", confidence=0.99,
        new_agent_id="codex", new_learning_id=9,
        target_state=state("codex", kind="wiki"))[2] == "non_learning_target"
    # Already superseded by another learning routes to review, not retire.
    assert _plan_updates_action(
        label="updates", direction="forward", confidence=0.99,
        new_agent_id="codex", new_learning_id=9,
        target_state=state("codex", status="superseded", by=3)) == (
            (), 7, "already_superseded")
    # This learning already applied its supersession: silent no-op.
    assert _plan_updates_action(
        label="updates", direction="forward", confidence=0.99,
        new_agent_id="codex", new_learning_id=9,
        target_state=state("codex", status="superseded", by=9)) == (
            (), None, None)
    # Lower band: edge persists, pending review, never retire (8.1.1).
    assert _plan_updates_action(
        label="updates", direction="forward", confidence=0.95,
        new_agent_id="codex", new_learning_id=9,
        target_state=state("codex")) == (
            (), 7, "below_supersede_floor")
    # Below the persist floor: true no-op.
    assert _plan_updates_action(
        label="updates", direction="forward", confidence=0.87,
        new_agent_id="codex", new_learning_id=9,
        target_state=state("codex")) == ((), None, None)


def test_operator_principal_cross_agent_target_queues_review(store):
    # Cross-agent updates pairs ARE reachable: the read gate denies plain
    # foreign-safe learning recall but admits operator principals, so a
    # governed cross-agent updates edge at the floor must queue
    # graph_update_review and never retire the foreign learning.
    db, config = store
    first = _seed_updates_target(db, config)
    op = EffectivePrincipal(agent_id="main", capabilities=["learn", "govern"])
    result = _commit_updates(db, config, CONTENT_B, 0.99,
                             target_doc=first.doc_id, principal=op)
    assert result.status == "ok", result.error
    assert len(result.edges) == 1
    edge = result.edges[0]
    assert (edge.source_doc_id, edge.target_doc_id) == (
        result.doc_id, first.doc_id)
    assert result.superseded_learning_ids == ()
    assert result.update_reviews_queued == 1
    reviews = _reviews(db)
    assert len(reviews) == 1
    assert reviews[0][0] == result.learning_id
    assert reviews[0][1] == first.learning_id
    assert reviews[0][4] == "other_agent_active"
    assert _learning_state(db, first.learning_id) == ("active", None)
    assert _doc_state(db, first.doc_id)[0] == "accepted"


def test_read_gate_cross_agent_boundary():
    # Pins the exact authorization boundary the operator test relies on:
    # a foreign safe learning doc is default-deny for plain principals
    # but visible to operator principals.
    from minni.principal import can_read_document

    meta = {"agent": "mallory", "privacy_level": "safe",
            "page_type": "learning", "memory_kind": "learning",
            "path": "vault/mallory/checklist.md", "page_status": "accepted"}
    plain = EffectivePrincipal(agent_id="codex", capabilities=["learn"])
    assert can_read_document(plain, "default", meta) is False
    op = EffectivePrincipal(agent_id="main", capabilities=["learn", "govern"])
    assert can_read_document(op, "default", meta) is True

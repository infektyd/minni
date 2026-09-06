"""Runtime graph adapter: disposable SQLite, fake model/classifier/engine.

No live vault, provider, network, or daemon. Covers store binding, outer
rollback, transaction boundaries, float32 copy delivery, invalid vectors,
refresh-failure retry token, and new-promotion fail-loud vs repair
degradation.
"""

import hashlib
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.graph_coordinator import (
    GraphCommitAborted,
    commit_prepared_learning,
)
from minni.minnid_runtime.graph_adapters import GraphRuntimeAdapter, canonical_store_id
from minni.principal import EffectivePrincipal

DIM = 8
CONTENT_A = "The staging deploy requires a signed release checklist."
CONTENT_B = "The staging deploy requires a signed release checklist! Please review."


def _encode(text):
    vec = np.zeros(DIM, dtype=np.float32)
    for index, ch in enumerate(text):
        vec[(index * 31 + ord(ch)) % DIM] += 1.0
    norm = float(np.linalg.norm(vec)) or 1.0
    return (vec / norm).astype(np.float32)


class _FakeChunker:
    def chunk_document(self, text):
        from minni.chunker import Chunk

        stripped = (text or "").strip() or text
        return [Chunk(text=stripped, heading="", heading_path="", chunk_index=0)]


class _FakeModel:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def encode(self, text, show_progress_bar=False):
        self.calls.append(text)
        if self.fail:
            raise RuntimeError("embedder offline")
        return _encode(text)


class _FakeFaiss:
    def __init__(self, dim=DIM):
        self.dim = dim
        self.ready = True
        self._ids = []
        self._vecs = []

    def search(self, query, top_k=20):
        if not self.ready or not self._ids:
            return []
        query = np.asarray(query, dtype=np.float32).reshape(-1)
        scored = []
        for chunk_id, vec in zip(self._ids, self._vecs):
            denom = float(np.linalg.norm(query) * np.linalg.norm(vec))
            cosine = float(np.dot(query, vec) / denom) if denom else 0.0
            scored.append((int(chunk_id), cosine))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:top_k]

    def add_batch(self, chunk_ids, embeddings):
        if not self.ready:
            return False
        for chunk_id, emb in zip(chunk_ids, embeddings):
            if int(chunk_id) in self._ids:
                continue
            self._ids.append(int(chunk_id))
            self._vecs.append(np.asarray(emb, dtype=np.float32).copy())
        return True


class _FakeEngine:
    def __init__(self, db, config, *, fail_embed=False, refresh_result=True):
        self.db = db
        self.config = config
        self.chunker = _FakeChunker()
        self.model = _FakeModel(fail=fail_embed)
        self.faiss_index = _FakeFaiss(dim=int(config.embedding_dim))
        self.refresh_calls = []
        self.refresh_in_transaction = []
        self._refresh_result = refresh_result

    def _ensure_faiss_loaded(self):
        return

    def _refresh_live_faiss(self, chunk_ids, vectors):
        conn = self.db._get_conn()
        self.refresh_in_transaction.append(bool(conn.in_transaction))
        copies = [np.asarray(vec, dtype=np.float32).copy() for vec in vectors]
        self.refresh_calls.append((list(chunk_ids), copies))
        if isinstance(self._refresh_result, Exception):
            raise self._refresh_result
        if self._refresh_result is False:
            self.faiss_index.ready = False
            return False
        self.faiss_index.add_batch(chunk_ids, copies)
        return True


class _FakeBatch:
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
    def classify(source, candidates):
        edges = [_edge(candidates[i]["pair_id"], label, direction, conf)
                 for i, label, direction, conf in specs]
        return _FakeBatch(source, candidates, edges=edges)

    return classify


def _fail_classifier():
    def classify(source, candidates):
        return _FakeBatch(source, candidates, ok=False,
                          status="provider_unavailable", error="offline")

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
        embedding_dim=DIM,
        embedding_model="test-model",
        reranker_enabled=False, attribution_enabled=False,
    )
    db = SovereignDB(config)
    run_migrations(db._get_conn())
    yield db, config
    db.close()


def _principal():
    return EffectivePrincipal(agent_id="codex", capabilities=["learn"])


def _adapter(db, config, *, classifier=None, retry=None, fail_embed=False,
             refresh_result=True):
    engine = _FakeEngine(db, config, fail_embed=fail_embed,
                         refresh_result=refresh_result)
    adapter = GraphRuntimeAdapter(
        engine, _principal(),
        classifier=classifier if classifier is not None else _ok_classifier(),
        on_refresh_retry=retry,
    )
    return adapter, engine


def _rows(db, sql, args=()):
    with db.cursor() as cursor:
        return [tuple(row) for row in cursor.execute(sql, args).fetchall()]


def _counts(db):
    return {
        table: _rows(db, f"SELECT COUNT(*) FROM {table}")[0][0]
        for table in ("learnings", "documents", "learning_documents",
                      "vault_fts", "chunk_embeddings", "memory_links")
    }


def _promote(adapter, db, content, classifier=None):
    if classifier is not None:
        adapter._classifier = classifier
    prepared = adapter.prepare(content)
    assert prepared.status == "ok", prepared.error
    with db.transaction() as cursor:
        staged = commit_prepared_learning(
            cursor, prepared.payload, db=db, principal=_principal(),
        )
    assert staged.status == "staged"
    delivered = adapter.deliver_postcommit_vectors(
        staged.new_chunk_ids, staged.new_chunk_vectors,
    )
    return staged, delivered


class _Rollback(Exception):
    pass


def test_adapter_binds_canonical_store_and_rejects_foreign_store(store):
    db, config = store
    adapter, _engine = _adapter(db, config)
    assert adapter.store_id == canonical_store_id(db)
    assert adapter.store_id == os.path.realpath(os.path.abspath(config.db_path))
    staged, delivered = _promote(adapter, db, CONTENT_A)
    assert delivered.status == "ok"
    doc_id = staged.doc_id
    meta = adapter.get_metadata(adapter.store_id, doc_id)
    assert meta["store_id"] == adapter.store_id
    assert meta["doc_id"] == doc_id
    with pytest.raises(ValueError, match="store_id"):
        adapter.get_metadata("/tmp/other-store.db", doc_id)
    with pytest.raises(ValueError, match="store_id"):
        adapter.get_content("/tmp/other-store.db", doc_id)


def test_prepare_then_outer_rollback_leaves_zero_rows_and_skips_refresh(store):
    db, config = store
    adapter, engine = _adapter(db, config)
    prepared = adapter.prepare(CONTENT_A)
    assert prepared.status == "ok" and prepared.payload is not None
    try:
        with db.transaction() as cursor:
            staged = commit_prepared_learning(
                cursor, prepared.payload, db=db, principal=_principal(),
            )
            assert staged.status == "staged"
            assert cursor.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 1
            raise _Rollback()
    except _Rollback:
        pass
    assert _counts(db)["learnings"] == 0
    assert _counts(db)["chunk_embeddings"] == 0
    assert engine.refresh_calls == []


def test_prepare_callbacks_not_in_transaction_and_deliver_refuses_open_txn(store):
    db, config = store
    seen = []
    canned = _ok_classifier((0, "extends", "forward", 0.85))

    def classify(source, candidates):
        seen.append(("classify", bool(db._get_conn().in_transaction)))
        return canned(source, candidates)

    adapter, engine = _adapter(db, config, classifier=classify)
    real_encode = engine.model.encode

    def encode(text, show_progress_bar=False):
        seen.append(("embed", bool(db._get_conn().in_transaction)))
        return real_encode(text, show_progress_bar=show_progress_bar)

    engine.model.encode = encode
    first, delivered = _promote(adapter, db, CONTENT_A)
    assert delivered.status == "ok"
    assert ("embed", False) in seen
    assert all(locked is False for _name, locked in seen)

    prepared = adapter.prepare(CONTENT_B)
    assert prepared.status == "ok"
    assert ("classify", False) in seen
    with db.transaction() as cursor:
        staged = commit_prepared_learning(
            cursor, prepared.payload, db=db, principal=_principal(),
        )
        refused = adapter.deliver_postcommit_vectors(
            staged.new_chunk_ids, staged.new_chunk_vectors,
        )
        assert cursor.connection.in_transaction
    assert refused.status == "error"
    assert refused.error_code == "refresh_in_transaction"
    assert refused.retry_requested is False
    assert engine.refresh_in_transaction == [False]
    assert _counts(db)["learnings"] == 2


def test_postcommit_decodes_float32_copies_matching_bytes(store):
    db, config = store
    adapter, engine = _adapter(db, config)
    source = np.linspace(-0.5, 0.5, DIM, dtype=np.float32)
    payload = source.tobytes()
    result = adapter.deliver_postcommit_vectors((42,), (payload,))
    assert result.status == "ok" and result.refreshed is True
    ids, arrays = engine.refresh_calls[0]
    assert ids == [42]
    assert arrays[0].dtype == np.float32
    assert arrays[0].shape == (DIM,)
    np.testing.assert_array_equal(arrays[0], source)
    arrays[0][0] = 99.0
    restored = np.frombuffer(payload, dtype=np.float32)
    assert restored[0] != 99.0
    assert engine.refresh_in_transaction == [False]


def test_invalid_vectors_do_not_refresh_and_request_retry(store):
    db, config = store
    retries = []
    adapter, engine = _adapter(db, config, retry=lambda: retries.append("token"))
    good = np.ones(DIM, dtype=np.float32).tobytes()

    count = adapter.deliver_postcommit_vectors((1, 2), (good,))
    assert count.status == "retry" and count.error_code == "count_mismatch"
    assert count.retry_requested is True

    wrong_dim = np.ones(DIM + 1, dtype=np.float32).tobytes()
    dim = adapter.deliver_postcommit_vectors((1,), (wrong_dim,))
    assert dim.status == "retry" and dim.error_code == "dimension_mismatch"

    broken = np.array([1.0, np.inf] + [0.0] * (DIM - 2), dtype=np.float32).tobytes()
    finite = adapter.deliver_postcommit_vectors((1,), (broken,))
    assert finite.status == "retry" and finite.error_code == "non_finite"

    nan = np.array([np.nan] + [0.0] * (DIM - 1), dtype=np.float32).tobytes()
    nan_result = adapter.deliver_postcommit_vectors((1,), (nan,))
    assert nan_result.status == "retry" and nan_result.error_code == "non_finite"

    assert engine.refresh_calls == []
    assert retries == ["token", "token", "token", "token"]


def test_refresh_failure_preserves_durability_and_requests_retry(store):
    db, config = store
    retries = []
    adapter, engine = _adapter(
        db, config, retry=lambda: retries.append("token"),
        refresh_result=False,
    )
    staged, delivered = _promote(adapter, db, CONTENT_A)
    assert delivered.status == "retry"
    assert delivered.retry_requested is True
    assert delivered.error_code == "index_cold"
    assert retries == ["token"]
    assert _counts(db)["learnings"] == 1
    assert _counts(db)["chunk_embeddings"] == 1
    assert staged.learning_id is not None
    row = _rows(db, "SELECT content FROM learnings WHERE learning_id=?",
                (staged.learning_id,))
    assert row == [(CONTENT_A,)]

    retries.clear()
    engine._refresh_result = RuntimeError("faiss add failed")
    engine.faiss_index.ready = True
    raised = adapter.deliver_postcommit_vectors(
        staged.new_chunk_ids, staged.new_chunk_vectors,
    )
    assert raised.status == "retry" and raised.error_code == "refresh_failed"
    assert retries == ["token"]
    assert _counts(db)["learnings"] == 1


def test_new_promotion_fail_loud_vs_repair_degradation(store):
    db, config = store
    adapter, engine = _adapter(db, config, fail_embed=True)
    failed = adapter.prepare(CONTENT_A)
    assert failed.status == "error"
    assert failed.error_code == "embed_failed"
    assert failed.payload is None
    assert _counts(db)["learnings"] == 0

    engine.model.fail = False
    first, delivered = _promote(adapter, db, CONTENT_A)
    assert delivered.status == "ok"
    before = _counts(db)

    adapter._classifier = _fail_classifier()
    second = adapter.prepare(CONTENT_B)
    assert second.status == "error"
    assert second.error_code == "edge_inference_failed"
    assert second.payload is None
    assert _counts(db) == before

    with db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO learnings (agent_id, category, content,"
            " confidence, created_at) VALUES ('codex', 'general', ?, 1.0, 0.0)",
            (CONTENT_B,),
        )
        raw_id = int(cursor.lastrowid)
    before_learning = _rows(db, "SELECT * FROM learnings WHERE learning_id=?",
                            (raw_id,))
    repaired = adapter.repair(raw_id)
    assert repaired.status == "complete", repaired.error
    assert repaired.edges_deferred == "degraded"
    assert repaired.edges == ()
    assert _rows(db, "SELECT * FROM learnings WHERE learning_id=?",
                 (raw_id,)) == before_learning

    with db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO learnings (agent_id, category, content,"
            " confidence, created_at) VALUES ('codex', 'general', ?, 1.0, 0.0)",
            ("A distinct lexical-only repair target about vault roots.",),
        )
        lexical_id = int(cursor.lastrowid)
    lexical_before = _rows(db, "SELECT * FROM learnings WHERE learning_id=?",
                           (lexical_id,))
    engine.model.fail = True
    lexical = adapter.repair(lexical_id)
    assert lexical.status == "incomplete_lexical_only", lexical.error
    assert _rows(db, "SELECT * FROM learnings WHERE learning_id=?",
                 (lexical_id,)) == lexical_before
    assert _rows(db, "SELECT COUNT(*) FROM chunk_embeddings WHERE doc_id=?",
                 (lexical.doc_id,)) == [(0,)]
    assert _rows(db, "SELECT COUNT(*) FROM vault_fts WHERE doc_id=?",
                 (lexical.doc_id,)) == [(1,)]


def _fts_content_reads(conn):
    import sqlite3

    reads = []

    def audit(action, table, column, *_args):
        if action == sqlite3.SQLITE_READ and table == "vault_fts" and column == "content":
            reads.append((table, column))
        return sqlite3.SQLITE_OK

    return reads, audit


def test_denied_metadata_does_not_read_fts(store):
    db, config = store
    adapter, _engine = _adapter(db, config)
    result, _ = _promote(adapter, db, CONTENT_A)
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE documents SET privacy_level='blocked' WHERE doc_id=?",
            (result.doc_id,),
        )
    reads, audit = _fts_content_reads(db._get_conn())
    conn = db._get_conn()
    conn.set_authorizer(audit)
    try:
        meta = adapter.get_metadata(adapter.store_id, result.doc_id)
        content = adapter.get_content(adapter.store_id, result.doc_id)
    finally:
        conn.set_authorizer(None)
    assert meta is None
    assert content is None
    assert reads == []


def test_get_content_rechecks_authorization_after_metadata_drift(store):
    db, config = store
    adapter, _engine = _adapter(db, config)
    result, _ = _promote(adapter, db, CONTENT_A)
    meta = adapter.get_metadata(adapter.store_id, result.doc_id)
    assert meta is not None
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE documents SET privacy_level='blocked' WHERE doc_id=?",
            (result.doc_id,),
        )
    reads, audit = _fts_content_reads(db._get_conn())
    conn = db._get_conn()
    conn.set_authorizer(audit)
    try:
        content = adapter.get_content(adapter.store_id, result.doc_id)
    finally:
        conn.set_authorizer(None)
    assert content is None
    assert reads == []


def test_retry_callback_exception_and_missing_callback_are_error_results(store):
    db, config = store

    def retry():
        raise OSError("retry callback failed")

    adapter, engine = _adapter(db, config, retry=retry, refresh_result=False)
    result, delivery = _promote(adapter, db, CONTENT_A)
    assert _counts(db)["learnings"] == 1
    assert delivery.status != "ok"
    assert delivery.retry_requested is False
    assert delivery.error_code == "retry_callback_failed"
    assert engine.refresh_calls

    missing, engine2 = _adapter(db, config, retry=None, refresh_result=False)
    again = missing.deliver_postcommit_vectors(
        result.new_chunk_ids, result.new_chunk_vectors,
    )
    assert again.status == "error"
    assert again.retry_requested is False
    assert again.error_code == "retry_callback_missing"
    assert _counts(db)["learnings"] == 1

    bad_id = adapter.deliver_postcommit_vectors(("not-an-id",), (b"xxxx",))
    assert bad_id.status == "error"
    assert bad_id.retry_requested is False
    assert bad_id.error_code == "invalid_chunk_id"


def test_search_loads_cold_engine_before_shortlist(store):
    db, config = store
    adapter, engine = _adapter(db, config)
    calls = []
    engine._ensure_faiss_loaded = lambda: calls.append("loaded")
    adapter.search_chunks(_encode(CONTENT_A).tobytes(), 48)
    assert calls == ["loaded"]


def test_cold_real_faiss_loads_sql_vectors(store):
    db, config = store
    vec = _encode(CONTENT_A)
    with db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO documents (path, agent, privacy_level, page_status,"
            " page_type, memory_kind) VALUES (?, 'codex', 'safe', 'accepted',"
            " 'learning', 'learning')",
            ("/vault/_durable/cold.md",),
        )
        doc_id = int(cursor.lastrowid)
        cursor.execute(
            "INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text,"
            " embedding, heading_context, model_name, computed_at)"
            " VALUES (?, 0, ?, ?, '', 'test-model', 0.0)",
            (doc_id, CONTENT_A, vec.tobytes()),
        )
        chunk_id = int(cursor.lastrowid)

    from minni.faiss_index import FAISSIndex
    from minni.retrieval import RetrievalEngine

    index = FAISSIndex(config)
    assert index.ready is False
    engine = RetrievalEngine(db, config, faiss_index=index)
    adapter = GraphRuntimeAdapter(
        engine, _principal(), classifier=_ok_classifier(),
    )
    hits = adapter.search_chunks(vec.tobytes(), 48)
    assert engine.faiss_index.ready is True
    assert hits
    assert hits[0]["store_id"] == adapter.store_id
    assert hits[0]["doc_id"] == doc_id
    assert hits[0]["chunk_id"] == chunk_id


def test_loader_failure_aborts_prepare_and_degrades_repair(store):
    db, config = store
    adapter, engine = _adapter(db, config)
    first, delivered = _promote(adapter, db, CONTENT_A)
    assert delivered.status == "ok"

    def boom():
        raise RuntimeError("loader down")

    engine._ensure_faiss_loaded = boom
    prepared = adapter.prepare(CONTENT_B)
    assert prepared.status == "error"
    assert prepared.error_code == "search_failed"
    assert prepared.payload is None
    assert _counts(db)["learnings"] == 1

    with db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO learnings (agent_id, category, content,"
            " confidence, created_at) VALUES ('codex', 'general', ?, 1.0, 0.0)",
            (CONTENT_B,),
        )
        raw_id = int(cursor.lastrowid)
    before = _rows(db, "SELECT * FROM learnings WHERE learning_id=?", (raw_id,))
    repaired = adapter.repair(raw_id)
    assert repaired.status == "complete", repaired.error
    assert repaired.edges_deferred == "degraded"
    assert _rows(db, "SELECT * FROM learnings WHERE learning_id=?",
                 (raw_id,)) == before


def test_engine_db_swap_fails_bound_store_on_every_public_call(store, tmp_path):
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.migrations import run_migrations

    db, config = store
    adapter, engine = _adapter(db, config)
    staged, delivered = _promote(adapter, db, CONTENT_A)
    assert delivered.status == "ok"

    config_b = SovereignConfig(
        db_path=str(tmp_path / "other.db"),
        vault_path=str(tmp_path / "vault_b"),
        writeback_path=str(tmp_path / "notes_b"),
        faiss_index_path=str(tmp_path / "index_b.faiss"),
        embedding_dim=DIM,
        embedding_model="test-model",
        reranker_enabled=False, attribution_enabled=False,
    )
    db_b = SovereignDB(config_b)
    try:
        run_migrations(db_b._get_conn())
        engine.db = db_b
        with pytest.raises(ValueError, match="bound store"):
            adapter.get_metadata(adapter.store_id, staged.doc_id)
        with pytest.raises(ValueError, match="bound store"):
            adapter.get_content(adapter.store_id, staged.doc_id)
        with pytest.raises(ValueError, match="bound store"):
            adapter.search_chunks(_encode(CONTENT_A).tobytes(), 8)
        with pytest.raises(ValueError, match="bound store"):
            adapter.embed_text(CONTENT_A)
        swapped = adapter.deliver_postcommit_vectors(
            staged.new_chunk_ids, staged.new_chunk_vectors,
        )
        assert swapped.status == "error"
        assert swapped.error_code == "store_binding_mismatch"
        assert swapped.retry_requested is False
        assert _counts(db_b)["chunk_embeddings"] == 0
    finally:
        db_b.close()

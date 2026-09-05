"""Cross-corpus scoring, eligible top-k, and bounded model contention."""
from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest

from minni.principal import EffectivePrincipal
from minni.rerank_cache import RerankCache
from minni.retrieval import RetrievalEngine
import minni.retrieval as retrieval


class FakeModel:
    model_name = "audit-fake"
    version = "1"

    def __init__(self):
        self.calls = []

    def predict(self, pairs, **kwargs):
        self.calls.append(pairs)
        return [float(len(pair[1])) for pair in pairs]


def scoring_engine(corpus, model):
    engine = object.__new__(RetrievalEngine)
    engine.config = SimpleNamespace(db_path=corpus, reranker_model="audit-fake")
    engine._reranker = model
    engine._apply_rerank_score_adjustments = lambda _rows: None
    return engine


def test_cache_scopes_corpus_body_and_heading(monkeypatch):
    import minni.rerank_cache as caches
    cache = RerankCache()
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", cache)
    model = FakeModel()
    a = scoring_engine("/corpus-a.db", model)
    b = scoring_engine("/corpus-b.db", model)

    def score(engine, text, heading=""):
        return engine._rerank("query", [{"chunk_id": 1, "chunk_text": text,
                                          "heading_context": heading}])[0]["rerank_score"]

    assert score(a, "aaa") == 3
    assert score(b, "aaa") == 3
    assert score(b, "different") == 9
    assert score(a, "changed") == 7
    assert score(a, "changed", "section") == len("[section] changed")
    assert len(model.calls) == 5
    score(a, "changed", "section")
    assert len(model.calls) == 5
    # Legacy indexer invalidation deliberately covers the id in every corpus.
    assert cache.invalidate_chunks([1]) == 5
    score(a, "changed", "section")
    score(b, "different")
    assert len(model.calls) == 7


@pytest.mark.parametrize("corpus", [None, ":memory:"])
@pytest.mark.parametrize("db_objects", [False, True])
def test_memory_cache_scopes_distinct_databases_or_engines(monkeypatch, corpus, db_objects):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", RerankCache())
    model = FakeModel()
    first = scoring_engine(corpus, model)
    second = scoring_engine(corpus, model)
    first.db = object() if db_objects else None
    second.db = object() if db_objects else None
    for engine in (first, second, first):
        engine._rerank("query", [{"chunk_id": 1, "chunk_text": "body"}])
    assert len(model.calls) == 2


def test_same_disk_database_shares_cache_across_engines(monkeypatch):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", RerankCache())
    model = FakeModel()
    for corpus in ("/tmp/shared.db", "/tmp/nested/../shared.db"):
        engine = scoring_engine(corpus, model)
        engine._rerank("query", [{"chunk_id": 1, "chunk_text": "body"}])
    assert len(model.calls) == 1


@pytest.mark.parametrize("invalidation", ["chunks", "clear"])
def test_inflight_prediction_cannot_repopulate_invalidated_cache(monkeypatch, invalidation):
    import minni.rerank_cache as caches
    cache = RerankCache()
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", cache)

    class InvalidatingModel(FakeModel):
        def predict(self, pairs, **kwargs):
            if invalidation == "chunks":
                cache.invalidate_chunks([1])
            else:
                cache.clear()
            return super().predict(pairs, **kwargs)

    model = InvalidatingModel()
    engine = scoring_engine("/inflight.db", model)
    for _ in range(2):
        engine._rerank("query", [{"chunk_id": 1, "chunk_text": "body"}])
    assert len(model.calls) == 2
    assert not cache._scores


def row(doc_id, *, denied=False, blocked=False):
    return {"doc_id": doc_id, "chunk_id": doc_id, "agent": "foreign" if denied else "codex",
            "page_type": "session", "privacy_level": "blocked" if blocked else ("private" if denied else "safe"),
            "page_status": "accepted", "path": f"/tmp/v/{doc_id}.md", "chunk_text": "matching content",
            "score": 1 / doc_id, "sigil": "T", "layer": "knowledge"}


def eligible_engine():
    engine = object.__new__(RetrievalEngine)
    engine.config = SimpleNamespace(reranker_enabled=False, reranker_top_k=1, reranker_final_k=1,
                                    hyde_enabled=False, hyde_confidence_floor=1,
                                    rrf_k=60, fts_weight=1, semantic_weight=1)
    engine._reranker = None
    engine.db = None
    engine.last_trace_id = None
    engine._chunk_index_empty = lambda: False
    engine._resolve_query_variants = lambda query, expand: [query]
    engine._apply_feedback_demotions = lambda rows, *_args: rows
    engine._rrf_merge = lambda fts, sem, limit: (fts + sem)[:limit]
    engine._encode_query = lambda *_args: np.ones(1, dtype=np.float32)
    return engine


def retrieve(engine, **kwargs):
    principal = EffectivePrincipal(agent_id="codex", capabilities=["search", "read"],
                                   allowed_vault_roots=["/tmp/v"])
    return engine.retrieve("match", limit=1, principal=principal, workspace="default",
                           update_access=False, budget_tokens=False, depth="chunk",
                           expand=False, **kwargs)


@pytest.mark.parametrize("branch", ["fts", "semantic", "chronological", "explicit", "one-backend", "multi-backend"])
def test_eligible_result_refills_bounded_windows(monkeypatch, branch):
    engine = eligible_engine()
    # The allowed match is outside the initial top-k and the FTS 3k window.
    candidates = [row(i, denied=True) for i in range(1, 8)] + [row(8, blocked=True), row(9)]
    windows = []

    def fetch(_query, limit, *args, **kwargs):
        windows.append(limit)
        return [dict(r) for r in candidates[:limit]]

    engine._fts_search = fetch if branch == "fts" else lambda *_a, **_k: []
    engine._semantic_search = fetch if branch == "semantic" else lambda *_a, **_k: []
    engine._chronological_search = fetch
    engine._backend_search = fetch
    engine._resolve_backends = lambda _names: [SimpleNamespace(name="first", dim=1)] + (
        [SimpleNamespace(name="second", dim=1)] if branch == "multi-backend" else [])
    if branch == "multi-backend":
        from minni.backends.multi import MultiBackend
        monkeypatch.setattr(MultiBackend, "search", lambda _self, query, k: fetch(query, k))
        engine._hits_to_dicts = lambda hits, _emb: hits
    options = {"sort": "chronological"} if branch == "chronological" else {}
    if branch == "explicit":
        options["backend"] = SimpleNamespace(name="custom")
    elif branch in {"one-backend", "multi-backend"}:
        options["backend"] = ["faiss-disk"]
    results = retrieve(engine, **options)
    assert [r["doc_id"] for r in results] == [9]
    assert windows == [1, 2, 4, 8, 16]
    assert engine.last_auth_suppression is None


def test_hyde_filters_before_its_fusion_window(monkeypatch):
    import minni.hyde as hyde
    engine = eligible_engine()
    engine.config.hyde_enabled = True
    engine._fts_search = lambda query, window, **kwargs: (
        [row(1, denied=True), row(2)][:window] if query == "hypothetical" else [row(3)])
    engine._semantic_search = lambda *_a, **_k: []
    monkeypatch.setattr(hyde, "should_trigger_hyde", lambda *_a, **_k: True)
    monkeypatch.setattr(hyde, "generate_hypothetical_answer", lambda *_a, **_k: "hypothetical")
    captured = []

    def merge(first, second, **kwargs):
        captured.extend(second)
        return second

    monkeypatch.setattr(hyde, "merge_hyde_results", merge)
    assert [r["doc_id"] for r in retrieve(engine)] == [2]
    assert [r["doc_id"] for r in captured] == [2]


def test_fully_denied_window_stops_at_ceiling_and_keeps_suppression():
    engine = eligible_engine()
    windows = []

    def fetch(query, limit, **kwargs):
        windows.append(limit)
        return [row(i, denied=True) for i in range(1, limit + 1)]

    engine._fts_search = fetch
    engine._semantic_search = lambda *_a, **_k: []
    assert retrieve(engine) == []
    assert max(windows) == 512
    assert engine.last_auth_suppression["suppressed"] == 512


@pytest.mark.parametrize("kind", ["rerank", "embed", "attribution"])
def test_contended_model_lock_respects_remaining_budget(monkeypatch, kind):
    import minni.models as models
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", None)
    monkeypatch.setattr(retrieval, "should_skip_cold_model_load", lambda *_a: False)
    model = FakeModel()
    engine = scoring_engine("/deadline.db", model)
    engine._model = model
    engine._attribution_model = model
    engine.config.attribution_enabled = True
    engine.vector_model_down = False
    lock = threading.Lock()
    lock.acquire()
    getter = {"rerank": "get_cross_encoder_lock", "embed": "get_embedder_lock", "attribution": "get_attribution_lock"}[kind]
    monkeypatch.setattr(models, getter, lambda: lock)
    engine._set_current_deadline(time.monotonic() + retrieval.SEARCH_STAGE_MIN_REMAINING_S + 0.05)
    started = time.monotonic()
    try:
        if kind == "rerank":
            engine._rerank("query", [{"chunk_id": 1, "chunk_text": "body"}])
            assert "deadline" in engine.last_rerank_degraded
        elif kind == "embed":
            assert engine._encode_query("query").size == 0
            assert "deadline" in engine.last_vector_degraded
        else:
            assert engine._score_attribution("claim", "body") is None
        assert time.monotonic() - started < 0.2
        assert lock.locked(), "a waiter must not release the owner's lock"
        assert not model.calls
    finally:
        lock.release()


@pytest.mark.parametrize("sort", ["semantic", "chronological"])
def test_real_sql_window_refills_after_foreign_private_rows(tmp_path, monkeypatch, sort):
    from test_retrieval_visibility import _make_expand_db, _seed_expand_doc

    db, config = _make_expand_db(tmp_path, "eligible.db")
    config.reranker_enabled = False
    config.hyde_enabled = False
    config.feedback_enabled = False
    conn = db._get_conn()
    for number in range(12):
        agent = "codex" if number == 11 else "foreign"
        privacy = "safe" if number == 11 else "private"
        path = f"/tmp/v/{number}.md"
        doc_id = _seed_expand_doc(conn, path, agent, privacy)
        text = "match extra irrelevant words" if number == 11 else "match match match"
        conn.execute("INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, ?, 'T')",
                     (doc_id, path, text, agent))
    conn.commit()
    engine = RetrievalEngine(db, config)
    monkeypatch.setattr(engine, "_semantic_search", lambda *_a, **_k: [])
    results = retrieve(engine, sort=sort)
    assert [r["doc_id"] for r in results] == [doc_id]
    assert engine.last_auth_suppression is None


def test_started_predict_is_not_cancelled_and_reports_expired_deadline(monkeypatch):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", None)
    now = [100.0]
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: now[0])

    class SlowModel(FakeModel):
        def predict(self, pairs, **kwargs):
            now[0] = 110.0  # Represents nonpreemptible inference passing cutoff.
            return super().predict(pairs, **kwargs)

    model = SlowModel()
    engine = scoring_engine("/predict.db", model)
    engine._set_current_deadline(105.0)
    results = engine._rerank("query", [{"chunk_id": 1, "chunk_text": "body"}])
    assert results[0]["rerank_score"] == 4
    assert len(model.calls) == 1
    assert "nonpreemptible" in engine.last_rerank_degraded


def test_semantic_taxonomy_filter_does_not_hide_window_exhaustion():
    engine = eligible_engine()
    candidates = [row(1), row(2)]
    candidates[0]["agent"] = "other"
    engine._fts_search = lambda *_a, **_k: []
    seen = []

    def fetch(query, window, agent_filter=None):
        seen.append(window)
        rows = candidates[:window]
        # Emulate metadata-side filtering after the FAISS top-k window.
        return [r for r in rows if not agent_filter or r["agent"] in agent_filter]

    engine._semantic_search = fetch
    assert [r["doc_id"] for r in retrieve(engine, document_agent_filter=["codex"])] == [2]
    assert seen == [1, 2]


def test_expanded_variants_keep_eligible_matches_before_final_merge():
    engine = eligible_engine()
    engine._resolve_query_variants = lambda query, expand: [query, "variant"] if expand else [query]
    engine._fts_search = lambda query, window, **kwargs: [row(1, denied=True), row(2)][:window]
    engine._semantic_search = lambda *_a, **_k: []
    principal = EffectivePrincipal(agent_id="codex", capabilities=["search", "read"],
                                   allowed_vault_roots=["/tmp/v"])
    results = engine.retrieve("match", limit=1, principal=principal, update_access=False,
                              budget_tokens=False, depth="chunk", expand=True)
    assert [r["doc_id"] for r in results] == [2]


def test_deadline_fts_still_filters_before_final_limit():
    engine = eligible_engine()
    engine._fts_search = lambda *_a, **_k: [row(1, denied=True), row(2)]
    engine._semantic_search = lambda *_a, **_k: pytest.fail("expired search must skip semantic")
    results = retrieve(engine, deadline_monotonic=time.monotonic() - 1)
    assert [r["doc_id"] for r in results] == [2]


@pytest.mark.parametrize("branch", ["default", "explicit"])
def test_multichunk_denied_document_does_not_prove_vector_exhaustion(tmp_path, monkeypatch, branch):
    from test_retrieval_visibility import _make_expand_db, _seed_expand_doc
    from minni.backends.faiss_mem import FaissMemBackend
    from minni.vector_backend import VectorItem
    import minni.models as models

    db, config = _make_expand_db(tmp_path, "chunk-window.db")
    config.reranker_enabled = False
    config.hyde_enabled = False
    config.feedback_enabled = False
    conn = db._get_conn()
    denied = _seed_expand_doc(conn, "/tmp/v/denied.md", "foreign", "private")
    allowed = _seed_expand_doc(conn, "/tmp/v/allowed.md", "codex", "safe")
    for chunk_index in range(1, 10):
        conn.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, heading_context, embedding, computed_at)
               VALUES (?, ?, 'denied body', '', ?, ?)""",
            (denied, chunk_index, np.zeros(config.embedding_dim, dtype=np.float32).tobytes(), time.time()),
        )
    conn.commit()
    backend = FaissMemBackend(config)
    items = []
    for chunk in conn.execute("SELECT chunk_id, doc_id, chunk_index FROM chunk_embeddings"):
        vector = np.zeros(config.embedding_dim, dtype=np.float32)
        vector[0] = 1
        vector[1] = 0.5 if chunk["doc_id"] == allowed else 0.001 * chunk["chunk_index"]
        items.append(VectorItem(chunk_id=chunk["chunk_id"], doc_id=chunk["doc_id"], vector=vector))
    backend.upsert(items)

    class Embedder:
        calls = 0

        def encode(self, query, **kwargs):
            self.calls += 1
            vector = np.zeros(config.embedding_dim, dtype=np.float32)
            vector[0] = 1
            return vector

    model = Embedder()
    monkeypatch.setattr(models, "get_embedder", lambda: model)
    engine = RetrievalEngine(db, config)
    monkeypatch.setattr(engine, "_fts_search", lambda *_a, **_k: [])
    windows = []
    if branch == "default":
        engine.faiss_index = backend._faiss
        search = engine.faiss_index.search

        def tracked(query, top_k):
            windows.append(top_k)
            return search(query, top_k=top_k)

        monkeypatch.setattr(engine.faiss_index, "search", tracked)
        options = {}
    else:
        search = backend.search

        def tracked(query, k, filter=None):
            windows.append(k)
            return search(query, k=k, filter=filter)

        monkeypatch.setattr(backend, "search", tracked)
        options = {"backend": backend}
    # Windows 5 and 10 both deduplicate to precisely the same denied doc.
    # The allowed document appears only after refilling to 20 chunks.
    for request_number in (1, 2):
        windows.clear()
        results = retrieve(engine, **options)
        assert [r["doc_id"] for r in results] == [allowed]
        assert windows == [5, 10, 20]
        assert model.calls == request_number, "encode once per request, never once per refill"
        assert getattr(engine._degradation_local, "query_embeddings", None) is None


def test_query_embedding_scope_restores_after_errors_and_honors_deadline(monkeypatch):
    import minni.models as models

    class Embedder:
        calls = []

        def encode(self, query, **kwargs):
            self.calls.append(query)
            return np.ones(2, dtype=np.float32)

    model = Embedder()
    monkeypatch.setattr(models, "get_embedder", lambda: model)
    engine = scoring_engine("/embedding-scope.db", FakeModel())
    engine.vector_model_down = False
    engine._set_current_deadline(None)
    with pytest.raises(RuntimeError, match="backend failure"):
        with engine._query_encoding_scope():
            vector = engine._encode_query("query")
            vector[:] = 99  # Consumer mutation must not corrupt later refills.
            assert np.array_equal(engine._encode_query("query"), np.ones(2))
            engine._encode_query("hyde probe")
            assert model.calls == ["query", "hyde probe"]
            engine._set_current_deadline(time.monotonic() - 1)
            assert engine._encode_query("query").size == 0
            raise RuntimeError("backend failure")
    assert getattr(engine._degradation_local, "query_embeddings", None) is None
    engine._set_current_deadline(None)
    engine._encode_query("query")
    assert model.calls == ["query", "hyde probe", "query"]

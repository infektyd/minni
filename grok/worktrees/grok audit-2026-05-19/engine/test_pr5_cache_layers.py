import os
import sqlite3
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        vector_backends=["faiss-disk"],
        reranker_enabled=True,
        reranker_top_k=10,
        reranker_final_k=10,
        context_budget_tokens=0,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _insert_doc(db_obj, *, path, text, layer="knowledge", agent="unknown",
                indexed_at=None, page_type=None, whole_document=0):
    now = indexed_at if indexed_at is not None else time.time()
    vec = np.ones(384, dtype=np.float32)
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, indexed_at, last_modified, page_type, whole_document, layer)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (path, agent, "?", now, now, page_type, whole_document, layer),
        )
        doc_id = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, heading_context,
                model_name, computed_at, layer)
               VALUES (?, 0, ?, ?, '', 'test-embed', ?, ?)""",
            (doc_id, text, vec.tobytes(), now, layer),
        )
        chunk_id = c.lastrowid
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, ?, ?)",
            (doc_id, path, text, agent, "?"),
        )
    return doc_id, chunk_id


class CountingReranker:
    def __init__(self):
        self.calls = 0

    def predict(self, pairs):
        self.calls += 1
        return [float(len(pairs) - i) for i, _ in enumerate(pairs)]


def test_rerank_cache_reuses_scores_and_invalidates_by_chunk_id(tmp_path):
    from retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    cfg.reranker_enabled = False
    _insert_doc(db_obj, path="wiki/a.md", text="alpha cache body")
    _insert_doc(db_obj, path="wiki/b.md", text="beta cache body")

    engine = RetrievalEngine(db_obj, cfg)
    reranker = CountingReranker()
    engine._reranker = reranker

    candidates = [
        {"doc_id": 1, "chunk_id": 1, "chunk_text": "alpha cache body"},
        {"doc_id": 2, "chunk_id": 2, "chunk_text": "beta cache body"},
    ]

    first = engine._rerank("cache query", [dict(c) for c in candidates])
    second = engine._rerank("cache query", [dict(c) for c in candidates])

    assert reranker.calls == 1
    assert [r["rerank_score"] for r in second] == [r["rerank_score"] for r in first]

    from rerank_cache import invalidate_chunks

    invalidate_chunks([1])
    engine._rerank("cache query", [dict(c) for c in candidates])
    assert reranker.calls == 2


def test_rerank_cache_key_includes_model_version():
    from rerank_cache import RerankCache

    cache = RerankCache(capacity=2)
    cache.set("model-a", "v1", "same query", 7, 0.42)

    assert cache.get("model-a", "v1", "same query", 7) == 0.42
    assert cache.get("model-a", "v2", "same query", 7) is None
    assert cache.get("model-b", "v1", "same query", 7) is None


def test_migration_004_adds_nullable_layer_columns(tmp_path):
    from migrations import run_migrations

    conn = sqlite3.connect(tmp_path / "migration.db")
    conn.execute(
        """CREATE TABLE documents (
           doc_id INTEGER PRIMARY KEY, agent TEXT, whole_document INTEGER, page_type TEXT
        )"""
    )
    conn.execute("CREATE TABLE chunk_embeddings (chunk_id INTEGER PRIMARY KEY, doc_id INTEGER)")
    conn.commit()
    run_migrations(conn)
    doc_cols = {row[1]: row for row in conn.execute("PRAGMA table_info(documents)")}
    chunk_cols = {row[1]: row for row in conn.execute("PRAGMA table_info(chunk_embeddings)")}
    conn.close()

    assert "layer" in doc_cols
    assert doc_cols["layer"][3] == 0
    assert "layer" in chunk_cols
    assert chunk_cols["layer"][3] == 0


def test_migration_004_runs_when_user_version_already_ahead(tmp_path):
    from migrations import run_migrations

    conn = sqlite3.connect(tmp_path / "migration-ahead.db")
    conn.execute(
        """CREATE TABLE documents (
           doc_id INTEGER PRIMARY KEY, agent TEXT, whole_document INTEGER, page_type TEXT
        )"""
    )
    conn.execute("CREATE TABLE chunk_embeddings (chunk_id INTEGER PRIMARY KEY, doc_id INTEGER)")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    run_migrations(conn)
    doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
    chunk_cols = {row[1] for row in conn.execute("PRAGMA table_info(chunk_embeddings)").fetchall()}
    conn.close()

    assert "layer" in doc_cols
    assert "layer" in chunk_cols


def test_migration_004_backfills_existing_document_and_chunk_layers(tmp_path):
    from migrations import run_migrations

    vec = np.ones(384, dtype=np.float32).tobytes()
    conn = sqlite3.connect(tmp_path / "migration-backfill.db")
    conn.execute(
        """CREATE TABLE documents (
           doc_id INTEGER PRIMARY KEY, path TEXT, agent TEXT, whole_document INTEGER,
           page_type TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE chunk_embeddings (
           chunk_id INTEGER PRIMARY KEY, doc_id INTEGER, chunk_index INTEGER,
           chunk_text TEXT, embedding BLOB
        )"""
    )
    conn.execute(
        "INSERT INTO documents VALUES (1, 'identity.md', 'identity:codex', 1, 'schema')"
    )
    conn.execute(
        "INSERT INTO documents VALUES (2, 'artifact.md', 'wiki:artifacts', 0, 'artifact')"
    )
    conn.execute(
        "INSERT INTO documents VALUES (3, 'concept.md', 'codex', 0, 'concept')"
    )
    for doc_id in (1, 2, 3):
        conn.execute(
            "INSERT INTO chunk_embeddings (doc_id, chunk_index, chunk_text, embedding) VALUES (?, 0, 'x', ?)",
            (doc_id, vec),
        )
    conn.commit()

    run_migrations(conn)
    rows = conn.execute("SELECT doc_id, layer FROM documents ORDER BY doc_id").fetchall()
    chunk_rows = conn.execute(
        "SELECT doc_id, layer FROM chunk_embeddings ORDER BY doc_id"
    ).fetchall()
    conn.close()

    assert rows == [(1, "identity"), (2, "artifact"), (3, "knowledge")]
    assert chunk_rows == [(1, "identity"), (2, "artifact"), (3, "knowledge")]


def test_indexer_infers_layers_from_frontmatter_and_identity_agent():
    from indexer import VaultIndexer

    identity = VaultIndexer._extract_frontmatter(
        "---\nagent: identity:codex\ntype: schema\n---\nIdentity"
    )
    artifact = VaultIndexer._extract_frontmatter(
        "---\nagent: wiki:artifacts\ntype: artifact\n---\nArtifact"
    )
    knowledge = VaultIndexer._extract_frontmatter(
        "---\nagent: codex\ntype: concept\n---\nKnowledge"
    )

    assert identity["agent"] == "identity:codex"
    assert identity["layer"] == "identity"
    assert artifact["layer"] == "artifact"
    assert knowledge["layer"] == "knowledge"


def test_layer_filter_is_opt_in_and_filters_results(tmp_path):
    from retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    cfg.reranker_enabled = False
    _insert_doc(db_obj, path="wiki/identity.md", text="shared search token", layer="identity")
    _insert_doc(db_obj, path="wiki/knowledge.md", text="shared search token", layer="knowledge")

    engine = RetrievalEngine(db_obj, cfg)
    engine._semantic_search = lambda query, limit: []
    engine._rerank = lambda query, candidates: candidates

    default_results = engine.retrieve("shared search token", limit=10, budget_tokens=False)
    none_results = engine.retrieve("shared search token", limit=10, layers=None, budget_tokens=False)
    identity_results = engine.retrieve(
        "shared search token", limit=10, layers=["identity"], budget_tokens=False
    )

    assert [r["source"] for r in default_results] == [r["source"] for r in none_results]
    assert len(identity_results) == 1
    assert identity_results[0]["source"].endswith("identity.md")
    assert identity_results[0]["layer"] == "identity"


def test_chronological_mode_orders_by_created_time_and_filters_dates(tmp_path):
    from retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    jan_01 = 1735732800.0
    jan_02 = 1735819200.0
    jan_03 = 1735905600.0
    _insert_doc(db_obj, path="wiki/third.md", text="timeline token third", indexed_at=jan_03)
    _insert_doc(db_obj, path="wiki/first.md", text="timeline token first", indexed_at=jan_01)
    _insert_doc(db_obj, path="wiki/second.md", text="timeline token second", indexed_at=jan_02)

    engine = RetrievalEngine(db_obj, cfg)
    engine._semantic_search = lambda query, limit: (_ for _ in ()).throw(
        AssertionError("semantic search must be bypassed")
    )
    engine._rerank = lambda query, candidates: (_ for _ in ()).throw(
        AssertionError("rerank must be bypassed")
    )

    results = engine.retrieve(
        "timeline token",
        limit=10,
        sort="chronological",
        start_date="2025-01-02",
        end_date="2025-01-03",
        budget_tokens=False,
    )

    assert [os.path.basename(r["source"]) for r in results] == ["second.md", "third.md"]


def test_sovrd_search_forwards_layer_sort_and_dates(monkeypatch):
    import sovrd

    captured = {}

    class FakeEngine:
        def retrieve(self, **kwargs):
            captured.update(kwargs)
            return [{"source": "ok"}]

    monkeypatch.setattr(sovrd, "_lazy_retrieval", lambda: FakeEngine())

    resp = sovrd._handle_search({
        "query": "q",
        "agent_id": "codex",
        "layers": ["identity"],
        "sort": "chronological",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    }, request_id=1)

    assert resp["result"]["count"] == 1
    assert captured["layers"] == ["identity"]
    assert captured["sort"] == "chronological"
    assert captured["start_date"] == "2025-01-01"
    assert captured["end_date"] == "2025-01-31"

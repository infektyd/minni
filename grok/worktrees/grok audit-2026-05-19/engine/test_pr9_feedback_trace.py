"""
PR-9 tests: feedback storage/demotion and per-query trace ring.
"""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path, feedback_enabled=True):
    import db as db_mod
    from config import SovereignConfig
    from db import SovereignDB

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        feedback_enabled=feedback_enabled,
        reranker_enabled=False,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _make_engine(tmp_path, feedback_enabled=True):
    from faiss_index import FAISSIndex
    from retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path, feedback_enabled=feedback_enabled)
    return RetrievalEngine(db_obj, cfg, FAISSIndex(cfg)), db_obj, cfg


def _seed_doc(db_obj, path, agent, text):
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at, page_status, privacy_level)
               VALUES (?, ?, '?', ?, ?, 'accepted', 'safe')""",
            (path, agent, now, now),
        )
        doc_id = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, heading_context, computed_at)
               VALUES (?, 0, ?, ?, '', ?)""",
            (doc_id, text, np.zeros(384, dtype=np.float32).tobytes(), now),
        )
        chunk_id = c.lastrowid
    return doc_id, chunk_id


def test_migration_006_creates_feedback_table(tmp_path):
    db_obj, _ = _make_db(tmp_path)
    with db_obj.cursor() as c:
        cols = {
            row["name"]
            for row in c.execute("PRAGMA table_info(feedback)").fetchall()
        }

    assert {
        "id",
        "query_hash",
        "query_text",
        "doc_id",
        "chunk_id",
        "agent_id",
        "useful",
        "created_at",
    }.issubset(cols)


def test_daemon_feedback_stores_row(monkeypatch, tmp_path):
    import sovrd

    engine, db_obj, _ = _make_engine(tmp_path)
    doc_id, chunk_id = _seed_doc(db_obj, "/wiki/auth.md", "test-agent", "auth migration")

    monkeypatch.setattr(sovrd, "_retrieval", engine)
    response = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "feedback",
        "params": {
            "query": "auth migration",
            "result_id": chunk_id,
            "useful": False,
            "agent_id": "test-agent",
        },
    })

    assert response["result"]["status"] == "ok"
    assert response["result"]["doc_id"] == doc_id
    with db_obj.cursor() as c:
        row = c.execute("SELECT * FROM feedback").fetchone()
    assert row["query_text"] == "auth migration"
    assert row["chunk_id"] == chunk_id
    assert row["doc_id"] == doc_id
    assert row["useful"] == 0


def test_negative_feedback_demotes_matching_agent_query_and_doc(monkeypatch, tmp_path):
    engine, db_obj, _ = _make_engine(tmp_path)
    doc1, _ = _seed_doc(db_obj, "/wiki/first.md", "test-agent", "first text")
    doc2, _ = _seed_doc(db_obj, "/wiki/second.md", "test-agent", "second text")

    with db_obj.cursor() as c:
        for _ in range(10):
            c.execute(
                """INSERT INTO feedback
                   (query_hash, query_text, doc_id, chunk_id, agent_id, useful, created_at)
                   VALUES (?, ?, ?, NULL, ?, 0, ?)""",
                ("unused", "auth migration", doc1, "test-agent", int(time.time())),
            )

    fts_hits = [
        {
            "doc_id": doc1,
            "path": "/wiki/first.md",
            "agent": "test-agent",
            "sigil": "?",
            "bm25_rank": -1.0,
            "decay_score": 1.0,
            "page_status": "accepted",
            "privacy_level": "safe",
        },
        {
            "doc_id": doc2,
            "path": "/wiki/second.md",
            "agent": "test-agent",
            "sigil": "?",
            "bm25_rank": -2.0,
            "decay_score": 1.0,
            "page_status": "accepted",
            "privacy_level": "safe",
        },
    ]
    monkeypatch.setattr(engine, "_fts_search", lambda query, limit: list(fts_hits))
    monkeypatch.setattr(engine, "_semantic_search", lambda query, limit: [])

    results = engine.retrieve(
        "auth migration",
        agent_id="test-agent",
        limit=2,
        depth="headline",
        budget_tokens=False,
    )

    assert [r["doc_id"] for r in results] == [doc2, doc1]
    demoted = next(r for r in results if r["doc_id"] == doc1)
    assert demoted["provenance"]["feedback_demote"] == -0.3


def test_feedback_toggle_disables_demotion(monkeypatch, tmp_path):
    engine, db_obj, _ = _make_engine(tmp_path, feedback_enabled=False)
    doc1, _ = _seed_doc(db_obj, "/wiki/first.md", "test-agent", "first text")
    doc2, _ = _seed_doc(db_obj, "/wiki/second.md", "test-agent", "second text")
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO feedback
               (query_hash, query_text, doc_id, chunk_id, agent_id, useful, created_at)
               VALUES (?, ?, ?, NULL, ?, 0, ?)""",
            ("unused", "auth migration", doc1, "test-agent", int(time.time())),
        )

    monkeypatch.setattr(engine, "_fts_search", lambda query, limit: [
        {
            "doc_id": doc1,
            "path": "/wiki/first.md",
            "agent": "test-agent",
            "sigil": "?",
            "bm25_rank": -1.0,
            "decay_score": 1.0,
            "page_status": "accepted",
            "privacy_level": "safe",
        },
        {
            "doc_id": doc2,
            "path": "/wiki/second.md",
            "agent": "test-agent",
            "sigil": "?",
            "bm25_rank": -2.0,
            "decay_score": 1.0,
            "page_status": "accepted",
            "privacy_level": "safe",
        },
    ])
    monkeypatch.setattr(engine, "_semantic_search", lambda query, limit: [])

    results = engine.retrieve(
        "auth migration",
        agent_id="test-agent",
        limit=2,
        depth="headline",
        budget_tokens=False,
    )
    assert [r["doc_id"] for r in results] == [doc1, doc2]


def test_trace_ring_capacity_and_size_bound():
    from trace import TraceRing

    ring = TraceRing(capacity=3, max_bytes=600)
    first = ring.add({"query": "q0", "payload": "x" * 100})
    for i in range(1, 8):
        ring.add({"query": f"q{i}", "payload": "x" * 100})

    assert ring.get(first) is None
    assert len(ring) <= 3
    assert ring.approx_bytes <= 600


def test_daemon_trace_round_trip(monkeypatch, tmp_path):
    import sovrd

    engine, db_obj, _ = _make_engine(tmp_path)
    doc_id, _ = _seed_doc(db_obj, "/wiki/auth.md", "test-agent", "auth migration")
    monkeypatch.setattr(engine, "_fts_search", lambda query, limit: [
        {
            "doc_id": doc_id,
            "path": "/wiki/auth.md",
            "agent": "test-agent",
            "sigil": "?",
            "bm25_rank": -1.0,
            "decay_score": 1.0,
            "page_status": "accepted",
            "privacy_level": "safe",
        }
    ])
    monkeypatch.setattr(engine, "_semantic_search", lambda query, limit: [])
    monkeypatch.setattr(sovrd, "_retrieval", engine)

    search_response = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "search",
        "params": {"query": "auth migration", "agent_id": "test-agent"},
    })
    trace_id = search_response["result"]["trace_id"]

    trace_response = sovrd._dispatch({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "trace",
        "params": {"trace_id": trace_id},
    })

    trace = trace_response["result"]["trace"]
    assert trace["query"] == "auth migration"
    assert "fts_hits" in trace
    assert "timing" in trace

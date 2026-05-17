"""
PR-7 tests: query expansion and graph neighborhood summaries.
"""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import db as db_mod
    from config import SovereignConfig
    from db import SovereignDB
    from faiss_index import FAISSIndex
    from retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        reranker_enabled=False,
        context_budget_tokens=0,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return RetrievalEngine(db_obj, cfg, FAISSIndex(cfg)), db_obj, cfg


def _insert_doc(db_obj, *, path, text, agent="unknown", page_type="concept"):
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, indexed_at, last_modified, page_status, privacy_level, page_type)
               VALUES (?, ?, '?', ?, ?, 'accepted', 'safe', ?)""",
            (path, agent, now, now, page_type),
        )
        doc_id = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, heading_context, computed_at)
               VALUES (?, 0, ?, ?, '', ?)""",
            (doc_id, text, np.ones(384, dtype=np.float32).tobytes(), now),
        )
        chunk_id = c.lastrowid
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, ?, '?')",
            (doc_id, path, text, agent),
        )
    return doc_id, chunk_id


def test_rule_expansion_adds_codebase_variants():
    from query_expand import expand

    variants = expand("AFM MCP recall", mode="rule")

    assert variants[0] == "AFM MCP recall"
    assert any("Apple Foundation Models" in v for v in variants)
    assert any("Model Context Protocol" in v for v in variants)
    assert len(variants) == len(dict.fromkeys(variants))


def test_afm_expansion_degrades_to_rule(monkeypatch):
    import query_expand

    def fail(*args, **kwargs):
        raise OSError("bridge down")

    monkeypatch.setattr(query_expand.urllib.request, "urlopen", fail)

    variants = query_expand.expand("AFM recall", mode="afm")

    assert variants[0] == "AFM recall"
    assert any("Apple Foundation Models" in v for v in variants)


def test_retrieve_expands_variants_and_merges_by_rrf(monkeypatch, tmp_path):
    engine, db_obj, _ = _make_db(tmp_path)
    doc1, _ = _insert_doc(db_obj, path="/wiki/afm.md", text="Apple Foundation Models adapter")
    doc2, _ = _insert_doc(db_obj, path="/wiki/mcp.md", text="Model Context Protocol bridge")

    calls = []

    def fake_fts(query, limit):
        calls.append(query)
        if "Apple Foundation Models" in query:
            return [{
                "doc_id": doc1, "path": "/wiki/afm.md", "agent": "unknown", "sigil": "?",
                "bm25_rank": -1.0, "decay_score": 1.0, "page_status": "accepted",
                "privacy_level": "safe", "page_type": "concept",
            }]
        if "Model Context Protocol" in query:
            return [{
                "doc_id": doc2, "path": "/wiki/mcp.md", "agent": "unknown", "sigil": "?",
                "bm25_rank": -1.0, "decay_score": 1.0, "page_status": "accepted",
                "privacy_level": "safe", "page_type": "concept",
            }]
        return []

    monkeypatch.setattr(engine, "_fts_search", fake_fts)
    monkeypatch.setattr(engine, "_semantic_search", lambda query, limit: [])

    results = engine.retrieve("AFM MCP", limit=5, expand=True, budget_tokens=False)

    assert any("Apple Foundation Models" in q for q in calls)
    assert any("Model Context Protocol" in q for q in calls)
    assert {r["doc_id"] for r in results} == {doc1, doc2}
    assert results[0]["query_variants"][0] == "AFM MCP"
    assert all("expansion_rrf_score" in r["provenance"] for r in results)


def test_sovrd_search_forwards_expand_and_summarize(monkeypatch):
    import sovrd

    captured = {}

    class FakeEngine:
        last_trace_id = "trace-1"

        def retrieve(self, **kwargs):
            captured.update(kwargs)
            return [{"source": "ok", "query_variants": ["q", "expanded q"]}]

    monkeypatch.setattr(sovrd, "_lazy_retrieval", lambda: FakeEngine())

    resp = sovrd._handle_search({
        "query": "q",
        "expand": "afm",
        "summarize_neighborhood": True,
    }, request_id=1)

    assert resp["result"]["query_variants"] == ["q", "expanded q"]
    assert captured["expand"] == "afm"
    assert captured["summarize_neighborhood"] is True


def test_neighborhood_summary_degrades_when_afm_unavailable(monkeypatch, tmp_path):
    import retrieval

    engine, db_obj, _ = _make_db(tmp_path)
    doc_id, _ = _insert_doc(
        db_obj,
        path="/wiki/entity.md",
        text="Entity page links to [[wiki/concepts/linked]].",
        page_type="entity",
    )
    _insert_doc(
        db_obj,
        path="/wiki/concepts/linked.md",
        text="Linked page context.",
        page_type="concept",
    )
    monkeypatch.setattr(retrieval, "summarize_with_afm", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "_semantic_search", lambda query, limit: [])

    results = engine.retrieve(
        "Entity",
        limit=1,
        expand=False,
        summarize_neighborhood=True,
        depth="chunk",
        budget_tokens=False,
    )

    assert results[0]["doc_id"] == doc_id
    assert results[0]["neighborhood_summary"]["status"] == "unavailable"
    assert results[0]["neighborhood_summary"]["links"]


def test_neighborhood_summary_uses_afm_when_available(monkeypatch, tmp_path):
    import retrieval

    engine, db_obj, _ = _make_db(tmp_path)
    _insert_doc(
        db_obj,
        path="/wiki/entity.md",
        text="Entity page links to [[wiki/concepts/linked]].",
        page_type="entity",
    )
    _insert_doc(
        db_obj,
        path="/wiki/concepts/linked.md",
        text="Linked page context.",
        page_type="concept",
    )
    monkeypatch.setattr(retrieval, "summarize_with_afm", lambda prompt: "Linked context summary.")
    monkeypatch.setattr(engine, "_semantic_search", lambda query, limit: [])

    results = engine.retrieve(
        "Entity",
        limit=1,
        expand=False,
        summarize_neighborhood=True,
        depth="chunk",
        budget_tokens=False,
    )

    assert results[0]["neighborhood_summary"]["status"] == "ok"
    assert results[0]["neighborhood_summary"]["summary"] == "Linked context summary."

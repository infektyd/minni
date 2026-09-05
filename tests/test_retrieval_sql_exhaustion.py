"""SQL exhaustion ends eligibility refill without weakening vector coverage."""
import pytest

from minni.retrieval import RetrievalEngine, _trace_ring
from test_retrieval_audit_regressions import retrieve
from test_retrieval_visibility import _make_expand_db, _seed_expand_doc


@pytest.mark.parametrize("sort", ["semantic", "chronological"])
@pytest.mark.parametrize("allowed_tail", [False, True])
def test_short_sql_window_stops_after_full_authorization_scan(tmp_path, monkeypatch, sort, allowed_tail):
    db, config = _make_expand_db(tmp_path, "exhaustion.db")
    config.reranker_enabled = False
    config.hyde_enabled = False
    config.feedback_enabled = False
    conn = db._get_conn()
    for number in range(12):
        allowed = allowed_tail and number == 11
        agent = "codex" if allowed else "foreign"
        path = f"/tmp/v/{number}.md"
        doc_id = _seed_expand_doc(conn, path, agent, "safe" if allowed else "private")
        text = "match extra irrelevant words" if allowed else "match match match"
        conn.execute("INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, ?, 'T')",
                     (doc_id, path, text, agent))
    conn.commit()
    engine = RetrievalEngine(db, config)
    monkeypatch.setattr(engine, "_semantic_search", lambda *_a, **_k: [])
    windows = []
    method = "_fts_search" if sort == "semantic" else "_chronological_search"
    original = getattr(engine, method)

    def counted(query, window, *args, **kwargs):
        windows.append(window)
        return original(query, window, *args, **kwargs)

    monkeypatch.setattr(engine, method, counted)
    try:
        results = retrieve(engine, sort=sort)
        if allowed_tail:
            assert [result["doc_id"] for result in results] == [doc_id]
            assert engine.last_auth_suppression is None
            assert windows == ([1, 2, 4] if sort == "semantic" else [1, 2, 4, 8, 16])
        else:
            assert results == []
            assert engine.last_auth_suppression["suppressed"] == 12
            assert windows == [1, 2, 4, 8, 16]
        trace = _trace_ring().get(engine.last_trace_id, requester="codex")
        assert trace is not None
        assert not trace.get("candidate_window_exhausted")
    finally:
        db.close()

"""
PR-8 tests: HyDE cold-query second pass and provenance.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))


def test_should_trigger_hyde_only_when_all_top_results_below_floor():
    from hyde import should_trigger_hyde

    assert should_trigger_hyde(
        [{"confidence": 0.1}, {"confidence": 0.39}],
        enabled=True,
        floor=0.4,
    )
    assert not should_trigger_hyde(
        [{"confidence": 0.1}, {"confidence": 0.4}],
        enabled=True,
        floor=0.4,
    )
    assert not should_trigger_hyde(
        [{"confidence": 0.1}],
        enabled=False,
        floor=0.4,
    )
    assert not should_trigger_hyde([], enabled=True, floor=0.4)


def test_generate_hypothetical_answer_returns_none_when_afm_fails():
    from hyde import generate_hypothetical_answer

    def failing_client(_payload, _url, _timeout):
        raise OSError("bridge down")

    answer = generate_hypothetical_answer(
        "cold query",
        client=failing_client,
        url="http://127.0.0.1:1/v1/chat/completions",
    )

    assert answer is None


def test_rrf_merge_marks_hyde_contributed_results():
    from hyde import merge_hyde_results

    original = [
        {"doc_id": 1, "path": "one.md", "final_score": 0.2, "provenance": {"doc_id": 1}},
        {"doc_id": 2, "path": "two.md", "final_score": 0.1},
    ]
    hyde = [
        {"doc_id": 3, "path": "three.md", "final_score": 0.9},
        {"doc_id": 1, "path": "one.md", "final_score": 0.8},
    ]

    merged = merge_hyde_results(original, hyde, limit=3, rrf_k=60)

    by_id = {r["doc_id"]: r for r in merged}
    assert by_id[3]["provenance"]["via_hyde"] is True
    assert by_id[1]["provenance"]["via_hyde"] is True
    assert by_id[2].get("provenance", {}).get("via_hyde") is not True


def test_retrieve_runs_one_hyde_pass_for_low_confidence_results(monkeypatch, tmp_path):
    import db as db_mod
    from config import SovereignConfig
    from db import SovereignDB
    from retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        reranker_enabled=False,
        context_budget_tokens=0,
        hyde_enabled=True,
        hyde_confidence_floor=0.4,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    engine = RetrievalEngine(db_obj, cfg)
    calls = []
    now = time.time()

    def hit(doc_id, text):
        return {
            "doc_id": doc_id,
            "chunk_id": doc_id * 10,
            "path": f"/wiki/{doc_id}.md",
            "agent": "unknown",
            "sigil": "?",
            "decay_score": 1.0,
            "page_status": "accepted",
            "privacy_level": "safe",
            "page_type": "concept",
            "evidence_refs": None,
            "indexed_at": now,
            "layer": "knowledge",
            "chunk_text": text,
            "heading_context": "",
        }

    def fake_fts(query, limit):
        calls.append(query)
        if query == "hypothetical answer":
            return [hit(2, "hyde result")]
        return [hit(1, "original result")]

    monkeypatch.setattr(engine, "_fts_search", fake_fts)
    monkeypatch.setattr(engine, "_semantic_search", lambda query, limit: [])
    monkeypatch.setattr("hyde.generate_hypothetical_answer", lambda query, config=None: "hypothetical answer")

    results = engine.retrieve("cold query", limit=2, depth="chunk", budget_tokens=False)

    assert calls == ["cold query", "hypothetical answer"]
    hyde_result = next(r for r in results if r["doc_id"] == 2)
    assert hyde_result["provenance"]["via_hyde"] is True

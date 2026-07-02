from types import SimpleNamespace

import pytest

import retrieval as retrieval_module
from retrieval import RetrievalEngine


def _engine_for_attribution():
    engine = object.__new__(RetrievalEngine)
    engine.config = SimpleNamespace(
        attribution_enabled=True,
        attribution_model="fake-nli",
    )
    engine._attribution_model = None
    return engine


def test_attribution_scoring_skips_model_without_claim():
    engine = _engine_for_attribution()

    class ExplodingModel:
        def predict(self, _pairs):
            raise AssertionError("attribution model should not be called without a claim")

    engine._attribution_model = ExplodingModel()

    assert engine._score_attribution(None, "Evidence text") is None
    assert engine._score_attribution("   ", "Evidence text") is None


def test_attribution_scoring_uses_nli_cross_encoder_for_claim():
    engine = _engine_for_attribution()

    class FakeNLI:
        def predict(self, pairs):
            assert pairs == [("Evidence says Paris is in France.", "Paris is in France.")]
            return [[0.01, 3.0, 0.02]]

    engine._attribution_model = FakeNLI()

    scored = engine._score_attribution("Paris is in France.", "Evidence says Paris is in France.")

    assert scored["attribution"] == "entailed"
    assert scored["attribution_score"] == pytest.approx(0.90, abs=0.05)
    assert scored["attribution_model"] == "fake-nli"


def test_attribution_config_flag_disables_scoring():
    engine = _engine_for_attribution()
    engine.config.attribution_enabled = False

    class ExplodingModel:
        def predict(self, _pairs):
            raise AssertionError("disabled attribution must not call the model")

    engine._attribution_model = ExplodingModel()

    assert engine._score_attribution("claim", "Evidence text") is None


def test_retrieve_with_claim_surfaces_attribution_and_logs_trace(monkeypatch):
    engine = object.__new__(RetrievalEngine)
    engine.config = SimpleNamespace(
        attribution_enabled=True,
        attribution_model="fake-nli",
        reranker_enabled=False,
        reranker_top_k=5,
        reranker_final_k=5,
        hyde_enabled=False,
        rrf_k=60,
        fts_weight=0.35,
        semantic_weight=0.65,
        query_expand_default="off",
        feedback_enabled=False,
        context_budget_tokens=4096,
        token_model="cl100k_base",
    )
    engine.db = object()
    engine._feedback_cache = {}
    engine._feedback_cache_loaded_at = 0.0
    engine._correction_types = set()
    engine.last_trace_id = None

    row = {
        "doc_id": 7,
        "chunk_id": 70,
        "path": "/tmp/minni/wiki/paris.md",
        "agent": "wiki:shared",
        "sigil": "book",
        "final_score": 0.7,
        "rrf_score": 0.03,
        "fts_rank": 1,
        "sem_rank": None,
        "chunk_text": "Evidence says Paris is in France.",
        "heading_context": "",
        "decay_score": 1.0,
        "page_status": "accepted",
        "privacy_level": "safe",
        "page_type": "wiki",
        "evidence_refs": None,
        "indexed_at": None,
        "layer": "knowledge",
    }

    monkeypatch.setattr(engine, "_fts_search", lambda *_args, **_kwargs: [row])
    monkeypatch.setattr(engine, "_semantic_search", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(engine, "_rrf_merge", lambda *_args, **_kwargs: [dict(row)])
    monkeypatch.setattr(engine, "_filter_candidates", lambda rows, *_args, **_kwargs: rows)
    monkeypatch.setattr(engine, "_apply_feedback_demotions", lambda rows, *_args, **_kwargs: rows)
    monkeypatch.setattr(engine, "_score_attribution", lambda claim, text: {
        "attribution": "entailed",
        "attribution_score": 0.91,
        "attribution_model": "fake-nli",
    })

    captured = {}

    class FakeTraceRing:
        def add(self, trace):
            captured.update(trace)
            return "trace-attribution"

    monkeypatch.setattr(retrieval_module, "_trace_ring", lambda: FakeTraceRing())

    results = engine.retrieve(
        "Paris location",
        claim="Paris is in France.",
        limit=1,
        update_access=False,
        budget_tokens=False,
        expand=False,
    )

    assert results[0]["attribution"] == "entailed"
    assert results[0]["attribution_score"] == 0.91
    assert 'attribution="entailed"' in results[0]["text"]
    assert captured["claim"] == "Paris is in France."
    assert captured["attribution_scores"] == [
        {"doc_id": 7, "chunk_id": 70, "attribution": "entailed", "score": 0.91}
    ]

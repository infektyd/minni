"""Pinned-CPU CrossEncoder.predict batch cap: kwargs, fallback, cache, deadline."""
from types import SimpleNamespace
import time

import pytest

from minni.config import DEFAULT_CONFIG, SovereignConfig
from minni.rerank_cache import RerankCache
from minni.retrieval import RetrievalEngine
import minni.models as models
import minni.retrieval as retrieval


class RecordingReranker:
    model_name = "cpu-batch-fake"
    version = "1"

    def __init__(self):
        self.calls = []

    def predict(self, pairs, **kwargs):
        self.calls.append({"pairs": [list(p) for p in pairs], "kwargs": dict(kwargs)})
        return [float(len(pair[1])) for pair in pairs]


def _engine(model, *, batch_size=8, corpus="/tmp/cpu-batch.db"):
    engine = object.__new__(RetrievalEngine)
    engine.config = SimpleNamespace(
        db_path=corpus,
        reranker_model="cpu-batch-fake",
        reranker_cpu_predict_batch_size=batch_size,
    )
    engine._reranker = model
    engine._apply_rerank_score_adjustments = lambda rows: None
    engine._set_current_deadline(None)
    return engine


def _candidates(n=20):
    return [
        {"chunk_id": i + 1, "chunk_text": "x" * (i + 1), "heading_context": ""}
        for i in range(n)
    ]


def test_default_config_cpu_predict_batch_size_is_eight():
    assert DEFAULT_CONFIG.reranker_cpu_predict_batch_size == 8
    assert SovereignConfig().reranker_cpu_predict_batch_size == 8


def test_pinned_cpu_path_passes_capped_batch_size(monkeypatch):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", RerankCache())
    monkeypatch.setattr(models, "cross_encoder_unlocked_predict_safe", lambda: True)
    model = RecordingReranker()
    engine = _engine(model, batch_size=8)
    rows = engine._rerank("q", _candidates(12))
    assert len(model.calls) == 1
    assert model.calls[0]["kwargs"] == {
        "show_progress_bar": False,
        "batch_size": 8,
    }
    assert len(model.calls[0]["pairs"]) == 12
    assert [r["chunk_id"] for r in rows] == list(range(12, 0, -1))
    assert {r["chunk_id"] for r in rows} == set(range(1, 13))


@pytest.mark.parametrize("raw, expected_batch", [(4, 4), (1, 1), (8, 8), (32, 8), (100, 8)])
def test_cpu_batch_size_config_boundaries_on_pinned_path(monkeypatch, raw, expected_batch):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", RerankCache())
    monkeypatch.setattr(models, "cross_encoder_unlocked_predict_safe", lambda: True)
    model = RecordingReranker()
    engine = _engine(model, batch_size=raw)
    engine._rerank("q", _candidates(3))
    assert model.calls[0]["kwargs"]["batch_size"] == expected_batch
    assert model.calls[0]["kwargs"]["show_progress_bar"] is False


@pytest.mark.parametrize("raw", [0, -1, True, "8", None, 0.0, 2.5, float("nan"), float("inf"), float("-inf")])
def test_invalid_cpu_batch_size_omits_batch_kwarg_on_pinned_path(monkeypatch, raw):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", RerankCache())
    monkeypatch.setattr(models, "cross_encoder_unlocked_predict_safe", lambda: True)
    model = RecordingReranker()
    engine = _engine(model, batch_size=raw)
    engine._rerank("q", _candidates(2))
    assert model.calls[0]["kwargs"] == {"show_progress_bar": False}


def test_locked_fallback_predict_kwargs_unchanged(monkeypatch):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", RerankCache())
    monkeypatch.setattr(models, "cross_encoder_unlocked_predict_safe", lambda: False)
    model = RecordingReranker()
    engine = _engine(model, batch_size=8)
    rows = engine._rerank("q", _candidates(5))
    assert len(model.calls) == 1
    assert model.calls[0]["kwargs"] == {"show_progress_bar": False}
    assert "batch_size" not in model.calls[0]["kwargs"]
    assert len(rows) == 5
    assert {r["chunk_id"] for r in rows} == {1, 2, 3, 4, 5}


def test_expired_deadline_skips_predict_on_pinned_cpu_path(monkeypatch):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", RerankCache())
    monkeypatch.setattr(models, "cross_encoder_unlocked_predict_safe", lambda: True)
    model = RecordingReranker()
    engine = _engine(model)
    engine._set_current_deadline(time.monotonic() - 1)
    rows = engine._rerank("q", _candidates(4))
    assert model.calls == []
    assert [r["chunk_id"] for r in rows] == [1, 2, 3, 4]
    assert "deadline" in str(engine.last_rerank_degraded)


def test_nonpreemptible_predict_keeps_scores_after_deadline(monkeypatch):
    import minni.rerank_cache as caches
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", None)
    monkeypatch.setattr(models, "cross_encoder_unlocked_predict_safe", lambda: True)
    now = [100.0]
    monkeypatch.setattr(retrieval.time, "monotonic", lambda: now[0])

    class Slow(RecordingReranker):
        def predict(self, pairs, **kwargs):
            now[0] = 110.0
            return super().predict(pairs, **kwargs)

    model = Slow()
    engine = _engine(model)
    engine._set_current_deadline(105.0)
    rows = engine._rerank("q", [{"chunk_id": 1, "chunk_text": "abcd"}])
    assert len(model.calls) == 1
    assert model.calls[0]["kwargs"]["batch_size"] == 8
    assert rows[0]["rerank_score"] == 4
    assert "nonpreemptible" in engine.last_rerank_degraded


def test_cache_hit_skips_predict_on_pinned_cpu_path(monkeypatch):
    import minni.rerank_cache as caches
    cache = RerankCache()
    monkeypatch.setattr(caches, "GLOBAL_RERANK_CACHE", cache)
    monkeypatch.setattr(models, "cross_encoder_unlocked_predict_safe", lambda: True)
    model = RecordingReranker()
    engine = _engine(model)
    rows = _candidates(3)
    first = engine._rerank("same", [dict(r) for r in rows])
    second = engine._rerank("same", [dict(r) for r in rows])
    assert len(model.calls) == 1
    assert model.calls[0]["kwargs"]["batch_size"] == 8
    assert [r["chunk_id"] for r in second] == [r["chunk_id"] for r in first]
    assert [r["rerank_score"] for r in second] == [r["rerank_score"] for r in first]

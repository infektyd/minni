"""Concurrent searches must publish and expose only their own trace IDs."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

import minni.retrieval as retrieval
from minni.retrieval import RetrievalEngine


def _engine(cls):
    engine = object.__new__(cls)
    engine.config = SimpleNamespace(
        reranker_top_k=10, reranker_enabled=False, reranker_final_k=10,
        rrf_k=60, fts_weight=1.0, semantic_weight=1.0,
        hyde_enabled=False, hyde_confidence_floor=0.0,
    )
    engine.db = None
    engine._reranker = None
    engine.last_trace_id = "main-thread"
    engine._resolve_query_variants = lambda query, expand: (
        [query + " one", query + " two"] if expand else [query]
    )
    engine._fts_search = lambda *args, **kwargs: [{
        "doc_id": 1, "agent": "codex", "page_type": "session",
        "privacy_level": "safe", "path": "/tmp/v/own.md",
        "chunk_text": "context", "score": 1.0, "sigil": "T",
    }]
    engine._semantic_search = lambda *args, **kwargs: []
    engine._rrf_merge = lambda fts, semantic, limit: list(fts)
    engine._filter_candidates = lambda rows, *args: list(rows)
    engine._apply_feedback_demotions = lambda rows, *args: list(rows)
    return engine


@pytest.mark.parametrize("expand", [False, True])
@pytest.mark.parametrize("second_capture_fails", [False, True])
def test_concurrent_trace_publication(monkeypatch, expand, second_capture_fails):
    first_published = threading.Event()
    second_published = threading.Event()
    release_timed_out = threading.Event()

    class ScheduledEngine(RetrievalEngine):
        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            # Pause A after publishing, before projecting the ID into rows.
            if name == "last_trace_id" and value == "A":
                first_published.set()
                if not second_published.wait(5):
                    release_timed_out.set()
            elif name == "last_trace_id" and first_published.is_set():
                if value == "B" or (second_capture_fails and value is None):
                    second_published.set()

    class Ring:
        def add(self, trace, **kwargs):
            if second_capture_fails and trace["query"] == "B":
                raise RuntimeError("trace store unavailable")
            return trace["query"]

    engine = _engine(ScheduledEngine)
    monkeypatch.setattr(retrieval, "_trace_ring", lambda: Ring())

    def run(query):
        rows = engine.retrieve(
            query, limit=1, update_access=False, expand=expand, budget_tokens=False,
        )
        return rows, engine.last_trace_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "A")
        try:
            assert first_published.wait(5)
            second = pool.submit(run, "B")
            second_rows, second_id = second.result(timeout=5)
        finally:
            second_published.set()
        first_rows, first_id = first.result(timeout=5)

    assert not release_timed_out.is_set()
    assert first_rows and {row["trace_id"] for row in first_rows} == {"A"}
    assert first_id == "A"
    assert second_id == (None if second_capture_fails else "B")
    if not second_capture_fails:
        assert second_rows and {row["trace_id"] for row in second_rows} == {"B"}
    assert engine.last_trace_id == "main-thread"


def test_trace_id_defaults_and_same_thread_compatibility():
    engine = object.__new__(RetrievalEngine)
    assert engine.last_trace_id is None
    engine.last_trace_id = "trace-1"
    assert engine.last_trace_id == "trace-1"
    engine.last_trace_id = None
    assert engine.last_trace_id is None

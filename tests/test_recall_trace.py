"""Tests for the durable recall trace added to
minni.minnid_runtime.recall.handle_search: a successful search records a TTL'd
episodic event (via context.lazy_episodic().add_event(...)) when
config.recall_trace is truthy and a principal resolved, gated so trace
failures never fail the search itself.

Builds a RecallContext directly out of fakes/stubs (all its fields are
injectable callables — see recall.RecallContext), the same style test doubles
used elsewhere in this suite (tests/test_retrieval_visibility.py,
tests/test_attribution_recall.py) for RetrievalEngine/EffectivePrincipal.
"""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from minni.config import DEFAULT_CONFIG
from minni.minnid_runtime.recall import RecallContext, handle_search
from minni.principal import EffectivePrincipal


def _principal(agent_id="agent-a", session_id=None):
    return EffectivePrincipal(agent_id=agent_id, workspace_id="default",
                              capabilities=["*"], session_id=session_id)


class _FakeRetrievalEngine:
    """Mimics RetrievalEngine just enough for handle_search's happy path."""

    def __init__(self, rows):
        self._rows = rows
        self.last_trace_id = None

    def retrieve(self, **kwargs):
        return [dict(r) for r in self._rows]

    def search_learnings(self, *args, **kwargs):
        return []


class _RecordingEpisodic:
    def __init__(self, raise_on_add=False):
        self.calls = []
        self._raise = raise_on_add

    def add_event(self, **kwargs):
        if self._raise:
            raise RuntimeError("episodic store is unavailable")
        self.calls.append(kwargs)


def _make_context(*, rows, recall_trace, principal, episodic=None):
    engine = _FakeRetrievalEngine(rows)
    # A real config instance (not a bare SimpleNamespace) so resolve_backend()
    # and any other config-attribute reads handle_search performs along the
    # way keep working; only recall_trace is overridden for the case at hand.
    config = dataclasses.replace(DEFAULT_CONFIG, recall_trace=recall_trace)

    def handler_principal(params, request_id):
        return principal, None

    return RecallContext(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}, "id": rid},
        make_response=lambda result, rid: {"result": result, "id": rid},
        handler_principal=handler_principal,
        lazy_retrieval=lambda: engine,
        agent_vault_retrieval=lambda agent_id: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: None,
        record_latency=lambda *a, **k: None,
        lazy_episodic=(lambda: episodic) if episodic is not None else None,
        default_config=config,
    )


ROWS = [
    {"doc_id": 1, "score": 0.87, "chunk_text": "alpha"},
    {"doc_id": 2, "score": 0.42, "chunk_text": "beta"},
]


def test_search_records_recall_trace_when_enabled_and_principal_present():
    episodic = _RecordingEpisodic()
    context = _make_context(rows=ROWS, recall_trace=True,
                            principal=_principal("agent-a"), episodic=episodic)

    response = handle_search({"query": "what is the plan"}, 1, context)

    assert "result" in response
    assert len(episodic.calls) == 1
    call = episodic.calls[0]
    assert call["agent_id"] == "agent-a"
    assert call["event_type"] == "recall"
    assert call["content"] == 'recall "what is the plan" — 2 hits, top 0.87'
    assert call["thread_id"] is None  # no session_id param passed
    assert call["metadata"]["hits"] == 2
    assert call["metadata"]["learnings"] == 0
    assert call["metadata"]["top_score"] == pytest.approx(0.87)
    assert call["metadata"]["depth"] == "snippet"
    expected_hash = hashlib.sha256(b"what is the plan").hexdigest()[:12]
    assert call["metadata"]["query_sha256_12"] == expected_hash


def test_search_trace_thread_id_passes_through_session_id_param():
    episodic = _RecordingEpisodic()
    context = _make_context(rows=ROWS, recall_trace=True,
                            principal=_principal("agent-a"), episodic=episodic)

    handle_search({"query": "q", "session_id": 4242}, 1, context)

    assert episodic.calls[0]["thread_id"] == "4242"


def test_search_trace_query_truncated_to_120_chars_in_content():
    episodic = _RecordingEpisodic()
    context = _make_context(rows=[], recall_trace=True,
                            principal=_principal("agent-a"), episodic=episodic)
    long_query = "x" * 200

    handle_search({"query": long_query}, 1, context)

    content = episodic.calls[0]["content"]
    assert content == f'recall "{"x" * 120}" — 0 hits, top 0.00'


def test_search_no_trace_when_recall_trace_disabled():
    episodic = _RecordingEpisodic()
    context = _make_context(rows=ROWS, recall_trace=False,
                            principal=_principal("agent-a"), episodic=episodic)

    response = handle_search({"query": "q"}, 1, context)

    assert "result" in response
    assert episodic.calls == []


def test_search_no_trace_when_principal_is_none():
    episodic = _RecordingEpisodic()
    context = _make_context(rows=ROWS, recall_trace=True,
                            principal=None, episodic=episodic)

    response = handle_search({"query": "q"}, 1, context)

    assert "result" in response
    assert episodic.calls == []


def test_search_no_trace_when_lazy_episodic_is_none():
    context = _make_context(rows=ROWS, recall_trace=True,
                            principal=_principal("agent-a"), episodic=None)
    assert context.lazy_episodic is None

    response = handle_search({"query": "q"}, 1, context)

    assert "result" in response  # search still succeeds


def test_search_survives_episodic_add_event_raising():
    episodic = _RecordingEpisodic(raise_on_add=True)
    context = _make_context(rows=ROWS, recall_trace=True,
                            principal=_principal("agent-a"), episodic=episodic)

    response = handle_search({"query": "q"}, 1, context)

    assert "result" in response
    assert response["result"]["count"] == 2

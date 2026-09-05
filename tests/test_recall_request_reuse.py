"""Own-vault reuse preserves the real search handler's scoped response."""

from contextlib import contextmanager
from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest

from minni.minnid_runtime import recall
from minni.principal import EffectivePrincipal


class Engine:
    def __init__(self, name, score):
        self.name = name
        self.config = SimpleNamespace(embedding_model="test", recall_trace=False)
        self.rows = [{
            "doc_id": 1, "source": f"{name}/page.md", "score": score,
            "text": f"{name} content", "source_agent": name,
            "provenance": {"source_agent": name, "citations": ["evidence"]},
        }]
        self.calls = []
        self.learning_calls = []
        self.last_vector_degraded = None
        self.last_auth_suppression = None
        self.before_retrieve = lambda: None
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE documents(doc_id INTEGER, access_count INTEGER, last_accessed REAL)")
        self.conn.execute("INSERT INTO documents VALUES(1,0,NULL)")
        self.db = self

    @contextmanager
    def cursor(self):
        with self.conn:
            yield self.conn.cursor()

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        self.before_retrieve()
        return deepcopy(self.rows)

    def search_learnings(self, query, **kwargs):
        self.learning_calls.append(kwargs)
        return []

    def accesses(self):
        return self.conn.execute("SELECT access_count FROM documents").fetchone()[0]


class SeparateEngineView:
    """Same results/config via a different engine identity: no reuse eligible.

    This runs the old two-retrieve shape through the same real handler and
    provides a response oracle without duplicating the handler implementation.
    """
    def __init__(self, engine):
        self.engine = engine

    def __getattr__(self, name):
        return getattr(self.engine, name)


@pytest.fixture
def engines():
    fleet = [Engine("own", 0.8), Engine("other", 0.9), Engine("shared", 0.4)]
    yield fleet
    for engine in fleet:
        engine.conn.close()


def context(engines, *, distinct=False, agent="codex", all_vaults=None):
    own, other, shared = engines
    principal = EffectivePrincipal(agent_id=agent, workspace_id="workspace-a", capabilities=["recall"])
    combined_own = SeparateEngineView(own) if distinct else own
    return recall.RecallContext(
        make_response=lambda result, rid: {"result": result},
        make_error=lambda code, message, rid: {"error": {"code": code, "message": message}},
        handler_principal=lambda *a: (principal, None),
        lazy_retrieval=lambda: shared,
        agent_vault_retrieval=lambda a: (own, agent, "own.db"),
        all_vault_retrievals=all_vaults or (lambda: [
            (combined_own, agent, "own.db"), (other, "cursor", "other.db"),
        ]),
        trace_ring=lambda: None, record_latency=lambda *a: None,
        default_config=SimpleNamespace(vector_backends=["faiss-disk"], recall_trace=False),
    )


def search(ctx, **params):
    response = recall.handle_search({"query": "socket", "layers": "knowledge", **params}, 1, ctx)
    assert "error" not in response, response
    return response["result"]


@pytest.mark.parametrize("scope", ["personal", "combined", "both", None])
@pytest.mark.parametrize("degraded", [False, True])
def test_reuse_matches_uncached_response_and_access_accounting(engines, scope, degraded):
    own, other, shared = engines
    if degraded:
        own.last_vector_degraded = "encoder unavailable"
        own.last_auth_suppression = {"pre_gate": 3, "suppressed": 2}
    params = {"scope": scope} if scope is not None else {}
    expected = search(context(engines, distinct=True), **params)
    old_calls = len(own.calls)
    old_accesses = [e.accesses() for e in engines]
    for engine in engines:
        engine.calls.clear()
        engine.conn.execute("UPDATE documents SET access_count=0")
    actual = search(context(engines), **params)
    assert actual == expected
    assert len(own.calls) == 1
    assert old_calls == (2 if scope in {"both", None} else 1)
    assert [e.accesses() for e in engines] == old_accesses
    assert all(call["update_access"] is False for e in engines for call in e.calls)
    if scope in {"both", None}:
        assert [(r["source"], r["src"]) for r in actual["results"]] == [
            ("other/page.md", "c"), ("own/page.md", "p"), ("shared/page.md", "c"),
        ]


def test_consumers_get_independent_nested_rows(engines, monkeypatch):
    original_tag = recall.tag_document_results
    tagged = []

    def mutate_nested(rows, *, src):
        result = original_tag(rows, src=src)
        for row in result:
            row["provenance"]["citations"].append(src)
            if row["source"] == "own/page.md":
                tagged.append(row)
        return result

    monkeypatch.setattr(recall, "tag_document_results", mutate_nested)
    result = search(context(engines), scope="both")
    assert len(engines[0].calls) == 1
    assert [r["provenance"]["citations"] for r in tagged] == [["evidence", "p"], ["evidence", "c"]]
    assert tagged[0]["provenance"] is not tagged[1]["provenance"]
    assert next(r for r in result["results"] if r["src"] == "p")["provenance"] == {
        "citations": ["evidence", "p"],
    }


def test_snapshot_keeps_auth_degradation_and_deadline_verdict(engines):
    own, other, shared = engines
    own.last_vector_degraded = "search deadline: semantic skipped"
    own.last_auth_suppression = {"pre_gate": 3, "suppressed": 2}
    other.rows = shared.rows = []

    def mutate_diagnostics_between_legs():
        own.last_vector_degraded = None
        own.last_auth_suppression["suppressed"] = 0
        return [(own, "codex", "own.db")]

    result = search(context(engines, all_vaults=mutate_diagnostics_between_legs), scope="both")
    assert len(own.calls) == 1
    own_reports = result["degradation"][:2]
    assert [r["src"] for r in own_reports] == ["p", "c"]
    assert all(r["vector_degraded"] is True for r in own_reports)
    assert [r["suppressed"] for r in result["auth_suppression"]] == [2, 2]
    assert own_reports[1]["source_agent"] == "codex"
    assert own.accesses() == 0
    assert shared.learning_calls[-1]["update_access"] is False


def test_failed_personal_call_can_retry_in_combined(engines):
    own = engines[0]

    def fail_once():
        if len(own.calls) == 1:
            raise RuntimeError("temporary vault failure")

    own.before_retrieve = fail_once
    result = search(context(engines), scope="both")
    assert len(own.calls) == 2
    assert any(r["source"] == "own/page.md" for r in result["results"])
    assert any(d.get("personal_index_failed") for d in result["degradation"])


def test_empty_personal_result_is_reused_without_losing_auth_suppression(engines):
    own = engines[0]
    own.rows = []
    own.last_auth_suppression = {"pre_gate": 2, "suppressed": 2}
    result = search(context(engines), scope="both")
    assert len(own.calls) == 1
    assert [r["src"] for r in result["auth_suppression"]] == ["p", "c"]
    assert all(r["src"] == "c" for r in result["results"])
    assert own.accesses() == 0


def test_request_state_and_deadline_are_not_reused_across_calls(engines, monkeypatch):
    monkeypatch.setattr(recall.time, "monotonic", lambda: 1010.0)
    ctx = context(engines)
    search(ctx, scope="both", timeout_ms=30_000, _accepted_monotonic=1000.0,
           backend=["faiss-disk"], claim="a claim", expand=False, depth="chunk")
    for engine in engines:
        assert len(engine.calls) == 1
        call = engine.calls[0]
        assert call["deadline_monotonic"] == 1027.0
        assert call["agent_id"] == "codex"
        assert call["principal"].agent_id == "codex"
        assert call["workspace"] == "workspace-a"
        assert call["cross_agent"] is False
        assert call["layers"] == ["knowledge"]
        assert call["backend"] == ["faiss-disk"]
        assert call["claim"] == "a claim"
        assert call["expand"] is False
        assert call["depth"] == "chunk"
    engines[0].rows[0]["text"] = "new content"
    result = search(context(engines, agent="claude-code"), scope="both", query="different")
    assert len(engines[0].calls) == 2
    assert engines[0].calls[-1]["query"] == "different"
    assert engines[0].calls[-1]["principal"].agent_id == "claude-code"
    assert any(r["text"] == "new content" for r in result["results"])


def install_trace_capture(engine):
    def capture():
        engine.last_trace_id = f"{engine.name}-{len(engine.calls)}"
        for row in engine.rows:
            row["trace_id"] = engine.last_trace_id
    engine.before_retrieve = capture


@pytest.mark.parametrize("empty", [False, True])
def test_personal_response_trace_is_current_even_with_stale_shared_trace(engines, empty):
    own, _, shared = engines
    shared.last_trace_id = "previous-shared-request"
    install_trace_capture(own)
    if empty:
        own.rows = []
    result = search(context(engines), scope="personal")
    assert result["trace_id"] == "own-1"
    assert result["trace_ids"] == ["own-1"]
    assert result["trace_scope"] == "retrieval_leg"
    assert not shared.calls
    assert all(row["trace_id"] == "own-1" for row in result["results"])


def test_multicorpus_response_retains_each_trace_without_claiming_one_request_trace(engines):
    for engine in engines:
        install_trace_capture(engine)
    result = search(context(engines), scope="both")
    assert result["trace_id"] is None
    assert result["trace_ids"] == ["own-1", "other-1", "shared-1"]
    assert {row["trace_id"] for row in result["results"]} == set(result["trace_ids"])


@pytest.mark.parametrize("failure", [False, True])
def test_previous_trace_is_excluded_when_retrieval_produces_no_new_trace(engines, failure):
    own, _, shared = engines
    own.last_trace_id = "stale-own"
    shared.last_trace_id = "stale-shared"
    own.rows = shared.rows = []
    if failure:
        def fail():
            raise RuntimeError("temporary failure before trace capture")
        own.before_retrieve = fail
    result = search(context(engines), scope="personal")
    assert result["trace_id"] is None
    assert result["trace_ids"] == []

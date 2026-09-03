"""Serial-vs-parallel parity for the #388 search fan-out (wright, zero-change).

Compares the legacy serial order against the bounded-pool fan-out with the
same query + params and requires byte-identical merged results AND identical
suppression/degradation aggregates:

* retrieval level: per-variant recursive retrieve() fan-out
  (RETRIEVAL_VARIANT_PARALLEL), incl. the rerank / query-expand / vector /
  auth-suppression aggregations.
* recall level: per-corpus legs in retrieve_combined + both-scope
  personal/combined legs (RECALL_LEG_PARALLEL), incl. degradation ordering,
  soft-fail entries, prefer_personal merge order, and envelope trace_id.

Only trace ring ids are normalized (fresh ring entries per run by design);
everything else must compare exactly equal.
"""

import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minni.retrieval as retrieval_mod
import minni.minnid_runtime.recall as recall_mod
from minni.minnid_runtime.recall import RecallContext, handle_search
from minni.principal import EffectivePrincipal


# ── shared fixtures ─────────────────────────────────────────────────────────


def _make_engine(tmp_path):
    import numpy as np

    import minni.db as db_mod
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.faiss_index import FAISSIndex
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        reranker_enabled=True,
        context_budget_tokens=0,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return RetrievalEngine(db_obj, cfg, FAISSIndex(cfg)), db_obj


def _insert_doc(db_obj, *, path, text, agent="codex"):
    import numpy as np

    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, indexed_at, last_modified, page_status, privacy_level, page_type)
               VALUES (?, ?, '?', ?, ?, 'accepted', 'safe', 'concept')""",
            (path, agent, now, now),
        )
        doc_id = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, heading_context, computed_at)
               VALUES (?, 0, ?, ?, '', ?)""",
            (doc_id, text, np.ones(384, dtype=np.float32).tobytes(), now),
        )
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, ?, '?')",
            (doc_id, path, text, agent),
        )
    return doc_id


def _seed_docs(db_obj):
    _insert_doc(db_obj, path="wiki/alpha.md", text="alpha beta reconciliation ledger")
    _insert_doc(db_obj, path="wiki/gamma.md", text="gamma delta migration ledger")
    _insert_doc(db_obj, path="wiki/both.md", text="alpha beta gamma delta combined ledger")


def _norm_results(rows):
    """Copy result rows with per-run trace ids normalized out."""
    normed = []
    for r in rows:
        c = dict(r)
        if "trace_id" in c:
            c["trace_id"] = "<trace>"
        normed.append(c)
    return normed


def _snapshot(engine, rows):
    return (
        _norm_results(rows),
        engine.last_auth_suppression,
        engine.last_rerank_degraded,
        engine.last_query_expand_degraded,
        engine.last_vector_degraded,
        engine.last_hyde_degraded,
        engine.last_trace_id is not None,
    )


def _run_retrieve(engine, principal):
    return engine.retrieve(
        query="alpha beta gamma",
        limit=5,
        update_access=True,
        use_hyde=False,
        principal=principal,
        workspace="default",
    )


@pytest.fixture
def stubbed_engine(tmp_path, monkeypatch):
    """Real engine, real FTS/DB, but: no model downloads, fixed 2 variants.

    * embedder -> None, so _encode_query takes the real FTS-only degrade path
      (last_vector_degraded set, no weights fetched);
    * reranker -> raising stub, so _rerank takes the real R5 fallback path
      (last_rerank_degraded set);
    * expand -> two fixed variants + a parent-level degrade reason, so the
      multi-variant recursion + parent_expand_degraded capture both run.
    """
    import minni.models as models_mod

    engine, _db = _make_engine(tmp_path)
    _seed_docs(_db)

    monkeypatch.setattr(models_mod, "get_embedder", lambda: None)

    class _BoomReranker:
        def predict(self, *args, **kwargs):
            raise RuntimeError("reranker down for parity test")

    engine._reranker = _BoomReranker()
    monkeypatch.setattr(
        retrieval_mod,
        "expand_query_with_status",
        lambda query, mode="rule": (
            ["alpha beta", "gamma delta"],
            "afm_unavailable_for_test",
        ),
    )
    return engine


def _owner():
    return EffectivePrincipal(
        agent_id="codex",
        workspace_id="default",
        capabilities=["search", "recall"],
        allowed_vault_roots=["/tmp/test-vault"],
    )


def _intruder():
    return EffectivePrincipal(
        agent_id="intruder",
        workspace_id="default",
        capabilities=["search", "recall"],
        allowed_vault_roots=["/tmp/test-vault"],
    )


# ── retrieval-level parity ──────────────────────────────────────────────────


def test_variant_fanout_matches_serial_allowed(stubbed_engine, monkeypatch):
    """Passing read gate: identical ordered hits + identical aggregates."""
    monkeypatch.setattr(retrieval_mod, "RETRIEVAL_VARIANT_PARALLEL", False)
    serial = _snapshot(stubbed_engine, _run_retrieve(stubbed_engine, _owner()))
    assert len(serial[0]) > 0, "seeded docs must match for a non-vacuous test"

    monkeypatch.setattr(retrieval_mod, "RETRIEVAL_VARIANT_PARALLEL", True)
    parallel = _snapshot(stubbed_engine, _run_retrieve(stubbed_engine, _owner()))

    assert parallel == serial


def test_variant_fanout_matches_serial_suppressed(stubbed_engine, monkeypatch):
    """Denying read gate: identical (empty) merge + identical suppression."""
    monkeypatch.setattr(retrieval_mod, "RETRIEVAL_VARIANT_PARALLEL", False)
    serial = _snapshot(stubbed_engine, _run_retrieve(stubbed_engine, _intruder()))
    assert serial[0] == []
    assert serial[1] is not None, "denied gate must record a suppression"
    assert serial[1]["suppressed"] == serial[1]["pre_gate"] > 0
    assert len(serial[1]["variants"]) == 2

    monkeypatch.setattr(retrieval_mod, "RETRIEVAL_VARIANT_PARALLEL", True)
    parallel = _snapshot(stubbed_engine, _run_retrieve(stubbed_engine, _intruder()))

    assert parallel == serial


def test_variant_degradation_aggregates_present(stubbed_engine, monkeypatch):
    """The parity comparison above is non-vacuous on the degrade axes."""
    monkeypatch.setattr(retrieval_mod, "RETRIEVAL_VARIANT_PARALLEL", True)
    rows, auth, rerank, expand, vector, hyde, traced = _snapshot(
        stubbed_engine, _run_retrieve(stubbed_engine, _owner())
    )
    assert rows, "expected hits"
    assert rerank is not None and "alpha beta" in rerank and "gamma delta" in rerank
    assert expand is not None and "afm_unavailable_for_test" in expand
    assert vector is not None and "alpha beta" in vector and "gamma delta" in vector
    assert auth is None
    assert traced is True


# ── recall-level parity ─────────────────────────────────────────────────────


class _FakeEngine:
    """Deterministic canned corpus leg with a small sleep per call."""

    def __init__(self, name, rows, *, delay=0.05, fail=False):
        self._name = name
        self._rows = rows
        self._delay = delay
        self._fail = fail
        self.last_auth_suppression = None
        self.last_rerank_degraded = None
        self.last_query_expand_degraded = None
        self.last_vector_degraded = None
        self.last_hyde_degraded = None
        self.last_trace_id = None
        import types

        self.config = types.SimpleNamespace(embedding_model="fake-model")
        self.db = object()
        self.EPISODIC_NON_MEMORY_TYPES = []

    def retrieve(self, **kwargs):
        time.sleep(self._delay)
        if self._fail:
            raise RuntimeError(f"boom-index {self._name}")
        self.last_trace_id = f"trace-{self._name}"
        return [dict(r) for r in self._rows]

    def search_learnings(self, *args, **kwargs):
        return []

    def search_episodic(self, *args, **kwargs):
        return []


def _row(doc, score, src_hint=""):
    return {
        "doc_id": doc,
        "chunk_id": doc * 10,
        "path": f"wiki/{doc}.md",
        "source": f"wiki/{doc}.md",
        "score": score,
        "chunk_text": f"body {doc} {src_hint}",
    }


def _recall_harness(monkeypatch):
    shared = _FakeEngine("shared", [_row(1, 0.9), _row(2, 0.7)])
    personal = _FakeEngine("personal", [_row(2, 0.95), _row(3, 0.6)])
    vault_a = _FakeEngine("vault-a", [_row(4, 0.8)])
    vault_b = _FakeEngine("vault-b", [_row(5, 0.85)], fail=True)

    calls = {"agent_vault": 0, "all_vaults": 0}

    def agent_vault_retrieval(agent_id):
        calls["agent_vault"] += 1
        return (personal, "codex", "/db/personal.db")

    def all_vault_retrievals():
        calls["all_vaults"] += 1
        return [(vault_a, "a", "/db/a.db"), (vault_b, "b", "/db/b.db")]

    principal = EffectivePrincipal(
        agent_id="codex",
        workspace_id="default",
        capabilities=["search", "recall"],
        allowed_vault_roots=["/tmp/test-vault"],
    )
    context = RecallContext(
        make_error=lambda code, msg, rid: {"ok": False, "id": rid, "error": [code, msg]},
        make_response=lambda payload, rid: {"ok": True, "id": rid, "result": payload},
        handler_principal=lambda params, rid: (principal, None),
        lazy_retrieval=lambda: shared,
        agent_vault_retrieval=agent_vault_retrieval,
        all_vault_retrievals=all_vault_retrievals,
        trace_ring=lambda: None,
        record_latency=lambda *a: None,
        increment_request_count=lambda: None,
        logger=logging.getLogger("test-parity"),
    )
    return context, calls


def _run_scope(context, scope):
    t0 = time.perf_counter()
    out = handle_search({"query": "q", "scope": scope, "limit": 5}, 1, context)
    return out, time.perf_counter() - t0


@pytest.mark.parametrize("scope", ["both", "combined", "personal"])
def test_leg_fanout_matches_serial(monkeypatch, scope):
    """Corpus legs: identical envelope, ordering, diagnostics, trace."""
    context, calls = _recall_harness(monkeypatch)

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", False)
    serial, t_serial = _run_scope(context, scope)

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    parallel, t_parallel = _run_scope(context, scope)

    assert serial["ok"] is True and parallel["ok"] is True
    assert parallel == serial
    # The failing vault leg must soft-fail identically in combined/both.
    if scope in ("both", "combined"):
        degs = parallel["result"]["degradation"]
        assert any(
            d.get("combined_index_failed") for d in degs
        ), f"expected soft-failed vault entry: {degs}"
        assert parallel["result"]["trace_id"] == "trace-shared"
    print(f"\nscope={scope}: serial={t_serial:.3f}s parallel={t_parallel:.3f}s")

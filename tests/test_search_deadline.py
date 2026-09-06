"""Search must yield under the client JSON-RPC kill (audit slice).

Live: search p50 ~202s vs plugin DEFAULT_JSON_RPC_TIMEOUT_MS = 30s. The
handler runs in asyncio.to_thread (uncancellable). If retrieve() never
checks a deadline, FTS+FAISS+expand+CE burn the worker after the client
has already destroyed the socket.

These tests require:
1. retrieve() skips the semantic/CE legs when deadline_monotonic is already past
   and still returns FTS hits, marked degraded.
2. handle_search threads a deadline into retrieve (from timeout_ms, else a
   default under 30s).
3. A deadline still in the future does not start encode/FAISS/HyDE/CE when the
   leftover budget is below the stage floor, after the embedder lock, or after
   HyDE AFM. Deadline FTS-only hits must not calibrate or qty-delta access_count.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

import numpy as np

from minni.config import DEFAULT_CONFIG, SovereignConfig
from minni.db import SovereignDB
from minni.minnid_runtime.recall import (
    DEFAULT_SEARCH_BUDGET_MS,
    RecallContext,
    SEARCH_BUDGET_CLIENT_FRACTION,
    _DEADLINE_POISONED_KEY,
    _search_deadline_monotonic,
    handle_search,
    handle_sm_export_pack,
    merge_document_results,
)
from minni.principal import EffectivePrincipal
from minni.retrieval import RetrievalEngine, SEARCH_FAISS_REBUILD_MIN_REMAINING_S


def _engine(tmp_path, **cfg_overrides) -> RetrievalEngine:
    cfg_kwargs = dict(
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        vault_path=str(tmp_path / "vault/"),
        writeback_path=str(tmp_path / "learnings/"),
        graph_export_dir=str(tmp_path / "graphs/"),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
        query_expand_default="off",
    )
    cfg_kwargs.update(cfg_overrides)
    cfg = SovereignConfig(**cfg_kwargs)
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    engine.index_durable_document(
        content="# Deadline slice\n\nFTS must still find this paragraph about sockets.\n",
        path="wiki/concepts/deadline-slice.md",
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="accepted",
        layer="knowledge",
    )
    return engine


def test_retrieve_skips_semantic_when_deadline_already_past(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    calls = {"semantic": 0}

    def boom(self, *args, **kwargs):
        calls["semantic"] += 1
        raise AssertionError("semantic search must not run after the deadline")

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", boom)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=time.monotonic() - 1.0,
    )
    assert calls["semantic"] == 0
    assert rows, "FTS-only path must still return the indexed document"
    assert engine.last_vector_degraded
    assert "deadline" in str(engine.last_vector_degraded).lower()


def test_retrieve_skips_reranker_load_when_deadline_already_past(tmp_path, monkeypatch):
    # Production default is reranker_enabled=True. The shared fixture disables
    # it, which short-circuits `self.reranker` and hides the lazy CE load.
    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        faiss_index_path=str(tmp_path / "test.faiss"),
        vault_path=str(tmp_path / "vault/"),
        writeback_path=str(tmp_path / "learnings/"),
        graph_export_dir=str(tmp_path / "graphs/"),
        reranker_enabled=True,
        hyde_enabled=False,
        feedback_enabled=False,
        query_expand_default="off",
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    engine.index_durable_document(
        content="# Deadline slice\n\nFTS must still find this paragraph about sockets.\n",
        path="wiki/concepts/deadline-slice.md",
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="accepted",
        layer="knowledge",
    )
    loads = {"ce": 0}

    def boom_load(*args, **kwargs):
        loads["ce"] += 1
        raise AssertionError("cross-encoder must not load after the deadline")

    def boom_property(self):
        loads["ce"] += 1
        raise AssertionError("self.reranker must not be evaluated after the deadline")

    monkeypatch.setattr("minni.models.get_cross_encoder", boom_load)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(boom_property))

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=time.monotonic() - 1.0,
    )
    assert loads["ce"] == 0
    assert rows, "FTS-only path must still return the indexed document"
    assert engine.last_rerank_degraded
    assert "deadline" in str(engine.last_rerank_degraded).lower()


def test_retrieve_skips_query_expand_when_deadline_already_past(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    calls = {"expand": 0}

    def boom(self, query, expand):
        calls["expand"] += 1
        raise AssertionError("query expand must not run after the deadline")

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", boom)

    engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=True,
        deadline_monotonic=time.monotonic() - 1.0,
    )
    assert calls["expand"] == 0
    assert engine.last_query_expand_degraded
    assert "deadline" in str(engine.last_query_expand_degraded).lower()


class _RecordingEngine:
    def __init__(self):
        self.retrieve_kwargs = []
        self.last_trace_id = None
        self.last_auth_suppression = None
        self.last_vector_degraded = None
        self.last_rerank_degraded = None
        self.last_query_expand_degraded = None
        self.last_hyde_degraded = None
        self.config = DEFAULT_CONFIG

    def retrieve(self, **kwargs):
        self.retrieve_kwargs.append(kwargs)
        return [{"doc_id": 1, "score": 0.5, "text": "hit"}]

    def search_learnings(self, *args, **kwargs):
        return []

    def search_episodic(self, *args, **kwargs):
        return []


def test_handle_search_passes_deadline_from_timeout_ms():
    engine = _RecordingEngine()
    principal = EffectivePrincipal(
        agent_id="grok-build",
        workspace_id="default",
        capabilities=["*"],
    )
    config = dataclasses.replace(DEFAULT_CONFIG, recall_trace=False)
    context = RecallContext(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}, "id": rid},
        make_response=lambda result, rid: {"result": result, "id": rid},
        handler_principal=lambda params, rid: (principal, None),
        lazy_retrieval=lambda: engine,
        agent_vault_retrieval=lambda agent_id: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: None,
        record_latency=lambda *a, **k: None,
        lazy_episodic=None,
        default_config=config,
    )
    before = time.monotonic()
    handle_search({"query": "q", "timeout_ms": 8000}, 1, context)
    after = time.monotonic()
    assert engine.retrieve_kwargs, "handle_search must call retrieve"
    deadline = engine.retrieve_kwargs[0].get("deadline_monotonic")
    assert deadline is not None
    # 90% of 8000ms = 7.2s of work budget
    assert before < deadline <= after + 7.2 + 0.5


def test_handle_search_default_budget_is_under_jsonrpc_30s():
    engine = _RecordingEngine()
    principal = EffectivePrincipal(
        agent_id="grok-build",
        workspace_id="default",
        capabilities=["*"],
    )
    config = dataclasses.replace(DEFAULT_CONFIG, recall_trace=False)
    context = RecallContext(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}, "id": rid},
        make_response=lambda result, rid: {"result": result, "id": rid},
        handler_principal=lambda params, rid: (principal, None),
        lazy_retrieval=lambda: engine,
        agent_vault_retrieval=lambda agent_id: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: None,
        record_latency=lambda *a, **k: None,
        lazy_episodic=None,
        default_config=config,
    )
    before = time.monotonic()
    handle_search({"query": "q"}, 1, context)
    deadline = engine.retrieve_kwargs[0].get("deadline_monotonic")
    assert deadline is not None
    remaining = deadline - before
    assert remaining < 30.0
    assert remaining > 5.0


def _search_context(engine, *, vault_engine=None):
    principal = EffectivePrincipal(
        agent_id="grok-build",
        workspace_id="default",
        capabilities=["*"],
    )
    config = dataclasses.replace(DEFAULT_CONFIG, recall_trace=False)

    def agent_vault_retrieval(agent_id):
        if vault_engine is None:
            return None
        return (vault_engine, agent_id, None)

    context = RecallContext(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}, "id": rid},
        make_response=lambda result, rid: {"result": result, "id": rid},
        handler_principal=lambda params, rid: (principal, None),
        lazy_retrieval=lambda: engine,
        agent_vault_retrieval=agent_vault_retrieval,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: None,
        record_latency=lambda *a, **k: None,
        lazy_episodic=None,
        default_config=config,
    )
    return context


def _access_count(engine) -> int:
    with engine.db.cursor() as c:
        row = c.execute("SELECT access_count FROM documents LIMIT 1").fetchone()
    return int(row["access_count"])


def test_past_search_deadline_treats_sliver_remaining_as_elapsed():
    from minni.retrieval import past_search_deadline

    assert past_search_deadline(None) is False
    assert past_search_deadline(time.monotonic() - 1.0) is True
    assert past_search_deadline(time.monotonic() + 0.05) is True
    assert past_search_deadline(time.monotonic() + 30.0) is False


def test_retrieve_skips_semantic_when_remaining_budget_is_a_sliver(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    calls = {"semantic": 0}

    def boom(self, *args, **kwargs):
        calls["semantic"] += 1
        raise AssertionError("semantic search must not start with a sliver of budget")

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", boom)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=time.monotonic() + 0.05,
    )
    assert calls["semantic"] == 0
    assert rows, "FTS-only path must still return the indexed document"
    assert engine.last_vector_degraded
    assert "deadline" in str(engine.last_vector_degraded).lower()


def test_retrieve_starts_semantic_when_budget_is_above_the_floor(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    calls = {"semantic": 0}

    def fake(self, *args, **kwargs):
        calls["semantic"] += 1
        return []

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake)
    engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=time.monotonic() + 30.0,
    )
    assert calls["semantic"] == 1
    assert engine.last_vector_degraded is None


def test_hyde_rechecks_deadline_before_afm_second_faiss_and_ce(tmp_path, monkeypatch):
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, hyde_enabled=True, reranker_enabled=True)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    calls = {"semantic": [], "rerank": 0, "afm": 0}

    def fake_semantic(self, query, *args, **kwargs):
        calls["semantic"].append(query)
        return []

    def fake_rerank(self, query, merged):
        calls["rerank"] += 1
        return merged

    def fake_afm(query, config=None, timeout=2.0, **kwargs):
        calls["afm"] += 1
        calls["afm_timeout"] = timeout
        clock["now"] = 1_010.0
        return "A hypothetical note about sockets and JSON-RPC deadlines."

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", fake_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))
    monkeypatch.setattr("minni.hyde.should_trigger_hyde", lambda *a, **k: True)
    monkeypatch.setattr("minni.hyde.generate_hypothetical_answer", fake_afm)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        use_hyde=True,
        deadline_monotonic=1_005.0,
    )
    assert rows
    assert calls["afm"] == 1
    assert calls["semantic"] == ["sockets"]
    assert calls["rerank"] == 1
    assert engine.last_hyde_degraded
    assert "deadline" in str(engine.last_hyde_degraded).lower()


def test_encode_skips_after_embedder_lock_if_deadline_elapsed(tmp_path, monkeypatch):
    import minni.models as models
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    clock = {"now": 1_000.0}
    encode_calls = {"n": 0}
    waiting = threading.Event()
    real_lock = models.get_embedder_lock()

    class _FakeModel:
        def encode(self, query, show_progress_bar=False):
            encode_calls["n"] += 1
            return np.zeros(384, dtype=np.float32)

    class _GateLock:
        def acquire(self, blocking=True, timeout=-1):
            waiting.set()
            return real_lock.acquire(blocking, timeout)

        def release(self):
            return real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

        def locked(self):
            return real_lock.locked()

    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: _FakeModel()))
    monkeypatch.setattr(models, "get_embedder_lock", lambda: _GateLock())

    seen = {}
    released = False
    real_lock.acquire()
    try:
        def _run():
            engine.retrieve(
                "sockets",
                limit=5,
                budget_tokens=False,
                expand=False,
                deadline_monotonic=1_040.0,
            )
            seen["vector"] = engine.last_vector_degraded
            seen["encode"] = encode_calls["n"]

        thread = threading.Thread(target=_run)
        thread.start()
        assert waiting.wait(timeout=5), "retrieve never waited on the embedder lock"
        clock["now"] = 1_050.0
        real_lock.release()
        released = True
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        if not released and real_lock.locked():
            real_lock.release()

    assert seen.get("encode") == 0
    assert seen.get("vector")
    assert "deadline" in str(seen.get("vector")).lower()


def test_handle_search_skips_score_record_when_vector_deadline_degraded(tmp_path):
    engine = _engine(tmp_path)
    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 1,
            "expand": False,
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    results = resp["result"]["results"]
    assert results
    assert all("confidence_raw" not in row for row in results)
    with engine.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
    assert n == 0, "FTS-only deadline scores must not enter the hybrid calibration window"


def test_handle_search_records_scores_when_hybrid_completes(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 30_000,
            "expand": False,
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    assert resp["result"]["results"]
    with engine.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
    assert n >= 1


def test_handle_search_skips_score_record_when_rerank_deadline_degraded(
    tmp_path, monkeypatch
):
    """FAISS finished, CE skipped for deadline: RRF-without-CE must not feed
    the hybrid window. skip_score_record used to key only off last_vector_degraded
    containing "search deadline", so this path recorded kind=combined and the
    next search calibrated against a poisoned window.
    """
    import minni.minnid_runtime.recall as recall_mod
    import minni.retrieval as retrieval_mod
    from minni.scoring import record_score

    engine = _engine(tmp_path, reranker_enabled=True)
    seeded = (0.80, 0.82, 0.84, 0.86, 0.88)
    for raw in seeded:
        record_score(raw, "combined", engine.db)

    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])

    def fake_semantic(self, query, *args, **kwargs):
        clock["now"] = 1_010.0
        return []

    def boom_rerank(self, query, merged):
        raise AssertionError("cross-encoder must not run after the deadline")

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", boom_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))

    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 10_000,
            "expand": False,
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    results = resp["result"]["results"]
    assert results
    assert all("confidence_raw" not in row for row in results)
    assert engine.last_vector_degraded is None, (
        "FAISS must have finished; this is the CE-skip path, not FTS-only"
    )
    assert engine.last_rerank_degraded
    assert "deadline" in str(engine.last_rerank_degraded).lower()
    with engine.db.cursor() as c:
        rows = list(
            c.execute("SELECT raw_score FROM score_distribution ORDER BY id")
        )
    assert [row["raw_score"] for row in rows] == list(seeded), (
        "deadline-skipped rerank must not insert RRF-only scores into the "
        "hybrid calibration window"
    )


def test_expand_deadline_keeps_truncation_and_stamps_only_ran_variants(
    tmp_path, monkeypatch
):
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return ["sockets", "socket timeout", "rpc kill"]

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    orig_fts = RetrievalEngine._fts_search
    fts_calls = {"n": 0}

    def counting_fts(self, *args, **kwargs):
        fts_calls["n"] += 1
        rows = orig_fts(self, *args, **kwargs)
        if fts_calls["n"] >= 1:
            clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "_fts_search", counting_fts)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=True,
        deadline_monotonic=1_005.0,
        update_access=False,
    )
    assert rows
    assert fts_calls["n"] == 1
    assert engine.last_query_expand_degraded
    assert "truncated" in str(engine.last_query_expand_degraded).lower()
    assert rows[0]["query_variants"] == ["sockets"]


def test_expand_deadline_does_not_propagate_unused_later_variant_raise(
    tmp_path, monkeypatch
):
    """P1: deadline truncation must keep the first ranking if a later unused
    variant would raise. Eager pool.map started that child and aborted the
    retrieve; serial (and deadline-aware) never starts it.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])

    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return ["sockets", "later-raises"]

    orig_fts = RetrievalEngine._fts_search
    fts_calls = {"n": 0, "queries": []}

    def counting_fts(self, query, *args, **kwargs):
        fts_calls["n"] += 1
        fts_calls["queries"].append(query)
        if query == "later-raises":
            raise RuntimeError("unused later variant")
        rows = orig_fts(self, query, *args, **kwargs)
        clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(RetrievalEngine, "_fts_search", counting_fts)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=True,
        deadline_monotonic=1_005.0,
        update_access=False,
    )
    assert rows
    assert fts_calls["n"] == 1, fts_calls
    assert fts_calls["queries"] == ["sockets"]
    assert "truncated" in str(engine.last_query_expand_degraded).lower()


def test_deadline_fts_does_not_bump_access_count(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    before = _access_count(engine)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        update_access=True,
        deadline_monotonic=time.monotonic() - 1.0,
    )
    assert rows
    assert _access_count(engine) == before


def test_healthy_retrieve_still_bumps_access_count(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    before = _access_count(engine)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        update_access=True,
    )
    assert rows
    assert _access_count(engine) == before + 1


def _access_counts_by_path(engine) -> dict:
    with engine.db.cursor() as c:
        rows = c.execute("SELECT path, access_count FROM documents").fetchall()
    return {row["path"]: int(row["access_count"]) for row in rows}


def _assert_qty_once_on_winner(engine, before, *, winner="deadline-slice", skipped="banana"):
    after = _access_counts_by_path(engine)
    winner_paths = [path for path in after if winner in path]
    assert winner_paths, after
    for path in winner_paths:
        assert after[path] == before.get(path, 0) + 1, (path, before, after)
    for path in after:
        if skipped in path:
            assert after[path] == before.get(path, 0), (path, before, after)


def test_hyde_skips_cross_encoder_property_after_second_pass_deadline(
    tmp_path, monkeypatch
):
    """HyDE must not evaluate self.reranker after AFM/second-FAISS burns the budget.

    The first-pass CE path checks past_search_deadline before the lazy
    property. The HyDE branch used to do `reranker_enabled and self.reranker`
    first, which calls get_cross_encoder() on an already-dead client.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, hyde_enabled=True, reranker_enabled=True)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    loads = {"ce": 0, "allow": True, "hyde_ce": 0}

    def counting_reranker(self):
        loads["ce"] += 1
        if not loads["allow"]:
            # Do not raise: HyDE's except Exception would swallow it and stamp
            # last_hyde_degraded from the message, hiding the lazy load.
            loads["hyde_ce"] += 1
        return object()

    def fake_rerank(self, query, merged):
        return merged

    def fake_semantic(self, query, *args, **kwargs):
        if query != "sockets":
            raise AssertionError("HyDE second FAISS must not run after the deadline")
        return []

    def fake_afm(query, config=None, timeout=2.0, **kwargs):
        loads["allow"] = False
        return "Banana pudding caramel recipe for dessert night."

    orig_fts = RetrievalEngine._fts_search

    def expire_on_hyde_fts(self, query, *args, **kwargs):
        rows = orig_fts(self, query, *args, **kwargs)
        if "banana" in str(query).lower():
            clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "reranker", property(counting_reranker))
    monkeypatch.setattr(RetrievalEngine, "_rerank", fake_rerank)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_fts_search", expire_on_hyde_fts)
    monkeypatch.setattr("minni.hyde.should_trigger_hyde", lambda *a, **k: True)
    monkeypatch.setattr("minni.hyde.generate_hypothetical_answer", fake_afm)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        use_hyde=True,
        deadline_monotonic=1_005.0,
    )
    assert rows
    assert loads["ce"] >= 1, "first-pass CE may load before HyDE"
    assert loads["hyde_ce"] == 0
    assert engine.last_hyde_degraded
    assert "deadline" in str(engine.last_hyde_degraded).lower()
    assert "skipped hyde" in str(engine.last_hyde_degraded).lower()


def test_rerank_skips_predict_after_ce_lock_if_deadline_elapsed(tmp_path, monkeypatch):
    import minni.models as models
    import minni.retrieval as retrieval_mod
    import minni.rerank_cache as rerank_cache

    engine = _engine(tmp_path, reranker_enabled=True)
    clock = {"now": 1_000.0}
    predict_calls = {"n": 0}
    waiting = threading.Event()
    real_lock = models.get_cross_encoder_lock()

    class _DummyCE:
        def predict(self, pairs, show_progress_bar=False):
            predict_calls["n"] += 1
            return [0.1] * len(pairs)

    class _GateLock:
        def acquire(self, blocking=True, timeout=-1):
            waiting.set()
            return real_lock.acquire(blocking, timeout)

        def release(self):
            return real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

        def locked(self):
            return real_lock.locked()

    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: _DummyCE()))
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(models, "get_cross_encoder_lock", lambda: _GateLock())
    monkeypatch.setattr(rerank_cache, "GLOBAL_RERANK_CACHE", None)

    seen = {}
    released = False
    real_lock.acquire()
    try:
        def _run():
            engine.retrieve(
                "sockets",
                limit=5,
                budget_tokens=False,
                expand=False,
                deadline_monotonic=1_005.0,
            )
            seen["rerank"] = engine.last_rerank_degraded
            seen["predict"] = predict_calls["n"]

        thread = threading.Thread(target=_run)
        thread.start()
        assert waiting.wait(timeout=5), "retrieve never waited on the cross-encoder lock"
        clock["now"] = 1_010.0
        real_lock.release()
        released = True
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        if not released and real_lock.locked():
            real_lock.release()

    assert seen.get("predict") == 0
    assert seen.get("rerank")
    assert "deadline" in str(seen.get("rerank")).lower()


def test_hyde_rerank_skips_merge_after_ce_lock_if_deadline_elapsed(
    tmp_path, monkeypatch
):
    """HyDE mirror of test_rerank_skips_predict_after_ce_lock_if_deadline_elapsed.

    First-pass CE must finish; HyDE `_rerank` waits on the lock, deadline
    elapses, predict is skipped, and unscored HyDE hits must not merge.
    """
    import minni.models as models
    import minni.retrieval as retrieval_mod
    import minni.rerank_cache as rerank_cache

    engine = _engine(tmp_path, hyde_enabled=True, reranker_enabled=True)
    engine.index_durable_document(
        content="# Dessert\n\nBanana pudding caramel recipe for dessert night.\n",
        path="wiki/concepts/banana-pudding.md",
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="accepted",
        layer="knowledge",
    )
    clock = {"now": 1_000.0}
    predict_calls = {"n": 0}
    waiting = threading.Event()
    real_lock = models.get_cross_encoder_lock()

    class _DummyCE:
        def predict(self, pairs, show_progress_bar=False):
            predict_calls["n"] += 1
            return [0.1] * len(pairs)

    class _GateLock:
        def __init__(self):
            self.acquires = 0
            self._need_real_release = False

        def acquire(self, blocking=True, timeout=-1):
            self.acquires += 1
            if self.acquires == 1:
                return True
            waiting.set()
            ok = real_lock.acquire(blocking, timeout)
            self._need_real_release = bool(ok)
            return ok

        def release(self):
            if self._need_real_release:
                self._need_real_release = False
                return real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

        def locked(self):
            return real_lock.locked()

    gate = _GateLock()
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: _DummyCE()))
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(models, "get_cross_encoder_lock", lambda: gate)
    monkeypatch.setattr(rerank_cache, "GLOBAL_RERANK_CACHE", None)
    monkeypatch.setattr("minni.hyde.should_trigger_hyde", lambda *a, **k: True)
    monkeypatch.setattr(
        "minni.hyde.generate_hypothetical_answer",
        lambda *a, **k: "Banana pudding caramel recipe for dessert night.",
    )

    before = _access_counts_by_path(engine)
    seen = {}
    released = False
    real_lock.acquire()
    try:
        def _run():
            rows = engine.retrieve(
                "sockets",
                limit=5,
                budget_tokens=False,
                expand=False,
                use_hyde=True,
                update_access=True,
                deadline_monotonic=1_005.0,
            )
            seen["rows"] = rows
            seen["hyde"] = engine.last_hyde_degraded
            seen["rerank"] = engine.last_rerank_degraded
            seen["predict"] = predict_calls["n"]

        thread = threading.Thread(target=_run)
        thread.start()
        assert waiting.wait(timeout=5), "HyDE rerank never waited on the cross-encoder lock"
        clock["now"] = 1_010.0
        real_lock.release()
        released = True
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        if not released and real_lock.locked():
            real_lock.release()

    rows = seen.get("rows") or []
    assert rows
    assert seen.get("predict") == 1, "only first-pass CE may predict; HyDE CE must skip"
    paths = [str(r.get("path") or r.get("source") or "") for r in rows]
    assert any("deadline-slice" in p for p in paths)
    assert not any("banana" in p for p in paths)
    assert not any((r.get("provenance") or {}).get("via_hyde") for r in rows)
    assert seen.get("hyde")
    assert "deadline" in str(seen.get("hyde")).lower()
    assert "skipped hyde" in str(seen.get("hyde")).lower()
    assert seen.get("rerank") is None, (
        "first-pass CE completed; HyDE CE skip must restore last_rerank_degraded"
    )
    _assert_qty_once_on_winner(engine, before)


def test_hyde_deadline_skip_keeps_original_ranking_and_qty_deltas_hybrid(tmp_path, monkeypatch):
    """Deadline-skipped HyDE must not merge hypothetical-FTS, but first-pass hybrid still qty."""
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, hyde_enabled=True, reranker_enabled=False)
    engine.index_durable_document(
        content="# Dessert\n\nBanana pudding caramel recipe for dessert night.\n",
        path="wiki/concepts/banana-pudding.md",
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="accepted",
        layer="knowledge",
    )
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])

    def fake_afm(query, config=None, timeout=2.0, **kwargs):
        return "Banana pudding caramel recipe for dessert night."

    orig_fts = RetrievalEngine._fts_search

    def expire_on_hyde_fts(self, query, *args, **kwargs):
        rows = orig_fts(self, query, *args, **kwargs)
        if "banana" in str(query).lower():
            clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(RetrievalEngine, "_fts_search", expire_on_hyde_fts)
    monkeypatch.setattr("minni.hyde.should_trigger_hyde", lambda *a, **k: True)
    monkeypatch.setattr("minni.hyde.generate_hypothetical_answer", fake_afm)

    before = _access_counts_by_path(engine)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        use_hyde=True,
        update_access=True,
        deadline_monotonic=1_005.0,
    )
    assert rows
    paths = [str(r.get("path") or r.get("source") or "") for r in rows]
    assert any("deadline-slice" in p for p in paths)
    assert not any("banana" in p for p in paths)
    assert not any((r.get("provenance") or {}).get("via_hyde") for r in rows)
    assert engine.last_hyde_degraded
    assert "deadline" in str(engine.last_hyde_degraded).lower()
    assert engine.last_vector_degraded is None
    _assert_qty_once_on_winner(engine, before)


def test_handle_search_records_scores_when_hyde_deadline_skipped_after_hybrid(
    tmp_path, monkeypatch
):
    import minni.minnid_runtime.recall as recall_mod
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, hyde_enabled=True, reranker_enabled=False)
    engine.index_durable_document(
        content="# Dessert\n\nBanana pudding caramel recipe for dessert night.\n",
        path="wiki/concepts/banana-pudding.md",
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="accepted",
        layer="knowledge",
    )
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])

    def fake_afm(query, config=None, timeout=2.0, **kwargs):
        return "Banana pudding caramel recipe for dessert night."

    orig_fts = RetrievalEngine._fts_search

    def expire_on_hyde_fts(self, query, *args, **kwargs):
        rows = orig_fts(self, query, *args, **kwargs)
        if "banana" in str(query).lower():
            clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(RetrievalEngine, "_fts_search", expire_on_hyde_fts)
    monkeypatch.setattr("minni.hyde.should_trigger_hyde", lambda *a, **k: True)
    monkeypatch.setattr("minni.hyde.generate_hypothetical_answer", fake_afm)

    now = time.time()
    with engine.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'sockets JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid
    before = _access_counts_by_path(engine)

    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 10_000,
            "expand": False,
            "scope": "personal",
            "use_hyde": True,
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    assert resp["result"]["results"]
    assert engine.last_hyde_degraded
    assert "deadline" in str(engine.last_hyde_degraded).lower()
    with engine.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
    assert n >= 1, (
        "skipped HyDE enrichment must not withhold calibration from a completed hybrid fill"
    )
    # Whole-request budgeting preserves accounting for the completed document
    # ranking, but does not start a new learning search after expiry.
    assert int(access["access_count"] or 0) == 0
    assert any(d.get("stage") == "learnings" for d in resp["result"]["degradation"])
    _assert_qty_once_on_winner(engine, before)


def test_retrieve_skips_neighborhood_afm_when_deadline_past(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    calls = {"neighborhood": 0}

    def boom(self, *args, **kwargs):
        calls["neighborhood"] += 1
        raise AssertionError("neighborhood AFM must not run after the deadline")

    monkeypatch.setattr(RetrievalEngine, "_add_neighborhood_summaries", boom)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        summarize_neighborhood=True,
        deadline_monotonic=time.monotonic() - 1.0,
    )
    assert rows
    assert calls["neighborhood"] == 0


def test_expand_merge_skips_neighborhood_afm_when_deadline_elapsed(
    tmp_path, monkeypatch
):
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    calls = {"neighborhood": 0}

    def boom(self, *args, **kwargs):
        calls["neighborhood"] += 1
        raise AssertionError("expand-merge neighborhood AFM must not run after the deadline")

    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return ["sockets", "socket timeout"]

    orig_fts = RetrievalEngine._fts_search
    fts_calls = {"n": 0}

    def counting_fts(self, *args, **kwargs):
        fts_calls["n"] += 1
        rows = orig_fts(self, *args, **kwargs)
        if fts_calls["n"] >= 1:
            clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "_add_neighborhood_summaries", boom)
    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(RetrievalEngine, "_fts_search", counting_fts)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=True,
        summarize_neighborhood=True,
        deadline_monotonic=1_005.0,
        update_access=False,
    )
    assert rows
    assert calls["neighborhood"] == 0


def test_neighborhood_afm_stops_mid_loop_when_deadline_elapses(tmp_path, monkeypatch):
    """Mid-loop expire must stop remaining summarize_with_afm calls.

    Does not mock `_add_neighborhood_summaries` away — the helper itself
    re-checks the deadline before each AFM call.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    for name in ("alpha", "beta"):
        engine.index_durable_document(
            content=(
                f"# {name.title()}\n\n"
                f"neighdeadlinetoken {name} [[wiki/concepts/linked-{name}]].\n"
            ),
            path=f"wiki/concepts/{name}.md",
            agent="claude-code",
            sigil="📄",
            privacy_level="safe",
            page_status="accepted",
            layer="knowledge",
        )
        engine.index_durable_document(
            content=f"# Linked {name}\n\nNeighbor context for {name}.\n",
            path=f"wiki/concepts/linked-{name}.md",
            agent="claude-code",
            sigil="📄",
            privacy_level="safe",
            page_status="accepted",
            layer="knowledge",
        )

    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    calls = {"n": 0}

    def fake_summarize(prompt, timeout=1.5):
        calls["n"] += 1
        clock["now"] = 1_010.0
        return "Neighbor summary."

    monkeypatch.setattr(retrieval_mod, "summarize_with_afm", fake_summarize)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    principal = EffectivePrincipal(
        agent_id="claude-code", capabilities=["search", "read"]
    )

    rows = engine.retrieve(
        "neighdeadlinetoken",
        limit=5,
        budget_tokens=False,
        expand=False,
        summarize_neighborhood=True,
        principal=principal,
        deadline_monotonic=1_005.0,
        update_access=False,
    )
    sources = [str(r.get("path") or r.get("source") or "") for r in rows]
    linked_rows = [
        r
        for r, src in zip(rows, sources)
        if src.endswith("wiki/concepts/alpha.md")
        or src.endswith("wiki/concepts/beta.md")
    ]
    assert len(linked_rows) >= 2, (
        f"need two wikilinked hits so the loop can expire mid-way; got {sources!r}"
    )
    assert calls["n"] == 1, "deadline after first AFM must stop the rest of the loop"
    ok_summaries = [
        r
        for r in rows
        if (r.get("neighborhood_summary") or {}).get("status") == "ok"
    ]
    assert len(ok_summaries) == 1


def test_retrieve_skips_claim_nli_load_when_deadline_past(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    loads = {"nli": 0}

    def boom_load(*args, **kwargs):
        loads["nli"] += 1
        raise AssertionError("claim NLI must not load after the deadline")

    def boom_property(self):
        loads["nli"] += 1
        raise AssertionError("attribution_model must not be evaluated after the deadline")

    monkeypatch.setattr("minni.models.get_attribution_cross_encoder", boom_load)
    monkeypatch.setattr(RetrievalEngine, "attribution_model", property(boom_property))

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        claim="sockets cause JSON-RPC timeouts",
        deadline_monotonic=time.monotonic() - 1.0,
    )
    assert rows
    assert loads["nli"] == 0


def _index_banana_pudding(engine):
    engine.index_durable_document(
        content="# Dessert\n\nBanana pudding caramel recipe for dessert night.\n",
        path="wiki/concepts/banana-pudding.md",
        agent="claude-code",
        sigil="📄",
        privacy_level="safe",
        page_status="accepted",
        layer="knowledge",
    )


def _assert_no_hyde_wipe(rows):
    paths = [str(r.get("path") or r.get("source") or "") for r in rows]
    assert any("deadline-slice" in p for p in paths)
    assert not any("banana-pudding" in p for p in paths)
    assert not any((r.get("provenance") or {}).get("via_hyde") for r in rows)


def test_hyde_in_lock_ce_deadline_does_not_merge_and_restores_rerank(tmp_path, monkeypatch):
    """HyDE CE can abort after the lock while hyde_apply is still True.

    merge_hyde_results is keyed by doc_id; injecting HyDE-FTS docs would
    overwrite the first-pass ranking (banana-pudding wipe). First-pass CE
    completed, so qty still applies and last_rerank_degraded must be restored.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, hyde_enabled=True, reranker_enabled=True)
    _index_banana_pudding(engine)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    calls = {"rerank": 0}

    def fake_rerank(self, query, merged):
        calls["rerank"] += 1
        if calls["rerank"] > 1:
            self.last_rerank_degraded = "search deadline; skipped rerank"
        return merged

    def fake_afm(query, config=None, timeout=2.0, **kwargs):
        return "Banana pudding caramel recipe for dessert night."

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    monkeypatch.setattr(RetrievalEngine, "_rerank", fake_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))
    monkeypatch.setattr("minni.hyde.should_trigger_hyde", lambda *a, **k: True)
    monkeypatch.setattr("minni.hyde.generate_hypothetical_answer", fake_afm)

    before = _access_counts_by_path(engine)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        use_hyde=True,
        update_access=True,
        deadline_monotonic=1_005.0,
    )
    assert rows
    assert calls["rerank"] >= 2, "HyDE must reach in-lock CE so the skip is post-lock"
    _assert_no_hyde_wipe(rows)
    assert engine.last_hyde_degraded
    assert "deadline" in str(engine.last_hyde_degraded).lower()
    assert engine.last_rerank_degraded is None, (
        "HyDE CE skip must restore first-pass last_rerank_degraded"
    )
    _assert_qty_once_on_winner(engine, before)


def test_deadline_skipped_rerank_does_not_bump_access_count(tmp_path, monkeypatch):
    """FAISS finished, CE skipped for deadline: qty must not treat RRF as a fill.

    Production default is reranker_enabled=True and hyde_enabled=False.
    skip_score_record already keys off last_rerank_degraded; access_count
    UPDATE used to key only last_vector_degraded / last_hyde_degraded.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])

    def fake_semantic(self, query, *args, **kwargs):
        clock["now"] = 1_010.0
        return []

    def boom_rerank(self, query, merged):
        raise AssertionError("cross-encoder must not run after the deadline")

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", boom_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))

    before = _access_count(engine)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        update_access=True,
        deadline_monotonic=1_005.0,
    )
    assert rows
    assert engine.last_vector_degraded is None, (
        "FAISS must have finished; this is the CE-skip path, not FTS-only"
    )
    assert engine.last_rerank_degraded
    assert "deadline" in str(engine.last_rerank_degraded).lower()
    assert _access_count(engine) == before


def test_hyde_inner_semantic_deadline_does_not_merge(tmp_path, monkeypatch):
    """Deadline skip inside HyDE FAISS/encode must not merge HyDE-FTS docs.

    The pre-stage check can pass, then _semantic_search sets last_vector_degraded
    and returns [] while hyde_apply stays True.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, hyde_enabled=True, reranker_enabled=False)
    _index_banana_pudding(engine)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])

    def fake_semantic(self, query, *args, **kwargs):
        if query != "sockets":
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return []
        return []

    def fake_afm(query, config=None, timeout=2.0, **kwargs):
        return "Banana pudding caramel recipe for dessert night."

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr("minni.hyde.should_trigger_hyde", lambda *a, **k: True)
    monkeypatch.setattr("minni.hyde.generate_hypothetical_answer", fake_afm)

    before = _access_counts_by_path(engine)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        use_hyde=True,
        update_access=True,
        deadline_monotonic=1_005.0,
    )
    assert rows
    _assert_no_hyde_wipe(rows)
    assert engine.last_hyde_degraded
    assert "deadline" in str(engine.last_hyde_degraded).lower()
    assert engine.last_vector_degraded is None, (
        "HyDE inner FAISS skip must restore first-pass last_vector_degraded"
    )
    _assert_qty_once_on_winner(engine, before)


def test_handle_search_deadline_does_not_bump_learnings_access_count(tmp_path):
    """A deadline-aborted document cycle must not qty-delta learnings either."""
    engine = _engine(tmp_path)
    now = time.time()
    with engine.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'sockets JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid

    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 1,
            "expand": False,
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    assert resp["result"]["results"]
    with engine.db.cursor() as c:
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
        reads = c.execute(
            "SELECT COUNT(*) AS n FROM learning_reads WHERE learning_id = ?",
            (lid,),
        ).fetchone()["n"]
    assert int(access["access_count"] or 0) == 0
    assert reads == 0


def test_retrieve_skips_hf_load_when_remaining_just_over_one_second(
    tmp_path, monkeypatch
):
    """1.1s left is above the old 1s sliver floor but not enough for HF load/encode."""
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    loads = {"embedder": 0, "ce": 0}

    def boom_embedder(*args, **kwargs):
        loads["embedder"] += 1
        raise AssertionError("embedder/HF must not load with ~1.1s remaining")

    def boom_ce(*args, **kwargs):
        loads["ce"] += 1
        raise AssertionError("cross-encoder/HF must not load with ~1.1s remaining")

    monkeypatch.setattr("minni.models.get_embedder", boom_embedder)
    monkeypatch.setattr("minni.models.get_cross_encoder", boom_ce)
    monkeypatch.setattr(
        RetrievalEngine, "model", property(lambda self: boom_embedder())
    )
    monkeypatch.setattr(
        RetrievalEngine, "reranker", property(lambda self: boom_ce())
    )

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=1_001.1,
    )
    assert loads["embedder"] == 0
    assert loads["ce"] == 0
    assert rows, "FTS-only path must still return the indexed document"
    assert engine.last_vector_degraded or engine.last_rerank_degraded


def test_scope_both_bumps_access_count_once(tmp_path, monkeypatch):
    """scope=both re-runs the shared engine; qty is one fill per cycle."""
    engine = _engine(tmp_path)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    before = _access_count(engine)
    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 30_000,
            "expand": False,
            "scope": "both",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    assert resp["result"]["results"]
    assert _access_count(engine) == before + 1


def test_handle_search_mixed_legs_keeps_calibration_after_healthy_then_deadline(
    tmp_path, monkeypatch
):
    """A later deadline leg must not skip score/learnings qty after a hybrid fill."""
    import minni.minnid_runtime.recall as recall_mod
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    now = time.time()
    with engine.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'sockets JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid

    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])

    orig_retrieve = RetrievalEngine.retrieve
    calls = {"n": 0}

    def expire_after_first(self, *args, **kwargs):
        calls["n"] += 1
        rows = orig_retrieve(self, *args, **kwargs)
        clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "retrieve", expire_after_first)

    before = _access_count(engine)
    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 10_000,
            "expand": False,
            "scope": "both",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    assert resp["result"]["results"]
    assert calls["n"] >= 2, "scope=both must run more than one retrieve_from"
    with engine.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
    assert n >= 1, (
        "healthy first-pass hybrid scores must still enter the calibration window"
    )
    # Whole-request budgeting preserves accounting for the completed document
    # ranking, but does not start a new learning search after expiry.
    assert int(access["access_count"] or 0) == 0
    assert any(d.get("stage") == "learnings" for d in resp["result"]["degradation"])
    assert _access_count(engine) == before + 1


def test_handle_search_poisoned_only_ranking_does_not_qty_learnings_after_empty_personal_miss(
    tmp_path, monkeypatch
):
    """Empty personal miss is not a healthy fill. Poisoned-only merged ranking
    must fail-closed on learnings qty, calibration, and shared document qty.
    """
    shared = _engine(tmp_path / "shared")
    vault_cfg = SovereignConfig(
        db_path=str(tmp_path / "personal" / "test.db"),
        faiss_index_path=str(tmp_path / "personal" / "test.faiss"),
        vault_path=str(tmp_path / "personal" / "vault/"),
        writeback_path=str(tmp_path / "personal" / "learnings/"),
        graph_export_dir=str(tmp_path / "personal" / "graphs/"),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
        query_expand_default="off",
    )
    vault = RetrievalEngine(SovereignDB(vault_cfg), vault_cfg)

    now = time.time()
    with shared.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'sockets JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid

    monkeypatch.setattr(RetrievalEngine, "_semantic_search", lambda *a, **k: [])
    orig_retrieve = RetrievalEngine.retrieve
    calls = {"vault": 0, "shared": 0}

    def personal_miss_then_deadline_shared(self, *args, **kwargs):
        if self is vault:
            calls["vault"] += 1
            rows = orig_retrieve(self, *args, **kwargs)
            assert rows == []
            assert self.last_vector_degraded is None
            assert self.last_rerank_degraded is None
            return rows
        calls["shared"] += 1
        forced = dict(kwargs)
        forced["deadline_monotonic"] = time.monotonic() - 1.0
        return orig_retrieve(self, *args, **forced)

    monkeypatch.setattr(RetrievalEngine, "retrieve", personal_miss_then_deadline_shared)

    before_docs = _access_count(shared)
    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 30_000,
            "expand": False,
            "scope": "both",
        },
        1,
        _search_context(shared, vault_engine=vault),
    )
    assert "error" not in resp
    assert resp["result"]["results"], "shared FTS-only deadline hits must still surface"
    assert calls["vault"] >= 1, "personal vault retrieve_from must run"
    assert calls["shared"] >= 1, "combined/shared retrieve_from must run"
    with shared.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
        reads = c.execute(
            "SELECT COUNT(*) AS n FROM learning_reads WHERE learning_id = ?",
            (lid,),
        ).fetchone()["n"]
    assert n == 0, (
        "poisoned-only visible ranking must not enter the hybrid calibration window"
    )
    assert int(access["access_count"] or 0) == 0, (
        "empty personal miss must not fail-open learnings qty on poisoned-only ranking"
    )
    assert reads == 0
    assert _access_count(shared) == before_docs


def test_expand_in_flight_deadline_variant_does_not_merge_or_withhold_qty(
    tmp_path, monkeypatch
):
    """In-flight variant-2 FTS/CE-skip must not RRF-merge into first-pass hybrid.

    Existing truncation tests expire the clock on variant-1 FTS so variant 2
    never starts. This pin lets the second retrieve run, skip CE, and still
    drop its FTS-only banana-pudding hits before _merge_expanded_results.
    Qty/calibration follow the completed first-pass ranking, not a sticky
    any-variant last_vector_degraded / last_rerank_degraded join.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    _index_banana_pudding(engine)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])

    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return ["sockets", "banana pudding"]

    calls = {"semantic": [], "rerank": 0}

    def fake_semantic(self, query, *args, **kwargs):
        calls["semantic"].append(query)
        if query != "sockets":
            clock["now"] = 1_010.0
        return []

    def fake_rerank(self, query, merged):
        calls["rerank"] += 1
        if calls["rerank"] > 1:
            raise AssertionError("variant-2 CE must not run after the deadline")
        return merged

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", fake_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))

    before = _access_counts_by_path(engine)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=True,
        update_access=True,
        deadline_monotonic=1_005.0,
    )
    assert rows
    assert "banana pudding" in calls["semantic"], (
        "variant 2 must start (in-flight retrieve), not be skipped at the loop gate"
    )
    paths = [str(r.get("path") or r.get("source") or "") for r in rows]
    assert any("deadline-slice" in p for p in paths)
    assert not any("banana-pudding" in p for p in paths), (
        "deadline-poisoned expand variant must not RRF-merge by doc_id"
    )
    assert engine.last_query_expand_degraded
    assert "truncated" in str(engine.last_query_expand_degraded).lower()
    assert engine.last_vector_degraded is None, (
        "first-pass hybrid must not inherit variant-2 last_vector_degraded"
    )
    assert engine.last_rerank_degraded is None, (
        "first-pass CE completed; dropped variant must not sticky-poison rerank"
    )
    _assert_qty_once_on_winner(engine, before)


def test_handle_search_expand_deadline_variant_still_records_hybrid_qty(
    tmp_path, monkeypatch
):
    """Completed expand first-pass hybrid must still calibrate and qty-delta learnings."""
    import minni.minnid_runtime.recall as recall_mod
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    _index_banana_pudding(engine)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])

    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return ["sockets", "banana pudding"]

    def fake_semantic(self, query, *args, **kwargs):
        if query != "sockets":
            clock["now"] = 1_010.0
        return []

    def fake_rerank(self, query, merged):
        return merged

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", fake_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))

    now = time.time()
    with engine.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'sockets JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid
    before = _access_counts_by_path(engine)

    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 10_000,
            "expand": True,
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    results = resp["result"]["results"]
    assert results
    paths = [str(r.get("path") or r.get("source") or "") for r in results]
    assert any("deadline-slice" in p for p in paths)
    assert not any("banana-pudding" in p for p in paths)
    assert engine.last_vector_degraded is None
    assert engine.last_rerank_degraded is None
    with engine.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
    assert n >= 1, (
        "expand deadline variant must not withhold calibration from a completed hybrid fill"
    )
    # Whole-request budgeting preserves accounting for the completed document
    # ranking, but does not start a new learning search after expiry.
    assert int(access["access_count"] or 0) == 0
    assert any(d.get("stage") == "learnings" for d in resp["result"]["degradation"])
    _assert_qty_once_on_winner(engine, before)


def test_expand_in_flight_deadline_variant_keeps_degraded_fill_after_first_pass_miss(
    tmp_path, monkeypatch
):
    """Original-query miss must not drop in-flight variant-2 FTS/CE-skip hits.

    per_variant == [[]] is list-of-lists nonempty, so the expand drop used to
    discard banana-pudding FTS before _merge_expanded_results and return
    empty under default expand=True. Keep the degraded later fill.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    _index_banana_pudding(engine)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    miss_query = "qwerty-no-hit"

    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return [q, "banana pudding"]

    calls = {"semantic": []}

    def fake_semantic(self, query, *args, **kwargs):
        calls["semantic"].append(query)
        if query != miss_query:
            clock["now"] = 1_010.0
        return []

    def fake_rerank(self, query, merged):
        raise AssertionError("variant-2 CE must not run after the deadline")

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", fake_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))

    before = _access_counts_by_path(engine)
    rows = engine.retrieve(
        miss_query,
        limit=5,
        budget_tokens=False,
        expand=True,
        update_access=True,
        deadline_monotonic=1_005.0,
    )
    assert rows, "degraded variant-2 FTS fill must surface after an original-query miss"
    assert "banana pudding" in calls["semantic"], (
        "variant 2 must start (in-flight retrieve), not be skipped at the loop gate"
    )
    assert calls["semantic"][0] == miss_query
    paths = [str(r.get("path") or r.get("source") or "") for r in rows]
    assert any("banana-pudding" in p for p in paths)
    assert not any("deadline-slice" in p for p in paths)
    assert engine.last_rerank_degraded
    assert "deadline" in str(engine.last_rerank_degraded).lower()
    after = _access_counts_by_path(engine)
    assert after == before, (
        "deadline-poisoned later fill must not qty-delta access_count"
    )


def test_handle_search_expand_deadline_after_miss_returns_degraded_fill(
    tmp_path, monkeypatch
):
    """Default expand=True miss + in-flight variant-2 FTS must return hits, withhold qty."""
    import minni.minnid_runtime.recall as recall_mod
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    _index_banana_pudding(engine)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])
    miss_query = "qwerty-no-hit"

    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return [q, "banana pudding"]

    def fake_semantic(self, query, *args, **kwargs):
        if query != miss_query:
            clock["now"] = 1_010.0
        return []

    def fake_rerank(self, query, merged):
        raise AssertionError("variant-2 CE must not run after the deadline")

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", fake_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))

    now = time.time()
    with engine.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'qwerty-no-hit JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid
    before = _access_counts_by_path(engine)

    resp = handle_search(
        {
            "query": miss_query,
            "timeout_ms": 10_000,
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    results = resp["result"]["results"]
    assert results, "default expand=True must return the degraded later fill, not empty"
    paths = [str(r.get("path") or r.get("source") or "") for r in results]
    assert any("banana-pudding" in p for p in paths)
    assert not any("deadline-slice" in p for p in paths)
    assert engine.last_rerank_degraded
    assert "deadline" in str(engine.last_rerank_degraded).lower()
    with engine.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
        reads = c.execute(
            "SELECT COUNT(*) AS n FROM learning_reads WHERE learning_id = ?",
            (lid,),
        ).fetchone()["n"]
    assert n == 0, (
        "deadline-poisoned later fill must not enter the hybrid calibration window"
    )
    assert int(access["access_count"] or 0) == 0, (
        "deadline-poisoned later fill must not qty-delta learnings"
    )
    assert reads == 0
    assert _access_counts_by_path(engine) == before


def test_expand_deadline_between_variants_keeps_degraded_fill_after_original_miss(
    tmp_path, monkeypatch
):
    """Loop gate must not treat per_variant == [[]] as a completed ranking.

    The existing in-flight pin expires the clock *inside* variant-2 semantic,
    so the loop-gate hole stays green. Expire after the original-query child
    returns empty: variant 2 must still start, and cheap FTS after the
    deadline must be kept (post-child drop already keys off any(per_variant)).
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    _index_banana_pudding(engine)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    miss_query = "qwerty-no-hit"

    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return [q, "banana pudding"]

    def fake_semantic(self, query, *args, **kwargs):
        return []

    def boom_rerank(self, query, merged):
        raise AssertionError("CE must not run after the between-variant deadline")

    orig_retrieve = RetrievalEngine.retrieve
    child_queries = []

    def expire_after_original_miss(self, *args, **kwargs):
        query = args[0] if args else kwargs.get("query")
        expand = kwargs.get("expand", True)
        rows = orig_retrieve(self, *args, **kwargs)
        if expand is False:
            child_queries.append(query)
            if query == miss_query:
                clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", boom_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))
    monkeypatch.setattr(RetrievalEngine, "retrieve", expire_after_original_miss)

    before = _access_counts_by_path(engine)
    rows = engine.retrieve(
        miss_query,
        limit=5,
        budget_tokens=False,
        expand=True,
        update_access=True,
        deadline_monotonic=1_005.0,
    )
    assert "banana pudding" in child_queries, (
        "variant 2 must start after an original-query miss even when the "
        "deadline fires between variants"
    )
    assert child_queries[0] == miss_query
    assert rows, "degraded variant-2 FTS fill must surface after a between-variant deadline"
    paths = [str(r.get("path") or r.get("source") or "") for r in rows]
    assert any("banana-pudding" in p for p in paths)
    assert not any("deadline-slice" in p for p in paths)
    assert engine.last_vector_degraded or engine.last_rerank_degraded
    after = _access_counts_by_path(engine)
    assert after == before, (
        "deadline-poisoned later fill must not qty-delta access_count"
    )


def test_handle_search_expand_deadline_between_variants_returns_degraded_fill(
    tmp_path, monkeypatch
):
    """Default expand=True miss + between-variant deadline must return FTS fill."""
    import minni.minnid_runtime.recall as recall_mod
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    _index_banana_pudding(engine)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])
    miss_query = "qwerty-no-hit"

    def fake_variants(self, q, expand):
        if expand in (False, None, "off"):
            return [q]
        return [q, "banana pudding"]

    def fake_semantic(self, query, *args, **kwargs):
        return []

    def boom_rerank(self, query, merged):
        raise AssertionError("CE must not run after the between-variant deadline")

    orig_retrieve = RetrievalEngine.retrieve
    child_queries = []

    def expire_after_original_miss(self, *args, **kwargs):
        query = args[0] if args else kwargs.get("query")
        expand = kwargs.get("expand", True)
        rows = orig_retrieve(self, *args, **kwargs)
        if expand is False:
            child_queries.append(query)
            if query == miss_query:
                clock["now"] = 1_010.0
        return rows

    monkeypatch.setattr(RetrievalEngine, "_resolve_query_variants", fake_variants)
    monkeypatch.setattr(RetrievalEngine, "_semantic_search", fake_semantic)
    monkeypatch.setattr(RetrievalEngine, "_rerank", boom_rerank)
    monkeypatch.setattr(RetrievalEngine, "reranker", property(lambda self: object()))
    monkeypatch.setattr(RetrievalEngine, "retrieve", expire_after_original_miss)

    now = time.time()
    with engine.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'qwerty-no-hit JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid
    before = _access_counts_by_path(engine)

    resp = handle_search(
        {
            "query": miss_query,
            "timeout_ms": 10_000,
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    results = resp["result"]["results"]
    assert "banana pudding" in child_queries, (
        "variant 2 must start after an original-query miss even when the "
        "deadline fires between variants"
    )
    assert results, "default expand=True must return the degraded later fill, not empty"
    paths = [str(r.get("path") or r.get("source") or "") for r in results]
    assert any("banana-pudding" in p for p in paths)
    assert not any("deadline-slice" in p for p in paths)
    with engine.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM score_distribution").fetchone()["n"]
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
        reads = c.execute(
            "SELECT COUNT(*) AS n FROM learning_reads WHERE learning_id = ?",
            (lid,),
        ).fetchone()["n"]
    assert n == 0, (
        "deadline-poisoned later fill must not enter the hybrid calibration window"
    )
    assert int(access["access_count"] or 0) == 0, (
        "deadline-poisoned later fill must not qty-delta learnings"
    )
    assert reads == 0
    assert _access_counts_by_path(engine) == before


def test_handle_search_deadline_fts_miss_does_not_bump_learnings_access_count(
    tmp_path,
):
    """Empty deadline document ranking is an aborted search: learnings stay 0.

    _caller_visible_deadline_poisoned([]) is False, so skip_score_record used
    to fail-open and search_learnings still bumped access_count.
    """
    engine = _engine(tmp_path)
    now = time.time()
    with engine.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'qwerty-no-hit JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid

    resp = handle_search(
        {
            "query": "qwerty-no-hit",
            "timeout_ms": 1,
            "expand": False,
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    assert resp["result"]["results"] == []
    assert engine.last_vector_degraded or engine.last_rerank_degraded
    with engine.db.cursor() as c:
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
        reads = c.execute(
            "SELECT COUNT(*) AS n FROM learning_reads WHERE learning_id = ?",
            (lid,),
        ).fetchone()["n"]
    assert int(access["access_count"] or 0) == 0, (
        "deadline FTS miss must not qty-delta learnings access_count"
    )
    assert reads == 0


def test_handle_search_personal_vault_deadline_fts_miss_does_not_bump_learnings(
    tmp_path, monkeypatch
):
    """scope=personal vault empty deadline miss must not key skip off shared.

    retrieve_from(vault) returns [] after a deadline FTS miss and never
    touches shared, so skip_score_record used to stay False while
    search_learnings still qty-delta'd the shared learnings row.
    """
    shared = _engine(tmp_path / "shared")
    vault_cfg = SovereignConfig(
        db_path=str(tmp_path / "personal" / "test.db"),
        faiss_index_path=str(tmp_path / "personal" / "test.faiss"),
        vault_path=str(tmp_path / "personal" / "vault/"),
        writeback_path=str(tmp_path / "personal" / "learnings/"),
        graph_export_dir=str(tmp_path / "personal" / "graphs/"),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
        query_expand_default="off",
    )
    vault = RetrievalEngine(SovereignDB(vault_cfg), vault_cfg)

    now = time.time()
    with shared.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'qwerty-no-hit JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid

    orig_retrieve = RetrievalEngine.retrieve
    calls = {"vault": 0, "shared": 0}

    def expire_vault_only(self, *args, **kwargs):
        if self is vault:
            calls["vault"] += 1
            forced = dict(kwargs)
            forced["deadline_monotonic"] = time.monotonic() - 1.0
            return orig_retrieve(self, *args, **forced)
        calls["shared"] += 1
        return orig_retrieve(self, *args, **kwargs)

    monkeypatch.setattr(RetrievalEngine, "retrieve", expire_vault_only)

    resp = handle_search(
        {
            "query": "qwerty-no-hit",
            "timeout_ms": 30_000,
            "expand": False,
            "scope": "personal",
        },
        1,
        _search_context(shared, vault_engine=vault),
    )
    assert "error" not in resp
    assert resp["result"]["results"] == []
    assert calls["vault"] >= 1, "personal vault retrieve_from must run"
    assert calls["shared"] == 0, "personal vault miss must not fall through to shared"
    assert shared.last_vector_degraded is None
    assert shared.last_rerank_degraded is None
    assert vault.last_vector_degraded or vault.last_rerank_degraded
    with shared.db.cursor() as c:
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
        reads = c.execute(
            "SELECT COUNT(*) AS n FROM learning_reads WHERE learning_id = ?",
            (lid,),
        ).fetchone()["n"]
    assert int(access["access_count"] or 0) == 0, (
        "personal-vault deadline FTS miss must not qty-delta shared learnings"
    )
    assert reads == 0


def test_merge_document_results_does_not_replace_hybrid_with_poisoned_twin():
    """scope=both identity merge must not let a later FTS-only twin win.

    Negative CE logit vs RRF>0 used to replace a completed hybrid row with
    a deadline-poisoned later-scope copy of the same identity.
    """
    hybrid = {
        "doc_id": 1,
        "chunk_id": 10,
        "path": "wiki/concepts/deadline-slice.md",
        "score": -1.2,
        "src": "c",
    }
    poisoned = {
        "doc_id": 1,
        "chunk_id": 10,
        "path": "wiki/concepts/deadline-slice.md",
        "score": 0.4,
        "src": "c",
        _DEADLINE_POISONED_KEY: True,
    }
    merged = merge_document_results([[hybrid], [poisoned]], 5, prefer_personal=True)
    assert len(merged) == 1
    assert merged[0]["score"] == -1.2
    assert not merged[0].get(_DEADLINE_POISONED_KEY)


def test_merge_document_results_poisoned_fts_does_not_evict_hybrid_at_limit():
    """Unmatched deadline FTS (RRF>0) must not take every [:limit] slot.

    Negative CE logits sort below FTS RRF, so extra poisoned identities used
    to wipe a completed hybrid fill after identity-dedupe of twins only.
    """
    hybrid = [
        {
            "doc_id": i,
            "chunk_id": 10 + i,
            "path": f"wiki/concepts/hybrid-{i}.md",
            "score": -1.2,
            "src": "p",
        }
        for i in range(1, 6)
    ]
    poisoned = [
        {
            "doc_id": 100 + i,
            "chunk_id": 200 + i,
            "path": f"wiki/concepts/fts-{i}.md",
            "score": 0.4,
            "src": "c",
            _DEADLINE_POISONED_KEY: True,
        }
        for i in range(1, 6)
    ]
    merged = merge_document_results([hybrid, poisoned], 5, prefer_personal=True)
    assert len(merged) == 5
    assert {row["doc_id"] for row in merged} == {1, 2, 3, 4, 5}
    assert all(not row.get(_DEADLINE_POISONED_KEY) for row in merged)


def test_merge_document_results_poisoned_fts_does_not_evict_hybrid_combined():
    """retrieve_combined merges without prefer_personal; same eviction pin."""
    hybrid = [
        {
            "doc_id": i,
            "chunk_id": 10 + i,
            "path": f"wiki/concepts/hybrid-{i}.md",
            "score": -1.2,
            "src": "c",
        }
        for i in range(1, 6)
    ]
    poisoned = [
        {
            "doc_id": 100 + i,
            "chunk_id": 200 + i,
            "path": f"wiki/concepts/fts-{i}.md",
            "score": 0.4,
            "src": "c",
            _DEADLINE_POISONED_KEY: True,
        }
        for i in range(1, 6)
    ]
    merged = merge_document_results([hybrid, poisoned], 5)
    assert len(merged) == 5
    assert {row["doc_id"] for row in merged} == {1, 2, 3, 4, 5}
    assert all(not row.get(_DEADLINE_POISONED_KEY) for row in merged)


def test_merge_document_results_keeps_poisoned_fill_when_hybrid_missed():
    """A deadline-only later fill must survive an empty earlier ranking."""
    poisoned = {
        "doc_id": 9,
        "chunk_id": 90,
        "path": "wiki/concepts/fts-only.md",
        "score": 0.4,
        "src": "c",
        _DEADLINE_POISONED_KEY: True,
    }
    merged = merge_document_results([[], [poisoned]], 5)
    assert len(merged) == 1
    assert merged[0]["doc_id"] == 9
    assert merged[0].get(_DEADLINE_POISONED_KEY)


def test_semantic_search_skips_cold_faiss_rebuild_after_deadline(
    tmp_path, monkeypatch
):
    """Empty FAISS search must not start _ensure_faiss_loaded after the deadline."""
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        RetrievalEngine,
        "_encode_query",
        lambda self, query, deadline_monotonic=None: np.ones(384, dtype=np.float32),
    )
    calls = {"ensure": 0}

    def boom_ensure(self):
        calls["ensure"] += 1
        raise AssertionError("cold FAISS rebuild must not start after the deadline")

    def empty_search(*args, **kwargs):
        clock["now"] = 1_010.0
        return []

    monkeypatch.setattr(RetrievalEngine, "_ensure_faiss_loaded", boom_ensure)
    monkeypatch.setattr(engine.faiss_index, "search", empty_search)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=1_005.0,
    )
    assert rows, "FTS-only path must still return the indexed document"
    assert calls["ensure"] == 0
    assert engine.last_vector_degraded
    assert "deadline" in str(engine.last_vector_degraded).lower()


def test_semantic_search_skips_cold_faiss_rebuild_with_leftover_budget(
    tmp_path, monkeypatch
):
    """2.1s left passes SEARCH_STAGE_MIN_REMAINING_S=2.0 but cannot finish rebuild."""
    _assert_cold_faiss_rebuild_skipped(tmp_path, monkeypatch, leftover_s=2.1)


def test_semantic_search_skips_cold_faiss_rebuild_with_twenty_s_leftover(
    tmp_path, monkeypatch
):
    """~20s leftover is still in-budget for the 2s floor but cannot finish rebuild."""
    _assert_cold_faiss_rebuild_skipped(tmp_path, monkeypatch, leftover_s=20.0)


def test_semantic_search_skips_cold_faiss_rebuild_with_default_handle_search_leftover(
    tmp_path, monkeypatch
):
    """Default handle_search leftover is 22.5s (25s * 0.9). Disk restore must
    still run; a disk-miss rebuild must not start."""
    leftover_s = (DEFAULT_SEARCH_BUDGET_MS / 1000.0) * SEARCH_BUDGET_CLIENT_FRACTION
    assert leftover_s == 22.5
    _assert_cold_faiss_rebuild_skipped(tmp_path, monkeypatch, leftover_s=22.5)


def test_semantic_search_skips_cold_faiss_rebuild_with_recall_memory_leftover(
    tmp_path, monkeypatch
):
    """MCP/CLI omit timeoutMs so recallMemory leftover is 27s (30s * 0.9)."""
    leftover_s = 30.0 * SEARCH_BUDGET_CLIENT_FRACTION
    assert leftover_s == 27.0
    _assert_cold_faiss_rebuild_skipped(tmp_path, monkeypatch, leftover_s=27.0)


def test_semantic_search_disk_hit_warms_faiss_with_default_handle_search_leftover(
    tmp_path, monkeypatch
):
    """Default leftover 22.5s is <= the 27s rebuild floor; a disk hit must
    still make FAISS ready instead of skipping _ensure_faiss_loaded entirely."""
    leftover_s = (DEFAULT_SEARCH_BUDGET_MS / 1000.0) * SEARCH_BUDGET_CLIENT_FRACTION
    assert leftover_s == 22.5
    _assert_leftover_disk_hit_becomes_ready(tmp_path, monkeypatch, leftover_s=22.5)


def test_semantic_search_disk_hit_warms_faiss_with_recall_memory_leftover(
    tmp_path, monkeypatch
):
    leftover_s = 30.0 * SEARCH_BUDGET_CLIENT_FRACTION
    assert leftover_s == 27.0
    _assert_leftover_disk_hit_becomes_ready(tmp_path, monkeypatch, leftover_s=27.0)


def test_faiss_load_lock_timeout_is_surplus_above_rebuild_floor(monkeypatch):
    """Default leftover (22.5s / 27s) must not wait on a held rebuild lock.
    Remaining above SEARCH_FAISS_REBUILD_MIN_REMAINING_S may wait the surplus.
    Unbounded (no deadline) blocks."""
    import minni.retrieval as retrieval_mod

    fn = getattr(retrieval_mod, "faiss_load_lock_timeout", None)
    assert fn is not None, (
        "faiss_load_lock_timeout must bound acquire so leftover search "
        "does not wait out warmup/vault-watch rebuild"
    )
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    assert fn(None) is None
    assert fn(1_000.0 + 22.5) == 0.0
    assert fn(1_000.0 + 27.0) == 0.0
    surplus = fn(1_000.0 + SEARCH_FAISS_REBUILD_MIN_REMAINING_S + 0.1)
    assert surplus is not None and abs(surplus - 0.1) < 1e-9
    leftover_50 = fn(1_000.0 + 50.0)
    assert leftover_50 is not None
    assert abs(leftover_50 - (50.0 - SEARCH_FAISS_REBUILD_MIN_REMAINING_S)) < 1e-9


def test_invalidate_shared_faiss_reloads_unbounded(tmp_path, monkeypatch):
    """Vault-watch invalidate used to leave FAISS cold for 'the next search'.
    Default leftover is <=27s, so that next search now skips in-request
    rebuild. Reload off the RPC, even if this thread has a stale leftover
    deadline and disk misses."""
    import minni.minnid as minnid

    engine = _engine(tmp_path)
    _ensure_chunk_embeddings(engine)
    engine._ensure_faiss_loaded()
    assert engine.faiss_index.ready
    monkeypatch.setattr(
        engine.faiss_index, "try_load_from_disk", lambda db_conn=None: False
    )
    engine._set_current_deadline(time.monotonic() + 22.5)
    monkeypatch.setattr(minnid, "_retrieval", engine)
    minnid._invalidate_shared_faiss()
    assert engine.faiss_index.ready, (
        "vault-watch must unbounded-ensure FAISS after invalidate; default "
        "leftover never rebuilds in-request"
    )


def test_refresh_live_faiss_exception_reloads_unbounded(tmp_path, monkeypatch):
    """`_refresh_live_faiss` on exception invalidate()s so 'next search
    reloads from DB', but default leftover now skips that rebuild. Disk
    restore also misses because generation/checksum moved. Unbounded
    reload after invalidate, even if this thread holds a leftover
    deadline."""
    engine = _engine(tmp_path)
    _ensure_chunk_embeddings(engine)
    engine._ensure_faiss_loaded()
    assert engine.faiss_index.ready
    monkeypatch.setattr(
        engine.faiss_index, "try_load_from_disk", lambda db_conn=None: False
    )
    engine._set_current_deadline(time.monotonic() + 22.5)

    def boom_add(*args, **kwargs):
        raise RuntimeError("add_batch exploded")

    monkeypatch.setattr(engine.faiss_index, "add_batch", boom_add)
    vec = np.zeros(engine.config.embedding_dim, dtype=np.float32)
    vec[0] = 1.0
    engine._refresh_live_faiss([999], [vec])
    assert engine.faiss_index.ready, (
        "exception invalidate must unbounded-ensure FAISS; default leftover "
        "never rebuilds in-request"
    )


def _ensure_chunk_embeddings(engine: RetrievalEngine) -> None:
    """index_durable_document is fail-open on embedder miss; pin a vector."""
    cfg = engine.config
    with engine.db.cursor() as c:
        n = c.execute("SELECT COUNT(*) AS n FROM chunk_embeddings").fetchone()["n"]
        if n:
            return
        row = c.execute("SELECT doc_id FROM documents LIMIT 1").fetchone()
        assert row is not None, "index_durable_document must insert a document"
        vec = np.zeros(cfg.embedding_dim, dtype=np.float32)
        vec[0] = 1.0
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, model_name, computed_at)
               VALUES (?, 0, 'sockets JSON-RPC timeout', ?, 'test', 1.0)""",
            (row["doc_id"], vec.tobytes()),
        )


def _assert_leftover_disk_hit_becomes_ready(
    tmp_path, monkeypatch, leftover_s: float
) -> None:
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    _ensure_chunk_embeddings(engine)
    engine._ensure_faiss_loaded()
    assert engine.faiss_index.ready, "unbounded ensure must rebuild from DB"
    loaded = []
    original_load = engine.faiss_index.try_load_from_disk

    def spy_load(db_conn=None):
        result = original_load(db_conn=db_conn)
        loaded.append(result)
        return result

    engine.faiss_index.try_load_from_disk = spy_load

    def boom_stage(*args, **kwargs):
        raise AssertionError("leftover disk-hit must not start a FAISS rebuild")

    monkeypatch.setattr(engine.faiss_index, "stage_build", boom_stage)
    engine.faiss_index.invalidate()
    assert not engine.faiss_index.ready

    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        RetrievalEngine,
        "_encode_query",
        lambda self, query, deadline_monotonic=None: np.ones(384, dtype=np.float32),
    )
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=1_000.0 + leftover_s,
    )
    assert loaded == [True], f"expected disk-cache HIT, got {loaded}"
    assert engine.faiss_index.ready, (
        f"leftover {leftover_s}s disk-hit must make FAISS ready"
    )
    assert rows, "retrieve must still return the indexed document"


def _assert_cold_faiss_rebuild_skipped(tmp_path, monkeypatch, leftover_s: float) -> None:
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    assert not engine.faiss_index.ready, "index_durable_document leaves FAISS cold"
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        RetrievalEngine,
        "_encode_query",
        lambda self, query, deadline_monotonic=None: np.ones(384, dtype=np.float32),
    )
    rebuilds = {"stage": 0}

    def boom_stage(*args, **kwargs):
        rebuilds["stage"] += 1
        raise AssertionError(
            "cold FAISS rebuild must not start with leftover budget that cannot finish"
        )

    monkeypatch.setattr(engine.faiss_index, "stage_build", boom_stage)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=1_000.0 + leftover_s,
    )
    assert rows, "FTS-only path must still return the indexed document"
    assert rebuilds["stage"] == 0
    assert not engine.faiss_index.ready
    assert engine.last_vector_degraded
    assert "deadline" in str(engine.last_vector_degraded).lower()


def test_semantic_search_lock_wait_leftover_skip_sets_vector_degraded(
    tmp_path, monkeypatch
):
    """27.1s leftover is above SEARCH_STAGE_MIN_REMAINING_S so ensure runs.
    After lock-wait, leftover in (2, 27] and FAISS still cold must not look
    like a genuine miss.

    Boom-mocking _ensure_faiss_loaded hid this: the caller only re-checked
    past_search_deadline (2s floor) and returned [] with no last_vector_degraded.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    assert not engine.faiss_index.ready, "index_durable_document leaves FAISS cold"
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        RetrievalEngine,
        "_encode_query",
        lambda self, query, deadline_monotonic=None: np.ones(384, dtype=np.float32),
    )

    waiting = threading.Event()
    real_lock = engine._faiss_load_lock

    class _GateLock:
        def acquire(self, blocking=True, timeout=-1):
            waiting.set()
            return real_lock.acquire(blocking, timeout)

        def release(self):
            return real_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

        def locked(self):
            return real_lock.locked()

    engine._faiss_load_lock = _GateLock()
    seen = {}
    released = False
    real_lock.acquire()
    try:
        def _run():
            rows = engine.retrieve(
                "sockets",
                limit=5,
                budget_tokens=False,
                expand=False,
                deadline_monotonic=1_000.0 + 27.1,
            )
            seen["rows"] = rows
            seen["vector"] = engine.last_vector_degraded
            seen["ready"] = engine.faiss_index.ready

        thread = threading.Thread(target=_run)
        thread.start()
        assert waiting.wait(timeout=5), "retrieve never waited on the FAISS load lock"
        clock["now"] = 1_010.0
        real_lock.release()
        released = True
        thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        if not released and real_lock.locked():
            real_lock.release()

    assert seen.get("rows"), "FTS-only path must still return the indexed document"
    assert seen.get("vector"), (
        "still-cold leftover skip after _ensure_faiss_loaded must set "
        "last_vector_degraded"
    )
    assert "deadline" in str(seen.get("vector")).lower()
    assert seen.get("ready") is False


def test_semantic_search_skips_held_faiss_lock_without_waiting_out_rebuild(
    tmp_path, monkeypatch
):
    """Default leftover still took `_faiss_load_lock` blocking *before*
    `should_skip_faiss_rebuild`. Warmup/vault-watch hold that lock for the
    whole SELECT+build; a 25s/30s worker then outlives DEFAULT_JSON_RPC
    kill. Pin: lock held → leftover skip without waiting out the rebuild.
    """
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    assert not engine.faiss_index.ready, "index_durable_document leaves FAISS cold"
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        RetrievalEngine,
        "_encode_query",
        lambda self, query, deadline_monotonic=None: np.ones(384, dtype=np.float32),
    )
    rebuilds = {"stage": 0}

    def boom_stage(*args, **kwargs):
        rebuilds["stage"] += 1
        raise AssertionError(
            "held-lock leftover skip must not start a FAISS rebuild"
        )

    monkeypatch.setattr(engine.faiss_index, "stage_build", boom_stage)
    leftover_s = (DEFAULT_SEARCH_BUDGET_MS / 1000.0) * SEARCH_BUDGET_CLIENT_FRACTION
    assert leftover_s == 22.5

    seen = {}
    engine._faiss_load_lock.acquire()
    try:
        def _run():
            rows = engine.retrieve(
                "sockets",
                limit=5,
                budget_tokens=False,
                expand=False,
                deadline_monotonic=1_000.0 + leftover_s,
            )
            seen["rows"] = rows
            seen["vector"] = engine.last_vector_degraded
            seen["ready"] = engine.faiss_index.ready

        thread = threading.Thread(target=_run)
        thread.start()
        thread.join(timeout=2.0)
        hung = thread.is_alive()
    finally:
        if engine._faiss_load_lock.locked():
            engine._faiss_load_lock.release()
    if hung:
        thread.join(timeout=5)

    assert not hung, (
        "retrieve must not block on a held FAISS load lock for the duration "
        "of warmup/vault-watch rebuild"
    )
    assert seen.get("rows"), "FTS-only path must still return the indexed document"
    assert seen.get("vector"), (
        "held-lock leftover skip must set last_vector_degraded"
    )
    assert "deadline" in str(seen.get("vector")).lower()
    assert seen.get("ready") is False
    assert rebuilds["stage"] == 0


def test_handle_search_faiss_leftover_skip_keeps_hybrid_sibling_at_limit(
    tmp_path, monkeypatch
):
    """Inner leftover skip must stamp poison so mixed-scope merge keeps hybrid.

    27.1s leftover enters real _ensure_faiss_loaded; disk-miss clock drop to
    leftover 17.1s skips rebuild and leaves FAISS cold. Unstamped FTS RRF>0
    otherwise occupies [:limit] ahead of a completed hybrid (negative CE logit).
    """
    import minni.minnid_runtime.recall as recall_mod
    import minni.retrieval as retrieval_mod

    vault = _engine(tmp_path / "personal")
    for i in range(1, 5):
        vault.index_durable_document(
            content=f"# FTS filler {i}\n\nMore sockets JSON-RPC timeout notes {i}.\n",
            path=f"wiki/concepts/fts-filler-{i}.md",
            agent="claude-code",
            sigil="📄",
            privacy_level="safe",
            page_status="accepted",
            layer="knowledge",
        )
    assert not vault.faiss_index.ready

    shared = _engine(tmp_path / "shared")
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        RetrievalEngine,
        "_encode_query",
        lambda self, query, deadline_monotonic=None: np.ones(384, dtype=np.float32),
    )

    def miss_disk_and_drop_budget(*args, **kwargs):
        clock["now"] = 1_010.0
        return False

    monkeypatch.setattr(vault.faiss_index, "try_load_from_disk", miss_disk_and_drop_budget)

    hybrid = {
        "doc_id": 1,
        "chunk_id": 10,
        "path": "wiki/concepts/hybrid-fill.md",
        "source": "wiki/concepts/hybrid-fill.md",
        "score": -1.2,
        "text": "completed hybrid sockets fill",
    }
    orig_retrieve = RetrievalEngine.retrieve

    def dispatch_retrieve(self, *args, **kwargs):
        if self is shared:
            return [dict(hybrid)]
        forced = dict(kwargs)
        forced["deadline_monotonic"] = 1_000.0 + 27.1
        return orig_retrieve(self, *args, **forced)

    monkeypatch.setattr(RetrievalEngine, "retrieve", dispatch_retrieve)

    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 30_000,
            "expand": False,
            "scope": "both",
            "limit": 5,
        },
        1,
        _search_context(shared, vault_engine=vault),
    )
    assert "error" not in resp
    results = resp["result"]["results"]
    assert vault.last_vector_degraded
    assert "deadline" in str(vault.last_vector_degraded).lower()
    assert not vault.faiss_index.ready
    sources = [str(r.get("source") or r.get("path") or "") for r in results]
    assert any("hybrid-fill" in s for s in sources), (
        "healthy hybrid sibling must occupy [:limit]"
    )
    assert all("fts-filler" not in s and "deadline-slice" not in s for s in sources), (
        "poisoned leftover-skip FTS must not occupy [:limit] when a hybrid sibling exists"
    )


def test_retrieve_skips_hf_load_when_remaining_just_over_stage_floor(
    tmp_path, monkeypatch
):
    """2.1s left passes SEARCH_STAGE_MIN_REMAINING_S=2.0 but cannot finish HF load."""
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path, reranker_enabled=True, hyde_enabled=False)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    loads = {"embedder": 0, "ce": 0, "nli": 0}

    def boom_embedder(*args, **kwargs):
        loads["embedder"] += 1
        raise AssertionError("embedder/HF must not load with ~2.1s remaining")

    def boom_ce(*args, **kwargs):
        loads["ce"] += 1
        raise AssertionError("cross-encoder/HF must not load with ~2.1s remaining")

    def boom_nli(*args, **kwargs):
        loads["nli"] += 1
        raise AssertionError("attribution/HF must not load with ~2.1s remaining")

    monkeypatch.setattr("minni.models.get_embedder", boom_embedder)
    monkeypatch.setattr("minni.models.get_cross_encoder", boom_ce)
    monkeypatch.setattr("minni.models.get_attribution_cross_encoder", boom_nli)

    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        claim="sockets cause JSON-RPC timeouts",
        deadline_monotonic=1_002.1,
    )
    assert loads["embedder"] == 0
    assert loads["ce"] == 0
    assert loads["nli"] == 0
    assert rows, "FTS-only path must still return the indexed document"
    assert engine.last_vector_degraded or engine.last_rerank_degraded


def test_handle_sm_export_pack_passes_deadline_from_timeout_ms():
    engine = _RecordingEngine()
    context = _search_context(engine)
    before = time.monotonic()
    handle_sm_export_pack({"query": "q", "timeout_ms": 8000}, 1, context)
    after = time.monotonic()
    assert engine.retrieve_kwargs, "handle_sm_export_pack must call retrieve"
    deadline = engine.retrieve_kwargs[0].get("deadline_monotonic")
    assert deadline is not None
    # 90% of 8000ms = 7.2s of work budget
    assert before < deadline <= after + 7.2 + 0.5


def test_handle_sm_export_pack_default_budget_is_under_jsonrpc_30s():
    engine = _RecordingEngine()
    context = _search_context(engine)
    before = time.monotonic()
    handle_sm_export_pack({"query": "q"}, 1, context)
    deadline = engine.retrieve_kwargs[0].get("deadline_monotonic")
    assert deadline is not None
    remaining = deadline - before
    assert remaining < 30.0
    assert remaining > 5.0
    default_leftover = (DEFAULT_SEARCH_BUDGET_MS / 1000.0) * SEARCH_BUDGET_CLIENT_FRACTION
    assert remaining <= default_leftover + 0.5


def test_encode_skips_cold_embedder_load_with_default_leftover(
    tmp_path, monkeypatch
):
    """Default leftover (~22.5s) is above the 8s cold-load floor and still
    cannot finish HuggingFace/SentenceTransformer() before JSON-RPC kill."""
    import minni.retrieval as retrieval_mod

    engine = _engine(tmp_path)
    clock = {"now": 1_000.0}
    monkeypatch.setattr(retrieval_mod.time, "monotonic", lambda: clock["now"])
    loads = {"embedder": 0}

    def boom_embedder(*args, **kwargs):
        loads["embedder"] += 1
        raise AssertionError(
            "cold get_embedder must not start with default leftover budget"
        )

    monkeypatch.setattr("minni.models.get_embedder", boom_embedder)

    leftover_s = (DEFAULT_SEARCH_BUDGET_MS / 1000.0) * SEARCH_BUDGET_CLIENT_FRACTION
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=1_000.0 + leftover_s,
    )
    assert loads["embedder"] == 0
    assert rows, "FTS-only path must still return the indexed document"
    assert engine.last_vector_degraded
    assert "deadline" in str(engine.last_vector_degraded).lower()


def test_retrieve_chronological_deadline_does_not_bump_access_count(tmp_path):
    engine = _engine(tmp_path)
    before = _access_count(engine)
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        sort="chronological",
        update_access=True,
        deadline_monotonic=time.monotonic() - 1.0,
    )
    assert rows, "chronological SQL must still return the indexed document"
    assert engine.last_vector_degraded
    assert "deadline" in str(engine.last_vector_degraded).lower()
    assert _access_count(engine) == before


def test_handle_search_chronological_deadline_does_not_bump_qty(tmp_path):
    """timeout_ms=1 + sort=chronological must stamp poison and skip qty."""
    engine = _engine(tmp_path)
    now = time.time()
    with engine.db.cursor() as c:
        c.execute(
            """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
               VALUES (?, 'fix', 'sockets JSON-RPC timeout budget', 1.0, ?)""",
            ("grok-build", now),
        )
        lid = c.lastrowid
    before = _access_count(engine)

    resp = handle_search(
        {
            "query": "sockets",
            "timeout_ms": 1,
            "expand": False,
            "sort": "chronological",
            "scope": "personal",
        },
        1,
        _search_context(engine),
    )
    assert "error" not in resp
    assert resp["result"]["results"]
    assert engine.last_vector_degraded
    assert "deadline" in str(engine.last_vector_degraded).lower()
    assert _access_count(engine) == before
    with engine.db.cursor() as c:
        access = c.execute(
            "SELECT access_count FROM learnings WHERE learning_id = ?",
            (lid,),
        ).fetchone()
        reads = c.execute(
            "SELECT COUNT(*) AS n FROM learning_reads WHERE learning_id = ?",
            (lid,),
        ).fetchone()["n"]
    assert int(access["access_count"] or 0) == 0
    assert reads == 0


def test_search_deadline_shrinks_by_to_thread_queue_wait(monkeypatch):
    """10s queue wait on a 30s plugin kill must not grant a fresh 27s budget."""
    import minni.minnid_runtime.recall as recall_mod

    clock = {"now": 1_010.0}
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])
    deadline = _search_deadline_monotonic(
        {"timeout_ms": 30_000, "_accepted_monotonic": 1_000.0}
    )
    leftover = deadline - clock["now"]
    assert abs(leftover - 17.0) < 1e-9


def test_handle_search_queue_wait_does_not_grant_fresh_budget(monkeypatch):
    import minni.minnid_runtime.recall as recall_mod

    engine = _RecordingEngine()
    clock = {"now": 1_010.0}
    monkeypatch.setattr(recall_mod.time, "monotonic", lambda: clock["now"])
    handle_search(
        {
            "query": "q",
            "timeout_ms": 30_000,
            "_accepted_monotonic": 1_000.0,
        },
        1,
        _search_context(engine),
    )
    deadline = engine.retrieve_kwargs[0].get("deadline_monotonic")
    leftover = deadline - clock["now"]
    assert abs(leftover - 17.0) < 1e-9


def test_dispatch_stamps_accepted_monotonic_before_to_thread_dequeue(monkeypatch):
    """Deadline is computed at request accept, not after asyncio.to_thread."""
    import minni.minnid_runtime.dispatch as dispatch_mod
    from minni.minnid_runtime.dispatch import DispatchContext, dispatch_request
    from minni.minnid_runtime.rpc import make_error, make_response

    clock = {"now": 1_000.0}
    monkeypatch.setattr(dispatch_mod.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        "minni.minnid_runtime.recall.time.monotonic", lambda: clock["now"]
    )
    seen = {}

    def handler(params, request_id):
        seen["accepted"] = params.get("_accepted_monotonic")
        seen["deadline"] = _search_deadline_monotonic(params)
        return make_response({"ok": True}, request_id)

    async def delayed_to_thread(fn, *args, **kwargs):
        clock["now"] = 1_010.0
        return fn(*args, **kwargs)

    monkeypatch.setattr(dispatch_mod.asyncio, "to_thread", delayed_to_thread)

    class Obs:
        def incr(self, key):
            return None

    class Logger:
        def exception(self, *args, **kwargs):
            raise AssertionError("logger.exception should not be called")

    context = DispatchContext(
        methods={"search": handler},
        recovery_allowed_methods=frozenset(),
        resolve_provenance=lambda request: type(
            "Resolved",
            (),
            {"recovery": None, "principal": None},
        )(),
        enforce_method_capability=lambda method, principal, request_id: None,
        make_error=make_error,
        make_response=make_response,
        obs=Obs(),
        logger=Logger(),
    )
    asyncio.run(
        dispatch_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "search",
                "params": {
                    "query": "q",
                    "timeout_ms": 30_000,
                    "_accepted_monotonic": 9_999.0,
                },
            },
            context,
        )
    )
    assert seen.get("accepted") == 1_000.0
    leftover = seen["deadline"] - 1_010.0
    assert abs(leftover - 17.0) < 1e-9


def test_backfill_clear_rewarms_vault_engines_unbounded(tmp_path, monkeypatch):
    """Backfill cleared the per-vault cache with no ensure: the replacements
    were cold FAISSIndex, and default leftover never rebuilds in-request — so
    the next personal search degraded to FTS-only. The sweep must
    unbounded-ensure after the clear (mirroring the vault watch), so an
    immediate personal search is semantic, not FTS-only."""
    import minni.backfill as backfill_mod
    import minni.minnid as minnid

    engine = _engine(tmp_path)
    _ensure_chunk_embeddings(engine)
    engine.faiss_index.invalidate()
    assert not engine.faiss_index.ready
    engine._set_current_deadline(time.monotonic() + 22.5)
    monkeypatch.setattr(
        backfill_mod,
        "run_backfill_all_indexes",
        lambda config, on_vectors=None: {
            "vault1": {"documents": {"documents": 2}}
        },
    )
    monkeypatch.setattr(
        minnid, "_all_vault_retrievals", lambda: [(engine, "agent", "db")]
    )
    monkeypatch.setattr(
        RetrievalEngine,
        "_encode_query",
        lambda self, query, deadline_monotonic=None: np.ones(
            self.config.embedding_dim, dtype=np.float32
        ),
    )
    minnid._backfill_sweep_once()
    assert engine.faiss_index.ready, (
        "backfill must unbounded-ensure vault engines after the cache clear; "
        "default leftover never rebuilds a cold index in-request"
    )

    # Immediate personal search on a default leftover: semantic, no fallback.
    rows = engine.retrieve(
        "sockets",
        limit=5,
        budget_tokens=False,
        expand=False,
        deadline_monotonic=time.monotonic() + 22.5,
    )
    assert rows, "warmed vault engine must still return the indexed document"
    assert engine.last_vector_degraded is None, (
        "personal search right after backfill-clear must not fall back to "
        f"FTS-only (got {engine.last_vector_degraded!r})"
    )


def test_sighup_clear_rewarms_vault_engines_unbounded(tmp_path, monkeypatch):
    """_reload_runtime_config cleared the per-vault cache with no ensure —
    the same cold-strand class as backfill. Signal handlers are plain
    functions, so the real path is directly testable: after SIGHUP the vault
    engines must be warm despite a stale leftover deadline on this thread."""
    import minni.minnid as minnid

    engine = _engine(tmp_path)
    _ensure_chunk_embeddings(engine)
    engine.faiss_index.invalidate()
    assert not engine.faiss_index.ready
    engine._set_current_deadline(time.monotonic() + 22.5)
    monkeypatch.setattr(
        minnid, "_all_vault_retrievals", lambda: [(engine, "agent", "db")]
    )
    minnid._reload_runtime_config()
    assert engine.faiss_index.ready, (
        "SIGHUP must unbounded-ensure vault engines after the cache clear; "
        "default leftover never rebuilds a cold index in-request"
    )

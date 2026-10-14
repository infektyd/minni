"""R8 observability slice — every degrade path must be visible at the call site."""

import queue
import time

import pytest


def test_auth_suppression_reported_even_when_other_legs_returned_hits():
    """R3: `auth_suppression` was gated on `if not results` — a vault entirely"""
    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _Engine:
        config = None
        vector_model_down = False
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_auth_suppression = {"pre_gate": 4, "suppressed": 4}

        def retrieve(self, **kwargs):
            return [{"doc_id": 1, "path": "wiki/a.md"}]

    context = _make_context(_Engine(), captured)
    recall_mod.handle_search({"query": "anything"}, request_id=1, context=context)

    payload = captured["response"]
    assert payload["count"] >= 1, "precondition: this leg DID return hits"
    assert "auth_suppression" in payload, (
        "a fully-suppressed corpus must be reported even when the total result "
        "set is non-empty — otherwise the caller cannot tell 'this vault had "
        "nothing' from 'this vault was blacked out'"
    )
    assert payload["auth_suppression"][0]["suppressed"] == 4


def test_search_response_always_carries_vector_model_and_degraded():
    """R4(a): the response carried no `vector_model` or `degraded` field at"""
    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _Config:
        embedding_model = "all-MiniLM-L6-v2"

    class _Engine:
        config = _Config()
        vector_model_down = True  # semantic leg is DOWN
        last_vector_degraded = "embedding model unavailable; lexical (FTS) only"
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return [{"doc_id": 1, "path": "wiki/a.md"}]

    context = _make_context(_Engine(), captured)
    recall_mod.handle_search({"query": "anything"}, request_id=1, context=context)

    payload = captured["response"]
    assert payload["degraded"] is True, (
        "an FTS-only answer must not be presented as a healthy hybrid search"
    )
    entry = payload["degradation"][0]
    assert entry["vector_degraded"] is True
    assert entry["vector_model"] == "all-MiniLM-L6-v2"


def test_healthy_search_reports_degradation_field_as_false_not_absent():
    """The field is ALWAYS present, so 'absent' can never be misread as 'fine'."""
    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _Config:
        embedding_model = "all-MiniLM-L6-v2"

    class _Engine:
        config = _Config()
        vector_model_down = False
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return [{"doc_id": 1, "path": "wiki/a.md"}]

    context = _make_context(_Engine(), captured)
    recall_mod.handle_search({"query": "anything"}, request_id=1, context=context)

    payload = captured["response"]
    assert payload["degraded"] is False
    assert payload["degradation"][0]["vector_degraded"] is False


def test_vector_degradation_is_read_per_request_not_from_the_global():
    """Review round 2 (PR #260): one process-wide engine serves concurrent"""
    from minni.minnid_runtime import recall as recall_mod

    class _Config:
        embedding_model = "all-MiniLM-L6-v2"

    class _StaleGlobal:
        config = _Config()
        vector_model_down = True
        last_vector_degraded = None

    class _StaleClear:
        config = _Config()
        vector_model_down = False
        last_vector_degraded = "embedding model unavailable; lexical (FTS) only"

    healthy = recall_mod._degradation_for(_StaleGlobal(), "vault")
    assert healthy["vector_degraded"] is False
    assert healthy["degraded"] is False

    degraded = recall_mod._degradation_for(_StaleClear(), "vault")
    assert degraded["vector_degraded"] is True
    assert degraded["degraded"] is True


def test_encode_query_raises_the_degradation_flag_when_encoder_down(tmp_path, monkeypatch):
    """R4(b): the explicit-backend branches inlined"""
    engine = _engine_without_model(tmp_path, monkeypatch)

    assert engine.vector_model_down is False
    vector = engine._encode_query("some query")

    assert len(vector) == 0, "precondition: an empty vector is what got fed to the backend"
    assert engine.vector_model_down is True, (
        "the explicit-backend path must raise the SAME degradation flag as the "
        "default branch — otherwise lexical-only results are indistinguishable "
        "from a healthy hybrid search"
    )
    assert engine.last_vector_degraded is not None, (
        "the per-request verdict the response envelope reads must be raised too"
    )


class _LiveIndexBackend:
    """Stands in for a populated FAISS index: a real one raises a dimension"""

    name = "faiss-disk"

    def search(self, query_emb, k, filter=None):
        raise AssertionError("an empty query vector must never reach a live index")


def test_encoder_down_with_explicit_backend_degrades_instead_of_erroring(tmp_path, monkeypatch):
    """Review round 3 (PR #260): with the encoder down, the default `auto`"""
    engine = _engine_without_model(tmp_path, monkeypatch)

    results = engine._backend_search(engine._encode_query("some query"), 5, _LiveIndexBackend())
    assert results == [], "the semantic leg is down; the leg's contribution is empty"
    assert engine.last_vector_degraded is not None

    out = engine.retrieve("some query", limit=3, backend=_LiveIndexBackend(), expand=False)
    assert isinstance(out, list)
    assert engine.last_vector_degraded is not None, (
        "the response envelope must be able to report the lexical-only degrade"
    )


def test_explicit_backend_branches_use_the_flagging_encoder():
    """Regression pin on the call sites themselves: no branch may re-introduce"""
    import inspect
    from pathlib import Path

    import minni.retrieval as retrieval_mod
    from minni.retrieval import RetrievalEngine

    source = Path(retrieval_mod.__file__).read_text(encoding="utf-8")
    assert "self.model.encode(query).astype(np.float32) if self.model else" not in source, (
        "an explicit-backend branch is bypassing _encode_query and therefore "
        "the P0-B degradation flag"
    )
    sem_source = inspect.getsource(RetrievalEngine._semantic_search)
    assert "_encode_query" in sem_source, (
        "the default FAISS path must route through _encode_query too"
    )


def test_unknown_backend_value_is_invalid_params_not_internal_error():
    """R4(c): an unknown backend raised AttributeError on a str and surfaced as"""
    from minni.minnid_runtime.recall import resolve_backend

    with pytest.raises(ValueError) as excinfo:
        resolve_backend("faiss-disk-typo")

    message = str(excinfo.value)
    assert "faiss-disk" in message, "the error must name the valid values"


def test_documented_faiss_disk_backend_value_resolves_instead_of_crashing():
    """The documented `backend: 'faiss-disk'` form must reach the validated"""
    from minni.minnid_runtime.recall import resolve_backend

    assert resolve_backend("faiss-disk") == ["faiss-disk"]


def test_unknown_backend_in_list_form_is_invalid_params_not_internal_error():
    """Review round 4 (PR #260): R4(c) validated only the bare-string form."""
    from minni.minnid_runtime.recall import resolve_backend

    for bad in (["faiss-dsk"], ["faiss-disk", "nope"]):
        with pytest.raises(ValueError) as excinfo:
            resolve_backend(bad)
        message = str(excinfo.value)
        assert "faiss-disk" in message, "the error must name the valid values"

    with pytest.raises(ValueError):
        resolve_backend([])
    with pytest.raises(ValueError):
        resolve_backend(42)

    assert resolve_backend(["faiss-mem"]) == ["faiss-mem"]
    assert resolve_backend(["faiss-disk", "faiss-mem"]) == ["faiss-disk", "faiss-mem"]


def test_rerank_failure_is_recorded_not_swallowed(tmp_path, monkeypatch):
    """R5: a rerank failure left candidates with no rerank_score, dropping that"""
    engine = _engine_without_model(tmp_path, monkeypatch)

    class _ExplodingReranker:
        def predict(self, pairs, **kwargs):
            raise RuntimeError("reranker died")

    engine._reranker = _ExplodingReranker()
    engine.last_rerank_degraded = None
    candidates = [{"chunk_id": 1, "text": "a", "rrf_score": 0.1}]
    engine._rerank("query", candidates)

    assert engine.last_rerank_degraded is not None, (
        "a rerank failure silently evicts this corpus from a combined merge; "
        "it must be reported, not swallowed"
    )
    assert "reranker died" in engine.last_rerank_degraded


def test_probe_cache_is_bounded():
    """AFM-4: the cache had no eviction policy — measured live at 22 entries,"""
    from minni import afm_provider

    entries = {
        f"bridge|http://127.0.0.1:{port}/v1/chat/completions": {
            "reachable": True,
            "generation_verified": True,
            "detail": None,
            "probed_at": float(port),
        }
        for port in range(1000, 1000 + afm_provider.PROBE_CACHE_MAX_ENTRIES + 10)
    }
    afm_provider._evict_probe_entries(entries, "probed_at")

    assert len(entries) == afm_provider.PROBE_CACHE_MAX_ENTRIES
    survivors = sorted(entry["probed_at"] for entry in entries.values())
    assert survivors[0] > 1000.0, "eviction must drop the OLDEST probe, not an arbitrary one"


def test_probe_cache_write_is_serialized(tmp_path, monkeypatch):
    """AFM-4: the persistent cache was a read-modify-write with no mutual"""
    import threading

    from minni import afm_provider

    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", str(tmp_path / "probe-cache.json"))
    assert isinstance(afm_provider._probe_cache_lock, type(threading.RLock()))

    def _add(name):
        def _mutate(entries):
            entries[name] = {
                "reachable": True,
                "generation_verified": True,
                "detail": None,
                "probed_at_ms": 1.0,
            }

        return _mutate

    threads = [
        threading.Thread(target=afm_provider._persist_probe_mutation, args=(_add(f"bridge|u{i}"),))
        for i in range(16)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    persisted = afm_provider._read_persistent_probe_entries()
    assert len(persisted) == 16, "a concurrent probe must not lose another's entry"


def test_hyde_afm_unavailable_reaches_the_search_degradation_envelope(
    tmp_path, monkeypatch
):
    """AFM-6, driven end to end: a HyDE leg that TRIGGERED and then got nothing"""
    import minni.hyde as hyde_mod
    from minni.minnid_runtime import recall as recall_mod

    engine = _hyde_engine(tmp_path, monkeypatch)
    monkeypatch.setattr(hyde_mod, "generate_hypothetical_answer", lambda *a, **k: None)

    captured = {}
    context = _make_context(engine, captured)
    recall_mod.handle_search({"query": "aetherkernel"}, request_id=1, context=context)

    payload = captured.get("response") or {}
    assert "error" not in captured, f"HyDE degrade must not fail the search: {captured!r}"
    hyde_entries = [d for d in payload.get("degradation") or [] if d.get("hyde_degraded")]
    assert hyde_entries, (
        "a HyDE leg that triggered and produced nothing must be reported as "
        f"degraded on the response, got {payload.get('degradation')!r}"
    )
    assert "afm_unavailable" in str(hyde_entries[0]["hyde_degraded"])
    assert payload["degraded"] is True


def test_hyde_that_completes_is_not_reported_as_degraded(tmp_path, monkeypatch):
    """The other half of triggered-vs-completed: a leg that actually ran must"""
    import minni.hyde as hyde_mod
    from minni.minnid_runtime import recall as recall_mod

    engine = _hyde_engine(tmp_path, monkeypatch)
    calls = []

    def _answer(*args, **kwargs):
        calls.append(1)
        return "The aetherkernel note describes the xHCI window."

    monkeypatch.setattr(hyde_mod, "generate_hypothetical_answer", _answer)

    captured = {}
    context = _make_context(engine, captured)
    recall_mod.handle_search({"query": "aetherkernel"}, request_id=1, context=context)

    assert "error" not in captured, f"the search itself must succeed: {captured!r}"
    assert calls, "precondition: the HyDE leg must actually have been reached"
    payload = captured.get("response") or {}
    assert payload.get("degradation"), "precondition: the report must be present at all"
    hyde_entries = [d for d in payload["degradation"] if d.get("hyde_degraded")]
    assert not hyde_entries, (
        f"a completed HyDE leg must not be reported degraded: {hyde_entries!r}"
    )


def test_hyde_failure_surfaces_on_search_degradation_envelope():
    """Round 15: HyDE fail was trace-only; response looked healthy hybrid."""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    class _Engine:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_hyde_degraded = "afm_unavailable"
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return [{"doc_id": 1, "path": "wiki/a.md"}]

    captured = {}
    engine = _Engine()
    context = _make_context(engine, captured)
    recall_mod.handle_search(
        {"query": "anything"},
        request_id=1,
        context=context,
    )
    payload = captured.get("response") or {}
    assert payload.get("degraded") is True, (
        f"HyDE incomplete must mark response degraded, got {captured!r}"
    )
    hyde_entries = [
        d for d in payload.get("degradation") or [] if d.get("hyde_degraded")
    ]
    assert hyde_entries, f"expected hyde_degraded in degradation: {payload!r}"
    assert "afm_unavailable" in str(hyde_entries[0]["hyde_degraded"])


def test_query_expansion_failure_is_reachable_from_the_call_site(tmp_path, monkeypatch):
    """AFM-6: query-expansion degrade was a log line only, unreachable from the"""
    import minni.retrieval as retrieval_mod

    engine = _engine_without_model(tmp_path, monkeypatch)

    def _explode(query, mode="rule"):
        raise RuntimeError("expansion died")

    monkeypatch.setattr(retrieval_mod, "expand_query_with_status", _explode)
    monkeypatch.setattr(retrieval_mod, "expand_query", _explode)
    engine.last_query_expand_degraded = None
    variants = engine._resolve_query_variants("a query", True)

    assert variants == ["a query"], "precondition: it falls back to the bare query"
    assert engine.last_query_expand_degraded is not None, (
        "the caller must be able to tell an expanded search from a bare one"
    )


def test_writer_queue_is_bounded():
    """AFM-8: the queue was unbounded. Fails pre-R8: maxsize was 0 (infinite)."""
    from minni import afm_writer

    assert afm_writer._WORK_QUEUE.maxsize == afm_writer.WRITER_QUEUE_MAX
    assert afm_writer.WRITER_QUEUE_MAX > 0


def test_full_queue_rejects_loudly_and_counts_the_drop(monkeypatch):
    """AFM-8 drop policy: a submission past the bound is REJECTED and counted,"""
    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    full = queue.Queue(maxsize=1)
    full.put(("filler", None, None))
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", full)

    with pytest.raises(afm_writer.DraftQueueFull):
        afm_writer.submit_drafts({"pass_name": "p", "drafts": [{"title": "t"}]})

    assert afm_writer._WRITES_DROPPED == 1, "a dropped write must be countable"


def test_write_failure_is_its_own_error_type_not_a_bare_runtime_error(monkeypatch):
    """AFM-8: a write failure raised a bare RuntimeError, which the daemon's"""
    from minni import afm_writer

    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)

    def _fake_put(item):
        _job, done, out = item
        out["error"] = "vault is read-only"
        done.set()

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _fake_put)

    with pytest.raises(afm_writer.DraftWriteError):
        afm_writer.submit_drafts({"pass_name": "p", "drafts": []})


def test_daemon_compile_reports_write_failure_as_write_failed(monkeypatch, tmp_path):
    """AFM-8, end to end: a storage fault must not be reported as"""
    from minni import afm_writer

    captured = _compile_with_pass_error(
        monkeypatch, tmp_path, afm_writer.DraftWriteError("vault is read-only")
    )
    assert captured["status"] == "write_failed", (
        "a durable-storage problem must not be attributed to the model provider"
    )


def test_daemon_compile_still_reports_provider_faults_as_afm_unavailable(monkeypatch, tmp_path):
    """The counterpart: a genuine provider fault keeps its old status."""
    captured = _compile_with_pass_error(
        monkeypatch, tmp_path, RuntimeError("bridge unreachable")
    )
    assert captured["status"] == "afm_unavailable"


def test_writer_timeout_does_not_claim_the_drafts_landed(monkeypatch):
    """AFM-8: the timeout path returned status 'queued', so an unobserved batch"""
    from minni import afm_writer

    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", lambda item: None)

    result = afm_writer.submit_drafts(
        {"pass_name": "p", "drafts": [{"t": 1}, {"t": 2}]}, timeout=0.01
    )
    assert result["status"] == "write_timeout"
    assert result["drafts_written"] == []
    assert result["drafts_in_flight"] == 2


def test_pass_exception_records_attempt_and_failure(monkeypatch, tmp_path):
    """GA4-3: an exception skipped record_pass_attempt, so a pass raising every"""
    from minni import afm_writer

    afm_writer.reset_pass_counters()
    _compile_with_pass_error(monkeypatch, tmp_path, RuntimeError("pass exploded"))

    assert afm_writer._LAST_ATTEMPT_PER_PASS.get("consolidation"), (
        "a pass that ran and threw DID run; liveness must say so"
    )
    assert afm_writer._FAILURES_PER_PASS.get("consolidation") == 1, (
        "the fault needs its own counter — liveness alone reads as healthy"
    )


def test_failing_pass_reads_as_failing_not_ok():
    """GA4-3: the health verdict must distinguish a failing pass from an idle"""
    from minni.afm_writer import derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    state = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "failures_per_pass": {"consolidation": 7},
    }
    status, reasons = derive_loop_status(state, schedule=schedule, now=now)

    assert status == "failing"
    assert any("raising" in reason for reason in reasons)


def test_failing_clears_when_the_pass_recovers():
    """Review round 2 (PR #260): the failure counter is cumulative and nothing"""
    from minni.afm_writer import derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    failed_then_recovered = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "failures_per_pass": {"consolidation": 1},
        "last_failure_per_pass": {"consolidation": {"at": now - 600, "error": "blip"}},
    }
    status, reasons = derive_loop_status(failed_then_recovered, schedule=schedule, now=now)
    assert status == "ok", (
        "a pass whose LAST outcome succeeded is not failing — a one-way latch "
        "is an alarm operators learn to ignore"
    )
    assert not any("raising" in reason for reason in reasons)

    still_failing = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "failures_per_pass": {"consolidation": 3},
        "last_failure_per_pass": {"consolidation": {"at": now - 60, "error": "boom"}},
    }
    status, reasons = derive_loop_status(still_failing, schedule=schedule, now=now)
    assert status == "failing"
    assert any("3x" in reason for reason in reasons)


def test_healthy_pass_still_reads_ok():
    """The counterpart: no failures means the verdict is unchanged."""
    from minni.afm_writer import derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    state = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "failures_per_pass": {},
    }
    status, _ = derive_loop_status(state, schedule=schedule, now=now)
    assert status == "ok"


def test_queue_depth_is_consulted_by_the_status_derivation():
    """GA2-1: queue_depth was recorded in writer_status and never read by"""
    from minni.afm_writer import WRITER_QUEUE_BACKLOG, derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    state = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "queue_depth": WRITER_QUEUE_BACKLOG,
    }
    status, reasons = derive_loop_status(state, schedule=schedule, now=now)

    assert status == "backlogged"
    assert any("not draining" in reason for reason in reasons)


def test_dropped_writes_reach_the_status_verdict():
    """A rejected write is not a healthy loop."""
    from minni.afm_writer import derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    state = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "writes_dropped": 3,
    }
    status, reasons = derive_loop_status(state, schedule=schedule, now=now)

    assert status == "backlogged"
    assert any("REJECTED" in reason for reason in reasons)


def test_an_old_drop_does_not_latch_backlogged_forever():
    """Review round 2 (PR #260): writes_dropped is cumulative, so any drop —"""
    from minni.afm_writer import WRITES_DROPPED_RECENT_SECONDS, derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    old_drop = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "writes_dropped": 3,
        "last_drop_at": now - WRITES_DROPPED_RECENT_SECONDS - 60,
    }
    status, reasons = derive_loop_status(old_drop, schedule=schedule, now=now)
    assert status == "ok", "a drained writer with a months-old drop is not backlogged NOW"
    assert not any("REJECTED" in reason for reason in reasons)

    recent_drop = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "writes_dropped": 3,
        "last_drop_at": now - 60,
    }
    status, reasons = derive_loop_status(recent_drop, schedule=schedule, now=now)
    assert status == "backlogged"
    assert any("REJECTED" in reason for reason in reasons)
    assert any("lifetime" in reason for reason in reasons)


def test_write_timeout_is_counted_and_stamped(monkeypatch):
    """Review round 3 (PR #260): a slow-but-alive writer returns write_timeout"""
    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue.Queue(maxsize=4))

    res = afm_writer.submit_drafts(
        {"pass_name": "p", "drafts": [{"title": "t"}]}, timeout=0.01
    )
    assert res["status"] == "write_timeout", "precondition: nobody drains the queue"
    assert afm_writer._WRITE_TIMEOUTS == 1
    assert afm_writer._LAST_WRITE_TIMEOUT_AT is not None
    afm_writer.reset_pass_counters()


def test_resubmit_while_prior_job_in_flight_is_refused_not_duplicated(monkeypatch):
    """Round 5: a write_timeout response means the job is STILL queued and"""
    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue.Queue(maxsize=4))

    first = afm_writer.submit_drafts(
        {"pass_name": "p", "drafts": [{"title": "t"}]}, timeout=0.01
    )
    assert first["status"] == "write_timeout", "precondition: job queued, nobody draining"
    depth = afm_writer._WORK_QUEUE.qsize()

    second = afm_writer.submit_drafts(
        {"pass_name": "p", "drafts": [{"title": "t2"}]}, timeout=0.01
    )
    assert second["status"] == "write_in_flight"
    assert second["drafts_written"] == []
    assert afm_writer._WORK_QUEUE.qsize() == depth, (
        "the duplicate batch must be REFUSED, not enqueued behind the stuck one"
    )

    other = afm_writer.submit_drafts(
        {"pass_name": "q", "drafts": [{"title": "t3"}]}, timeout=0.01
    )
    assert other["status"] == "write_timeout"

    afm_writer._IN_FLIGHT_PER_PASS["p"].set()
    third = afm_writer.submit_drafts(
        {"pass_name": "p", "drafts": [{"title": "t4"}]}, timeout=0.01
    )
    assert third["status"] == "write_timeout", "a landed batch unblocks the pass"
    afm_writer.reset_pass_counters()


def _compile_consolidation_with_write_status(monkeypatch, tmp_path, write_result):
    """Drive handle_daemon_compile wet through a consolidation run that"""
    import minni.afm_passes.consolidation as consolidation_mod
    import minni.afm_writer as afm_writer
    import minni.db as db_mod
    import minni.minnid_runtime.afm as afm_mod
    from minni.config import MinniConfig
    from minni.principal import EffectivePrincipal

    def _run(db, config, **kwargs):
        return {
            "drafts": [{"page_id": "d1", "title": "t", "body": "b"}],
            "review_candidate_ids": [42],
        }

    monkeypatch.setattr(consolidation_mod, "run", _run)
    monkeypatch.setattr(afm_writer, "submit_drafts", lambda job, **kw: dict(write_result))

    applied = {"called": False}
    monkeypatch.setattr(
        afm_mod,
        "apply_consolidation_result",
        lambda result, context: applied.__setitem__("called", True),
    )

    captured = {}

    def _make_response(payload, request_id):
        if isinstance(payload, dict):
            captured.update(payload)
        return {"result": payload}

    cfg = MinniConfig(
        db_path=str(tmp_path / "afm.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )
    context = afm_mod.AFMContext(
        make_error=lambda code, message, request_id: {"error": {"code": code}},
        make_response=_make_response,
        guard_vault_root=lambda *a, **k: None,
        lazy_writeback=lambda: None,
        trace_ring=lambda: _NullRing(),
        record_latency=lambda name, elapsed: None,
        maybe_archive_inbox_source=lambda db, cid: None,
        sovereign_db=lambda config, *a, **k: _fresh_db(db_mod, cfg),
        default_config=cfg,
    )
    params = {
        "pass_name": "consolidation",
        "dry_run": False,
        "vault_path": str(tmp_path / "vault"),
        "_principal": EffectivePrincipal(
            agent_id="operator",
            workspace_id="default",
            transport="internal",
            capabilities=["govern"],
            allowed_vault_roots=[],
        ),
    }
    afm_mod.handle_daemon_compile(params, request_id=1, context=context)
    return captured, applied


def test_refused_drafts_do_not_apply_lifecycle_mutations(monkeypatch, tmp_path):
    """Round 8: write_in_flight returns WITHOUT raising and the drafts were"""
    captured, applied = _compile_consolidation_with_write_status(
        monkeypatch,
        tmp_path,
        {"status": "write_in_flight", "drafts_written": [], "drafts_deferred": 1},
    )
    assert captured["status"] == "write_in_flight", "precondition: the refusal surfaced"
    assert applied["called"] is False, (
        "lifecycle mutations must not be applied for a batch whose drafts "
        "were refused"
    )


def test_accepted_writes_still_apply_lifecycle_mutations(monkeypatch, tmp_path):
    """A successful write still applies lifecycle mutations."""
    _captured, applied = _compile_consolidation_with_write_status(
        monkeypatch,
        tmp_path,
        {"drafts_written": [{"page_id": "d1"}], "inbox_path": "inbox/x.json"},
    )
    assert applied["called"] is True


def test_write_timeout_does_not_apply_lifecycle_mutations(monkeypatch, tmp_path):
    """Round 9: write_timeout used to apply lifecycle optimistically. A late"""
    captured, applied = _compile_consolidation_with_write_status(
        monkeypatch,
        tmp_path,
        {"status": "write_timeout", "drafts_written": [], "drafts_in_flight": 1},
    )
    assert captured["status"] == "write_timeout"
    assert applied["called"] is False, (
        "lifecycle mutations must not run while drafts are only in-flight "
        "and unobserved"
    )


def test_lifecycle_recovered_does_not_apply_outer_lifecycle_mutations(
    monkeypatch, tmp_path
):
    """Round 16: lifecycle_recovered re-applies a PRIOR deferred decision set"""
    captured, applied = _compile_consolidation_with_write_status(
        monkeypatch,
        tmp_path,
        {
            "status": "lifecycle_recovered",
            "drafts_written": [],
            "drafts_deferred": 1,
            "lifecycle_recovered": True,
        },
    )
    assert captured["status"] == "lifecycle_recovered"
    assert applied["called"] is False, (
        "outer apply must not run on a NEW decision set when the writer only "
        "recovered a prior lifecycle and never wrote this batch's drafts"
    )


def test_a_late_write_failure_is_counted_even_with_no_waiter(monkeypatch):
    """Round 8: a batch failing AFTER its submitter timed out had no observer"""
    import threading

    from minni import afm_writer

    afm_writer.reset_pass_counters()

    def _explode(job):
        raise RuntimeError("vault vanished mid-write")

    monkeypatch.setattr(afm_writer, "_write_batch", _explode)
    done = threading.Event()
    out: dict = {}
    afm_writer._process_job({"pass_name": "p", "drafts": [{"title": "t"}]}, done, out)
    assert done.is_set(), "the Event must settle so in-flight state clears"
    assert "error" in out
    assert afm_writer._WRITE_FAILURES == 1
    assert afm_writer._LAST_WRITE_FAILURE_AT is not None
    assert afm_writer._UNRECOVERED_WRITE_FAILURES == 0, (
        "observed failures already have write_failed; unrecovered is for "
        "waiter-gone only"
    )

    done2 = threading.Event()
    out2: dict = {}
    afm_writer._process_job(
        {
            "pass_name": "p",
            "drafts": [{"title": "t2"}],
            "waiter_timed_out": True,
        },
        done2,
        out2,
    )
    assert afm_writer._WRITE_FAILURES == 2
    assert afm_writer._UNRECOVERED_WRITE_FAILURES == 1
    afm_writer.reset_pass_counters()


def test_recent_write_failures_reach_the_status_verdict():
    """The late failure must reach the verdict like timeouts do — and age out"""
    from minni.afm_writer import WRITE_FAILURES_RECENT_SECONDS, derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    recent = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "write_failures": 2,
        "last_write_failure_at": now - 60,
    }
    status, reasons = derive_loop_status(recent, schedule=schedule, now=now)
    assert status == "backlogged"
    assert any("FAILED in the writer" in reason for reason in reasons)

    old = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "write_failures": 2,
        "last_write_failure_at": now - WRITE_FAILURES_RECENT_SECONDS - 60,
    }
    status, reasons = derive_loop_status(old, schedule=schedule, now=now)
    assert status == "ok"


def test_unrecovered_write_failures_never_age_into_ok():
    """Round 9: recency-windowed write_failures alone aged into ok over a"""
    from minni.afm_writer import WRITE_FAILURES_RECENT_SECONDS, derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    state = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "write_failures": 2,
        "last_write_failure_at": now - WRITE_FAILURES_RECENT_SECONDS - 60,
        "unrecovered_write_failures": 2,
    }
    status, reasons = derive_loop_status(state, schedule=schedule, now=now)
    assert status == "backlogged"
    assert any("unrecovered write failure" in reason for reason in reasons)


def test_observed_write_failure_does_not_latch_unrecovered_backlogged(monkeypatch):
    """Round 13: observed DraftWriteError must not stamp unrecovered — a later"""
    import threading

    from minni import afm_writer
    from minni.afm_writer import WRITE_FAILURES_RECENT_SECONDS, derive_loop_status

    afm_writer.reset_pass_counters()

    def _explode(job):
        raise RuntimeError("transient vault blip")

    monkeypatch.setattr(afm_writer, "_write_batch", _explode)
    done = threading.Event()
    out: dict = {}
    afm_writer._process_job(
        {"pass_name": "consolidation", "drafts": [{"title": "t"}]}, done, out
    )
    assert afm_writer._WRITE_FAILURES == 1
    assert afm_writer._UNRECOVERED_WRITE_FAILURES == 0

    now = 1_000_000.0
    status, reasons = derive_loop_status(
        {
            "last_attempt_per_pass": {"consolidation": now - 60},
            "write_failures": afm_writer._WRITE_FAILURES,
            "last_write_failure_at": now - WRITE_FAILURES_RECENT_SECONDS - 60,
            "unrecovered_write_failures": afm_writer._UNRECOVERED_WRITE_FAILURES,
        },
        schedule={"passes": {"consolidation": {"interval_seconds": 3600}}},
        now=now,
    )
    assert status == "ok", f"observed failure must not latch unrecovered: {reasons}"
    afm_writer.reset_pass_counters()


def test_personal_vault_index_failure_is_in_degradation():
    """Round 13: personal leg exception was log-only; shared hits looked healthy."""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _Shared:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return [{"doc_id": 9, "path": "wiki/shared.md"}]

    class _PersonalBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError("personal index corrupt")

    shared = _Shared()
    personal = _PersonalBoom()
    context = _make_context(shared, captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context,
        "handler_principal",
        lambda params, request_id: (principal, None),
    )
    object.__setattr__(
        context,
        "agent_vault_retrieval",
        lambda agent_id: (personal, agent_id, "/tmp/p.db"),
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "personal"},
        request_id=1,
        context=context,
    )
    payload = captured["response"]
    assert payload["count"] >= 1, "shared fallback still returns hits"
    assert payload["degraded"] is True, (
        "personal index failure must not look like a healthy hybrid search"
    )
    personal_entries = [
        d for d in payload["degradation"] if d.get("src") == "p"
    ]
    assert personal_entries, f"expected personal degradation, got {payload['degradation']!r}"
    assert personal_entries[0].get("degraded") is True
    assert personal_entries[0].get("vector_model") == "m", (
        f"expected embedding model on personal degrade, got {personal_entries[0]!r}"
    )
    assert "personal index" in str(
        personal_entries[0].get("personal_index_failed")
        or personal_entries[0].get("reason")
        or ""
    ).lower() or "corrupt" in str(personal_entries[0]).lower()


def test_personal_and_shared_dual_failure_keeps_degradation():
    """Round 22 Medium: personal boom + hard shared fallback both raised →"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _SharedBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError("shared FTS locked")

    class _PersonalBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError("personal index corrupt")

    shared = _SharedBoom()
    personal = _PersonalBoom()
    context = _make_context(shared, captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context,
        "handler_principal",
        lambda params, request_id: (principal, None),
    )
    object.__setattr__(
        context,
        "agent_vault_retrieval",
        lambda agent_id: (personal, agent_id, "/tmp/p.db"),
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "personal"},
        request_id=1,
        context=context,
    )
    assert "error" not in captured, (
        f"dual-corpus failure must not −32000 (drops personal degrade): "
        f"{captured.get('error')!r}"
    )
    payload = captured["response"]
    assert payload["count"] == 0
    assert payload["degraded"] is True
    degs = payload.get("degradation") or []
    personal_entries = [
        d
        for d in degs
        if d.get("personal_index_failed")
        or (d.get("src") == "p" and d.get("degraded"))
    ]
    shared_entries = [
        d
        for d in degs
        if d.get("shared_index_failed")
        or "shared index" in str(d.get("reason") or "").lower()
    ]
    assert personal_entries, f"expected personal degrade entry, got {degs!r}"
    assert shared_entries, f"expected shared degrade entry, got {degs!r}"
    assert personal_entries[0].get("degraded") is True
    assert shared_entries[0].get("degraded") is True


def test_combined_vault_index_failure_returns_partial_and_degraded():
    """Round 16: one agent vault throw in retrieve_combined hard-failed the"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _Shared:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return [{"doc_id": 1, "path": "wiki/shared.md"}]

    class _HealthyVault:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return [{"doc_id": 2, "path": "wiki/agent-b.md"}]

    class _BoomVault:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError("agent-a index corrupt")

    shared = _Shared()
    boom = _BoomVault()
    healthy = _HealthyVault()
    context = _make_context(shared, captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context,
        "handler_principal",
        lambda params, request_id: (principal, None),
    )
    object.__setattr__(
        context,
        "all_vault_retrievals",
        lambda: [
            (boom, "agent-a", "/tmp/a.db"),
            (healthy, "agent-b", "/tmp/b.db"),
        ],
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "combined"},
        request_id=1,
        context=context,
    )
    assert "error" not in captured, (
        f"one vault throw must not hard-fail combined search: {captured.get('error')!r}"
    )
    payload = captured["response"]
    assert payload["count"] >= 1, "healthy vault + shared must still return hits"
    paths = {r.get("path") for r in payload.get("results") or []}
    assert "wiki/agent-b.md" in paths or "wiki/shared.md" in paths, (
        f"expected partial hits from non-failing engines, got {paths!r}"
    )
    assert payload["degraded"] is True, (
        "a combined leg failure must not look like a healthy hybrid search"
    )
    combined_entries = [
        d
        for d in payload.get("degradation") or []
        if d.get("combined_index_failed")
        or "combined vault" in str(d.get("reason") or "").lower()
        or d.get("source_agent") == "agent-a"
    ]
    assert combined_entries, (
        f"expected combined degradation entry, got {payload.get('degradation')!r}"
    )
    assert combined_entries[0].get("degraded") is True


def test_write_timeout_then_worker_success_applies_lifecycle_once(monkeypatch):
    """Round 10: timeout skips apply in compile, but a late successful write"""
    import threading

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    applied = []

    release = threading.Event()
    started = threading.Event()

    def _slow_write(job):
        started.set()
        release.wait(timeout=5)
        return {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"}

    monkeypatch.setattr(afm_writer, "_write_batch", _slow_write)
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    done = threading.Event()
    out: dict = {}
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "lifecycle": {
            "promote_candidate_ids": [1],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [2],
        },
        "lifecycle_handler": lambda life: applied.append(dict(life)),
    }

    def _worker():
        afm_writer._process_job(job, done, out)

    t = threading.Thread(target=_worker)
    t.start()
    assert started.wait(2), "worker must enter the write"
    job["defer_lifecycle_to_worker"] = True
    release.set()
    t.join(timeout=5)
    assert done.is_set()
    assert "result" in out
    assert len(applied) == 1, f"lifecycle must apply exactly once, got {applied!r}"
    assert applied[0]["promote_candidate_ids"] == [1]
    assert applied[0]["review_candidate_ids"] == [2]
    assert job.get("lifecycle_applied") is True
    afm_writer._maybe_apply_deferred_lifecycle(job)
    assert len(applied) == 1
    afm_writer.reset_pass_counters()


def test_write_finishes_before_defer_flag_still_applies_or_returns_result(monkeypatch):
    """Round 11: result present in out before done.is_set — must not count timeout."""
    from minni import afm_writer

    afm_writer.reset_pass_counters()
    applied = []
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t2"}],
        "lifecycle": {
            "promote_candidate_ids": [3],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [4],
        },
        "lifecycle_handler": lambda life: applied.append(dict(life)),
        "defer_lifecycle_to_worker": True,
    }
    afm_writer._maybe_apply_deferred_lifecycle(job)
    assert len(applied) == 1
    assert applied[0]["promote_candidate_ids"] == [3]
    afm_writer._maybe_apply_deferred_lifecycle(job)
    assert len(applied) == 1
    afm_writer.reset_pass_counters()


def test_deferred_lifecycle_holds_in_flight_until_handler_returns(monkeypatch):
    """Round 12: in-flight must not clear before deferred lifecycle finishes,"""
    import threading
    import queue as queue_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))

    handler_entered = threading.Event()
    handler_release = threading.Event()
    applied = []

    def _slow_handler(life):
        handler_entered.set()
        handler_release.wait(timeout=5)
        applied.append(dict(life))

    def _fast_write(job):
        return {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"}

    monkeypatch.setattr(afm_writer, "_write_batch", _fast_write)

    done = threading.Event()
    out: dict = {}
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "lifecycle": {
            "promote_candidate_ids": [1],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [],
        },
        "lifecycle_handler": _slow_handler,
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done

    def _worker():
        afm_writer._process_job(job, done, out)

    t = threading.Thread(target=_worker)
    t.start()
    assert handler_entered.wait(2), "lifecycle handler must start before done.set"
    assert not done.is_set(), (
        "in-flight Event must stay unset while deferred lifecycle runs"
    )
    second = afm_writer.submit_drafts(
        {"pass_name": "consolidation", "drafts": [{"title": "dup"}]},
        timeout=0.05,
    )
    assert second["status"] == "write_in_flight", (
        f"resubmit during deferred lifecycle must be refused, got {second!r}"
    )
    handler_release.set()
    t.join(timeout=5)
    assert done.is_set()
    assert len(applied) == 1
    afm_writer.reset_pass_counters()


def test_deferred_lifecycle_failure_refuses_resubmit_and_surfaces(monkeypatch):
    """Round 13: deferred apply raises after drafts land → sticky refuse + status."""
    import threading
    import queue as queue_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))
    writes = []

    def _write(job):
        writes.append(job.get("pass_name"))
        return {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"}

    monkeypatch.setattr(afm_writer, "_write_batch", _write)

    calls = {"n": 0, "applied": []}

    def _handler(life):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip during lifecycle")
        life["applied"] = {"promoted": 1, "deduped": 0, "reviewed": 0}
        calls["applied"].append(dict(life))

    done = threading.Event()
    out: dict = {}
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "lifecycle": {
            "promote_candidate_ids": [1],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [],
        },
        "lifecycle_handler": _handler,
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done

    afm_writer._process_job(job, done, out)
    assert not done.is_set(), "lifecycle failure must keep in-flight set"
    assert afm_writer._LIFECYCLE_APPLY_FAILURES == 1
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE
    assert writes == ["consolidation"]

    put_calls = []
    real_put = afm_writer._WORK_QUEUE.put_nowait

    def _spy_put(item):
        put_calls.append(item)
        return real_put(item)

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy_put)
    second = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "drafts": [{"title": "dup"}],
            "lifecycle_handler": _handler,
        },
        timeout=0.05,
    )
    assert second.get("status") == "lifecycle_recovered"
    assert second.get("lifecycle_recovered") is True
    assert "consolidation" not in afm_writer._PENDING_LIFECYCLE
    assert len(calls["applied"]) == 1
    assert calls["applied"][0]["promote_candidate_ids"] == [1]
    assert writes == ["consolidation"], f"unexpected writes: {writes}"
    assert put_calls == [], "re-apply must not enqueue a second draft job"
    assert done.is_set(), "re-apply must clear the sticky in-flight Event"

    third = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "drafts": [{"title": "after-reapply"}],
            "lifecycle_handler": _handler,
        },
        timeout=0.05,
    )
    assert third.get("lifecycle_pending") is not True
    assert third.get("status") != "write_in_flight", (
        f"third submit after re-apply must not be refused: {third!r}"
    )
    assert len(put_calls) == 1, f"third submit should enqueue once: {put_calls!r}"

    status, reasons = afm_writer.derive_loop_status(
        {
            "last_attempt_per_pass": {"consolidation": 1_000_000.0},
            "pending_lifecycle_passes": 1,
            "lifecycle_apply_failures": 1,
        },
        schedule={"passes": {"consolidation": {"interval_seconds": 3600}}},
        now=1_000_000.0,
    )
    assert status == "backlogged"
    assert any("deferred lifecycle" in r for r in reasons)
    afm_writer.reset_pass_counters()


def test_stale_sticky_noop_reapply_enqueues_wet_batch(monkeypatch):
    """Round 22 Medium: sticky re-apply that terminalizes nothing (applied"""
    import threading
    import queue as queue_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))

    def _write(job):
        return {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"}

    monkeypatch.setattr(afm_writer, "_write_batch", _write)

    calls = {"n": 0}

    def _handler(life):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip during lifecycle")
        life["applied"] = {"promoted": 0, "deduped": 0, "reviewed": 0}

    done = threading.Event()
    out: dict = {}
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "lifecycle": {
            "promote_candidate_ids": [1],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [],
        },
        "lifecycle_handler": _handler,
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done

    afm_writer._process_job(job, done, out)
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE

    put_calls = []
    real_put = afm_writer._WORK_QUEUE.put_nowait

    def _spy_put(item):
        put_calls.append(item)
        return real_put(item)

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy_put)
    second = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "drafts": [{"title": "new-decision-set"}],
            "lifecycle_handler": _handler,
        },
        timeout=0.05,
    )
    assert second.get("status") != "lifecycle_recovered", (
        f"stale sticky no-op must not discard wet batch: {second!r}"
    )
    assert second.get("lifecycle_recovered") is not True
    assert "consolidation" not in afm_writer._PENDING_LIFECYCLE
    assert len(put_calls) == 1, (
        f"stale sticky must enqueue wet batch once, got {put_calls!r}"
    )
    enqueued_job = put_calls[0][0]
    assert enqueued_job.get("drafts") == [{"title": "new-decision-set"}]
    afm_writer.reset_pass_counters()


def test_sticky_deferred_lifecycle_survives_process_restart_simulation(
    tmp_path, monkeypatch
):
    """Round 18: sticky pending was process-memory only. After drafts land and"""
    import threading
    import queue as queue_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))

    vault = tmp_path / "vault"
    vault.mkdir()
    writes = []

    def _write(job):
        writes.append(list((job.get("lifecycle") or {}).get("review_candidate_ids") or []))
        return {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"}

    monkeypatch.setattr(afm_writer, "_write_batch", _write)

    calls = {"n": 0, "applied": []}

    def _handler(life):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip during lifecycle")
        calls["applied"].append(dict(life))

    done = threading.Event()
    out: dict = {}
    job = {
        "pass_name": "consolidation",
        "vault_path": str(vault),
        "drafts": [{"title": "review-c1", "page_id": "consolidation-review-1-t1"}],
        "lifecycle": {
            "promote_candidate_ids": [10],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [1, 2],
        },
        "lifecycle_handler": _handler,
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done

    afm_writer._process_job(job, done, out)
    assert not done.is_set()
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE
    side = afm_writer._pending_lifecycle_path(vault)
    assert side.exists(), f"expected durable sticky file at {side}"
    on_disk = afm_writer._read_pending_lifecycle_file(vault)
    assert "consolidation" in on_disk
    assert on_disk["consolidation"]["review_candidate_ids"] == [1, 2]
    assert writes == [[1, 2]]

    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._PENDING_LIFECYCLE.clear()
        afm_writer._IN_FLIGHT_PER_PASS.clear()
    assert "consolidation" not in afm_writer._PENDING_LIFECYCLE

    put_calls = []
    real_put = afm_writer._WORK_QUEUE.put_nowait

    def _spy_put(item):
        put_calls.append(item)
        return real_put(item)

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy_put)

    second = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "vault_path": str(vault),
            "drafts": [
                {"title": "review-c1-dup", "page_id": "consolidation-review-1-t2"},
                {"title": "review-c2-dup", "page_id": "consolidation-review-2-t2"},
            ],
            "lifecycle_handler": _handler,
        },
        timeout=0.05,
    )
    assert second.get("status") == "lifecycle_recovered", second
    assert second.get("lifecycle_recovered") is True
    assert "consolidation" not in afm_writer._PENDING_LIFECYCLE
    assert len(calls["applied"]) == 1
    assert calls["applied"][0]["review_candidate_ids"] == [1, 2]
    assert calls["applied"][0]["promote_candidate_ids"] == [10]
    assert writes == [[1, 2]], f"must not write a second draft batch: {writes!r}"
    assert put_calls == [], "post-restart re-apply must not enqueue drafts"
    assert not side.exists(), "durable sticky must clear after successful re-apply"
    afm_writer.reset_pass_counters()


def test_sticky_persist_cannot_resurrect_after_concurrent_reapply(
    tmp_path, monkeypatch
):
    """Round 19: fail deferred apply → set memory under lock → persist was"""
    import threading
    import queue as queue_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))

    vault = tmp_path / "vault"
    vault.mkdir()
    side = afm_writer._pending_lifecycle_path(vault)

    entered_persist = threading.Event()
    persist_gate = threading.Event()
    real_persist = afm_writer._persist_pending_lifecycle

    def _gated_persist(pass_name, lifecycle, vault_path):
        entered_persist.set()
        assert persist_gate.wait(timeout=5.0), "persist gate timed out"
        return real_persist(pass_name, lifecycle, vault_path)

    monkeypatch.setattr(afm_writer, "_persist_pending_lifecycle", _gated_persist)

    def _write(job):
        return {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"}

    monkeypatch.setattr(afm_writer, "_write_batch", _write)

    calls = {"n": 0, "applied": []}

    def _handler(life):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db blip during lifecycle")
        calls["applied"].append(dict(life))

    done = threading.Event()
    out: dict = {}
    job = {
        "pass_name": "consolidation",
        "vault_path": str(vault),
        "drafts": [{"title": "t", "page_id": "c-1"}],
        "lifecycle": {
            "promote_candidate_ids": [1],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [9],
        },
        "lifecycle_handler": _handler,
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done

    worker_done = threading.Event()

    def _run_worker():
        try:
            afm_writer._process_job(job, done, out)
        finally:
            worker_done.set()

    t = threading.Thread(target=_run_worker, name="sticky-race-worker")
    t.start()
    assert entered_persist.wait(timeout=5.0), "worker never entered persist"
    reapply_result: dict = {}

    def _run_reapply():
        reapply_result["r"] = afm_writer.submit_drafts(
            {
                "pass_name": "consolidation",
                "vault_path": str(vault),
                "drafts": [{"title": "dup", "page_id": "c-2"}],
                "lifecycle_handler": _handler,
            },
            timeout=0.05,
        )

    re_t = threading.Thread(target=_run_reapply, name="sticky-race-reapply")
    re_t.start()
    time.sleep(0.05)
    persist_gate.set()
    t.join(timeout=5.0)
    re_t.join(timeout=5.0)
    assert worker_done.is_set()
    assert re_t.is_alive() is False

    assert reapply_result.get("r", {}).get("status") == "lifecycle_recovered", (
        reapply_result
    )
    assert "consolidation" not in afm_writer._PENDING_LIFECYCLE
    assert not side.exists(), (
        "sidecar must stay empty after re-apply; a late worker persist must "
        f"not resurrect sticky state (file={side}, exists={side.exists()})"
    )
    if side.exists():
        on_disk = afm_writer._read_pending_lifecycle_file(vault)
        assert "consolidation" not in on_disk

    put_calls = []
    real_put = afm_writer._WORK_QUEUE.put_nowait

    def _spy_put(item):
        put_calls.append(item)
        return real_put(item)

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy_put)
    follow = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "vault_path": str(vault),
            "drafts": [{"title": "after", "page_id": "c-3"}],
            "lifecycle_handler": _handler,
        },
        timeout=0.05,
    )
    assert follow.get("lifecycle_pending") is not True, follow
    assert follow.get("status") != "write_in_flight", follow
    assert follow.get("status") != "lifecycle_recovered", (
        f"phantom sticky re-held pass after clear: {follow!r}"
    )
    assert len(put_calls) == 1, f"following submit must enqueue: {put_calls!r}"
    afm_writer.reset_pass_counters()


def test_sticky_reapply_does_not_hold_inflight_lock_across_handler(
    tmp_path, monkeypatch
):
    """Round 20 High: submit_drafts re-applied sticky lifecycle while holding"""
    import threading
    import queue as queue_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))

    vault = tmp_path / "vault"
    vault.mkdir()

    entered_handler = threading.Event()
    release_handler = threading.Event()

    def _slow_handler(_life):
        entered_handler.set()
        assert release_handler.wait(timeout=5.0), "slow handler gate timed out"

    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._PENDING_LIFECYCLE["consolidation"] = {
            "promote_candidate_ids": [1],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [2],
            "_vault_path": str(vault),
        }

    put_calls = []
    real_put = afm_writer._WORK_QUEUE.put_nowait

    def _spy_put(item):
        put_calls.append(item)
        return real_put(item)

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy_put)

    reapply_result: dict = {}

    def _run_reapply():
        reapply_result["r"] = afm_writer.submit_drafts(
            {
                "pass_name": "consolidation",
                "vault_path": str(vault),
                "drafts": [{"title": "dup", "page_id": "c-dup"}],
                "lifecycle_handler": _slow_handler,
            },
            timeout=0.05,
        )

    re_t = threading.Thread(target=_run_reapply, name="slow-reapply")
    re_t.start()
    assert entered_handler.wait(timeout=2.0), "re-apply never entered handler"

    other = afm_writer.submit_drafts(
        {
            "pass_name": "synthesis",
            "vault_path": str(vault),
            "drafts": [{"title": "other", "page_id": "s-1"}],
        },
        wait=False,
    )
    assert other.get("status") == "queued", (
        f"other pass blocked on sticky re-apply lock: {other!r}"
    )
    assert any(
        (item[0].get("pass_name") if isinstance(item, tuple) else None) == "synthesis"
        or (
            isinstance(item, tuple)
            and isinstance(item[0], dict)
            and item[0].get("pass_name") == "synthesis"
        )
        for item in put_calls
    ), f"synthesis must enqueue while re-apply runs: {put_calls!r}"

    release_handler.set()
    re_t.join(timeout=5.0)
    assert re_t.is_alive() is False
    assert reapply_result.get("r", {}).get("status") == "lifecycle_recovered", (
        reapply_result
    )
    assert "consolidation" not in afm_writer._PENDING_LIFECYCLE
    assert "consolidation" not in afm_writer._REAPPLYING_LIFECYCLE
    afm_writer.reset_pass_counters()


def test_writer_status_hydrates_durable_sticky_after_restart(tmp_path, monkeypatch):
    """Round 20 Medium: durable sticky lived in the vault sidecar, but"""
    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)

    vault = tmp_path / "vault"
    vault.mkdir()
    life = {
        "promote_candidate_ids": [3],
        "dedup_candidate_ids": [],
        "review_candidate_ids": [7, 8],
        "_vault_path": str(vault),
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._PENDING_LIFECYCLE["consolidation"] = life
        afm_writer._persist_pending_lifecycle("consolidation", life, str(vault))
        afm_writer._PENDING_LIFECYCLE.clear()

    assert "consolidation" not in afm_writer._PENDING_LIFECYCLE
    assert afm_writer._pending_lifecycle_path(vault).exists()

    state = afm_writer.writer_status(
        vault_path=str(vault),
        schedule={"passes": {"consolidation": {"interval_seconds": 3600}}},
        now=1_000_000.0,
    )
    assert state.get("pending_lifecycle_passes", 0) >= 1, state
    assert "consolidation" in (state.get("pending_lifecycle_pass_names") or [])
    assert state.get("status") != "ok", state
    assert any(
        "deferred lifecycle" in r for r in (state.get("status_reasons") or [])
    ), state
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE
    afm_writer.reset_pass_counters()


def test_shared_index_failure_returns_partial_and_degraded():
    """Round 18: shared engine throw after agent-vault hits hard-failed the"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _SharedBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError("shared FTS locked")

    class _HealthyVault:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return [{"doc_id": 2, "path": "wiki/agent-b.md"}]

    shared = _SharedBoom()
    healthy = _HealthyVault()
    context = _make_context(shared, captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context,
        "handler_principal",
        lambda params, request_id: (principal, None),
    )
    object.__setattr__(
        context,
        "all_vault_retrievals",
        lambda: [(healthy, "agent-b", "/tmp/b.db")],
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "combined"},
        request_id=1,
        context=context,
    )
    assert "error" not in captured, (
        f"shared throw must not hard-fail combined search: {captured.get('error')!r}"
    )
    payload = captured["response"]
    assert payload["count"] >= 1, "agent vault hits must survive shared failure"
    paths = {r.get("path") for r in payload.get("results") or []}
    assert "wiki/agent-b.md" in paths, f"expected agent-b hits, got {paths!r}"
    assert payload["degraded"] is True
    shared_entries = [
        d
        for d in payload.get("degradation") or []
        if d.get("shared_index_failed")
        or "shared index" in str(d.get("reason") or "").lower()
    ]
    assert shared_entries, (
        f"expected shared degradation entry, got {payload.get('degradation')!r}"
    )
    assert shared_entries[0].get("degraded") is True


def test_both_scope_soft_fails_personal_shared_fallback():
    """Round 19: scope 'both' ran retrieve_personal() first; personal vault"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _SharedBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError("shared FTS locked")

    class _PersonalBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError("personal index corrupt")

    class _HealthyCombined:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return [{"doc_id": 7, "path": "wiki/agent-b.md"}]

    shared = _SharedBoom()
    personal = _PersonalBoom()
    healthy = _HealthyCombined()
    context = _make_context(shared, captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context,
        "handler_principal",
        lambda params, request_id: (principal, None),
    )
    object.__setattr__(
        context,
        "agent_vault_retrieval",
        lambda agent_id: (personal, agent_id, "/tmp/p.db"),
    )
    object.__setattr__(
        context,
        "all_vault_retrievals",
        lambda: [(healthy, "agent-b", "/tmp/b.db")],
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "both"},
        request_id=1,
        context=context,
    )
    assert "error" not in captured, (
        f"scope both must not −32000 when personal+shared boom but combined "
        f"has hits: {captured.get('error')!r}"
    )
    payload = captured["response"]
    assert payload["count"] >= 1, "combined agent hits must survive personal path boom"
    paths = {r.get("path") for r in payload.get("results") or []}
    assert "wiki/agent-b.md" in paths, f"expected agent-b hits, got {paths!r}"
    assert payload["degraded"] is True
    personal_entries = [
        d for d in payload.get("degradation") or [] if d.get("src") == "p"
    ]
    assert personal_entries, (
        f"expected personal degradation, got {payload.get('degradation')!r}"
    )


def test_both_scope_reports_one_shared_failure_once():
    """F4: scope 'both' runs the shared engine through two legs — the personal"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _SharedBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError("shared FTS locked")

    context = _make_context(_SharedBoom(), captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context,
        "handler_principal",
        lambda params, request_id: (principal, None),
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "both"},
        request_id=1,
        context=context,
    )
    assert "error" not in captured, (
        f"both-scope shared boom must stay a 200 with degradation: {captured.get('error')!r}"
    )
    payload = captured["response"]
    shared_entries = [
        d for d in payload.get("degradation") or [] if d.get("shared_index_failed")
    ]
    assert len(shared_entries) == 1, (
        "one shared-index failure must be reported exactly once, got "
        f"{payload.get('degradation')!r}"
    )
    assert payload["degraded"] is True


def test_a_shared_leg_that_degrades_only_on_the_second_pass_is_still_reported():
    """Review round 2: the first attempt at the F4 fix let the FIRST shared leg"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _HealthyThenBoom:
        """Leg one answers; leg two — the same engine, later in the request —"""

        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_hyde_degraded = None
        last_auth_suppression = None

        def __init__(self):
            self.calls = 0

        def retrieve(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return [{"doc_id": 1, "path": "wiki/a.md"}]
            raise RuntimeError("shared index vanished mid-request")

    shared = _HealthyThenBoom()
    context = _make_context(shared, captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context, "handler_principal", lambda params, request_id: (principal, None)
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "both"}, request_id=1, context=context
    )
    payload = captured["response"]
    assert shared.calls == 2, "precondition: scope both must reach the shared engine twice"
    assert payload["degraded"] is True, (
        "a shared index that threw on the second pass must not be reported as a "
        f"healthy hybrid recall: {payload['degradation']!r}"
    )
    assert [d for d in payload["degradation"] if d.get("shared_index_failed")], (
        f"the failure itself must be named: {payload['degradation']!r}"
    )


def test_one_shared_blackout_is_announced_to_the_agent_once():
    """Review round 2: `auth_suppression` had the identical double-report on"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _Blacked:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_hyde_degraded = None
        last_auth_suppression = {"pre_gate": 4, "suppressed": 4, "reason": "scope"}

        def retrieve(self, **kwargs):
            return []

    context = _make_context(_Blacked(), captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context, "handler_principal", lambda params, request_id: (principal, None)
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "both"}, request_id=1, context=context
    )
    payload = captured["response"]
    assert len(payload["auth_suppression"]) == 1, (
        "one blackout of one corpus is one report: "
        f"{payload['auth_suppression']!r}"
    )


def test_two_distinct_degraded_vaults_are_reported_separately():
    """The inverse of the F4 fix, and the reason it is scoped to the shared leg"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    def _vault():
        return SimpleNamespace(
            config=SimpleNamespace(embedding_model="m"),
            last_vector_degraded="embedding model unavailable",
            last_rerank_degraded=None,
            last_query_expand_degraded=None,
            last_hyde_degraded=None,
            last_auth_suppression=None,
            retrieve=lambda **kwargs: [{"doc_id": 1, "path": "wiki/a.md"}],
        )

    class _HealthyShared:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_hyde_degraded = None
        last_auth_suppression = None

        def retrieve(self, **kwargs):
            return []

    context = _make_context(_HealthyShared(), captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context, "handler_principal", lambda params, request_id: (principal, None)
    )
    object.__setattr__(
        context,
        "all_vault_retrievals",
        lambda: [(_vault(), "agent-b", "/tmp/b.db"), (_vault(), "agent-c", "/tmp/c.db")],
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "combined"},
        request_id=1,
        context=context,
    )
    payload = captured["response"]
    degraded_entries = [d for d in payload["degradation"] if d.get("vector_degraded")]
    assert len(degraded_entries) == 2, (
        "two degraded corpora must be reported as two, not collapsed into one: "
        f"{payload['degradation']!r}"
    )
    assert {d.get("source_agent") for d in degraded_entries} == {"agent-b", "agent-c"}, (
        "each vault's report must name the vault: " f"{degraded_entries!r}"
    )


def test_provider_leg_degrade_details_are_redacted_too():
    """Review round 1: `_degrade_detail` covered only the three index-failure"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    leak = "/Users/operator/.minni/index.db AFM call failed (api_key=sk-live-abc123)"
    captured = {}

    class _Engine:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_auth_suppression = None
        last_rerank_degraded = leak
        last_query_expand_degraded = leak
        last_hyde_degraded = leak

        def retrieve(self, **kwargs):
            return [{"doc_id": 1, "path": "wiki/a.md"}]

    context = _make_context(_Engine(), captured)
    recall_mod.handle_search({"query": "anything"}, request_id=1, context=context)

    entry = captured["response"]["degradation"][0]
    for key in ("rerank_degraded", "query_expand_degraded", "hyde_degraded"):
        assert "sk-live-abc123" not in str(entry[key]), f"{key} leaked a secret: {entry!r}"
        assert "/Users/operator" not in str(entry[key]), f"{key} leaked a path: {entry!r}"
        assert "[REDACTED]" in str(entry[key]) and "[REDACTED_PATH]" in str(entry[key])
    long_engine = _Engine()
    long_engine.last_rerank_degraded = "x" * 5000
    context = _make_context(long_engine, captured)
    recall_mod.handle_search({"query": "anything"}, request_id=1, context=context)
    assert len(captured["response"]["degradation"][0]["rerank_degraded"]) <= 400

    bool_engine = _Engine()
    bool_engine.last_rerank_degraded = True
    bool_engine.last_query_expand_degraded = None
    bool_engine.last_hyde_degraded = None
    context = _make_context(bool_engine, captured)
    recall_mod.handle_search({"query": "anything"}, request_id=1, context=context)
    assert captured["response"]["degradation"][0]["rerank_degraded"] is True, (
        "a flag with no detail must stay a bool, not become the string 'True'"
    )


def test_degrade_details_are_redacted_before_they_leave_the_daemon():
    """F5: the three index-failure details embedded raw `str(exc)`, bypassing"""
    from types import SimpleNamespace

    from minni.minnid_runtime import recall as recall_mod

    leak = "/Users/operator/.minni/index.db locked (api_key=sk-live-abc123)"
    captured = {}

    class _PersonalBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError(leak)

    class _SharedBoom:
        config = SimpleNamespace(embedding_model="m")

        def retrieve(self, **kwargs):
            raise RuntimeError(leak)

    context = _make_context(_SharedBoom(), captured)
    principal = SimpleNamespace(agent_id="agent-a", workspace_id="default")
    object.__setattr__(
        context,
        "handler_principal",
        lambda params, request_id: (principal, None),
    )
    object.__setattr__(
        context,
        "agent_vault_retrieval",
        lambda agent_id: (_PersonalBoom(), agent_id, "/tmp/p.db"),
    )
    object.__setattr__(
        context,
        "all_vault_retrievals",
        lambda: [(_PersonalBoom(), "agent-b", "/tmp/b.db")],
    )
    recall_mod.handle_search(
        {"query": "anything", "scope": "both"},
        request_id=1,
        context=context,
    )
    payload = captured["response"]
    rendered = repr(payload.get("degradation"))
    assert "sk-live-abc123" not in rendered, f"secret survived on the wire: {rendered}"
    assert "/Users/operator" not in rendered, f"operator path survived on the wire: {rendered}"
    assert "[REDACTED]" in rendered and "[REDACTED_PATH]" in rendered, (
        f"the detail must still say WHAT failed, redacted: {rendered}"
    )
    failures = [
        d
        for d in payload.get("degradation") or []
        if d.get("personal_index_failed")
        or d.get("shared_index_failed")
        or d.get("combined_index_failed")
    ]
    assert len(failures) >= 3, f"expected all three legs to have failed: {failures!r}"


def test_encode_query_encode_raise_keeps_vector_down_and_empty_vector(
    tmp_path, monkeypatch
):
    """Round 18: encode() raise after optimistic vector_model_down=False left"""
    import minni.models as models_mod
    import minni.retrieval as retrieval_mod

    class _BoomModel:
        def encode(self, query, **kwargs):
            raise RuntimeError("OOM during encode")

    boom = _BoomModel()
    monkeypatch.setattr(models_mod, "get_embedder", lambda: boom)
    if hasattr(retrieval_mod, "get_embedder"):
        monkeypatch.setattr(retrieval_mod, "get_embedder", lambda: boom)

    engine = _engine_without_model(tmp_path, monkeypatch)
    monkeypatch.setattr(models_mod, "get_embedder", lambda: boom)
    if hasattr(retrieval_mod, "get_embedder"):
        monkeypatch.setattr(retrieval_mod, "get_embedder", lambda: boom)
    engine.vector_model_down = False
    import numpy as np

    assert engine.model is boom, "precondition: encode path must see the boom model"
    vec = engine._encode_query("hello")
    assert isinstance(vec, np.ndarray)
    assert vec.size == 0, "encode failure must return empty vector (FTS fallback)"
    assert engine.vector_model_down is True, (
        "process-wide down flag must stay set after encode throw"
    )
    assert engine.last_vector_degraded, "per-request vector degrade must be set"
    assert "encode" in str(engine.last_vector_degraded).lower() or "oom" in str(
        engine.last_vector_degraded
    ).lower()


def test_timeout_counter_not_bumped_when_result_present_under_lock(monkeypatch):
    """Round 12: if out already has a result when the wait times out, do not"""
    import threading
    import queue as queue_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))

    hold = threading.Event()

    def _write_publish_then_hold(job):
        return {"drafts_written": [{"page_id": "late"}], "inbox_path": "x"}

    monkeypatch.setattr(afm_writer, "_write_batch", _write_publish_then_hold)

    original_put = afm_writer._WORK_QUEUE.put_nowait

    def _put_and_prefill(item):
        job, done, out = item
        out["result"] = {"drafts_written": [{"page_id": "prefilled"}]}
        original_put(item)

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _put_and_prefill)

    res = afm_writer.submit_drafts(
        {
            "pass_name": "p-late",
            "drafts": [{"title": "t"}],
            "lifecycle": {"promote_candidate_ids": [1]},
        },
        timeout=0.05,
    )
    assert "drafts_written" in res, f"expected success result, got {res!r}"
    assert afm_writer._WRITE_TIMEOUTS == 0, (
        "a result present under the lock must not count as write_timeout"
    )
    afm_writer.reset_pass_counters()


def test_probe_cache_file_lock_is_shared_protocol():
    """Round 10: Python must use the same lock sibling path as afm.ts."""
    from minni import afm_provider

    path = afm_provider._probe_cache_file_path()
    assert afm_provider._probe_cache_lock_path() == path + ".lock"
    assert afm_provider.PROBE_CACHE_LOCK_STALE_SECONDS == 10.0


def test_stale_probe_cache_lock_is_reclaimed(tmp_path, monkeypatch):
    """Round 10: an orphan lockfile must not wedge every later write unlocked."""
    import os
    import time as time_mod

    from minni import afm_provider

    cache = tmp_path / "afm-probe-cache.json"
    lock = tmp_path / "afm-probe-cache.json.lock"
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", str(cache))
    lock.write_text("orphan", encoding="utf-8")
    old = time_mod.time() - afm_provider.PROBE_CACHE_LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))
    assert afm_provider._reclaim_stale_probe_cache_lock() is True
    assert not lock.exists()


def test_recent_write_timeouts_reach_the_status_verdict():
    """The chronic-timeout writer must not read `ok`; an old timeout must not"""
    from minni.afm_writer import WRITE_TIMEOUTS_RECENT_SECONDS, derive_loop_status

    now = 1_000_000.0
    schedule = {"passes": {"consolidation": {"interval_seconds": 3600}}}
    recent = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "write_timeouts": 4,
        "last_write_timeout_at": now - 60,
    }
    status, reasons = derive_loop_status(recent, schedule=schedule, now=now)
    assert status == "backlogged"
    assert any("timed out" in reason for reason in reasons)
    assert any("lifetime" in reason for reason in reasons)

    old = {
        "last_attempt_per_pass": {"consolidation": now - 60},
        "write_timeouts": 4,
        "last_write_timeout_at": now - WRITE_TIMEOUTS_RECENT_SECONDS - 60,
    }
    status, reasons = derive_loop_status(old, schedule=schedule, now=now)
    assert status == "ok", "a long-recovered writer is not backlogged NOW"
    assert not any("timed out" in reason for reason in reasons)


def test_a_hung_in_flight_job_never_ages_off_the_status_surface(monkeypatch):
    """Round 6: a worker hung MID-JOB has queue_depth 0 (already dequeued),"""
    import time as time_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue.Queue(maxsize=4))

    first = afm_writer.submit_drafts(
        {"pass_name": "p", "drafts": [{"title": "t"}]}, timeout=0.01
    )
    assert first["status"] == "write_timeout", "precondition: the job is hung"
    afm_writer._WORK_QUEUE.get_nowait()

    later = time_mod.time() + afm_writer.WRITE_TIMEOUTS_RECENT_SECONDS + 60
    state = afm_writer.writer_status(schedule={"passes": {}}, now=later)
    assert state["jobs_in_flight"] == 1
    assert state["in_flight_passes"] == ["p"]
    assert state["status"] != "ok", (
        "an unfinished write job is a CURRENT fault; it must not age off the "
        "surface with the timeout stamp"
    )
    assert state["status"] == "backlogged"
    assert any("in flight" in r for r in state["status_reasons"])

    afm_writer._IN_FLIGHT_PER_PASS["p"].set()
    state = afm_writer.writer_status(schedule={"passes": {}}, now=later)
    assert state["jobs_in_flight"] == 0
    assert state["status"] == "ok"
    afm_writer.reset_pass_counters()


def test_writer_status_exposes_the_fields_the_verdict_reads():
    """The state dict and the derivation must not drift apart again."""
    from minni.afm_writer import writer_status

    state = writer_status()
    for key in (
        "queue_depth",
        "queue_max",
        "writes_dropped",
        "last_drop_at",
        "write_timeouts",
        "last_write_timeout_at",
        "write_failures",
        "last_write_failure_at",
        "jobs_in_flight",
        "in_flight_passes",
        "failures_per_pass",
        "last_failure_per_pass",
    ):
        assert key in state, f"writer_status must report {key}"


@pytest.mark.parametrize(
    "sub_op,counter",
    [
        ("inbox_ingest", "inbox_ingest_failures_total"),
        ("inbox_quarantine", "inbox_quarantine_failures_total"),
        ("inbox_archive", "inbox_archive_failures_total"),
        ("compact_distillation", "compact_distillation_failures_total"),
    ],
)
def test_a_failing_consolidation_sub_op_is_counted_and_attributed(
    sub_op, counter, tmp_path, monkeypatch
):
    """GA6-2, driven end to end: a consolidation sub-op that raises on every"""
    import minni.obs as obs

    before = obs.metrics_snapshot().get(counter, 0)
    _run_one_afm_loop_tick(tmp_path, monkeypatch, failing=sub_op)

    after = obs.metrics_snapshot().get(counter, 0)
    assert after == before + 1, (
        f"a {sub_op} failure must increment {counter} — a swallowed exception "
        "with a flat counter is indistinguishable from a sub-op with no work"
    )
    attributed = [e for e in obs.recent_errors() if e.get("method") == f"afm_loop.{sub_op}"]
    assert attributed, (
        f"a {sub_op} failure must reach the error ring so it is attributable, "
        f"got {[e.get('method') for e in obs.recent_errors()]!r}"
    )


def test_failed_tick_does_not_consume_the_full_retry_window():
    """AFM-8: `last_run[name] = time.time()` sat OUTSIDE the try, so a run that"""
    from minni.minnid_runtime.afm import _FAILURE_RETRY_SECONDS, next_last_run

    interval = 24 * 60 * 60
    now = 1_000_000.0

    healthy = next_last_run(interval, now, failed=False)
    assert healthy == now, "a successful tick records now"

    failed = next_last_run(interval, now, failed=True)
    retry_in = failed + interval - now
    assert retry_in == _FAILURE_RETRY_SECONDS, (
        "a failed tick must be retried after the backoff, not after the full "
        f"interval (got {retry_in}s, expected {_FAILURE_RETRY_SECONDS}s)"
    )
    assert _FAILURE_RETRY_SECONDS < interval


def test_a_returned_failure_status_counts_as_a_failed_tick():
    """The load-bearing half of the fix: handle_daemon_compile does NOT raise,"""
    from minni.minnid_runtime.afm import compile_failure_status

    assert compile_failure_status({"result": {"status": "afm_unavailable"}}) == "afm_unavailable"
    assert compile_failure_status({"result": {"status": "write_failed"}}) == "write_failed"
    assert compile_failure_status({"result": {"status": "write_timeout"}}) == "write_timeout"
    assert compile_failure_status({"result": {"status": "write_in_flight"}}) == "write_in_flight"
    assert compile_failure_status(
        {"result": {"status": "lifecycle_recovered", "drafts_deferred": 2}}
    ) == "lifecycle_recovered"
    assert compile_failure_status(
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": "unsupported pass_name: nope"}}
    ) == "rpc_error"
    assert compile_failure_status({"result": {"status": "ok", "summary": {}}}) is None
    assert compile_failure_status({"result": {"summary": {"examined": 3}}}) is None
    assert compile_failure_status(None) is None


def test_an_rpc_error_records_attempt_and_failure():
    """Round 5: a make_error response returns BEFORE handle_daemon_compile's"""
    from minni import afm_writer
    from minni.minnid_runtime.afm import record_rpc_error

    afm_writer.reset_pass_counters()
    record_rpc_error(
        "synthesis",
        {"jsonrpc": "2.0", "id": None, "error": {"code": -32602, "message": "unsupported pass_name: synthesis"}},
    )
    assert afm_writer._FAILURES_PER_PASS["synthesis"] == 1
    assert afm_writer._LAST_ATTEMPT_PER_PASS["synthesis"] > 0
    assert "unsupported pass_name" in afm_writer._LAST_FAILURE_PER_PASS["synthesis"]["error"]
    afm_writer.reset_pass_counters()


def test_a_write_stall_backs_off_longer_than_a_recoverable_fault():
    """Round 5: write_timeout/write_in_flight mean the previous batch is"""
    from minni.minnid_runtime.afm import (
        _FAILURE_RETRY_SECONDS,
        _WRITE_STALL_RETRY_SECONDS,
        _WRITE_STALL_STATUSES,
        next_last_run,
    )

    interval = 24 * 60 * 60
    now = 1_000_000.0

    assert _WRITE_STALL_STATUSES == {"write_timeout", "write_in_flight"}
    assert _WRITE_STALL_RETRY_SECONDS > _FAILURE_RETRY_SECONDS
    assert _WRITE_STALL_RETRY_SECONDS > 30.0, "must exceed the writer wait timeout plus drain margin"

    stalled = next_last_run(interval, now, True, retry_seconds=_WRITE_STALL_RETRY_SECONDS)
    assert stalled + interval - now == _WRITE_STALL_RETRY_SECONDS
    assert next_last_run(interval, now, True) + interval - now == _FAILURE_RETRY_SECONDS


def test_loop_consults_the_returned_status_not_only_exceptions():
    """Pins the wiring itself: a tick's last_run decision must be reachable"""
    from pathlib import Path

    import minni.minnid_runtime.afm as afm_mod

    source = Path(afm_mod.__file__).read_text(encoding="utf-8")
    assert "compile_failure_status(res)" in source, (
        "the loop must inspect the compile response — the exception path alone "
        "is dead code for the failure mode AFM-8 is about"
    )
    assert "tick_failed" in source and "next_last_run(" in source
    assert "tick_failure in _WRITE_STALL_STATUSES" in source
    assert "record_rpc_error(name, res)" in source
    assert '(res.get("error") or {}).get("message")' in source
    assert "lifecycle_recovered" in source
    assert 'failure == "lifecycle_recovered"' in source


def test_lifecycle_recovered_is_soft_failure_not_full_interval():
    """Round 17: a tick that only re-applied a prior deferred lifecycle and"""
    from minni.minnid_runtime.afm import (
        _FAILURE_RETRY_SECONDS,
        _WRITE_STALL_STATUSES,
        compile_failure_status,
        next_last_run,
    )

    status = compile_failure_status(
        {
            "result": {
                "status": "lifecycle_recovered",
                "drafts_deferred": 4,
                "lifecycle_recovered": True,
            }
        }
    )
    assert status == "lifecycle_recovered"
    assert status not in _WRITE_STALL_STATUSES

    interval = 24 * 60 * 60
    now = 2_000_000.0
    stamped = next_last_run(interval, now, failed=True)
    assert stamped + interval - now == _FAILURE_RETRY_SECONDS


def test_unreadable_distillation_file_is_counted_and_logged(tmp_path, caplog, monkeypatch):
    """AFM-9: the malformed-payload branch dropped the group with no counter"""
    import logging

    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "broken.json").write_text("{not json at all", encoding="utf-8")

    db_obj, cfg = _distill_db(tmp_path)
    with caplog.at_level(logging.WARNING):
        res = mod.distill(db_obj, cfg, inboxes=[inbox], dry_run=True)

    assert res["skipped"].get("_unreadable") == 1, "a discarded group must be counted"
    assert any("unreadable" in record.message for record in caplog.records), (
        "and visible above DEBUG"
    )


def test_over_candidate_cap_counts_only_candidate_eligible_sections():
    """Round 6: the cap-break count was `len(sections) - i - 1`, which swept"""
    import minni.afm_passes.compact_distillation as mod

    filler = "durable transferable learning content, well past the floor."
    shared = [
        f"{i}. Key Learnings:\n{filler} section {i}\n"
        for i in range(1, mod.MAX_CANDIDATES_PER_FILE + 2)
    ]
    tail = [
        f"{len(shared) + 1}. Random Notes:\n{filler}\n",
        f"{len(shared) + 2}. All User Messages:\n{filler}\n",
        f"{len(shared) + 3}. Current Work:\n{filler}\n",
    ]
    doc = {"summary_text": "".join(shared + tail)}

    candidates, _personal, dropped = mod._distill_file(doc, afm_chain=None)
    assert len(candidates) == mod.MAX_CANDIDATES_PER_FILE, "precondition: cap hit"
    assert dropped["_over_candidate_cap"] == 1, (
        "only the ONE remaining shared section was candidate-eligible; the "
        "personal and skip-titled tail was never distillation input"
    )


def test_distillation_reports_dropped_sections(tmp_path, monkeypatch):
    """AFM-9: sections dropped mid-distillation vanished on a bare `continue`."""
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)

    db_obj, cfg = _distill_db(tmp_path)
    res = mod.distill(db_obj, cfg, inboxes=[inbox], dry_run=True)
    assert "dropped_sections" in res


def test_health_report_declares_consolidation_ingest_fields():
    """GA6-2: health had no ingest-failure field and no inbox-backlog field at"""
    from pathlib import Path

    import minni.minnid_runtime.health as health_mod

    source = Path(health_mod.__file__).read_text(encoding="utf-8")
    assert '"consolidation_ingest"' in source
    assert '"inbox_backlog"' in source


def test_degrade_flags_are_thread_local_like_auth_suppression(tmp_path, monkeypatch):
    """The daemon hands every `search` RPC the SAME process-wide engine on its"""
    import threading

    engine = _engine_without_model(tmp_path, monkeypatch)
    engine.last_rerank_degraded = "this thread's failure"
    engine.last_query_expand_degraded = "this thread's expand failure"
    engine.last_vector_degraded = "this thread's vector failure"

    seen = {}

    def _other_thread():
        engine.last_rerank_degraded = None
        engine.last_query_expand_degraded = None
        engine.last_vector_degraded = None
        seen["other_reads"] = engine.last_rerank_degraded

    thread = threading.Thread(target=_other_thread)
    thread.start()
    thread.join()

    assert seen["other_reads"] is None, "the other thread sees its own state"
    assert engine.last_rerank_degraded == "this thread's failure", (
        "a concurrent request must not be able to clear THIS request's degrade "
        "flag — that reports a degraded search as healthy"
    )
    assert engine.last_query_expand_degraded == "this thread's expand failure"
    assert engine.last_vector_degraded == "this thread's vector failure"


def test_degrade_flags_work_on_instances_that_bypass_init():
    """Test fakes built via object.__new__ must still get a working store —"""
    from minni.retrieval import RetrievalEngine

    engine = object.__new__(RetrievalEngine)
    assert engine.last_rerank_degraded is None
    engine.last_rerank_degraded = "boom"
    assert engine.last_rerank_degraded == "boom"


def test_rerank_degrade_survives_a_later_healthy_variant(tmp_path, monkeypatch):
    """Each recursive retrieve() clears the flags on entry, so an early"""
    import minni.retrieval as retrieval_mod

    engine = _engine_without_model(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retrieval_mod,
        "expand_query_with_status",
        lambda query, mode="rule": (["v1", "v2"], None),
    )
    monkeypatch.setattr(
        retrieval_mod, "expand_query", lambda query, mode="rule": ["v1", "v2"]
    )

    calls = {"n": 0}
    real_filter = engine._filter_candidates

    def _degrade_on_first_variant(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            engine.last_rerank_degraded = "reranker died on v1"
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(engine, "_filter_candidates", _degrade_on_first_variant)
    engine.retrieve(query="anything", limit=3)

    assert calls["n"] >= 2, "precondition: both variants ran"

    assert engine.last_rerank_degraded is not None, (
        "a degrade in the FIRST variant must survive a healthy second variant — "
        "the merge still mixed a leg that was not reranked"
    )
    assert "v1" in engine.last_rerank_degraded


def test_consolidation_ingest_failures_exclude_other_subsystems():
    """A bare `_failures_total` suffix filter also swept in"""
    from minni.minnid_runtime.health import CONSOLIDATION_FAILURE_COUNTERS

    assert "afm_pass_failures_total" not in CONSOLIDATION_FAILURE_COUNTERS
    assert "afm_loop_tick_failures_total" not in CONSOLIDATION_FAILURE_COUNTERS
    assert set(CONSOLIDATION_FAILURE_COUNTERS) == {
        "inbox_ingest_failures_total",
        "inbox_quarantine_failures_total",
        "inbox_archive_failures_total",
        "compact_distillation_failures_total",
    }


def test_counters_stamp_recency():
    """The recency source for latch-free verdicts: every incr stamps a time."""
    from minni.obs import Counters

    counters = Counters()
    assert counters.last_incremented_at("x") is None
    counters.incr("x")
    assert counters.last_incremented_at("x") is not None
    counters.reset()
    assert counters.last_incremented_at("x") is None


class _HealthFakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return {"max_rowid": 0, "n": 0}


class _HealthFakeDB:
    def __init__(self, *a, **k):
        pass

    def cursor(self):
        return _HealthFakeCursor()

    def close(self):
        pass


def test_consolidation_ingest_status_does_not_latch_on_old_failures(tmp_path, monkeypatch):
    """Review round 3 (PR #260): the ingest status was derived from cumulative"""
    import minni.config as cfg_mod
    import minni.minnid as minnid
    from minni import obs
    from minni.minnid_runtime.health import CONSOLIDATION_FAILURE_RECENT_SECONDS
    from minni.principal import EffectivePrincipal

    monkeypatch.setattr(minnid, "MinniDB", _HealthFakeDB)
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "CANONICAL_SOVEREIGN_HOME", str(tmp_path), raising=False
    )
    obs.METRICS.reset()
    try:
        obs.incr("inbox_ingest_failures_total")
        op = EffectivePrincipal(agent_id="main", capabilities=["*"])

        rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]
        ingest = rep["consolidation_ingest"]
        assert ingest["status"] == "failing"
        assert ingest["failures"] == {"inbox_ingest_failures_total": 1}
        assert ingest["recent_failures"] == {"inbox_ingest_failures_total": 1}

        monkeypatch.setattr(
            obs,
            "metrics_last_incremented_at",
            lambda name: time.time() - CONSOLIDATION_FAILURE_RECENT_SECONDS - 60,
        )
        rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]
        ingest = rep["consolidation_ingest"]
        assert ingest["status"] == "ok", (
            "a boot-time failure with no recurrence must not read failing forever"
        )
        assert ingest["failures"] == {"inbox_ingest_failures_total": 1}, (
            "the cumulative total stays visible as data"
        )
        assert ingest["recent_failures"] == {}
    finally:
        obs.METRICS.reset()


def _make_context(engine, captured):
    """A RecallContext stub that captures the response payload."""
    from minni.config import DEFAULT_CONFIG
    from minni.minnid_runtime.recall import RecallContext

    def _make_response(payload, request_id):
        captured["response"] = payload
        return {"result": payload}

    def _make_error(code, message, request_id):
        captured["error"] = {"code": code, "message": message}
        return {"error": captured["error"]}

    return RecallContext(
        make_error=_make_error,
        make_response=_make_response,
        handler_principal=lambda params, request_id: (None, None),
        lazy_retrieval=lambda: engine,
        agent_vault_retrieval=lambda agent_id: None,
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: _NullRing(),
        record_latency=lambda name, elapsed: None,
        default_config=DEFAULT_CONFIG,
    )


def _engine_without_model(tmp_path, monkeypatch):
    """A RetrievalEngine whose embedding model never loads."""
    import minni.db as db_mod
    import minni.models as models_mod
    import minni.retrieval as retrieval_mod
    from minni.config import MinniConfig
    from minni.retrieval import RetrievalEngine

    cfg = MinniConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
    )
    db_obj = _fresh_db(db_mod, cfg)
    monkeypatch.setattr(models_mod, "get_embedder", lambda: None)
    if hasattr(retrieval_mod, "get_embedder"):
        monkeypatch.setattr(retrieval_mod, "get_embedder", lambda: None)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
    engine._model = None
    assert engine.model is None, "precondition: the embedding model must be down"
    return engine


def _run_one_afm_loop_tick(tmp_path, monkeypatch, *, failing: str):
    """Run exactly one consolidation tick of the real AFM loop."""
    import asyncio
    from types import SimpleNamespace

    import minni.afm_passes.compact_distillation as distill_mod
    import minni.afm_passes.inbox_archive as archive_mod
    import minni.afm_passes.inbox_ingest as ingest_mod
    import minni.afm_passes.inbox_quarantine as quarantine_mod
    import minni.minnid_runtime.afm as afm_mod
    from minni.config import MinniConfig

    def _boom(name):
        def _raise(*args, **kwargs):
            raise RuntimeError(f"{name} exploded")

        return _raise

    stubs = {
        "inbox_ingest": (
            ingest_mod,
            "ingest",
            lambda *a, **k: {
                "inserted": 0,
                "eligible": 0,
                "already_present": 0,
                "skipped_by_kind": {"_agent_mismatch": 1},
            },
        ),
        "inbox_quarantine": (
            quarantine_mod,
            "quarantine_stale_agent_mismatch",
            lambda *a, **k: {"quarantined": 0},
        ),
        "inbox_archive": (archive_mod, "archive_inert_files", lambda *a, **k: {"archived": 0}),
        "compact_distillation": (
            distill_mod,
            "distill",
            lambda *a, **k: {"inserted": 0, "vault_notes_written": 0},
        ),
    }
    assert failing in stubs, f"unknown sub-op {failing!r}"
    for name, (module, attr, ok) in stubs.items():
        monkeypatch.setattr(module, attr, _boom(name) if name == failing else ok)

    monkeypatch.setattr(
        afm_mod,
        "handle_daemon_compile",
        lambda params, request_id, context: {"result": {"summary": {"examined": 0}}},
    )

    cfg = MinniConfig(
        db_path=str(tmp_path / "loop.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
        afm_loop_schedule={
            "enabled": True,
            "idle_seconds": 0,
            "passes": {"consolidation": {"interval_seconds": 0, "max_batches_per_tick": 1}},
        },
    )
    ticks = iter([True])
    context = afm_mod.AFMContext(
        make_error=lambda code, message, request_id: {"error": {"code": code}},
        make_response=lambda payload, request_id: {"result": payload},
        guard_vault_root=lambda *a, **k: None,
        lazy_writeback=lambda: SimpleNamespace(db=object()),
        trace_ring=lambda: None,
        record_latency=lambda name, elapsed: None,
        maybe_archive_inbox_source=lambda db, candidate_id: None,
        default_config=cfg,
        is_running=lambda: next(ticks, False),
    )
    asyncio.run(afm_mod.afm_loop_runner(context))


def _hyde_engine(tmp_path, monkeypatch):
    """A RetrievalEngine whose HyDE leg is guaranteed to TRIGGER."""
    import time

    import minni.db as db_mod
    import minni.models as models_mod
    import minni.retrieval as retrieval_mod
    from minni.config import MinniConfig
    from minni.retrieval import RetrievalEngine

    cfg = MinniConfig(
        db_path=str(tmp_path / "hyde.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
        reranker_enabled=False,
        hyde_enabled=True,
        hyde_confidence_floor=1.0,
    )
    db_obj = _fresh_db(db_mod, cfg)
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, indexed_at, last_modified, layer)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("wiki/aetherkernel.md", "unknown", "?", now, now, "knowledge"),
        )
        doc_id = c.lastrowid
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, ?, ?)",
            (doc_id, "wiki/aetherkernel.md", "aetherkernel xHCI window notes", "unknown", "?"),
        )
    monkeypatch.setattr(models_mod, "get_embedder", lambda: None)
    if hasattr(retrieval_mod, "get_embedder"):
        monkeypatch.setattr(retrieval_mod, "get_embedder", lambda: None)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
    engine._model = None
    monkeypatch.setattr(engine, "_semantic_search", lambda *_args, **_kwargs: [])
    return engine


def _fresh_db(db_mod, cfg):
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.MinniDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj


def _distill_db(tmp_path):
    import minni.db as db_mod
    from minni.config import MinniConfig

    cfg = MinniConfig(
        db_path=str(tmp_path / "distill.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
    )
    return _fresh_db(db_mod, cfg), cfg


def _compile_with_pass_error(monkeypatch, tmp_path, error):
    """Drive handle_daemon_compile through a consolidation pass that raises."""
    import minni.afm_passes.consolidation as consolidation_mod
    import minni.db as db_mod
    import minni.minnid_runtime.afm as afm_mod
    from minni.config import MinniConfig
    from minni.principal import EffectivePrincipal

    def _explode(db, config, **kwargs):
        raise error

    monkeypatch.setattr(consolidation_mod, "run", _explode)

    captured = {}

    def _make_response(payload, request_id):
        if isinstance(payload, dict):
            captured.update(payload)
        return {"result": payload}

    cfg = MinniConfig(
        db_path=str(tmp_path / "afm.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )
    context = afm_mod.AFMContext(
        make_error=lambda code, message, request_id: {"error": {"code": code}},
        make_response=_make_response,
        guard_vault_root=lambda *a, **k: None,
        lazy_writeback=lambda: None,
        trace_ring=lambda: _NullRing(),
        record_latency=lambda name, elapsed: None,
        maybe_archive_inbox_source=lambda db, cid: None,
        sovereign_db=lambda config, *a, **k: _fresh_db(db_mod, cfg),
        default_config=cfg,
    )
    params = {
        "pass_name": "consolidation",
        "dry_run": False,
        "vault_path": str(tmp_path / "vault"),
        "_principal": EffectivePrincipal(
            agent_id="operator",
            workspace_id="default",
            transport="internal",
            capabilities=["govern"],
            allowed_vault_roots=[],
        ),
    }
    afm_mod.handle_daemon_compile(params, request_id=1, context=context)
    return captured


class _NullRing:
    def put(self, *args, **kwargs):
        return None


def test_deferred_lifecycle_missing_handler_records_sticky_and_is_recoverable(
    tmp_path, monkeypatch,
):
    """Medium #1 (PR #260 tip RC): deferred lifecycle with no handler used to"""
    import queue as queue_mod
    import threading

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))
    writes = []

    def _write(job):
        writes.append(job.get("pass_name"))
        return {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"}

    monkeypatch.setattr(afm_writer, "_write_batch", _write)

    vault = tmp_path / "vault"
    vault.mkdir()
    done = threading.Event()
    out: dict = {}
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "vault_path": str(vault),
        "lifecycle": {
            "promote_candidate_ids": [7],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [],
        },
        "lifecycle_handler": None,
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done

    afm_writer._process_job(job, done, out)
    assert not done.is_set(), "missing handler must keep in-flight set (hold)"
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE, (
        "sticky must be recorded so a later pass with a handler can re-apply"
    )
    assert afm_writer._LIFECYCLE_APPLY_FAILURES >= 1
    pending = afm_writer._PENDING_LIFECYCLE["consolidation"]
    assert pending.get("promote_candidate_ids") == [7]
    assert writes == ["consolidation"], "drafts already landed before lifecycle"

    applied = []

    def _handler(life):
        life["applied"] = {"promoted": 1, "deduped": 0, "reviewed": 0}
        applied.append(dict(life))

    second = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "drafts": [{"title": "dup"}],
            "lifecycle_handler": _handler,
            "vault_path": str(vault),
        },
        timeout=0.05,
    )
    assert second.get("status") == "lifecycle_recovered", second
    assert second.get("lifecycle_recovered") is True
    assert "consolidation" not in afm_writer._PENDING_LIFECYCLE
    assert done.is_set(), "re-apply must clear the sticky in-flight Event"
    assert len(applied) == 1
    assert applied[0]["promote_candidate_ids"] == [7]


def test_afm_query_expand_soft_fail_sets_degraded_flag(tmp_path, monkeypatch):
    """Low #3 (PR #260 tip RC): mode=afm with empty AFM used to fall back to"""
    import minni.retrieval as retrieval_mod
    import minni.query_expand as qe

    engine = _engine_without_model(tmp_path, monkeypatch)
    monkeypatch.setattr(qe, "_afm_expand", lambda query: [])
    monkeypatch.setattr(
        retrieval_mod,
        "expand_query_with_status",
        lambda query, mode="rule": qe.expand_with_status(query, mode=mode),
    )
    engine.last_query_expand_degraded = None
    variants = engine._resolve_query_variants("AFM recall", "afm")

    assert variants, "rule fallback must still produce variants"
    assert engine.last_query_expand_degraded == "afm_unavailable", (
        f"soft AFM miss must be reachable from the call site, got "
        f"{engine.last_query_expand_degraded!r}"
    )


def test_expand_with_status_rule_mode_never_degrades():
    """Rule mode is local and complete — no afm_unavailable noise."""
    from minni.query_expand import expand_with_status

    variants, degraded = expand_with_status("minni recall", mode="rule")
    assert variants
    assert degraded is None


def test_deferred_worker_soft_partial_keeps_sticky_and_hold(monkeypatch):
    """High (PR #260 tip RC): first deferred apply soft-partial must sticky-hold"""
    import queue as queue_mod
    import threading

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))
    monkeypatch.setattr(
        afm_writer,
        "_write_batch",
        lambda job: {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"},
    )

    def _soft_partial(life):
        life["applied"] = {
            "promoted": 1,
            "deduped": 0,
            "reviewed": 0,
            "errors": 0,
            "remaining_proposed": [2, 3],
            "requested": {"promote": 3, "dedup": 0, "review": 0},
        }

    done = threading.Event()
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "lifecycle": {
            "promote_candidate_ids": [1, 2, 3],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [],
        },
        "lifecycle_handler": _soft_partial,
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done
    afm_writer._process_job(job, done, {})
    assert not done.is_set(), "soft partial must keep in-flight hold"
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE
    assert job.get("lifecycle_applied") is not True

    put_calls = []
    real_put = afm_writer._WORK_QUEUE.put_nowait

    def _spy(item):
        put_calls.append(item)
        return real_put(item)

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy)
    second = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "drafts": [{"title": "dup-mint-risk"}],
            "lifecycle_handler": _soft_partial,
        },
        timeout=0.05,
    )
    assert second.get("lifecycle_pending") is True or second.get("status") == "write_in_flight", (
        second
    )
    assert put_calls == [], "must not enqueue a second wet draft batch"
    afm_writer.reset_pass_counters()


def test_partial_sticky_reapply_keeps_hold_and_refuses_wet(monkeypatch):
    """High #1 (PR #260 tip RC): sticky re-apply that terminalizes only a"""
    import queue as queue_mod
    import threading

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))
    monkeypatch.setattr(
        afm_writer,
        "_write_batch",
        lambda job: {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"},
    )

    def _handler(life):
        life["applied"] = {
            "promoted": 1,
            "deduped": 0,
            "reviewed": 0,
            "errors": 0,
            "remaining_proposed": [2, 3],
            "requested": {"promote": 3, "dedup": 0, "review": 0},
        }

    done = threading.Event()
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "lifecycle": {
            "promote_candidate_ids": [1, 2, 3],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [],
        },
        "lifecycle_handler": lambda life: (_ for _ in ()).throw(
            RuntimeError("first apply fails → sticky")
        ),
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done
    afm_writer._process_job(job, done, {})
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE

    put_calls = []
    real_put = afm_writer._WORK_QUEUE.put_nowait

    def _spy(item):
        put_calls.append(item)
        return real_put(item)

    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", _spy)
    second = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "drafts": [{"title": "dup-mint-risk"}],
            "lifecycle_handler": _handler,
        },
        timeout=0.05,
    )
    assert second.get("lifecycle_pending") is True, second
    assert second.get("lifecycle_partial") is True, second
    assert second.get("status") == "write_in_flight", second
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE, (
        "partial re-apply must keep sticky for the remaining proposed IDs"
    )
    assert put_calls == [], "must not enqueue a second wet draft batch"
    assert not done.is_set(), "in-flight hold must remain until full recovery"
    afm_writer.reset_pass_counters()


def test_legacy_partial_applied_without_remaining_keeps_sticky(monkeypatch):
    """Legacy handlers that only set promoted=1 of N (no remaining_proposed)"""
    import queue as queue_mod
    import threading

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))
    monkeypatch.setattr(
        afm_writer,
        "_write_batch",
        lambda job: {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"},
    )

    def _handler(life):
        life["applied"] = {"promoted": 1, "deduped": 0, "reviewed": 0}

    done = threading.Event()
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "lifecycle": {
            "promote_candidate_ids": [1, 2, 3],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [],
        },
        "lifecycle_handler": lambda life: (_ for _ in ()).throw(RuntimeError("blip")),
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done
    afm_writer._process_job(job, done, {})

    second = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "drafts": [{"title": "x"}],
            "lifecycle_handler": _handler,
        },
        timeout=0.05,
    )
    assert second.get("lifecycle_pending") is True, second
    assert "consolidation" in afm_writer._PENDING_LIFECYCLE
    afm_writer.reset_pass_counters()


def test_lifecycle_recovered_counts_discarded_wet_drafts(monkeypatch):
    """Medium #3: lifecycle_recovered must not silently drop an LLM batch."""
    import queue as queue_mod
    import threading
    import time as time_mod

    from minni import afm_writer

    afm_writer.reset_pass_counters()
    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer, "_WORK_QUEUE", queue_mod.Queue(maxsize=4))
    monkeypatch.setattr(
        afm_writer,
        "_write_batch",
        lambda job: {"drafts_written": [{"page_id": "d1"}], "inbox_path": "x"},
    )

    def _handler(life):
        if not hasattr(_handler, "n"):
            _handler.n = 0
        _handler.n += 1
        if _handler.n == 1:
            raise RuntimeError("blip")
        life["applied"] = {
            "promoted": 1,
            "deduped": 0,
            "reviewed": 0,
            "errors": 0,
            "remaining_proposed": [],
        }

    done = threading.Event()
    job = {
        "pass_name": "consolidation",
        "drafts": [{"title": "t"}],
        "lifecycle": {
            "promote_candidate_ids": [1],
            "dedup_candidate_ids": [],
            "review_candidate_ids": [],
        },
        "lifecycle_handler": _handler,
        "defer_lifecycle_to_worker": True,
    }
    with afm_writer._IN_FLIGHT_LOCK:
        afm_writer._IN_FLIGHT_PER_PASS["consolidation"] = done
    afm_writer._process_job(job, done, {})

    second = afm_writer.submit_drafts(
        {
            "pass_name": "consolidation",
            "drafts": [{"title": "a"}, {"title": "b"}],
            "lifecycle_handler": _handler,
        },
        timeout=0.05,
    )
    assert second.get("status") == "lifecycle_recovered", second
    assert second.get("lifecycle_recovered_drafts_dropped") == 2
    assert afm_writer._LIFECYCLE_RECOVERED_DRAFTS_DROPPED == 2
    status = afm_writer.writer_status()
    assert status["lifecycle_recovered_drafts_dropped"] == 2
    assert status["status"] != "ok", status
    assert any("discarded" in r or "lifecycle recovery" in r for r in status["status_reasons"]), (
        status["status_reasons"]
    )
    aged = {
        "lifecycle_recovered_drafts_dropped": 2,
        "last_lifecycle_recovered_drop_at": time_mod.time()
        - afm_writer.LIFECYCLE_RECOVERED_DROP_RECENT_SECONDS
        - 10,
        "last_run_per_pass": {},
        "last_attempt_per_pass": {},
        "drafts_pending": 0,
        "queue_depth": 0,
        "writes_dropped": 0,
        "write_timeouts": 0,
        "write_failures": 0,
        "unrecovered_write_failures": 0,
        "pending_lifecycle_passes": 0,
        "jobs_in_flight": 0,
        "failures_per_pass": {},
    }
    aged_status, aged_reasons = afm_writer.derive_loop_status(aged, schedule={"passes": {}})
    assert aged_status == "ok", (aged_status, aged_reasons)
    afm_writer.reset_pass_counters()


def test_afm_expand_soft_fail_survives_multi_variant_retrieve(tmp_path, monkeypatch):
    """High (PR #260 tip RC): parent last_query_expand_degraded must survive"""
    import minni.retrieval as retrieval_mod
    import minni.query_expand as qe

    engine = _engine_without_model(tmp_path, monkeypatch)
    monkeypatch.setattr(qe, "_afm_expand", lambda query: [])
    monkeypatch.setattr(
        retrieval_mod,
        "expand_query_with_status",
        lambda query, mode="rule": (["v1", "v2"], "afm_unavailable"),
    )
    engine.last_query_expand_degraded = None
    engine.retrieve(query="AFM recall", limit=3, expand="afm")
    assert engine.last_query_expand_degraded is not None, (
        "multi-variant merge must not wipe parent expand soft-fail"
    )
    assert "afm_unavailable" in engine.last_query_expand_degraded

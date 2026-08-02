"""R8 observability slice — every degrade path must be visible at the call site.

Issues #226 (recall observability), #230 (AFM pass honesty and durability), and
the gap-audit findings folded into them (GA4-3, GA6-2, GA2-1).

The shared contract these tests enforce: a degraded result must never be
indistinguishable from a healthy one. Each test below fails against the
pre-R8 behavior — that is the point of it. Where a test pins a NEW field, the
old code omitted the field entirely; where it pins a value, the old code
reported the opposite.
"""

import queue
import time

import pytest


# ── #226 R3: partial auth suppression must be visible ────────────────────────


def test_auth_suppression_reported_even_when_other_legs_returned_hits():
    """R3: `auth_suppression` was gated on `if not results` — a vault entirely
    blacked out by authorization read as normal whenever ANY other leg had
    hits. Fails pre-R8: the key is absent from a non-empty response."""
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


# ── #226 R4(a): the response must carry a vector-degradation field ───────────


def test_search_response_always_carries_vector_model_and_degraded():
    """R4(a): the response carried no `vector_model` or `degraded` field at
    all. Fails pre-R8: both keys are absent."""
    from minni.minnid_runtime import recall as recall_mod

    captured = {}

    class _Config:
        embedding_model = "all-MiniLM-L6-v2"

    class _Engine:
        config = _Config()
        vector_model_down = True  # semantic leg is DOWN
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


# ── #226 R4(b): explicit-backend branches must not bypass the P0-B flag ──────


def test_encode_query_raises_the_degradation_flag_when_encoder_down(tmp_path, monkeypatch):
    """R4(b): the explicit-backend branches inlined
    `self.model.encode(q) if self.model else np.array([])`, feeding an EMPTY
    query vector into search with no log line and no flag. Fails pre-R8:
    `_encode_query` does not exist, and the inline expression it replaces left
    vector_model_down False."""
    engine = _engine_without_model(tmp_path, monkeypatch)

    assert engine.vector_model_down is False
    vector = engine._encode_query("some query")

    assert len(vector) == 0, "precondition: an empty vector is what got fed to the backend"
    assert engine.vector_model_down is True, (
        "the explicit-backend path must raise the SAME degradation flag as the "
        "default branch — otherwise lexical-only results are indistinguishable "
        "from a healthy hybrid search"
    )


def test_explicit_backend_branches_use_the_flagging_encoder():
    """Regression pin on the call sites themselves: no branch may re-introduce
    the unflagged inline encode. Fails pre-R8 — all three lines matched."""
    from pathlib import Path

    import minni.retrieval as retrieval_mod

    source = Path(retrieval_mod.__file__).read_text(encoding="utf-8")
    assert "self.model.encode(query).astype(np.float32) if self.model else" not in source, (
        "an explicit-backend branch is bypassing _encode_query and therefore "
        "the P0-B degradation flag"
    )


# ── #226 R4(c): a bad backend value is -32602, not -32000 ────────────────────


def test_unknown_backend_value_is_invalid_params_not_internal_error():
    """R4(c): an unknown backend raised AttributeError on a str and surfaced as
    -32000 (internal error) — a caller mistake reported as a server fault.
    Fails pre-R8: resolve_backend returned the bad string unchanged."""
    from minni.minnid_runtime.recall import resolve_backend

    with pytest.raises(ValueError) as excinfo:
        resolve_backend("faiss-disk-typo")

    message = str(excinfo.value)
    assert "faiss-disk" in message, "the error must name the valid values"


def test_documented_faiss_disk_backend_value_resolves_instead_of_crashing():
    """The documented `backend: "faiss-disk"` form must reach the validated
    list path. Fails pre-R8: it was passed through as a bare str."""
    from minni.minnid_runtime.recall import resolve_backend

    assert resolve_backend("faiss-disk") == ["faiss-disk"]


# ── #226 R5: a per-engine rerank failure must be reported ────────────────────


def test_rerank_failure_is_recorded_not_swallowed(tmp_path, monkeypatch):
    """R5: a rerank failure left candidates with no rerank_score, dropping that
    corpus to raw RRF magnitude in a cross-corpus merge. Fails pre-R8: the
    attribute does not exist and the failure left no trace."""
    engine = _engine_without_model(tmp_path, monkeypatch)

    class _ExplodingReranker:
        def predict(self, pairs):
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


# ── #230 AFM-4: the probe cache must evict ───────────────────────────────────


def test_probe_cache_is_bounded():
    """AFM-4: the cache had no eviction policy — measured live at 22 entries,
    21 of them stale test residue. Fails pre-R8: it grew without limit."""
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
    # Oldest probes go first: the surviving set is the newest N.
    survivors = sorted(entry["probed_at"] for entry in entries.values())
    assert survivors[0] > 1000.0, "eviction must drop the OLDEST probe, not an arbitrary one"


def test_probe_cache_write_is_serialized(tmp_path, monkeypatch):
    """AFM-4: the persistent cache was a read-modify-write with no mutual
    exclusion, so concurrent probes lost entries. Fails pre-R8 under
    contention; here we pin that the lock exists and the RMW holds it."""
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


# ── #230 AFM-6: a failed HyDE leg must not read as triggered-and-fine ────────


def test_hyde_trace_distinguishes_triggered_from_completed():
    """AFM-6: the trace recorded `triggered = true` for a leg that FAILED, so
    anyone reading it to explain a bad result was told the enrichment ran when
    it had not. Fails pre-R8: `completed` did not exist."""
    from pathlib import Path

    import minni.retrieval as retrieval_mod

    source = Path(retrieval_mod.__file__).read_text(encoding="utf-8")
    assert 'trace["hyde"]["completed"] = False' in source, (
        "a failed or skipped HyDE leg must be recorded as not-completed"
    )
    assert 'logger.debug("HyDE skipped after retrieval pass' not in source, (
        "a HyDE failure logged at DEBUG is invisible at normal log levels"
    )


def test_query_expansion_failure_is_reachable_from_the_call_site(tmp_path, monkeypatch):
    """AFM-6: query-expansion degrade was a log line only, unreachable from the
    caller. Fails pre-R8: the attribute does not exist."""
    import minni.retrieval as retrieval_mod

    engine = _engine_without_model(tmp_path, monkeypatch)

    def _explode(query, mode="rule"):
        raise RuntimeError("expansion died")

    monkeypatch.setattr(retrieval_mod, "expand_query", _explode)
    engine.last_query_expand_degraded = None
    variants = engine._resolve_query_variants("a query", True)

    assert variants == ["a query"], "precondition: it falls back to the bare query"
    assert engine.last_query_expand_degraded is not None, (
        "the caller must be able to tell an expanded search from a bare one"
    )


# ── #230 AFM-8: writer queue bound, drop policy, honest write failure ────────


def test_writer_queue_is_bounded():
    """AFM-8: the queue was unbounded. Fails pre-R8: maxsize was 0 (infinite)."""
    from minni import afm_writer

    assert afm_writer._WORK_QUEUE.maxsize == afm_writer.WRITER_QUEUE_MAX
    assert afm_writer.WRITER_QUEUE_MAX > 0


def test_full_queue_rejects_loudly_and_counts_the_drop(monkeypatch):
    """AFM-8 drop policy: a submission past the bound is REJECTED and counted,
    not silently accepted into a queue that will never drain. Fails pre-R8:
    the unbounded queue accepted it and reported status 'queued'."""
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
    """AFM-8: a write failure raised a bare RuntimeError, which the daemon's
    blanket handler reported as `afm_unavailable` — blaming the model provider
    for a durable-storage fault. Fails pre-R8: DraftWriteError did not exist."""
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
    """AFM-8, end to end: a storage fault must not be reported as
    `afm_unavailable`. Fails pre-R8: the blanket handler returned
    'afm_unavailable' for every exception including write failures."""
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
    """AFM-8: the timeout path returned status 'queued', so an unobserved batch
    read as a completed one. Fails pre-R8: status was 'queued'."""
    from minni import afm_writer

    monkeypatch.setattr(afm_writer, "_ensure_worker", lambda: None)
    monkeypatch.setattr(afm_writer._WORK_QUEUE, "put_nowait", lambda item: None)

    result = afm_writer.submit_drafts(
        {"pass_name": "p", "drafts": [{"t": 1}, {"t": 2}]}, timeout=0.01
    )
    assert result["status"] == "write_timeout"
    assert result["drafts_written"] == []
    assert result["drafts_in_flight"] == 2


# ── GA4-3: a pass that raises must record the attempt AND the failure ────────


def test_pass_exception_records_attempt_and_failure(monkeypatch, tmp_path):
    """GA4-3: an exception skipped record_pass_attempt, so a pass raising every
    tick read as 'never invoked' or went stale — and NO failure counter existed
    anywhere. Fails pre-R8 on both assertions."""
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
    """GA4-3: the health verdict must distinguish a failing pass from an idle
    one. Fails pre-R8: 'failing' was not a status derive_loop_status could
    return, and a pass that ran (attempt recorded) read 'ok'."""
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


# ── GA2-1: queue_depth must be consulted, not orphaned ───────────────────────


def test_queue_depth_is_consulted_by_the_status_derivation():
    """GA2-1: queue_depth was recorded in writer_status and never read by
    derive_loop_status — an orphaned metric reads as coverage that is not
    there. Fails pre-R8: the status was 'ok' at any queue depth."""
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


def test_writer_status_exposes_the_fields_the_verdict_reads():
    """The state dict and the derivation must not drift apart again."""
    from minni.afm_writer import writer_status

    state = writer_status()
    for key in ("queue_depth", "queue_max", "writes_dropped", "failures_per_pass"):
        assert key in state, f"writer_status must report {key}"


# ── GA6-2: consolidation sub-op failures must be counted ─────────────────────


@pytest.mark.parametrize(
    "counter",
    [
        "inbox_ingest_failures_total",
        "inbox_quarantine_failures_total",
        "inbox_archive_failures_total",
        "compact_distillation_failures_total",
    ],
)
def test_each_consolidation_sub_op_has_a_failure_counter(counter):
    """GA6-2: all four sub-ops incremented obs.incr only on their SUCCESS
    paths and swallowed exceptions, so a sub-op broken for days was
    indistinguishable from one with no work. Fails pre-R8: none of these
    counters is incremented anywhere."""
    from pathlib import Path

    import minni.minnid_runtime.afm as afm_mod

    source = Path(afm_mod.__file__).read_text(encoding="utf-8")
    assert f'obs.incr("{counter}")' in source


def test_consolidation_sub_op_failures_reach_the_error_ring():
    """GA6-2: record_error was only ever reachable from dispatch, so a sub-op
    fault never became attributable. Fails pre-R8."""
    from pathlib import Path

    import minni.minnid_runtime.afm as afm_mod

    source = Path(afm_mod.__file__).read_text(encoding="utf-8")
    for op in ("inbox_ingest", "inbox_quarantine", "inbox_archive", "compact_distillation"):
        assert f'obs.record_error("afm_loop.{op}"' in source


# ── AFM-8 (loop level): last_run must not consume the interval on failure ────


def test_failed_tick_does_not_consume_the_full_retry_window():
    """AFM-8: `last_run[name] = time.time()` sat OUTSIDE the try, so a run that
    accomplished nothing consumed the whole 24h window — a failing pass looked
    exactly like one that ran and had nothing to do.

    Behavioral, not a source grep. Review round 1 on PR #260 caught that the
    first version of this fix put the backoff in the loop's `except`, which
    handle_daemon_compile never reaches because it swallows the exception and
    RETURNS — so the line was dead and a grep-based test passed anyway.
    """
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
    """The load-bearing half of the fix: handle_daemon_compile does NOT raise,
    it RETURNS a failure status. If the loop does not read the response, the
    backoff above never fires and the original defect is still live."""
    from minni.minnid_runtime.afm import compile_failure_status

    assert compile_failure_status({"result": {"status": "afm_unavailable"}}) == "afm_unavailable"
    assert compile_failure_status({"result": {"status": "write_failed"}}) == "write_failed"
    # A successful compile must not be mistaken for a failure.
    assert compile_failure_status({"result": {"status": "ok", "summary": {}}}) is None
    assert compile_failure_status({"result": {"summary": {"examined": 3}}}) is None
    assert compile_failure_status(None) is None


def test_loop_consults_the_returned_status_not_only_exceptions():
    """Pins the wiring itself: a tick's last_run decision must be reachable
    from a RETURNED failure, which is the route real pass failures take."""
    from pathlib import Path

    import minni.minnid_runtime.afm as afm_mod

    source = Path(afm_mod.__file__).read_text(encoding="utf-8")
    assert "compile_failure_status(res)" in source, (
        "the loop must inspect the compile response — the exception path alone "
        "is dead code for the failure mode AFM-8 is about"
    )
    assert "next_last_run(interval, time.time(), tick_failed)" in source


# ── #230 AFM-9: dropped distillation groups must be counted ──────────────────


def test_unreadable_distillation_file_is_counted_and_logged(tmp_path, caplog, monkeypatch):
    """AFM-9: the malformed-payload branch dropped the group with no counter
    and no log line, so how much input was discarded was unanswerable from any
    surface. Fails pre-R8: `_unreadable` was never counted."""
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


def test_distillation_reports_dropped_sections(tmp_path, monkeypatch):
    """AFM-9: sections dropped mid-distillation vanished on a bare `continue`.
    Fails pre-R8: `dropped_sections` did not exist in the result."""
    import minni.afm_passes.compact_distillation as mod

    monkeypatch.setattr(mod, "resolve_afm_mode", lambda: "off")
    inbox = tmp_path / "codex-vault" / "inbox"
    inbox.mkdir(parents=True)

    db_obj, cfg = _distill_db(tmp_path)
    res = mod.distill(db_obj, cfg, inboxes=[inbox], dry_run=True)
    assert "dropped_sections" in res


# ── GA6-2: health must carry an ingest-failure and inbox-backlog field ───────


def test_health_report_declares_consolidation_ingest_fields():
    """GA6-2: health had no ingest-failure field and no inbox-backlog field at
    all. Fails pre-R8: the key does not exist in the report."""
    from pathlib import Path

    import minni.minnid_runtime.health as health_mod

    source = Path(health_mod.__file__).read_text(encoding="utf-8")
    assert '"consolidation_ingest"' in source
    assert '"inbox_backlog"' in source


# ── Review round 1 (PR #260): the degrade flags must be thread-safe ─────────


def test_degrade_flags_are_thread_local_like_auth_suppression(tmp_path, monkeypatch):
    """The daemon hands every `search` RPC the SAME process-wide engine on its
    own worker thread. A plain instance attribute lets a concurrent request
    clear this one's flag between retrieve() returning and the handler reading
    it — reporting a degraded search as healthy, or pinning one caller's
    failure on another. last_auth_suppression already solved this; the new
    flags must use the same pattern."""
    import threading

    engine = _engine_without_model(tmp_path, monkeypatch)
    engine.last_rerank_degraded = "this thread's failure"
    engine.last_query_expand_degraded = "this thread's expand failure"

    seen = {}

    def _other_thread():
        # A concurrent request clearing its own flags, as retrieve() does on entry.
        engine.last_rerank_degraded = None
        engine.last_query_expand_degraded = None
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


def test_degrade_flags_work_on_instances_that_bypass_init():
    """Test fakes built via object.__new__ must still get a working store —
    the same lazy-init guarantee the auth-suppression setter makes."""
    from minni.retrieval import RetrievalEngine

    engine = object.__new__(RetrievalEngine)
    assert engine.last_rerank_degraded is None
    engine.last_rerank_degraded = "boom"
    assert engine.last_rerank_degraded == "boom"


# ── Review round 1: a degrade in ANY query variant degrades the merge ───────


def test_rerank_degrade_survives_a_later_healthy_variant(tmp_path, monkeypatch):
    """Each recursive retrieve() clears the flags on entry, so an early
    variant's failure was wiped by a later healthy one and the merged response
    reported a clean cross-corpus ordering it did not have. Auth suppressions
    were already aggregated for exactly this reason; these were not."""
    import minni.retrieval as retrieval_mod

    engine = _engine_without_model(tmp_path, monkeypatch)
    monkeypatch.setattr(
        retrieval_mod, "expand_query", lambda query, mode="rule": ["v1", "v2"]
    )

    # _filter_candidates runs once per variant on the single-variant path, so
    # it is a reliable place to stand in for "this variant degraded" without
    # needing a live reranker configured.
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


# ── Review round 1: the ingest health field must stay ingest-scoped ─────────


def test_consolidation_ingest_failures_exclude_other_subsystems():
    """A bare `_failures_total` suffix filter also swept in
    afm_pass_failures_total and afm_loop_tick_failures_total, so a synthesis
    pass fault flipped the INGEST field to failing — one subsystem's problem
    reported as another's."""
    from minni.minnid_runtime.health import CONSOLIDATION_FAILURE_COUNTERS

    assert "afm_pass_failures_total" not in CONSOLIDATION_FAILURE_COUNTERS
    assert "afm_loop_tick_failures_total" not in CONSOLIDATION_FAILURE_COUNTERS
    assert set(CONSOLIDATION_FAILURE_COUNTERS) == {
        "inbox_ingest_failures_total",
        "inbox_quarantine_failures_total",
        "inbox_archive_failures_total",
        "compact_distillation_failures_total",
    }


# ── helpers ──────────────────────────────────────────────────────────────────


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
    """A RetrievalEngine whose embedding model never loads.

    ``model`` is a lazy property that re-resolves through models.get_embedder,
    so the patch has to stay in place for the life of the test — clearing
    ``_model`` alone just triggers a real (network) model load.
    """
    import minni.db as db_mod
    import minni.models as models_mod
    import minni.retrieval as retrieval_mod
    from minni.config import SovereignConfig
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
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


def _fresh_db(db_mod, cfg):
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj


def _distill_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(
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
    from minni.config import SovereignConfig
    from minni.principal import EffectivePrincipal

    def _explode(db, config, **kwargs):
        raise error

    monkeypatch.setattr(consolidation_mod, "run", _explode)

    captured = {}

    def _make_response(payload, request_id):
        if isinstance(payload, dict):
            captured.update(payload)
        return {"result": payload}

    cfg = SovereignConfig(
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

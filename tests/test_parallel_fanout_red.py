"""Cassandra pong regression tests for perf/parallel-fanout (wright, fix-only).

RED-1: trace_id is carried in RetrievalCallState, never sampled across
  threads — concurrent same-engine legs each observe their own call's id.
RED-2: the cross-encoder lock is skipped ONLY under the pinned-CPU
  precondition (CPU device + fired torch-thread pin); every other path
  holds get_cross_encoder_lock().
YELLOW-1/YELLOW-2: the documented exception-path deltas — pool.map runs
  every variant/leg past the first throw, and a raising variant's partials
  die with its state (parent publishes entry-cleared + parent_expand reason)
  — asserted here as specified behavior, in both serial and parallel modes.
"""

import logging
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import test_parallel_fanout_parity as parity

import minni.models as models_mod
import minni.retrieval as retrieval_mod
import minni.minnid_runtime.recall as recall_mod
from minni.minnid_runtime.recall import RecallContext, handle_search


# ── shared fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def stubbed_engine(tmp_path, monkeypatch):
    """Real engine, real FTS/DB; no downloads; fixed 2 variants + parent reason."""
    engine, _db = parity._make_engine(tmp_path)
    parity._seed_docs(_db)

    monkeypatch.setattr(models_mod, "get_embedder", lambda: None)

    class _BoomReranker:
        def predict(self, *args, **kwargs):
            raise RuntimeError("reranker down for red test")

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


def _retrieve(engine, principal):
    return engine.retrieve(
        query="alpha beta gamma",
        limit=5,
        update_access=False,
        use_hyde=False,
        principal=principal,
        workspace="default",
    )


# ── RED-1: trace isolation ──────────────────────────────────────────────────


def test_concurrent_retrieve_trace_self_consistent(stubbed_engine):
    """Same engine, concurrent writer: this thread's published last_trace_id
    must equal the trace stamped on its own rows, never a sibling's.

    The poisoner hammers engine.last_trace_id in a tight loop while the
    GIL switch interval is pinned to 1 us, forcing thread switches inside
    the victim's microsecond store→read gap. Pre-fix (plain shared
    attribute) a poison write deterministically lands between the victim's
    store and its re-read, so the published id mismatches the rows.
    Post-fix the victim's id lives in its own RetrievalCallState and the
    poisoner's writes land in the poisoner's thread-local slot: equal
    always, intruder never — immune by construction, not by timing.
    """
    engine = stubbed_engine
    principal = parity._owner()
    stop = threading.Event()

    def poisoner():
        while not stop.is_set():
            engine.last_trace_id = "intruder-trace"

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    poison = threading.Thread(target=poisoner, name="trace-poisoner")
    poison.start()
    try:
        # 60 rounds: pre-fix corruption hits ~3%/round here, so an
        # unfixed tree fails this with ~85% probability per run (and CI
        # runs it every time); post-fix immunity is structural.
        for _ in range(60):
            rows = _retrieve(engine, principal)
            assert rows, "seeded docs must match for a non-vacuous test"
            stamped = {r.get("trace_id") for r in rows}
            assert len(stamped) == 1, f"one call, one trace: {stamped}"
            published = engine.last_trace_id
            assert published != "intruder-trace", "sibling write leaked in"
            assert published in stamped, (
                f"published {published} not in {stamped}"
            )
    finally:
        stop.set()
        poison.join(timeout=60)
        sys.setswitchinterval(old_interval)
    assert not poison.is_alive(), "poisoner hung"


def _real_engine(tmp_path, monkeypatch, name, docs):
    """Real stubbed engine like stubbed_engine, but single-variant capable
    and seeded with the given (path, text) docs."""
    engine, db = parity._make_engine(tmp_path / name)
    for path, text in docs:
        parity._insert_doc(db, path=path, text=text)
    monkeypatch.setattr(models_mod, "get_embedder", lambda: None)

    class _BoomReranker:
        def predict(self, *args, **kwargs):
            raise RuntimeError("reranker down for red test")

    engine._reranker = _BoomReranker()
    return engine


class _CounterRing:
    """Deterministic stand-in for the global trace ring: ids are per-run
    sequence numbers, so runs with a known shared-call order produce
    literally assertable envelope traces."""

    def __init__(self):
        self.n = 0
        self._lock = threading.Lock()

    def add(self, payload, owner=None):
        with self._lock:
            self.n += 1
            return f"test-trace-{self.n}"


def _both_harness(tmp_path, monkeypatch, run_tag, *, reuse_personal=False):
    """both-scope over REAL engines with a forced shared-call order.

    One healthy vault (combined = vault leg + shared tail, a 2-callable
    batch so the parallel reps genuinely cross leg workers) and a
    SLOW-failing personal vault (0.5 s): serially the fallback is
    shared-call #1, the vault #2, the tail #3; in parallel the personal
    fallback still runs first (both-scope sequencing), then the vault and
    tail race for #2/#3. Ring ids are scrubbed before comparison, so only
    the 3-trace count and envelope equality are asserted.
    """
    ring = _CounterRing()
    monkeypatch.setattr(retrieval_mod, "_trace_ring", lambda: ring)
    shared = _real_engine(
        tmp_path, monkeypatch, f"shared-{run_tag}",
        [("wiki/s1.md", "alpha beta shared ledger"),
         ("wiki/s2.md", "beta gamma shared ledger")],
    )
    personal = _real_engine(
        tmp_path, monkeypatch, f"personal-{run_tag}",
        [("wiki/p1.md", "alpha beta personal ledger")],
    )
    personal_retrieve_calls = []
    vault = _real_engine(
        tmp_path, monkeypatch, f"vault-{run_tag}",
        [("wiki/v1.md", "alpha beta vault ledger")],
    )

    if reuse_personal:
        original_personal_retrieve = personal.retrieve

        def _count_personal_retrieve(**kwargs):
            personal_retrieve_calls.append(None)
            return original_personal_retrieve(**kwargs)

        monkeypatch.setattr(personal, "retrieve", _count_personal_retrieve)
    else:
        def _slow_fail(**kwargs):
            time.sleep(0.5)
            raise RuntimeError("boom-personal")

        monkeypatch.setattr(personal, "retrieve", _slow_fail)
    principal = parity._owner()
    context = RecallContext(
        make_error=lambda code, msg, rid: {"ok": False, "id": rid, "error": [code, msg]},
        make_response=lambda payload, rid: {"ok": True, "id": rid, "result": payload},
        handler_principal=lambda params, rid: (principal, None),
        lazy_retrieval=lambda: shared,
        agent_vault_retrieval=lambda agent_id: (personal, "codex", "/db/personal.db"),
        all_vault_retrievals=lambda: [
            (personal, "codex", "/db/personal.db")
            if reuse_personal
            else (vault, "vault-one", "/db/vault-one.db")
        ],
        trace_ring=lambda: None,
        record_latency=lambda *a: None,
        increment_request_count=lambda: None,
        logger=logging.getLogger("test-red"),
    )
    return context, personal_retrieve_calls


def _both_params():
    return {"query": "alpha beta", "scope": "both", "limit": 5, "expand": False}


def _scrub_traces(payload):
    """Copy an envelope with every trace id normalized out."""
    import copy

    scrubbed = copy.deepcopy(payload)
    scrubbed["result"]["trace_id"] = "<trace>"
    ids = scrubbed["result"].get("trace_ids")
    if isinstance(ids, list):
        scrubbed["result"]["trace_ids"] = ["<trace>"] * len(ids)
    for row in scrubbed["result"]["results"]:
        if "trace_id" in row:
            row["trace_id"] = "<trace>"
    return scrubbed


def test_both_scope_parallel_trace_matches_serial(tmp_path, monkeypatch):
    """RED-1 + origin/main: both-scope records each successful retrieval
    trace. Singular ``trace_id`` is unset when more than one leg traced.
    Three legs trace here (personal fallback, vault, shared tail); ring
    ids may number in wall-clock order across the pooled vault/tail batch,
    but gather-order ``trace_ids`` still has three entries and never a
    stale handler slot. Scrubbed envelopes match.

    Deadline-free throughout: the parallel reps must genuinely cross leg
    workers (any stamped deadline forces the serial loops by the deadline
    guard, which would make the "parallel" side vacuous).
    """
    parity._unbounded_deadline(monkeypatch)
    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", False)
    serial = handle_search(
        _both_params(), 1, _both_harness(tmp_path, monkeypatch, "serial")[0]
    )
    assert serial["ok"] is True
    assert serial["result"]["trace_id"] is None, serial["result"]["trace_id"]
    assert len(serial["result"]["trace_ids"]) == 3, serial["result"]["trace_ids"]
    assert serial["result"]["trace_scope"] == "retrieval_leg"

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    for rep in range(3):
        parallel = handle_search(
            _both_params(), 1, _both_harness(tmp_path, monkeypatch, f"par{rep}")[0]
        )
        assert parallel["ok"] is True
        assert parallel["result"]["trace_id"] is None, parallel["result"]["trace_id"]
        assert len(parallel["result"]["trace_ids"]) == 3
        assert "stale" not in "".join(parallel["result"]["trace_ids"])
        # Everything but the per-run trace ids is identical to serial.
        assert _scrub_traces(parallel) == _scrub_traces(serial)


def test_both_scope_parallel_reuses_personal_snapshot(tmp_path, monkeypatch):
    """The pooled combined own-vault leg reuses the personal snapshot."""
    parity._unbounded_deadline(monkeypatch)
    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", False)
    serial_context, serial_calls = _both_harness(
        tmp_path, monkeypatch, "reuse-serial", reuse_personal=True
    )
    serial = handle_search(_both_params(), 1, serial_context)
    assert serial["ok"] is True
    assert len(serial_calls) == 1

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    parallel_context, parallel_calls = _both_harness(
        tmp_path, monkeypatch, "reuse-parallel", reuse_personal=True
    )
    parallel = handle_search(_both_params(), 1, parallel_context)
    assert parallel["ok"] is True
    assert len(parallel_calls) == 1
    assert _scrub_traces(parallel) == _scrub_traces(serial)


# ── RED-2: gated cross-encoder lock ─────────────────────────────────────────


@pytest.mark.parametrize(
    "device,pinned,expected",
    [
        ("cpu", True, True),
        ("CPU:0", True, True),
        ("cpu", False, False),
        ("mps", True, False),
        ("cuda", True, False),
        (None, True, False),
        ("", True, False),
    ],
)
def test_unlocked_gate_mapping(monkeypatch, device, pinned, expected):
    """The unlocked path requires CPU device, thread pin, and env pins."""
    monkeypatch.setenv("OMP_NUM_THREADS", "1")
    monkeypatch.setenv("MKL_NUM_THREADS", "1")
    # Isolated without touching the shared functools.cache: the gate reads
    # only _CROSS_ENCODER_CONSTRUCTION_DEVICE + _resolve_model_device() +
    # _TORCH_THREADS_PINNED, so construction-device None (the
    # pre-construction fallback) exercises the live-env mapping with no
    # cache_clear — clearing the process-wide model cache here would evict
    # a model cached by an earlier test and force a reload elsewhere.
    # Model-free: this path imports neither torch nor sentence-transformers.
    monkeypatch.setattr(models_mod, "_CROSS_ENCODER_CONSTRUCTION_DEVICE", None)
    monkeypatch.setattr(models_mod, "_resolve_model_device", lambda: device)
    monkeypatch.setattr(models_mod, "_TORCH_THREADS_PINNED", pinned)
    assert models_mod.cross_encoder_unlocked_predict_safe() is expected


@pytest.mark.parametrize("missing", ["OMP_NUM_THREADS", "MKL_NUM_THREADS"])
def test_unlocked_gate_requires_openmp_env_pins(monkeypatch, missing):
    """A CPU thread pin alone is insufficient without the native env pins."""
    monkeypatch.setattr(models_mod, "_CROSS_ENCODER_CONSTRUCTION_DEVICE", "cpu")
    monkeypatch.setattr(models_mod, "_TORCH_THREADS_PINNED", True)
    monkeypatch.setenv("OMP_NUM_THREADS", "1")
    monkeypatch.setenv("MKL_NUM_THREADS", "1")
    monkeypatch.delenv(missing)
    assert models_mod.cross_encoder_unlocked_predict_safe() is False


def test_rerank_holds_lock_unless_gate(monkeypatch, tmp_path):
    """_rerank must hold the cross-encoder lock exactly when the gate says
    the pinned-CPU precondition does not hold (the RED-2 invariant)."""
    engine, _db = parity._make_engine(tmp_path)

    class _FakeReranker:
        def predict(self, pairs, show_progress_bar=False):
            return [0.25] * len(pairs)

    engine._reranker = _FakeReranker()

    acquired = {"n": 0}

    class _RecordingLock:
        def acquire(self, blocking=True, timeout=-1):
            acquired["n"] += 1
            return True

        def release(self):
            return None

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    lock = _RecordingLock()
    monkeypatch.setattr(models_mod, "get_cross_encoder_lock", lambda: lock)

    def run_once(query, chunk_base):
        cands = [
            {"chunk_id": chunk_base, "chunk_text": "passage one", "heading_context": ""},
            {"chunk_id": chunk_base + 1, "chunk_text": "passage two", "heading_context": ""},
        ]
        return engine._rerank(query, cands)

    monkeypatch.setattr(models_mod, "cross_encoder_unlocked_predict_safe", lambda: False)
    out = run_once("red2-locked-probe", 770001)
    assert acquired["n"] == 1, "non-CPU path must hold the lock"
    assert [c["rerank_score"] for c in out] == [0.25, 0.25]

    monkeypatch.setattr(models_mod, "cross_encoder_unlocked_predict_safe", lambda: True)
    out = run_once("red2-unlocked-probe", 770101)
    assert acquired["n"] == 1, "pinned-CPU path must skip the lock"
    assert [c["rerank_score"] for c in out] == [0.25, 0.25]


def test_cross_encoder_concurrent_predict_matches_serial():
    """The committed stress probe (tests/_ce_stress_probe.py): CPU-only,
    skipped without a model — EVIDENCE, NOT A GATE (YELLOW round 2).

    The probe only runs with a cached reranker model (CI sets
    HF_HUB_OFFLINE=1 with no cache, so it skips there by design); the
    merge gate is the lock-routing + pin-precondition unit tests above,
    which run everywhere. When the model IS present the probe runs at the
    worst-case width the caps admit (24 concurrent predicts).

    Guards the RED-2 precondition's empirical leg — concurrent predict()
    byte-identical to serial on the pinned CPU path. Runs in a FRESH
    interpreter via subprocess: torch hard-aborts on import when this
    process's faiss libomp is already loaded, so it must never import here.
    """
    import subprocess

    probe = os.path.join(os.path.dirname(__file__), "_ce_stress_probe.py")
    env = dict(os.environ)
    env["HF_HUB_OFFLINE"] = "1"
    env["KMP_DUPLICATE_LIB_OK"] = "TRUE"
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("MKL_NUM_THREADS", "1")
    proc = subprocess.run(
        [sys.executable, probe],
        capture_output=True,
        text=True,
        timeout=600,
        env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 3:
        pytest.skip("torch not installed")
    if proc.returncode == 4:
        pytest.skip(f"no cached reranker model: {out.strip().splitlines()[-1:]}")
    assert proc.returncode == 0, out
    assert "VERDICT: SAFE" in (proc.stdout or ""), out


# ── YELLOW-1/YELLOW-2: documented exception-path deltas ─────────────────────


def _fts_bomb(engine, monkeypatch, *, fail_query, fail_delay=0.0,
              slow_query=None, slow_flag=None):
    orig = engine._fts_search

    def _wrapped(query, *args, **kwargs):
        if query == fail_query:
            # fail_delay>0 lets an already-started sibling complete first,
            # proving bounded over-execution; fail_delay=0 with a pending
            # sibling proves pending futures never start (3.14 map abort).
            time.sleep(fail_delay)
            raise RuntimeError(f"boom-variant {fail_query}")
        if slow_query is not None and query == slow_query:
            if slow_flag is not None:
                slow_flag.append(query)
        return orig(query, *args, **kwargs)

    monkeypatch.setattr(engine, "_fts_search", _wrapped)


def test_variant_raise_publishes_documented_partials(stubbed_engine, monkeypatch):
    """YELLOW-1: a raising variant's partials die with its state. The parent
    publishes entry-cleared flags plus the pre-recursion parent_expand
    reason — NOT the raising variant's incremental writes (the serial
    observable). Same in serial and parallel mode."""
    for parallel in (False, True):
        monkeypatch.setattr(
            retrieval_mod, "RETRIEVAL_VARIANT_PARALLEL", parallel
        )
        _fts_bomb(stubbed_engine, monkeypatch, fail_query="gamma delta")
        with pytest.raises(RuntimeError, match="boom-variant"):
            _retrieve(stubbed_engine, parity._owner())
        assert stubbed_engine.last_query_expand_degraded == "afm_unavailable_for_test"
        assert stubbed_engine.last_rerank_degraded is None
        assert stubbed_engine.last_vector_degraded is None
        assert stubbed_engine.last_hyde_degraded is None
        assert stubbed_engine.last_auth_suppression is None
        assert stubbed_engine.last_trace_id is None


def test_variant_overexecution_documents_first_raise_wins(stubbed_engine, monkeypatch):
    """YELLOW-2: the FIRST variant's exception propagates (serial raise) in
    both modes. An already-started sibling completes (bounded
    over-execution, benign: variant bodies are read-only); serial mode never
    starts the later variant. Variant 0 raises slowly so sibling 1 is
    already running when the gather aborts."""
    expected = {}
    for parallel, must_run in ((False, False), (True, True)):
        monkeypatch.setattr(
            retrieval_mod, "RETRIEVAL_VARIANT_PARALLEL", parallel
        )
        ran = []
        _fts_bomb(
            stubbed_engine,
            monkeypatch,
            fail_query="alpha beta",
            fail_delay=0.3,
            slow_query="gamma delta",
            slow_flag=ran,
        )
        with pytest.raises(RuntimeError, match="boom-variant alpha beta"):
            _retrieve(stubbed_engine, parity._owner())
        assert (ran != []) is must_run, f"parallel={parallel}: ran={ran}"
        expected[parallel] = list(ran)
    assert expected[True] == ["gamma delta"]
    assert expected[False] == []


def test_both_scope_combined_throw_is_32000_both_modes(monkeypatch):
    """YELLOW-1, recall level: a combined throw discards the personal leg's
    un-replayed ops — unobservable, because both modes land in the outer
    except as -32000 with no per-leg diagnostics."""
    context, _calls = parity._recall_harness(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("boom-combined-merge")

    monkeypatch.setattr(recall_mod, "merge_document_results", _boom)
    outcomes = {}
    for parallel in (False, True):
        monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", parallel)
        out = handle_search({"query": "q", "scope": "both", "limit": 5}, 1, context)
        assert out["ok"] is False
        assert out["error"][0] == -32000, out
        outcomes[parallel] = out
    assert outcomes[True] == outcomes[False]


# ── YELLOW round 2 pong ───────────────────────────────────────────────────


def test_variant_abort_envelope_equal_despite_race(
    stubbed_engine, monkeypatch
):
    """Round-2 item 1: the fate of a QUEUED sibling when the gather aborts
    is a scheduling race — abandoning the map iterator cancels
    not-yet-started futures (iterator finally), but a worker that wins the
    race starts the sibling first and it then runs to completion. Probed on
    3.14 both ways: immediate-raise + single worker cancels 100/100 trials,
    while a GIL yield in the body (or a slow raise) lets the worker win and
    the queued siblings run. Neither "always runs" nor "never runs" is a
    sound contract. The deterministic invariant, pinned here over 10 trials
    per mode with a single worker and 3 variants (variant 0 raises
    immediately, leaving 1-2 queued at abort): the FIRST variant's error
    propagates — the serial envelope — in EVERY trial, and serial mode
    never starts later siblings. Already-running siblings completing is
    covered by the YELLOW-2 over-execution test (2 workers + slow raise)."""
    monkeypatch.setattr(retrieval_mod, "_MAX_VARIANT_WORKERS", 1)
    monkeypatch.setattr(
        retrieval_mod,
        "expand_query_with_status",
        lambda query, mode="rule": (
            ["boom now", "slow one", "slow two"],
            "afm_unavailable_for_test",
        ),
    )
    envelopes: list = []
    for _trial in range(10):
        for parallel in (False, True):
            monkeypatch.setattr(
                retrieval_mod, "RETRIEVAL_VARIANT_PARALLEL", parallel
            )
            ran: list = []
            orig = stubbed_engine._fts_search

            def _wrapped(query, *args, **kwargs):
                if query == "boom now":
                    raise RuntimeError("boom-variant boom now")
                if query in ("slow one", "slow two"):
                    ran.append(query)
                return orig(query, *args, **kwargs)

            monkeypatch.setattr(stubbed_engine, "_fts_search", _wrapped)
            with pytest.raises(RuntimeError) as excinfo:
                _retrieve(stubbed_engine, parity._owner())
            envelopes.append(
                (parallel, type(excinfo.value).__name__, str(excinfo.value))
            )
            if not parallel:
                assert ran == [], "serial never starts later siblings"
            else:
                # Queued outcome is timing-dependent; whatever ran is valid.
                assert set(ran) <= {"slow one", "slow two"}
    serial_env = {(t, m) for p, t, m in envelopes if not p}
    parallel_env = {(t, m) for p, t, m in envelopes if p}
    assert serial_env == {("RuntimeError", "boom-variant boom now")}
    assert parallel_env == serial_env, (
        "first-error envelope must hold every trial, either race outcome: "
        f"{parallel_env}"
    )


def test_both_scope_failed_shared_envelope_trace_is_none_not_stale(monkeypatch):
    """Round-2 item 2: a shared leg that RAN and failed publishes None (the
    partial), never the handler thread's slot — on reused threads that slot
    holds a PREVIOUS request's id. The never-ran sentinel keeps its slot
    fallback; failure must not."""
    context, _calls = parity._recall_harness(monkeypatch)
    shared = context.lazy_retrieval()
    shared._fail = True
    shared.last_trace_id = "stale-previous-request-trace"
    for parallel in (False, True):
        monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", parallel)
        out = handle_search({"query": "q", "scope": "both", "limit": 5}, 1, context)
        assert out["ok"] is True, out
        traces = out["result"].get("trace_ids") or []
        assert "stale-previous-request-trace" not in traces, traces
        assert out["result"]["trace_id"] != "stale-previous-request-trace"
        assert any(t.startswith("trace-personal-") for t in traces), traces
        assert not any(t.startswith("trace-shared-") for t in traces), traces
        assert out["result"]["degraded"] is True, out
        assert any(
            d.get("shared_index_failed") for d in out["result"]["degradation"]
        ), out["result"]["degradation"]


def test_pin_gate_answers_construction_device_both_orderings(monkeypatch):
    """Round-2 item 3 (TOCTOU) + round-3 auto-sentinel: the gate answers
    for the device the singleton was BUILT with, not the live env.
    MPS-build then env→cpu must stay locked; cpu-build then env→mps must
    stay unlocked-safe; AUTO-build (no device= kwarg, standard MPS route)
    then env→cpu must stay locked. The pin flag is forced fired in all
    orderings so only the device answer varies."""
    import types

    seen: dict = {}

    class _FakeCE:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", types.SimpleNamespace(
            CrossEncoder=_FakeCE
        ),
    )
    monkeypatch.setattr(
        models_mod, "_pin_torch_threads_for_cpu_once", lambda device: None
    )
    monkeypatch.setattr(models_mod, "_TORCH_THREADS_PINNED", True)
    try:
        # Ordering 1: built on MPS, env later flips to cpu → locked.
        monkeypatch.setenv("MINNI_MODEL_DEVICE", "mps")
        monkeypatch.setattr(
            models_mod, "_CROSS_ENCODER_CONSTRUCTION_DEVICE", None
        )
        models_mod.get_cross_encoder.cache_clear()
        assert models_mod.get_cross_encoder() is not None
        assert seen.get("device") == "mps"
        monkeypatch.setenv("MINNI_MODEL_DEVICE", "cpu")
        assert models_mod.cross_encoder_unlocked_predict_safe() is False
        # Ordering 2: built on CPU, env later flips to mps → unlocked-safe.
        seen.clear()
        monkeypatch.setenv("MINNI_MODEL_DEVICE", "cpu")
        monkeypatch.setattr(
            models_mod, "_CROSS_ENCODER_CONSTRUCTION_DEVICE", None
        )
        models_mod.get_cross_encoder.cache_clear()
        assert models_mod.get_cross_encoder() is not None
        assert seen.get("device") == "cpu"
        monkeypatch.setenv("MINNI_MODEL_DEVICE", "mps")
        assert models_mod.cross_encoder_unlocked_predict_safe() is True
        # Ordering 3 (YELLOW round 3): auto build (no device= kwarg, the
        # standard MPS route), env later set to cpu → stays locked. The
        # build must stash the 'auto' sentinel, never None, so the gate
        # cannot fall back to the live cpu resolution.
        seen.clear()
        monkeypatch.delenv("MINNI_MODEL_DEVICE", raising=False)
        _orig_resolve = models_mod._resolve_model_device
        monkeypatch.setattr(models_mod, "_resolve_model_device", lambda: None)
        monkeypatch.setattr(
            models_mod, "_CROSS_ENCODER_CONSTRUCTION_DEVICE", None
        )
        models_mod.get_cross_encoder.cache_clear()
        assert models_mod.get_cross_encoder() is not None
        assert "device" not in seen, seen
        assert models_mod._CROSS_ENCODER_CONSTRUCTION_DEVICE == "auto"
        monkeypatch.setattr(
            models_mod, "_resolve_model_device", _orig_resolve
        )
        monkeypatch.setenv("MINNI_MODEL_DEVICE", "cpu")
        assert models_mod.cross_encoder_unlocked_predict_safe() is False
    finally:
        models_mod.get_cross_encoder.cache_clear()


# ── leg-gather precise semantics (finding 4) ──────────────────────────────
#
# Production legs soft-fail by construction (per-vault try/except plus soft
# shared tails), so a raising pooled leg is unreachable via handle_search;
# these pin the defensive path directly against the extracted helper. No
# serial abort is claimed for the pool: every leg is submitted eagerly,
# the gather waits for the slowest STARTED sibling, and the
# submission-order-first error propagates (the serial raise's identity).
# Serial mode is pinned alongside as the honest contrast — it never starts
# legs past a raise.


def _leg_ok(marks, name, delay=0.0):
    def _run():
        if delay:
            time.sleep(delay)
        marks.append(name)
        return name

    return _run


def _leg_boom(name, delay=0.0):
    def _run():
        if delay:
            time.sleep(delay)
        raise RuntimeError(f"boom-leg {name}")

    return _run


def test_leg_gather_first_error_wins_both_modes(monkeypatch):
    """Two raisers: submission-order-first error, serial and parallel."""
    for parallel in (False, True):
        monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", parallel)
        with pytest.raises(RuntimeError, match="boom-leg A"):
            recall_mod._gather_leg_results(
                [_leg_boom("A"), _leg_boom("B")], None
            )


def test_leg_gather_waits_slowest_started_sibling(monkeypatch):
    """A fast raise does not shortcut a started sibling: the gather (plus
    the pool join on context exit) waits for it, and the started sibling
    runs to completion. Serial mode raises the same error but never starts
    the later leg — that never-starts-later contract belongs to serial
    ONLY and is asserted here so no reader infers it for the pool."""
    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    marks: list = []
    t0 = time.perf_counter()
    # The raiser waits 0.2 s so the sibling is deterministically STARTED
    # (worker spawn is millisecond-scale) before the gather aborts — the
    # fate of a never-started leg would be a scheduling race, not a pin.
    with pytest.raises(RuntimeError, match="boom-leg fast"):
        recall_mod._gather_leg_results(
            [_leg_boom("fast", delay=0.2), _leg_ok(marks, "slow", delay=0.5)],
            None,
        )
    elapsed = time.perf_counter() - t0
    assert marks == ["slow"], "started sibling must run to completion"
    assert elapsed >= 0.4, f"gather must wait the slowest sibling: {elapsed:.3f}s"

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", False)
    serial_marks: list = []
    with pytest.raises(RuntimeError, match="boom-leg fast"):
        recall_mod._gather_leg_results(
            [_leg_boom("fast"), _leg_ok(serial_marks, "slow", delay=0.5)], None
        )
    assert serial_marks == [], "serial never starts legs past a raise"


def test_leg_gather_slow_first_leg_delays_fast_raise(monkeypatch):
    """Submission order, not wall-clock order: a slow SUCCESSFUL first leg
    is awaited before the second leg's (already known) failure surfaces."""
    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    marks: list = []
    t0 = time.perf_counter()
    with pytest.raises(RuntimeError, match="boom-leg second"):
        recall_mod._gather_leg_results(
            [_leg_ok(marks, "first", delay=0.5), _leg_boom("second")], None
        )
    elapsed = time.perf_counter() - t0
    assert marks == ["first"]
    assert elapsed >= 0.4, f"gather yields in submission order: {elapsed:.3f}s"


def test_leg_gather_deadline_guard_skips_pool(monkeypatch):
    """Any supplied deadline (what every RPC carries) forces the serial
    loop even with the parallel flag on: a later leg never starts past an
    earlier raise. Deadline-free callers still get the pool."""
    import concurrent.futures

    created: list = []
    real_pool = concurrent.futures.ThreadPoolExecutor

    class _SpyPool(real_pool):
        def __init__(self, *args, **kwargs):
            created.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _SpyPool)
    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    # Supplied deadline: serial loop, no pool, later leg never starts.
    serial_marks: list = []
    with pytest.raises(RuntimeError, match="boom-leg fast"):
        recall_mod._gather_leg_results(
            [_leg_boom("fast"), _leg_ok(serial_marks, "slow")],
            time.monotonic() + 25.0,
        )
    assert serial_marks == []
    assert created == [], "a supplied deadline must not engage the pool"
    # Deadline-free: the pool engages for multi-leg batches.
    assert recall_mod._gather_leg_results(
        [lambda: "a", lambda: "b"], None
    ) == ["a", "b"]
    assert len(created) >= 1
    # Single-leg batches skip the pool either way.
    created.clear()
    assert recall_mod._gather_leg_results([lambda: "solo"], None) == ["solo"]
    assert created == []

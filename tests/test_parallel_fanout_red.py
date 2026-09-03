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

    def add(self, payload, owner=None):
        self.n += 1
        return f"test-trace-{self.n}"


def _both_harness(tmp_path, monkeypatch, run_tag):
    """both-scope over REAL engines with a forced shared-call order.

    No vaults (combined = shared tail only) and a SLOW-failing personal
    vault (0.5 s): serially the fallback is shared-call #1 and the tail #2;
    in parallel the tail wins the race (#1) and the fallback is #2. The
    0.5 s margins make scheduling jitter unable to flip either numbering,
    so each mode's envelope can assert the tail-wins rule literally.
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
        all_vault_retrievals=lambda: [],
        trace_ring=lambda: None,
        record_latency=lambda *a: None,
        increment_request_count=lambda: None,
        logger=logging.getLogger("test-red"),
    )
    return context


def _both_params():
    return {"query": "alpha beta", "scope": "both", "limit": 5, "expand": False}


def _scrub_traces(payload):
    """Copy an envelope with every trace id normalized out."""
    import copy

    scrubbed = copy.deepcopy(payload)
    scrubbed["result"]["trace_id"] = "<trace>"
    for row in scrubbed["result"]["results"]:
        if "trace_id" in row:
            row["trace_id"] = "<trace>"
    return scrubbed


def test_both_scope_parallel_trace_matches_serial(tmp_path, monkeypatch):
    """RED-1, recall level: the both-scope envelope must carry the combined
    TAIL's trace — the serially-last shared leg — in serial AND parallel
    mode. The fallback ran too, on the same engine; carrying its id, or the
    gathering thread's stale slot, fails this test. Serially the tail is
    shared-call #2; in parallel it wins the race (#1) — same rule, both
    asserted literally."""
    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", False)
    serial = handle_search(
        _both_params(), 1, _both_harness(tmp_path, monkeypatch, "serial")
    )
    assert serial["ok"] is True
    assert serial["result"]["trace_id"] == "test-trace-2", serial["result"]["trace_id"]

    monkeypatch.setattr(recall_mod, "RECALL_LEG_PARALLEL", True)
    for rep in range(3):
        parallel = handle_search(
            _both_params(), 1, _both_harness(tmp_path, monkeypatch, f"par{rep}")
        )
        assert parallel["ok"] is True
        assert parallel["result"]["trace_id"] == "test-trace-1", (
            parallel["result"]["trace_id"]
        )
        # Everything but the per-run trace ids is identical to serial.
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
    """The unlocked path requires CPU device AND a fired pin, nothing else."""
    monkeypatch.setattr(models_mod, "_resolve_model_device", lambda: device)
    monkeypatch.setattr(models_mod, "_TORCH_THREADS_PINNED", pinned)
    assert models_mod.cross_encoder_unlocked_predict_safe() is expected


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
        def __enter__(self):
            acquired["n"] += 1
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(models_mod, "get_cross_encoder_lock", _RecordingLock)

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
        trace = out["result"]["trace_id"]
        assert trace is None, f"parallel={parallel}: stale envelope {trace!r}"
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

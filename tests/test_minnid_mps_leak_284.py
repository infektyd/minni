"""Behavioral pins for #284: encode/predict serialization, progress-bar silence,
footprint watchdog, and health surfacing.

These are OBSERVED-behavior tests (threads, kwargs spies, injected caps) —
not source-grep assertions.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import List

import numpy as np


# ── Shared fakes ──────────────────────────────────────────────────────────


class _RecordingEncodeModel:
    """Fake SentenceTransformer: sleeps, records kwargs, tracks concurrency."""

    def __init__(self, sleep_s: float = 0.05):
        self.sleep_s = sleep_s
        self.lock = threading.Lock()
        self.concurrent = 0
        self.max_concurrent = 0
        self.calls: List[dict] = []

    def encode(self, *args, **kwargs):
        with self.lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            self.calls.append(dict(kwargs))
        try:
            time.sleep(self.sleep_s)
            return np.zeros(384, dtype=np.float32)
        finally:
            with self.lock:
                self.concurrent -= 1


class _RecordingPredictModel:
    def __init__(self):
        self.calls: List[dict] = []

    def predict(self, *args, **kwargs):
        self.calls.append(dict(kwargs))
        return [0.5]


# ── 1. Concurrency / serialization ───────────────────────────────────────


def test_embedder_lock_serializes_concurrent_encode():
    """N threads through a production lock-wrapped path must never overlap."""
    from minni.chunker import MarkdownChunker

    model = _RecordingEncodeModel(sleep_s=0.04)
    n = 8
    barrier = threading.Barrier(n)
    errors: List[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=5)
            MarkdownChunker._encode_for_merge(model, "serialize-me")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert len(model.calls) == n
    assert model.max_concurrent == 1, (
        f"expected serialized encode, saw max_concurrent={model.max_concurrent}"
    )


def test_unlocked_encode_overlaps_without_lock():
    """Control: without the lock, the same fake model overlaps under N threads."""
    model = _RecordingEncodeModel(sleep_s=0.04)
    n = 8
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait(timeout=5)
        model.encode("overlap-me")

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert model.max_concurrent > 1, (
        f"control failed: expected overlap without lock, got max={model.max_concurrent}"
    )


# ── 2. show_progress_bar=False (kwargs spy, not source grep) ─────────────


def test_chunker_encode_passes_show_progress_bar_false():
    from minni.chunker import MarkdownChunker

    model = _RecordingEncodeModel(sleep_s=0.0)
    MarkdownChunker._encode_for_merge(model, "quiet")
    assert model.calls, "encode was not called"
    assert model.calls[0].get("show_progress_bar") is False


def test_writeback_store_learning_passes_show_progress_bar_false(tmp_path):
    from dataclasses import replace

    from minni.config import DEFAULT_CONFIG
    from minni.db import SovereignDB
    from minni.writeback import WriteBackMemory

    cfg = replace(
        DEFAULT_CONFIG,
        db_path=str(tmp_path / "wb.db"),
        vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "learnings"),
    )
    (tmp_path / "vault").mkdir()
    (tmp_path / "learnings").mkdir()
    db = SovereignDB(cfg)
    model = _RecordingEncodeModel(sleep_s=0.0)

    class _WB(WriteBackMemory):
        @property
        def model(self):
            return model

    wb = _WB(db, cfg)
    wb.store_learning(agent_id="cursor", content="a durable finding about #284")
    assert model.calls, "encode was not called"
    assert model.calls[0].get("show_progress_bar") is False


def test_retrieval_score_attribution_passes_show_progress_bar(tmp_path):
    from dataclasses import replace

    from minni.config import DEFAULT_CONFIG
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = replace(
        DEFAULT_CONFIG,
        db_path=str(tmp_path / "r.db"),
        vault_path=str(tmp_path / "vault"),
        attribution_enabled=True,
    )
    (tmp_path / "vault").mkdir()
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    pred = _RecordingPredictModel()
    engine._attribution_model = pred

    engine._score_attribution("claim text", "evidence text")
    assert pred.calls, "attribution predict was not called"
    assert pred.calls[0].get("show_progress_bar") is False


def test_retrieval_rerank_passes_show_progress_bar(tmp_path):
    from dataclasses import replace

    from minni.config import DEFAULT_CONFIG
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = replace(
        DEFAULT_CONFIG,
        db_path=str(tmp_path / "r2.db"),
        vault_path=str(tmp_path / "vault"),
        reranker_enabled=True,
    )
    (tmp_path / "vault").mkdir()
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    pred = _RecordingPredictModel()
    engine._reranker = pred

    candidates = [
        {"chunk_id": 1, "chunk_text": "alpha", "heading_context": ""},
        {"chunk_id": 2, "chunk_text": "beta", "heading_context": "H"},
    ]
    engine._rerank("query", candidates)
    assert pred.calls, "reranker predict was not called"
    assert pred.calls[0].get("show_progress_bar") is False


def test_retrieval_encode_query_passes_show_progress_bar(tmp_path):
    from dataclasses import replace

    from minni.config import DEFAULT_CONFIG
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = replace(
        DEFAULT_CONFIG,
        db_path=str(tmp_path / "r3.db"),
        vault_path=str(tmp_path / "vault"),
    )
    (tmp_path / "vault").mkdir()
    db = SovereignDB(cfg)
    model = _RecordingEncodeModel(sleep_s=0.0)

    class _Eng(RetrievalEngine):
        @property
        def model(self):
            return model

    eng = _Eng(db, cfg)
    vec = eng._encode_query("realistic recall query about memory")
    assert vec.shape == (384,)
    assert model.calls[0].get("show_progress_bar") is False


# ── 3. Footprint watchdog ────────────────────────────────────────────────


def test_read_watchdog_state_missing_defaults(tmp_path):
    from minni.minnid import _read_watchdog_state

    state = _read_watchdog_state(tmp_path / "missing.json")
    assert state["restart_count"] == 0
    assert state["last_restart_reason"] is None


def test_read_watchdog_state_corrupt_defaults(tmp_path):
    from minni.minnid import _read_watchdog_state

    path = tmp_path / "watchdog_state.json"
    path.write_text("{not-json", encoding="utf-8")
    state = _read_watchdog_state(path)
    assert state["restart_count"] == 0


def test_footprint_exceeds_cap_arithmetic():
    from minni.minnid import _footprint_exceeds_cap

    assert _footprint_exceeds_cap(5, 4) is True
    assert _footprint_exceeds_cap(4, 4) is False
    assert _footprint_exceeds_cap(3, 4) is False


def test_footprint_cap_bytes_defensive(monkeypatch):
    from minni.minnid import _DEFAULT_FOOTPRINT_CAP_MB, _footprint_cap_bytes

    monkeypatch.delenv("MINNI_FOOTPRINT_CAP_MB", raising=False)
    assert _footprint_cap_bytes() == _DEFAULT_FOOTPRINT_CAP_MB * 1024 * 1024

    monkeypatch.setenv("MINNI_FOOTPRINT_CAP_MB", "not-a-number")
    assert _footprint_cap_bytes() == _DEFAULT_FOOTPRINT_CAP_MB * 1024 * 1024

    monkeypatch.setenv("MINNI_FOOTPRINT_CAP_MB", "1")
    assert _footprint_cap_bytes() == 1 * 1024 * 1024


def test_watchdog_tick_trips_writes_state_and_calls_shutdown(tmp_path, caplog):
    from minni.minnid import _footprint_watchdog_tick, _read_watchdog_state

    state_path = tmp_path / "watchdog_state.json"
    trips: List[str] = []

    with caplog.at_level(logging.WARNING):
        reason = _footprint_watchdog_tick(
            measure=lambda: 50 * 1024 * 1024,
            cap_bytes=1 * 1024 * 1024,
            state_path=state_path,
            on_trip=lambda r: trips.append(r),
        )

    assert reason is not None
    assert "footprint_cap_exceeded" in reason
    assert "50MB" in reason and "1MB" in reason
    assert trips == [reason]
    assert state_path.exists()
    state = _read_watchdog_state(state_path)
    assert state["restart_count"] == 1
    assert "footprint_cap_exceeded" in (state["last_restart_reason"] or "")
    assert state["last_restart_at"]


def test_watchdog_tick_under_cap_is_noop(tmp_path):
    from minni.minnid import _footprint_watchdog_tick, _read_watchdog_state

    state_path = tmp_path / "watchdog_state.json"
    trips: List[str] = []
    reason = _footprint_watchdog_tick(
        measure=lambda: 100,
        cap_bytes=1000,
        state_path=state_path,
        on_trip=lambda r: trips.append(r),
    )
    assert reason is None
    assert trips == []
    assert not state_path.exists()
    assert _read_watchdog_state(state_path)["restart_count"] == 0


def test_watchdog_runner_trips_and_shuts_down(tmp_path):
    """Async runner with tiny interval/cap: trip → on_trip → _running False."""
    from minni import minnid as minnid_mod
    from minni.minnid import _footprint_watchdog_runner

    state_path = tmp_path / "watchdog_state.json"
    trips: List[str] = []

    minnid_mod._running = True

    def on_trip(reason: str):
        trips.append(reason)
        minnid_mod._initiate_graceful_shutdown(
            f"Footprint watchdog: {reason} — shutting down for launchd restart",
            tasks=[],
        )

    async def _run():
        await _footprint_watchdog_runner(
            measure=lambda: 10 * 1024 * 1024,
            cap_bytes=1 * 1024 * 1024,
            state_path=state_path,
            interval=0.01,
            on_trip=on_trip,
        )

    asyncio.run(_run())

    assert trips, "watchdog never tripped"
    assert "footprint_cap_exceeded" in trips[0]
    assert minnid_mod._running is False
    state = minnid_mod._read_watchdog_state(state_path)
    assert state["restart_count"] >= 1


def test_current_footprint_bytes_is_positive_int():
    from minni.minnid import _current_footprint_bytes

    n = _current_footprint_bytes()
    assert isinstance(n, int)
    assert n > 0


# ── 4. Health / status surfacing ─────────────────────────────────────────


def _minimal_health_context(**overrides):
    from minni.minnid_runtime.health import HealthContext

    base = dict(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}},
        make_response=lambda result, rid: {"result": result},
        guard_vault_root=lambda *a, **k: None,
        latency_snapshot=lambda: {},
        metrics_snapshot=lambda: {},
        afm_loop_enabled=lambda cfg: False,
    )
    base.update(overrides)
    return HealthContext(**base)


def test_status_includes_watchdog_state_from_file(tmp_path):
    from minni.minnid import _read_watchdog_state, _record_watchdog_trip
    from minni.minnid_runtime.health import handle_status

    state_path = tmp_path / "watchdog_state.json"
    _record_watchdog_trip(
        "footprint_cap_exceeded: 5000MB > 4096MB cap",
        path=state_path,
    )
    ctx = _minimal_health_context(
        watchdog_state=lambda: _read_watchdog_state(state_path),
    )
    resp = handle_status({"vault": str(tmp_path)}, 1, ctx)
    wd = resp["result"]["daemon"]["footprint_watchdog"]
    assert wd["restart_count"] == 1
    assert "footprint_cap_exceeded" in (wd["last_restart_reason"] or "")


def test_status_watchdog_defaults_when_no_state_file(tmp_path):
    from minni.minnid import _read_watchdog_state
    from minni.minnid_runtime.health import handle_status

    missing = tmp_path / "nope.json"
    ctx = _minimal_health_context(
        watchdog_state=lambda: _read_watchdog_state(missing),
    )
    resp = handle_status({"vault": str(tmp_path)}, 1, ctx)
    wd = resp["result"]["daemon"]["footprint_watchdog"]
    assert wd["restart_count"] == 0
    assert wd["last_restart_reason"] is None


def test_health_report_includes_watchdog(tmp_path):
    from minni.minnid import _read_watchdog_state, _record_watchdog_trip
    from minni.minnid_runtime.health import handle_health_report
    from minni.principal import EffectivePrincipal

    state_path = tmp_path / "watchdog_state.json"
    _record_watchdog_trip("footprint_cap_exceeded: 9MB > 1MB cap", path=state_path)

    principal = EffectivePrincipal(
        agent_id="operator",
        capabilities=["govern", "recall", "learn", "*"],
    )
    ctx = _minimal_health_context(
        watchdog_state=lambda: _read_watchdog_state(state_path),
    )
    resp = handle_health_report(
        {"vault": str(tmp_path), "_recovery": False, "_principal": principal},
        1,
        ctx,
    )
    result = resp["result"]
    assert "footprint_watchdog" in result
    assert result["footprint_watchdog"]["restart_count"] == 1


# ── 5. Device config pin (daemon setdefault contract) ────────────────────


def test_resolve_model_device_respects_env(monkeypatch):
    from minni.models import _resolve_model_device

    monkeypatch.delenv("MINNI_MODEL_DEVICE", raising=False)
    _ = _resolve_model_device()

    monkeypatch.setenv("MINNI_MODEL_DEVICE", "cpu")
    assert _resolve_model_device() == "cpu"

    monkeypatch.setenv("MINNI_MODEL_DEVICE", "mps")
    assert _resolve_model_device() == "mps"


def test_model_device_config_field_exists():
    from minni.config import SovereignConfig

    assert "model_device" in SovereignConfig.__dataclass_fields__

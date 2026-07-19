"""W2 — Health opacity + errors.search traceback attribution (punch-list §4).

Covers the five additively-composable sub-fixes:
  (a) status surfaces daemon pid + started_at (restart visibility);
  (b) daemon VERSION resolves dynamically from importlib.metadata (pyproject is
      authoritative) with a non-crashing fallback;
  (c) counters surface deltas-since-previous-snapshot + a health_flags list
      instead of bare monotonic ints;
  (d) an in-band, govern-gated cache_reload RPC (tested in test_provenance_gate);
  (e) errors.search increments record the exception class+message into a bounded
      ring buffer, exposed in health_report behind the same operator gate that
      already redacts stale_docs/never_recalled/contradicting_learnings.

Hermetic: SovereignDB is stubbed and no real ~/.minni is opened.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minni.minnid as minnid  # type: ignore
import minni.obs as obs  # type: ignore


# ── (b) dynamic VERSION ──────────────────────────────────────────────────
def test_version_resolves_dynamically_from_importlib_metadata():
    dist_version = importlib.metadata.version("minni")
    # pyproject.toml is the authoritative value; the installed dist mirrors it.
    assert minnid.VERSION == dist_version
    # The dead "0.1.0" literal that never tracked pyproject must be gone.
    assert minnid.VERSION != "0.1.0"


def test_version_falls_back_when_dist_metadata_missing(monkeypatch):
    def _raise(_name):
        raise importlib.metadata.PackageNotFoundError("minni")

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    resolved = minnid._resolve_version()
    # From-source / non-installed path: reads pyproject.toml relative to the
    # module (not cwd) and never crashes on missing dist metadata.
    assert isinstance(resolved, str) and resolved
    assert resolved not in ("0.1.0", "unknown")


# ── (a) status pid + started_at ──────────────────────────────────────────
def test_status_reports_pid_and_started_at(monkeypatch):
    monkeypatch.setattr(minnid, "_request_count", 0)
    obs.METRICS.reset()
    daemon = minnid._handle_status({}, 1)["result"]["daemon"]
    assert daemon["pid"] == os.getpid()
    assert "started_at" in daemon
    # started_at is a parseable ISO8601 wall-clock timestamp.
    datetime.fromisoformat(daemon["started_at"])
    obs.METRICS.reset()


# ── (c) counter deltas + health_flags ────────────────────────────────────
def test_metrics_delta_snapshot_reports_change_since_previous():
    counters = obs.Counters()
    counters.incr("errors.search", 2)
    first = counters.delta_snapshot()
    assert first["errors.search"] == {"total": 2, "delta": 2}
    # No new increments → delta collapses to 0, cumulative total holds.
    second = counters.delta_snapshot()
    assert second["errors.search"] == {"total": 2, "delta": 0}
    counters.incr("errors.search", 5)
    third = counters.delta_snapshot()
    assert third["errors.search"] == {"total": 7, "delta": 5}


def test_health_flags_raise_only_after_threshold_crossed():
    # Below threshold: a single new error since last snapshot is not "rising".
    below = {"errors.search": {"total": 1, "delta": 1}}
    assert "errors_search_rising" not in obs.health_flags(below)
    # At/above threshold: the named boolean flag is raised.
    above = {"errors.search": {"total": 9, "delta": 9}}
    assert "errors_search_rising" in obs.health_flags(above)


def test_status_surfaces_counter_deltas_and_health_flags(monkeypatch):
    monkeypatch.setattr(minnid, "_request_count", 0)
    obs.METRICS.reset()
    for _ in range(9):
        obs.incr("errors.search")
    daemon = minnid._handle_status({}, 1)["result"]["daemon"]
    assert daemon["counter_deltas"]["errors.search"]["total"] == 9
    assert daemon["counter_deltas"]["errors.search"]["delta"] == 9
    assert "errors_search_rising" in daemon["health_flags"]
    obs.METRICS.reset()


# ── (e) exception ring buffer ────────────────────────────────────────────
def _dispatch_context(methods, obs_module):
    from minni.minnid_runtime.dispatch import DispatchContext
    from minni.minnid_runtime.rpc import make_error, make_response

    class _Logger:
        def exception(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

    return DispatchContext(
        methods=methods,
        recovery_allowed_methods=frozenset(),
        resolve_provenance=lambda request: type(
            "R", (), {"recovery": None, "principal": None}
        )(),
        enforce_method_capability=lambda m, p, r: None,
        make_error=make_error,
        make_response=make_response,
        obs=obs_module,
        logger=_Logger(),
    )


def test_dispatch_records_exception_class_and_message():
    from minni.minnid_runtime.dispatch import dispatch_request

    obs.ERRORS.reset()

    def boom(params, request_id):
        raise ValueError("boom-detail-42")

    context = _dispatch_context({"search": boom}, obs)
    with pytest.raises(ValueError):
        asyncio.run(
            dispatch_request(
                {"jsonrpc": "2.0", "id": "e1", "method": "search", "params": {}},
                context,
            )
        )

    entries = obs.recent_errors()
    assert entries, "the dispatch exception should be captured into the ring buffer"
    last = entries[-1]
    assert last["method"] == "search"
    assert last["exc_class"] == "ValueError"
    assert "boom-detail-42" in last["message"]
    obs.ERRORS.reset()


def test_error_ring_truncates_message_and_evicts_oldest():
    ring = obs.ErrorRing(maxlen=3)
    ring.record("search", ValueError("x" * 1000))
    assert len(ring.snapshot()[0]["message"]) <= 500
    for i in range(5):
        ring.record("search", RuntimeError(f"err{i}"))
    snap = ring.snapshot()
    assert len(snap) == 3  # bounded ring
    # The ValueError, err0 and err1 were evicted; err2 is now oldest.
    assert snap[0]["message"] == "err2"


class _FakeCursor:
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


class _FakeDB:
    def __init__(self, *a, **k):
        pass

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


def test_health_report_recent_errors_hidden_from_non_operator(monkeypatch):
    monkeypatch.setattr(minnid, "SovereignDB", _FakeDB)
    obs.ERRORS.reset()
    obs.record_error("search", ValueError("/Users/x/secret-path leaked in message"))

    # A pre-identity / recovery caller gets counts only, no traceback content.
    rep = minnid._handle_health_report({"_recovery": True}, 1)["result"]
    assert rep["recent_errors"] == []
    assert rep["recent_errors_count"] == 1
    assert "secret-path" not in json.dumps(rep)
    obs.ERRORS.reset()


def test_health_report_recent_errors_visible_to_operator(monkeypatch):
    from minni.principal import EffectivePrincipal

    monkeypatch.setattr(minnid, "SovereignDB", _FakeDB)
    obs.ERRORS.reset()
    obs.record_error("search", ValueError("boom-attributable"))

    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    rep = minnid._handle_health_report({"_recovery": False, "_principal": op}, 1)["result"]
    assert "redacted" not in rep
    entries = rep["recent_errors"]
    assert any(
        e["exc_class"] == "ValueError" and "boom-attributable" in e["message"]
        for e in entries
    )
    obs.ERRORS.reset()

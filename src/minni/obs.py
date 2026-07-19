"""
Centralized observability for the Minni engine: structured logging setup and a
lightweight, dependency-free in-process metrics surface.

Integration rationale (Rule 3 — why this connects the modules it does):
- Before this module, `minnid.py` configured logging with an ad hoc
  ``logging.basicConfig`` call that only ran inside ``minnid.main()``. Every
  other entry point into the engine (the ``minnid_client`` probe, repro/smoke
  scripts, the test suite, the AFM helpers) therefore ran with an unconfigured
  root logger and no machine-parseable output. ``configure_logging`` makes one
  configured logger the single setup path so the whole engine logs the same way,
  and adds an opt-in JSON formatter for log shippers without taking on a logging
  dependency.
- The daemon already exposes latency histograms and a request counter on the
  ``status`` RPC via ``minnid._record_latency`` / ``_latency_snapshot``. This
  module adds named counters (e.g. per-method error counts) that ride the same
  ``status`` surface, keeping the operational signal local-first and consistent
  with the existing stack instead of introducing a tracing/metrics backend.

Everything here is standard-library only and safe to import from any engine
module, including under pytest.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from collections import deque
from typing import Dict, List

LOGGER_NAME = "minnid"

# Marker attribute so repeated ``configure_logging`` calls (daemon restart in a
# long-lived test process, multiple entry points in one interpreter) are
# idempotent rather than stacking duplicate handlers on the root logger.
_CONFIGURED_FLAG = "_minni_obs_configured"

_VALID_FORMATS = ("text", "json")
_DEFAULT_TEXT_FORMAT = "%(asctime)s [minnid] %(levelname)s: %(message)s"


class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log record for structured log ingestion.

    Keeps the field set small and stable (timestamp, level, logger, message,
    and exception text when present) so downstream log shippers can parse it
    without bespoke handling. Extra attributes attached via ``logger.x(..., extra=)``
    are included when JSON-serializable.
    """

    _RESERVED = frozenset(
        vars(logging.makeLogRecord({})).keys()
    ) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            if not isinstance(value, (str, int, float, bool, type(None))):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = repr(value)
            payload[key] = value
        return json.dumps(payload, default=str, separators=(",", ":"))


def _resolve_level(verbose: bool) -> int:
    env_level = os.environ.get("MINNI_LOG_LEVEL", "").strip().upper()
    if env_level:
        resolved = logging.getLevelName(env_level)
        if isinstance(resolved, int):
            return resolved
    return logging.DEBUG if verbose else logging.INFO


def _resolve_format() -> str:
    fmt = os.environ.get("MINNI_LOG_FORMAT", "text").strip().lower()
    return fmt if fmt in _VALID_FORMATS else "text"


def configure_logging(verbose: bool = False, *, force: bool = False) -> logging.Logger:
    """Configure root logging for the engine once and return the ``minnid`` logger.

    Honors ``MINNI_LOG_LEVEL`` (e.g. ``DEBUG``/``INFO``) and
    ``MINNI_LOG_FORMAT`` (``text`` default, or ``json`` for structured output).
    Idempotent: calling it again is a no-op unless ``force=True`` (used by tests
    that need to swap the format mid-process).
    """
    root = logging.getLogger()
    if getattr(root, _CONFIGURED_FLAG, False) and not force:
        root.setLevel(_resolve_level(verbose))
        return logging.getLogger(LOGGER_NAME)

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stderr)
    if _resolve_format() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter(_DEFAULT_TEXT_FORMAT))

    root.addHandler(handler)
    root.setLevel(_resolve_level(verbose))
    setattr(root, _CONFIGURED_FLAG, True)
    return logging.getLogger(LOGGER_NAME)


class Counters:
    """Thread-safe, process-local named integer counters.

    Mirrors the bounded, in-memory philosophy of the retrieval trace ring:
    SQLite stays the durable source of truth; these counters are cheap
    operational signal surfaced on the ``status`` RPC.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}
        # Baseline captured on the previous ``delta_snapshot`` call, so a status
        # caller sees change-since-last-look rather than a bare cumulative int
        # (a monotonic counter conflates "never happened" with "long ago").
        self._previous: Dict[str, int] = {}

    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + int(amount)

    def get(self, name: str) -> int:
        with self._lock:
            return self._counts.get(name, 0)

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def delta_snapshot(self) -> Dict[str, Dict[str, int]]:
        """Return ``{name: {"total": n, "delta": n - previous}}`` and advance
        the baseline. ``delta`` is "since the previous ``delta_snapshot`` call"
        — a caller-driven polling interval, NOT a per-second rate. Read and
        baseline-swap happen under one lock so two concurrent status RPCs cannot
        each read a stale ``previous`` and double-count the same delta.
        """
        with self._lock:
            result = {
                name: {"total": total, "delta": total - self._previous.get(name, 0)}
                for name, total in self._counts.items()
            }
            self._previous = dict(self._counts)
            return result

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._previous.clear()


# Named health flags raised when a counter's delta since the previous snapshot
# crosses its threshold. Semantics are "N more since you last looked" (interval
# = your poll cadence), so a flag names a rising trend, not a rate.
_HEALTH_FLAG_THRESHOLDS: Dict[str, tuple[int, str]] = {
    "errors": (10, "errors_rising"),
    "errors.search": (3, "errors_search_rising"),
}


def health_flags(deltas: Dict[str, Dict[str, int]]) -> List[str]:
    """Derive named boolean health flags from a ``delta_snapshot`` mapping.

    Pure over its input so a status handler can compute deltas once (advancing
    the baseline exactly once) and pass them here without a second swap.
    """
    flags: List[str] = []
    for name, (threshold, flag) in _HEALTH_FLAG_THRESHOLDS.items():
        entry = deltas.get(name)
        if entry and entry.get("delta", 0) >= threshold:
            flags.append(flag)
    return flags


class ErrorRing:
    """Bounded ring of the most recent dispatch exceptions.

    Makes a climbing ``errors.<method>`` counter attributable to concrete
    failures instead of an opaque number. Each entry is the exception class +
    a truncated message + a timestamp. This is a SENSITIVE surface — an
    exception message can embed a filesystem path or payload — so it is exposed
    only to operator callers, gated by the same redaction path as the other
    per-record health_report keys.
    """

    _MESSAGE_MAX = 500

    def __init__(self, maxlen: int = 20) -> None:
        self._lock = threading.Lock()
        self._ring: deque = deque(maxlen=maxlen)

    def record(self, method: str, exc: BaseException) -> None:
        entry = {
            "method": str(method),
            "exc_class": type(exc).__name__,
            "message": str(exc)[: self._MESSAGE_MAX],
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with self._lock:
            self._ring.append(entry)

    def snapshot(self) -> List[dict]:
        with self._lock:
            return list(self._ring)

    def reset(self) -> None:
        with self._lock:
            self._ring.clear()


METRICS = Counters()
ERRORS = ErrorRing()


def incr(name: str, amount: int = 1) -> None:
    """Increment a named global counter (convenience wrapper around ``METRICS``)."""
    METRICS.incr(name, amount)


def metrics_snapshot() -> Dict[str, int]:
    """Return a copy of all global counters for status/diagnostics surfaces."""
    return METRICS.snapshot()


def metrics_delta_snapshot() -> Dict[str, Dict[str, int]]:
    """Return the global counters' change since the previous snapshot."""
    return METRICS.delta_snapshot()


def record_error(method: str, exc: BaseException) -> None:
    """Record a dispatch exception into the global ring for attribution."""
    ERRORS.record(method, exc)


def recent_errors() -> List[dict]:
    """Return the most-recent captured dispatch exceptions (operator-gated)."""
    return ERRORS.snapshot()

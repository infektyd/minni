"""
Ephemeral per-query trace ring for Sovereign Memory retrieval.

Trace entries are intentionally process-local and bounded. SQLite remains the
runtime source of truth; this module only keeps recent observability envelopes.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import secrets
import threading
from typing import Any, Dict, Optional


class TraceRing:
    """Bounded in-memory ring keyed by short random trace ids."""

    def __init__(self, capacity: int = 100, max_bytes: int = 5 * 1024 * 1024):
        self.capacity = capacity
        self.max_bytes = max_bytes
        self._entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._sizes: Dict[str, int] = {}
        self._approx_bytes = 0
        self._lock = threading.Lock()

    @property
    def approx_bytes(self) -> int:
        return self._approx_bytes

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, entry: Dict[str, Any]) -> str:
        """Store an entry and return its trace id."""
        trace_id = self._new_id()
        self.put(trace_id, entry)
        return trace_id

    def put(self, trace_id: str, entry: Dict[str, Any]) -> str:
        """Store an entry under a caller-provided trace id."""
        stored = dict(entry)
        stored["trace_id"] = trace_id
        size = self._entry_size(stored)
        if size > self.max_bytes:
            stored = {
                "trace_id": trace_id,
                "degraded": True,
                "reason": "trace entry exceeded max_bytes",
                "query": entry.get("query"),
                "timing": entry.get("timing", {}),
                "final_ordering": entry.get("final_ordering", []),
            }
            size = self._entry_size(stored)

        with self._lock:
            if trace_id in self._entries:
                self._approx_bytes -= self._sizes.pop(trace_id, 0)
            self._entries[trace_id] = stored
            self._sizes[trace_id] = size
            self._approx_bytes += size
            self._trim()
        return trace_id

    def get(self, trace_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._entries.get(trace_id)
            if entry is None:
                return None
            self._entries.move_to_end(trace_id)
            return dict(entry)

    def _trim(self) -> None:
        while len(self._entries) > self.capacity:
            self._pop_oldest()
        while self._approx_bytes > self.max_bytes and len(self._entries) > 1:
            self._pop_oldest()

    def _pop_oldest(self) -> None:
        old_id, _ = self._entries.popitem(last=False)
        self._approx_bytes -= self._sizes.pop(old_id, 0)

    def _new_id(self) -> str:
        while True:
            trace_id = f"t{secrets.token_hex(4)}"
            if trace_id not in self._entries:
                return trace_id

    @staticmethod
    def _entry_size(entry: Dict[str, Any]) -> int:
        try:
            return len(json.dumps(entry, default=str, separators=(",", ":")).encode("utf-8"))
        except Exception:
            return len(str(entry).encode("utf-8"))


GLOBAL_TRACE_RING = TraceRing()

"""
Ephemeral per-query trace ring for Minni retrieval.

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

    def add(self, entry: Dict[str, Any], owner: Optional[str] = None) -> str:
        """Store an entry and return its trace id."""
        trace_id = self._new_id()
        self.put(trace_id, entry, owner=owner)
        return trace_id

    def put(
        self, trace_id: str, entry: Dict[str, Any], owner: Optional[str] = None
    ) -> str:
        """Store an entry under a caller-provided trace id.

        R8: ``owner`` binds the creating principal's agent_id to the entry so a
        different authenticated principal cannot read another caller's trace just
        by knowing/guessing the trace_id. Stored under the private ``_owner`` key
        (stripped before the trace is returned to any caller).
        """
        stored = dict(entry)
        stored["trace_id"] = trace_id
        if owner is not None:
            stored["_owner"] = str(owner)
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
            if owner is not None:
                stored["_owner"] = str(owner)
            size = self._entry_size(stored)

        with self._lock:
            if trace_id in self._entries:
                self._approx_bytes -= self._sizes.pop(trace_id, 0)
            self._entries[trace_id] = stored
            self._sizes[trace_id] = size
            self._approx_bytes += size
            self._trim()
        return trace_id

    @staticmethod
    def _strip_owner(entry: Dict[str, Any]) -> Dict[str, Any]:
        """Return a copy with the private ownership key removed."""
        out = dict(entry)
        out.pop("_owner", None)
        return out

    def get(
        self, trace_id: str, requester: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Return the trace entry, enforcing owner binding when applicable.

        R8: when the stored entry carries an ``_owner`` and ``requester`` is
        supplied, a mismatched requester is denied (None) — the trace_id alone is
        not an authorization token. A legacy entry stored with no owner, or a
        call with no requester, preserves the prior behavior. The ``_owner`` key
        is always stripped from the returned copy.
        """
        with self._lock:
            entry = self._entries.get(trace_id)
            if entry is None:
                return None
            owner = entry.get("_owner")
            if owner is not None and requester is not None and str(requester) != owner:
                return None
            self._entries.move_to_end(trace_id)
            return self._strip_owner(entry)

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

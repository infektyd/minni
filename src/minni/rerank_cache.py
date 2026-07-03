"""In-memory LRU cache for cross-encoder rerank scores."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Iterable, Optional, Tuple


CacheKey = Tuple[str, str, str, int]


class RerankCache:
    """Small per-process LRU keyed by model identity, query hash, and chunk_id."""

    def __init__(self, capacity: int = 1024):
        self.capacity = max(1, int(capacity))
        self._scores: OrderedDict[CacheKey, float] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8", errors="ignore")).hexdigest()

    def _key(self, model_name: str, model_version: str, query: str, chunk_id: int) -> CacheKey:
        return (model_name or "unknown", model_version or "unknown",
                self._query_hash(query), int(chunk_id))

    def get(
        self,
        model_name: str,
        model_version: str,
        query: str,
        chunk_id: int,
    ) -> Optional[float]:
        key = self._key(model_name, model_version, query, chunk_id)
        with self._lock:
            if key not in self._scores:
                return None
            score = self._scores.pop(key)
            self._scores[key] = score
            return score

    def set(
        self,
        model_name: str,
        model_version: str,
        query: str,
        chunk_id: int,
        score: float,
    ) -> None:
        key = self._key(model_name, model_version, query, chunk_id)
        with self._lock:
            self._scores.pop(key, None)
            self._scores[key] = float(score)
            while len(self._scores) > self.capacity:
                self._scores.popitem(last=False)

    def invalidate_chunks(self, chunk_ids: Iterable[int]) -> int:
        doomed = {int(cid) for cid in chunk_ids if cid is not None}
        if not doomed:
            return 0
        with self._lock:
            keys = [key for key in self._scores if key[3] in doomed]
            for key in keys:
                self._scores.pop(key, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._scores.clear()


GLOBAL_RERANK_CACHE = RerankCache(capacity=1024)


def invalidate_chunks(chunk_ids: Iterable[int]) -> int:
    """Invalidate cached scores for chunks deleted or replaced by the indexer."""
    return GLOBAL_RERANK_CACHE.invalidate_chunks(chunk_ids)

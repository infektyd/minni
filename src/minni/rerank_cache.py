"""In-memory LRU cache for cross-encoder rerank scores."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Iterable, Optional, Tuple


CacheKey = Tuple[str, str, str, int, str, str]


class RerankCache:
    """LRU of raw model scores, scoped to corpus and exact model input."""

    def __init__(self, capacity: int = 1024):
        self.capacity = max(1, int(capacity))
        self._scores: OrderedDict[CacheKey, float] = OrderedDict()
        self._lock = threading.Lock()
        self._generation = 0

    @staticmethod
    def _query_hash(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8", errors="surrogatepass")).hexdigest()

    def _key(self, model_name: str, model_version: str, query: str, chunk_id: int,
             corpus: str, passage: str) -> CacheKey:
        return (model_name or "unknown", model_version or "unknown",
                self._query_hash(query), int(chunk_id), corpus, self._query_hash(passage))

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def get(
        self,
        model_name: str,
        model_version: str,
        query: str,
        chunk_id: int,
        *,
        corpus: str = "",
        passage: str = "",
    ) -> Optional[float]:
        key = self._key(model_name, model_version, query, chunk_id, corpus, passage)
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
        *,
        corpus: str = "",
        passage: str = "",
        expected_generation: Optional[int] = None,
    ) -> None:
        key = self._key(model_name, model_version, query, chunk_id, corpus, passage)
        with self._lock:
            # A prediction started before invalidation must not repopulate
            # the cache after the indexer has deleted/replaced its inputs.
            if expected_generation is not None and expected_generation != self._generation:
                return
            self._scores.pop(key, None)
            self._scores[key] = float(score)
            while len(self._scores) > self.capacity:
                self._scores.popitem(last=False)

    def invalidate_chunks(self, chunk_ids: Iterable[int]) -> int:
        doomed = {int(cid) for cid in chunk_ids if cid is not None}
        if not doomed:
            return 0
        with self._lock:
            self._generation += 1
            keys = [key for key in self._scores if key[3] in doomed]
            for key in keys:
                self._scores.pop(key, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._generation += 1
            self._scores.clear()


GLOBAL_RERANK_CACHE = RerankCache(capacity=1024)


def invalidate_chunks(chunk_ids: Iterable[int]) -> int:
    """Invalidate cached scores for chunks deleted or replaced by the indexer."""
    return GLOBAL_RERANK_CACHE.invalidate_chunks(chunk_ids)

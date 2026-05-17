"""
engine/backends/faiss_mem.py — Pure in-memory FAISS backend.

No disk persistence. Vectors are rebuilt from scratch on every process start.
Useful for:
  - Testing (cheap, no I/O)
  - Ephemeral agent sessions that don't need to survive restarts
  - Secondary backend in a multi-backend fan-out alongside faiss-disk

Available via: --vector-backend=faiss-mem
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np

from vector_backend import VectorBackend, VectorItem, VectorHit

logger = logging.getLogger("sovereign.backends.faiss_mem")


class FaissMemBackend:
    """VectorBackend backed by FAISSIndex with NO disk persistence."""

    name: str = "faiss-mem"

    def __init__(self, config=None):
        """
        Args:
            config : SovereignConfig (uses DEFAULT_CONFIG if None).
        """
        from config import DEFAULT_CONFIG
        from faiss_index import FAISSIndex

        self.config = config or DEFAULT_CONFIG
        self.dim: int = self.config.embedding_dim
        self._faiss = FAISSIndex(self.config)

    # ------------------------------------------------------------------
    # VectorBackend protocol
    # ------------------------------------------------------------------

    def upsert(self, items: List[VectorItem]) -> None:
        """Add or replace vectors in-memory."""
        for item in items:
            if item.vector is None or len(item.vector) == 0:
                continue
            vec = np.asarray(item.vector, dtype=np.float32)
            if vec.shape[0] != self.dim:
                logger.warning(
                    "FaissMemBackend: skipping item %d — wrong dim %d (expected %d)",
                    item.chunk_id, vec.shape[0], self.dim,
                )
                continue
            if item.chunk_id in self._faiss._reverse_map:
                self._faiss.remove(item.chunk_id)
            self._faiss.add(item.chunk_id, vec)

    def remove(self, chunk_ids: List[int]) -> None:
        """Mark vectors for removal."""
        for cid in chunk_ids:
            self._faiss.remove(cid)

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        filter: Optional[Dict] = None,
    ) -> List[VectorHit]:
        """Search the in-memory FAISS index."""
        if self._faiss.count == 0:
            return []

        query = np.asarray(query_vec, dtype=np.float32)
        search_k = min(k * 5, self._faiss.count)
        raw = self._faiss.search(query, top_k=search_k)

        hits = [
            VectorHit(chunk_id=cid, doc_id=0, score=score, backend=self.name)
            for cid, score in raw[:k]
        ]
        return hits

    def stats(self) -> Dict:
        """Return diagnostic stats dict."""
        faiss_stats = self._faiss.get_stats()
        return {
            "name": self.name,
            "dim": self.dim,
            "vector_count": self._faiss.count,
            "index_type": faiss_stats.get("index_type", "unknown"),
            "memory_bytes": faiss_stats.get("memory_bytes", 0),
            "persistent": False,
        }

    @property
    def vector_count(self) -> int:
        return self._faiss.count

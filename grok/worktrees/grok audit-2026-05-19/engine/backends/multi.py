"""
engine/backends/multi.py — Multi-backend fan-out adapter.

Wraps N VectorBackend instances.  On search():
  1. Queries each backend in parallel via ThreadPoolExecutor.
  2. Collects ranked lists (each backend's hits sorted by score).
  3. Merges all lists with Reciprocal Rank Fusion (RRF, k=60).
  4. Returns the top-k merged hits, each carrying a `backend` provenance field.

On upsert()/remove():
  - Fans out to all backends independently.
  - One slow backend does NOT block others (each runs in its own thread).
  - Errors from one backend are logged but do not propagate.

RRF formula: score(d) = Σ_backend  1 / (k + rank_in_backend(d))

This is the same formula used in retrieval.py for FTS + semantic fusion,
with k=60 (the standard convention, matching config.rrf_k default).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

import numpy as np

from vector_backend import VectorBackend, VectorItem, VectorHit

logger = logging.getLogger("sovereign.backends.multi")

# Standard RRF constant (same as retrieval.py default)
_RRF_K = 60


class MultiBackend:
    """
    Fan-out VectorBackend that wraps N backends and merges results via RRF.

    Example:
        from backends.faiss_disk import FaissDiskBackend
        from backends.faiss_mem import FaissMemBackend
        from backends.multi import MultiBackend

        b1 = FaissDiskBackend(config, db)
        b2 = FaissMemBackend(config)
        multi = MultiBackend([b1, b2])
        hits = multi.search(query_vec, k=10)
    """

    name: str = "multi"

    def __init__(self, backends: List, rrf_k: int = _RRF_K, max_workers: int = 4):
        """
        Args:
            backends   : List of VectorBackend instances.
            rrf_k      : RRF smoothing constant (default 60).
            max_workers: Max threads for parallel search.

        Raises:
            ValueError : If backends list is empty.
        """
        if not backends:
            raise ValueError("MultiBackend requires at least one backend")

        self._backends = backends
        self._rrf_k = rrf_k
        self._max_workers = max_workers

        # dim: must agree across all backends
        dims = {b.dim for b in backends}
        if len(dims) > 1:
            raise ValueError(
                f"MultiBackend: backends have mismatched dims: {dims}. "
                "All backends must share the same embedding dimension."
            )
        self.dim: int = dims.pop()

    @property
    def backend_names(self) -> List[str]:
        return [b.name for b in self._backends]

    # ------------------------------------------------------------------
    # VectorBackend protocol
    # ------------------------------------------------------------------

    def upsert(self, items: List[VectorItem]) -> None:
        """Fan-out upsert to all backends in parallel."""
        with ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            futures = {
                ex.submit(b.upsert, items): b.name
                for b in self._backends
            }
            for future in as_completed(futures):
                bname = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("MultiBackend.upsert: backend %r failed: %s", bname, exc)

    def remove(self, chunk_ids: List[int]) -> None:
        """Fan-out remove to all backends in parallel."""
        with ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            futures = {
                ex.submit(b.remove, chunk_ids): b.name
                for b in self._backends
            }
            for future in as_completed(futures):
                bname = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("MultiBackend.remove: backend %r failed: %s", bname, exc)

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        filter: Optional[Dict] = None,
    ) -> List[VectorHit]:
        """
        Parallel fan-out search + RRF merge.

        Each backend returns a ranked list. RRF weights each backend equally.
        The merged list carries the backend name from the highest-ranked backend
        that contributed that chunk_id.

        Args:
            query_vec : float32 array, shape (dim,).
            k         : Maximum results to return.
            filter    : Passed to each backend (may be ignored by FAISS backends).

        Returns:
            List of VectorHit sorted by RRF score descending, length <= k.
        """
        backend_results: Dict[str, List[VectorHit]] = {}

        with ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            futures = {
                ex.submit(b.search, query_vec, k * 2, filter): b.name
                for b in self._backends
            }
            for future in as_completed(futures):
                bname = futures[future]
                try:
                    hits = future.result()
                    backend_results[bname] = hits or []
                except Exception as exc:
                    logger.warning(
                        "MultiBackend.search: backend %r failed: %s — excluding from merge",
                        bname, exc,
                    )
                    backend_results[bname] = []

        return self._rrf_merge(backend_results, k)

    def stats(self) -> Dict:
        """Aggregate stats from all backends."""
        per_backend = {}
        for b in self._backends:
            try:
                per_backend[b.name] = b.stats()
            except Exception as exc:
                per_backend[b.name] = {"error": str(exc)}

        total_vectors = sum(
            s.get("vector_count", 0)
            for s in per_backend.values()
            if isinstance(s, dict)
        )
        return {
            "name": self.name,
            "dim": self.dim,
            "vector_count": total_vectors,
            "backends": per_backend,
            "rrf_k": self._rrf_k,
        }

    # ------------------------------------------------------------------
    # RRF merge
    # ------------------------------------------------------------------

    def _rrf_merge(
        self,
        backend_results: Dict[str, List[VectorHit]],
        k: int,
    ) -> List[VectorHit]:
        """
        Merge ranked lists from multiple backends using RRF.

        RRF(chunk_id) = Σ_backend  1 / (rrf_k + rank_in_backend)

        Where rank is 1-based (rank 1 = highest similarity in that backend).

        For each chunk_id, the winning backend (highest contributing backend)
        is recorded as the provenance.
        """
        rrf_k = self._rrf_k

        # chunk_id → accumulated RRF score
        rrf_scores: Dict[int, float] = {}
        # chunk_id → best VectorHit (for doc_id and backend provenance)
        best_hit: Dict[int, VectorHit] = {}
        # chunk_id → backend name that contributed the most RRF score
        backend_contrib: Dict[int, Dict[str, float]] = {}

        for bname, hits in backend_results.items():
            for rank, hit in enumerate(hits, start=1):
                cid = hit.chunk_id
                contrib = 1.0 / (rrf_k + rank)

                rrf_scores[cid] = rrf_scores.get(cid, 0.0) + contrib
                backend_contrib.setdefault(cid, {})[bname] = \
                    backend_contrib.get(cid, {}).get(bname, 0.0) + contrib

                # Track best hit (highest raw score) for doc_id
                if cid not in best_hit or hit.score > best_hit[cid].score:
                    best_hit[cid] = hit

        # Sort by accumulated RRF score
        ranked = sorted(rrf_scores.keys(), key=lambda cid: rrf_scores[cid], reverse=True)

        merged: List[VectorHit] = []
        for cid in ranked[:k]:
            base = best_hit[cid]
            # Attribute to the backend that contributed most RRF score
            winning_backend = max(
                backend_contrib[cid], key=lambda bn: backend_contrib[cid][bn]
            )
            merged.append(VectorHit(
                chunk_id=base.chunk_id,
                doc_id=base.doc_id,
                score=rrf_scores[cid],
                backend=winning_backend,
            ))

        return merged

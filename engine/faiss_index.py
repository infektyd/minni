"""
Sovereign Memory V3.1 — FAISS Index Manager.

V3 used raw numpy loops to compute cosine similarity against all chunks.
That works fine at small scale but becomes a bottleneck at 200K+ vectors.

V3.1 uses a proper FAISS index with auto-scaling:
- Flat (exact) index under hnsw_threshold vectors
- HNSW (approximate) index above threshold
- Automatic rebuild when threshold is crossed
- Persistent to disk — survives restarts
- All vectors are full-fidelity float32[384]
"""

import os
import logging
import sqlite3
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG

logger = logging.getLogger("sovereign.faiss")


class FAISSIndex:
    """
    FAISS index manager with auto-scaling between Flat and HNSW.

    Usage:
        idx = FAISSIndex(config)
        idx.add(chunk_id=42, embedding=np.array([...]))
        results = idx.search(query_embedding, top_k=20)
        # results: [(chunk_id, distance), ...]
    """

    def __init__(self, config: SovereignConfig = DEFAULT_CONFIG):
        self.config = config
        self.dim = config.embedding_dim
        self._index = None
        self._id_map: Dict[int, int] = {}     # internal_idx → chunk_id
        self._reverse_map: Dict[int, int] = {} # chunk_id → internal_idx
        self._vectors: List[np.ndarray] = []    # Keep raw vectors for rebuild
        self._chunk_ids: List[int] = []
        self._current_type = "flat"
        self._faiss = None

        self._load_faiss()

    def _load_faiss(self):
        """Import faiss with graceful fallback."""
        try:
            import faiss
            if not hasattr(faiss, "IndexFlatIP"):
                logger.warning(
                    "faiss module is importable but incomplete (%s). "
                    "Falling back to numpy brute-force search.",
                    getattr(faiss, "__file__", None)
                    or getattr(faiss, "__path__", "<unknown location>"),
                )
                self._faiss = None
                return
            self._faiss = faiss
        except ImportError:
            logger.warning(
                "faiss-cpu not installed. Install with: pip install faiss-cpu. "
                "Falling back to numpy brute-force search."
            )
            self._faiss = None

    @property
    def count(self) -> int:
        """Number of vectors in the index."""
        return len(self._chunk_ids)

    def build_from_vectors(
        self,
        chunk_ids: List[int],
        embeddings: np.ndarray,
    ) -> None:
        """
        Build (or rebuild) the index from a batch of vectors.

        Args:
            chunk_ids: List of chunk_id integers
            embeddings: numpy array of shape (N, dim), float32
        """
        if len(chunk_ids) == 0:
            return

        self._chunk_ids = list(chunk_ids)
        self._vectors = [embeddings[i] for i in range(len(chunk_ids))]
        self._id_map = {i: cid for i, cid in enumerate(chunk_ids)}
        self._reverse_map = {cid: i for i, cid in enumerate(chunk_ids)}

        n = len(chunk_ids)

        if self._faiss is None:
            # No FAISS — will use numpy fallback in search()
            self._current_type = "numpy"
            logger.info("Built numpy fallback index: %d vectors", n)
            return

        faiss = self._faiss

        # Normalize for cosine similarity (FAISS inner product on normalized = cosine)
        normalized = self._normalize(embeddings)

        if getattr(self.config, "embedding_quantization", "fp32") == "int8":
            if self._build_quantized_index(faiss, normalized, n):
                return
            logger.warning(
                "embedding_quantization=int8 requested, but this FAISS build "
                "does not support the required scalar quantized index; using fp32"
            )

        should_hnsw = (
            self.config.faiss_index_type == "hnsw"
            or (self.config.faiss_index_type == "auto" and n >= self.config.hnsw_threshold)
        )

        if should_hnsw and n >= 1000:
            # HNSW index
            index = faiss.IndexHNSWFlat(self.dim, self.config.hnsw_m)
            index.hnsw.efConstruction = self.config.hnsw_ef_construction
            index.hnsw.efSearch = self.config.hnsw_ef_search
            index.add(normalized)
            self._index = index
            self._current_type = "hnsw"
            logger.info("Built HNSW index: %d vectors (M=%d, ef=%d)",
                        n, self.config.hnsw_m, self.config.hnsw_ef_construction)
        else:
            # Flat index (exact search)
            index = faiss.IndexFlatIP(self.dim)  # Inner product on normalized = cosine
            index.add(normalized)
            self._index = index
            self._current_type = "flat"
            logger.info("Built Flat index: %d vectors", n)

    @staticmethod
    def _normalize(embeddings: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms < 1e-8, 1.0, norms)
        return (embeddings / norms).astype(np.float32)

    def _build_quantized_index(self, faiss, normalized: np.ndarray, n: int) -> bool:
        """Build optional int8 scalar quantization, with fp32 fallback on failure."""
        quantizer = getattr(faiss, "ScalarQuantizer", None)
        quantizer_type = getattr(quantizer, "QT_8bit", None)
        metric = getattr(faiss, "METRIC_INNER_PRODUCT", 0)
        if quantizer_type is None:
            return False

        hnsw_sq = getattr(faiss, "IndexHNSWSQ", None)
        if hnsw_sq is not None:
            try:
                index = hnsw_sq(self.dim, quantizer_type, self.config.hnsw_m, metric)
                if hasattr(index, "hnsw"):
                    index.hnsw.efConstruction = self.config.hnsw_ef_construction
                    index.hnsw.efSearch = self.config.hnsw_ef_search
                index.add(normalized)
                self._index = index
                self._current_type = "hnsw-sq-int8"
                logger.info(
                    "Built int8 HNSW scalar-quantized index: %d vectors (M=%d)",
                    n, self.config.hnsw_m,
                )
                return True
            except Exception as exc:
                logger.warning("IndexHNSWSQ unavailable or failed: %s", exc)

        scalar_quantizer = getattr(faiss, "IndexScalarQuantizer", None)
        if scalar_quantizer is not None:
            try:
                index = scalar_quantizer(self.dim, quantizer_type, metric)
                if hasattr(index, "is_trained") and not index.is_trained:
                    index.train(normalized)
                index.add(normalized)
                self._index = index
                self._current_type = "sq-int8"
                logger.info("Built int8 scalar-quantized index: %d vectors", n)
                return True
            except Exception as exc:
                logger.warning("IndexScalarQuantizer unavailable or failed: %s", exc)

        return False

    def add(self, chunk_id: int, embedding: np.ndarray) -> None:
        """
        Add a single vector to the index.
        For bulk operations, use build_from_vectors() instead.
        """
        self._chunk_ids.append(chunk_id)
        self._vectors.append(embedding.copy())
        idx = len(self._chunk_ids) - 1
        self._id_map[idx] = chunk_id
        self._reverse_map[chunk_id] = idx

        if self._faiss and self._index is not None:
            vec = embedding.astype(np.float32).reshape(1, -1)
            norm = np.linalg.norm(vec)
            if norm > 1e-8:
                vec = vec / norm
            self._index.add(vec)

            # Check if we need to upgrade to HNSW
            if (self._current_type == "flat"
                    and self.config.faiss_index_type == "auto"
                    and self.count >= self.config.hnsw_threshold):
                logger.info("Threshold crossed (%d vectors), rebuilding as HNSW...",
                            self.count)
                all_vecs = np.array(self._vectors, dtype=np.float32)
                self.build_from_vectors(self._chunk_ids, all_vecs)

    def remove(self, chunk_id: int) -> None:
        """
        Remove a vector by chunk_id.
        Marks it for exclusion — actual removal happens on next rebuild.
        """
        if chunk_id in self._reverse_map:
            idx = self._reverse_map.pop(chunk_id)
            self._id_map.pop(idx, None)
            # Note: FAISS doesn't support true deletion for HNSW.
            # We filter results in search() instead. Periodic rebuild cleans up.

    def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 20,
    ) -> List[Tuple[int, float]]:
        """
        Search for nearest neighbors.

        Args:
            query_embedding: float32 array of shape (dim,)
            top_k: Number of results

        Returns:
            List of (chunk_id, similarity_score) sorted by score descending.
        """
        if self.count == 0:
            return []

        query = query_embedding.astype(np.float32).reshape(1, -1)
        norm = np.linalg.norm(query)
        if norm > 1e-8:
            query = query / norm

        if self._faiss and self._index is not None:
            # FAISS search
            # Request extra results to account for removed vectors
            search_k = min(top_k * 2, self.count)
            scores, indices = self._index.search(query, search_k)

            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < 0:  # FAISS returns -1 for missing
                    continue
                chunk_id = self._id_map.get(int(idx))
                if chunk_id is not None and chunk_id in self._reverse_map:
                    results.append((chunk_id, float(score)))
                if len(results) >= top_k:
                    break

            return results

        else:
            # Numpy fallback (brute-force cosine similarity)
            if not self._vectors:
                return []

            all_vecs = np.array(self._vectors, dtype=np.float32)
            norms = np.linalg.norm(all_vecs, axis=1, keepdims=True)
            norms = np.where(norms < 1e-8, 1.0, norms)
            normalized = all_vecs / norms

            sims = (normalized @ query.T).flatten()
            top_indices = np.argsort(sims)[::-1][:top_k * 2]

            results = []
            for idx in top_indices:
                chunk_id = self._id_map.get(int(idx))
                if chunk_id is not None and chunk_id in self._reverse_map:
                    results.append((chunk_id, float(sims[idx])))
                if len(results) >= top_k:
                    break

            return results

    def rebuild(self) -> None:
        """Force a full rebuild from stored vectors (cleans up deletions)."""
        if self._chunk_ids and self._vectors:
            # Filter out removed
            live_ids = []
            live_vecs = []
            for i, cid in enumerate(self._chunk_ids):
                if cid in self._reverse_map:
                    live_ids.append(cid)
                    live_vecs.append(self._vectors[i])

            if live_vecs:
                all_vecs = np.array(live_vecs, dtype=np.float32)
                self.build_from_vectors(live_ids, all_vecs)
            else:
                self._chunk_ids = []
                self._vectors = []
                self._id_map = {}
                self._reverse_map = {}
                self._index = None

    def get_stats(self) -> Dict:
        """Return index statistics."""
        return {
            "total_vectors": self.count,
            "index_type": self._current_type,
            "dimension": self.dim,
            "hnsw_threshold": self.config.hnsw_threshold,
            "memory_bytes": self.count * self.dim * 4,  # float32
        }

    # ── Disk persistence (PR-2) ───────────────────────────────────────────────

    def _manifest_path(self) -> str:
        """Default manifest path: <db_dir>/faiss/index.manifest.json."""
        from faiss_persist import _faiss_dir_for_db
        faiss_dir = _faiss_dir_for_db(self.config.db_path)
        return os.path.join(faiss_dir, "index.manifest.json")

    def try_load_from_disk(self, db_conn=None) -> bool:
        """
        Attempt to load the FAISS index from disk cache.

        Returns True if the cache was valid and loaded, False on miss.
        Call this before building from DB on cold start.
        """
        from faiss_persist import compute_db_checksum, load

        manifest = self._manifest_path()

        # Compute current DB checksum (requires a live connection)
        if db_conn is None:
            logger.debug("No DB connection for checksum; skipping disk load")
            return False

        checksum = compute_db_checksum(db_conn)
        result = load(
            manifest,
            expected_db_checksum=checksum,
            expected_model=self.config.embedding_model,
            expected_dim=self.config.embedding_dim,
            expected_quantization=getattr(self.config, "embedding_quantization", "fp32"),
        )
        if result is None:
            return False

        faiss_index, chunk_ids, vectors = result

        if faiss_index is not None:
            # Restore from FAISS index: rebuild id maps from chunk_id_order
            self._index = faiss_index
            self._chunk_ids = list(chunk_ids)
            self._id_map = {i: cid for i, cid in enumerate(chunk_ids)}
            self._reverse_map = {cid: i for i, cid in enumerate(chunk_ids)}
            # We do not have raw vectors; set to empty (rebuild will re-load if needed)
            self._vectors = []
            quantization = getattr(self.config, "embedding_quantization", "fp32")
            if quantization == "int8" and hasattr(faiss_index, "hnsw"):
                self._current_type = "hnsw-sq-int8"
            elif quantization == "int8":
                self._current_type = "sq-int8"
            else:
                self._current_type = "flat" if not hasattr(faiss_index, "hnsw") else "hnsw"
            logger.info(
                "FAISS index restored from disk cache: %d vectors", len(chunk_ids)
            )
            return True

        if vectors:
            # numpy fallback: have raw vectors, rebuild index from them
            arr = np.array(vectors, dtype=np.float32)
            self.build_from_vectors(list(chunk_ids), arr)
            logger.info(
                "FAISS index rebuilt from numpy cache: %d vectors", len(chunk_ids)
            )
            return True

        return False

    def save_to_disk(self, db_conn=None) -> bool:
        """
        Save the current index to disk.

        Args:
            db_conn: An open sqlite3.Connection for computing the DB checksum.

        Returns:
            True on success, False if save is skipped or fails.
        """
        if self.count == 0:
            logger.debug("Skipping FAISS save: index is empty")
            return False

        from faiss_persist import compute_db_checksum, save

        checksum = "unknown"
        if db_conn is not None:
            checksum = compute_db_checksum(db_conn)

        manifest = self._manifest_path()
        return save(
            index=self._index,
            vectors=self._vectors,
            chunk_ids=self._chunk_ids,
            manifest_path=manifest,
            embedding_model=self.config.embedding_model,
            vector_dim=self.config.embedding_dim,
            embedding_quantization=getattr(self.config, "embedding_quantization", "fp32"),
            db_checksum=checksum,
        )

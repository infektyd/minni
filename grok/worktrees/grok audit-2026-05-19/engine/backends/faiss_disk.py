"""
engine/backends/faiss_disk.py — FAISS with disk persistence.

PR-3 default backend. Wraps engine/faiss_index.py (FAISSIndex) and
engine/faiss_persist.py (PR-2 save/load with manifest + db_checksum).

Behaviour:
  - On construction, if a SovereignDB connection is passed, attempts to
    load the FAISS index from disk cache (cold-start <500ms).
  - upsert() adds vectors to the live index and schedules a save.
  - remove() marks vectors for exclusion (HNSW doesn't support true delete;
    removal is cleaned up on next rebuild).
  - search() queries FAISS then post-filters via SQLite for metadata.
  - save_index() / load_index() expose the PR-2 persistence layer.

This backend produces bit-identical results to the pre-PR-3 code when used
alone (single backend mode), because it wraps the exact same FAISSIndex.

Thread safety:
  Delegates to FAISSIndex which is single-threaded; callers should serialize
  or use multi.py (which uses ThreadPoolExecutor with independent backend instances).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np

from vector_backend import VectorBackend, VectorItem, VectorHit

logger = logging.getLogger("sovereign.backends.faiss_disk")


class FaissDiskBackend:
    """VectorBackend wrapping FAISSIndex + faiss_persist (disk-backed)."""

    name: str = "faiss-disk"

    def __init__(self, config=None, db=None):
        """
        Args:
            config : SovereignConfig (uses DEFAULT_CONFIG if None).
            db     : SovereignDB (used for disk-cache checksum and post-filter).
                     May be None — search() will skip SQLite post-filter on chunk_ids.
        """
        from config import DEFAULT_CONFIG
        from faiss_index import FAISSIndex

        self.config = config or DEFAULT_CONFIG
        self.db = db
        self.dim: int = self.config.embedding_dim
        self._faiss = FAISSIndex(self.config)
        self._dirty = False  # True after upsert/remove, until save_index()

        # Attempt to load from disk cache on construction if DB is available
        if db is not None:
            try:
                conn = db._get_conn()
                if self._faiss.try_load_from_disk(db_conn=conn):
                    logger.debug(
                        "FaissDiskBackend: loaded %d vectors from disk cache",
                        self._faiss.count,
                    )
            except Exception as exc:
                logger.debug("FaissDiskBackend: disk cache load failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # VectorBackend protocol
    # ------------------------------------------------------------------

    def upsert(self, items: List[VectorItem]) -> None:
        """Add or replace vectors. Uses FAISSIndex.add() for each item."""
        for item in items:
            if item.vector is None or len(item.vector) == 0:
                logger.warning("FaissDiskBackend: skipping item %d — empty vector", item.chunk_id)
                continue
            vec = np.asarray(item.vector, dtype=np.float32)
            if vec.shape[0] != self.dim:
                logger.warning(
                    "FaissDiskBackend: skipping item %d — wrong dim %d (expected %d)",
                    item.chunk_id, vec.shape[0], self.dim,
                )
                continue
            # FAISSIndex.add() handles duplicates by appending — for true upsert
            # we remove first if already present, then add.
            if item.chunk_id in self._faiss._reverse_map:
                self._faiss.remove(item.chunk_id)
            self._faiss.add(item.chunk_id, vec)
        self._dirty = True

    def remove(self, chunk_ids: List[int]) -> None:
        """Mark vectors for removal (cleaned up on next rebuild)."""
        for cid in chunk_ids:
            self._faiss.remove(cid)
        self._dirty = True

    def search(
        self,
        query_vec: np.ndarray,
        k: int,
        filter: Optional[Dict] = None,
    ) -> List[VectorHit]:
        """
        Search the FAISS index and return VectorHit list.

        FAISS doesn't support native filtered search. We:
          1. Search for k*5 candidates to over-fetch.
          2. Fetch chunk→doc mapping from SQLite.
          3. Return top-k after dedup by doc_id (best chunk per doc).
        """
        if self._faiss.count == 0:
            return []

        query = np.asarray(query_vec, dtype=np.float32)
        search_k = min(k * 5, self._faiss.count)
        raw = self._faiss.search(query, top_k=search_k)
        # raw: [(chunk_id, score), ...]

        if not raw:
            return []

        chunk_ids = [cid for cid, _ in raw]
        score_map = {cid: score for cid, score in raw}

        # Fetch doc_id for each chunk from SQLite (post-filter / dedup step)
        hits: List[VectorHit] = []
        if self.db is not None:
            try:
                with self.db.cursor() as c:
                    placeholders = ",".join("?" * len(chunk_ids))
                    c.execute(
                        f"SELECT chunk_id, doc_id FROM chunk_embeddings "
                        f"WHERE chunk_id IN ({placeholders})",
                        chunk_ids,
                    )
                    rows = c.fetchall()

                seen_docs: Dict[int, float] = {}
                row_map = {row["chunk_id"]: row["doc_id"] for row in rows}
                for cid in chunk_ids:
                    did = row_map.get(cid)
                    if did is None:
                        continue
                    score = score_map[cid]
                    if did not in seen_docs or score > seen_docs[did]:
                        seen_docs[did] = score
                        # replace or add
                    hits = [h for h in hits if h.doc_id != did]
                    if did in seen_docs:
                        hits.append(VectorHit(
                            chunk_id=cid,
                            doc_id=did,
                            score=score_map[cid],
                            backend=self.name,
                        ))

                hits.sort(key=lambda h: h.score, reverse=True)
                return hits[:k]
            except Exception as exc:
                logger.warning("FaissDiskBackend: SQLite post-filter failed: %s — returning raw hits", exc)
        else:
            # No DB: return raw results without doc_id
            for cid, score in raw[:k]:
                hits.append(VectorHit(chunk_id=cid, doc_id=0, score=score, backend=self.name))
            return hits

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
            "dirty": self._dirty,
        }

    # ------------------------------------------------------------------
    # Persistence helpers (used by vector_sync.py and CLI)
    # ------------------------------------------------------------------

    def save_index(self, db_conn=None) -> bool:
        """Persist the in-memory FAISS index to disk."""
        ok = self._faiss.save_to_disk(db_conn=db_conn)
        if ok:
            self._dirty = False
        return ok

    def load_index(self, db_conn=None) -> bool:
        """Attempt to restore from disk cache. Returns True on hit."""
        return self._faiss.try_load_from_disk(db_conn=db_conn)

    def rebuild_from_db(self) -> int:
        """
        Rebuild the FAISS index from all chunk_embeddings in SQLite.
        Returns the number of vectors loaded.
        """
        if self.db is None:
            logger.warning("FaissDiskBackend: cannot rebuild — no DB connection")
            return 0

        chunk_ids = []
        embeddings = []
        with self.db.cursor() as c:
            c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
            for row in c.fetchall():
                vec = np.frombuffer(row["embedding"], dtype=np.float32)
                if vec.shape[0] == self.dim:
                    chunk_ids.append(row["chunk_id"])
                    embeddings.append(vec)

        if chunk_ids:
            all_vecs = np.array(embeddings, dtype=np.float32)
            self._faiss.build_from_vectors(chunk_ids, all_vecs)
            logger.info("FaissDiskBackend: rebuilt index with %d vectors", len(chunk_ids))

        return len(chunk_ids)

    @property
    def vector_count(self) -> int:
        return self._faiss.count

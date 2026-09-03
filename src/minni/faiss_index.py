"""
Minni V3.1 — FAISS Index Manager.

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

import threading

import numpy as np

from minni.config import SovereignConfig, DEFAULT_CONFIG

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
        # Guards every read/replace of the index state below. invalidate() is
        # called from the daemon's vault-watch thread while searches run on RPC
        # worker threads: clearing _index / _id_map / _chunk_ids in place under
        # a concurrent search let that search see a half-cleared structure --
        # dropped ids, empty hits, or an attribute error on a None index.
        # Reentrant because search() can fall through to a rebuild.
        self._lock = threading.RLock()
        # Cold/invalidated generation flag. Set only by invalidate(), cleared
        # only by a full rebuild (build_from_vectors) or a successful disk
        # restore. While set, single adds are no-ops: the DB is the source of
        # truth and the next ensure-load rebuilds fully. Without it, an add
        # that interleaves with invalidate() leaves a tiny non-zero index that
        # warm gates then treat as valid — a semantic blackout until restart.
        self._invalidated = False
        # "Warm" for control flow means _ready, not count>0: True only after
        # a VALIDATED full build (build_from_vectors, a committed staged
        # build, or a fenced disk restore). count>0 as the warm test is what
        # let every partially-published state become permanent.
        self._ready = False
        # Monotonic invalidation counter. A staged build commits only if the
        # generation it started from is still current, so an invalidate that
        # lands during the (unlocked) staging window can never be clobbered
        # by the stale build it interrupted.
        self._generation = 0

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

    @property
    def ready(self) -> bool:
        """True only while the index holds a complete, validated build."""
        with self._lock:
            return self._ready

    @property
    def generation(self) -> int:
        """Current invalidation generation (bumped by invalidate())."""
        with self._lock:
            return self._generation

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

        with self._lock:
            self._build_from_vectors_locked(chunk_ids, embeddings)

    def _build_from_vectors_locked(
        self,
        chunk_ids: List[int],
        embeddings: np.ndarray,
    ) -> None:
        self._apply_state_locked(self._construct_state(chunk_ids, embeddings))

    def _construct_state(
        self,
        chunk_ids: List[int],
        embeddings: np.ndarray,
    ) -> Dict:
        """Build a complete index state WITHOUT touching the live fields.

        Pure computation over the inputs (reads only config and the faiss
        module), so it is safe to run off the lock. The result is published
        atomically by _apply_state_locked / commit_staged — intermediate
        build states must never be observable as a warm index.
        """
        state = {
            "chunk_ids": list(chunk_ids),
            "vectors": [embeddings[i] for i in range(len(chunk_ids))],
            "id_map": {i: cid for i, cid in enumerate(chunk_ids)},
            "reverse_map": {cid: i for i, cid in enumerate(chunk_ids)},
            "index": None,
            "type": "numpy",
        }

        n = len(chunk_ids)

        if self._faiss is None:
            # No FAISS — will use numpy fallback in search()
            logger.info("Built numpy fallback index: %d vectors", n)
            return state

        faiss = self._faiss

        # Normalize for cosine similarity (FAISS inner product on normalized = cosine)
        normalized = self._normalize(embeddings)

        if getattr(self.config, "embedding_quantization", "fp32") == "int8":
            quantized = self._build_quantized_index(faiss, normalized, n)
            if quantized is not None:
                state["index"], state["type"] = quantized
                return state
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
            state["index"] = index
            state["type"] = "hnsw"
            logger.info("Built HNSW index: %d vectors (M=%d, ef=%d)",
                        n, self.config.hnsw_m, self.config.hnsw_ef_construction)
        else:
            # Flat index (exact search)
            index = faiss.IndexFlatIP(self.dim)  # Inner product on normalized = cosine
            index.add(normalized)
            state["index"] = index
            state["type"] = "flat"
            logger.info("Built Flat index: %d vectors", n)

        return state

    def _apply_state_locked(self, state: Dict) -> None:
        """Publish a constructed state as the live index, atomically."""
        self._invalidated = False
        self._ready = True
        self._chunk_ids = state["chunk_ids"]
        self._vectors = state["vectors"]
        self._id_map = state["id_map"]
        self._reverse_map = state["reverse_map"]
        self._index = state["index"]
        self._current_type = state["type"]

    def stage_build(self, chunk_ids: List[int], embeddings: np.ndarray) -> Dict:
        """Construct a full index state off to the side, without the lock.

        The live index is untouched: a concurrent search during staging sees
        the previous state (usually cold), never a half-built one. Commit the
        result with commit_staged.
        """
        return self._construct_state(chunk_ids, embeddings)

    def commit_staged(self, state: Dict, expected_generation: int) -> bool:
        """Publish a staged build, unless the world moved since it started.

        Returns False (leaving the index untouched) when an invalidate()
        advanced the generation after the caller sampled it — the staged
        vectors were built from a snapshot that invalidate declared stale,
        and publishing them would resurrect exactly the warm-incomplete
        state the generation flag exists to prevent.
        """
        with self._lock:
            if self._generation != expected_generation:
                return False
            self._apply_state_locked(state)
            return True

    @staticmethod
    def _normalize(embeddings: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms < 1e-8, 1.0, norms)
        return (embeddings / norms).astype(np.float32)

    def _build_quantized_index(self, faiss, normalized: np.ndarray, n: int):
        """Build optional int8 scalar quantization, with fp32 fallback on failure.

        Returns (index, type_label) on success, None if unsupported — the
        caller publishes it; nothing is bound to live state here.
        """
        quantizer = getattr(faiss, "ScalarQuantizer", None)
        quantizer_type = getattr(quantizer, "QT_8bit", None)
        metric = getattr(faiss, "METRIC_INNER_PRODUCT", 0)
        if quantizer_type is None:
            return None

        hnsw_sq = getattr(faiss, "IndexHNSWSQ", None)
        if hnsw_sq is not None:
            try:
                index = hnsw_sq(self.dim, quantizer_type, self.config.hnsw_m, metric)
                if hasattr(index, "hnsw"):
                    index.hnsw.efConstruction = self.config.hnsw_ef_construction
                    index.hnsw.efSearch = self.config.hnsw_ef_search
                index.add(normalized)
                logger.info(
                    "Built int8 HNSW scalar-quantized index: %d vectors (M=%d)",
                    n, self.config.hnsw_m,
                )
                return index, "hnsw-sq-int8"
            except Exception as exc:
                logger.warning("IndexHNSWSQ unavailable or failed: %s", exc)

        scalar_quantizer = getattr(faiss, "IndexScalarQuantizer", None)
        if scalar_quantizer is not None:
            try:
                index = scalar_quantizer(self.dim, quantizer_type, metric)
                if hasattr(index, "is_trained") and not index.is_trained:
                    index.train(normalized)
                index.add(normalized)
                logger.info("Built int8 scalar-quantized index: %d vectors", n)
                return index, "sq-int8"
            except Exception as exc:
                logger.warning("IndexScalarQuantizer unavailable or failed: %s", exc)

        return None

    def add(self, chunk_id: int, embedding: np.ndarray) -> None:
        """
        Add a single vector to the index.
        For bulk operations, use build_from_vectors() instead.
        """
        with self._lock:
            self._add_locked(chunk_id, embedding)

    def _add_locked(self, chunk_id: int, embedding: np.ndarray) -> None:
        # An invalidated index must stay empty until a full rebuild: appending
        # here would resurrect count>0 with only this chunk, and the count>0
        # gate in _ensure_faiss_loaded would then never rebuild from the DB.
        if self._invalidated:
            return
        # GA4-4 follow-up (grok-review round 4, finding 2): a warm start whose
        # reconstruct failed leaves _chunk_ids populated and _vectors empty.
        # Appending to BOTH here would create a partial _vectors that satisfies
        # rebuild()'s `_chunk_ids and _vectors` guard while the arrays are
        # mispaired — the exact desync the empty-_vectors skip path exists to
        # prevent, and the scheduled backfill makes this the steady-state add
        # path on such an index. Recover the raw vectors first; if that fails,
        # update only the live index and id maps and keep _vectors honestly
        # empty so rebuild() keeps taking its logged skip path.
        if self._chunk_ids and len(self._vectors) != len(self._chunk_ids):
            if not self._reconstruct_vectors_from_index():
                self._vectors = []
                self._chunk_ids.append(chunk_id)
                idx = len(self._chunk_ids) - 1
                self._id_map[idx] = chunk_id
                self._reverse_map[chunk_id] = idx
                self._index_add_normalized(embedding)
                return

        self._chunk_ids.append(chunk_id)
        self._vectors.append(embedding.copy())
        idx = len(self._chunk_ids) - 1
        self._id_map[idx] = chunk_id
        self._reverse_map[chunk_id] = idx

        if self._index_add_normalized(embedding):
            # Check if we need to upgrade to HNSW
            if (self._current_type == "flat"
                    and self.config.faiss_index_type == "auto"
                    and self.count >= self.config.hnsw_threshold):
                logger.info("Threshold crossed (%d vectors), rebuilding as HNSW...",
                            self.count)
                all_vecs = np.array(self._vectors, dtype=np.float32)
                self.build_from_vectors(self._chunk_ids, all_vecs)

    def _index_add_normalized(self, embedding: np.ndarray) -> bool:
        """Append one normalized vector to the live FAISS index, if any."""
        if not (self._faiss and self._index is not None):
            return False
        vec = embedding.astype(np.float32).reshape(1, -1)
        norm = np.linalg.norm(vec)
        if norm > 1e-8:
            vec = vec / norm
        self._index.add(vec)
        return True

    def remove(self, chunk_id: int) -> None:
        """
        Remove a vector by chunk_id.
        Marks it for exclusion — actual removal happens on next rebuild.
        """
        with self._lock:
            if chunk_id in self._reverse_map:
                idx = self._reverse_map.pop(chunk_id)
                self._id_map.pop(idx, None)
            # Note: FAISS doesn't support true deletion for HNSW.
            # We filter results in search() instead. Periodic rebuild cleans up.

    def add_batch(self, chunk_ids: List[int], embeddings: List[np.ndarray]) -> bool:
        """Add several vectors as one atomic operation, only while warm.

        The warm check happens under the lock, so invalidate() can run either
        before the whole batch (nothing lands) or after it (everything is
        cleared together) — never between two adds, which is the interleave
        that leaves a partial residual index. Returns False when cold or
        invalidated: the caller's rows are already in the DB.

        SKIP FLOOR: a False return does NOT mean "the next ensure-load
        rebuilds fully". Under the search-deadline floor
        (SEARCH_FAISS_REBUILD_MIN_REMAINING_S = 27s) an in-request ensure
        after a disk miss SKIPS the rebuild — default leftover (22.5s / 27s)
        is at or below the floor — so a cold index stays cold for every
        default-budget search. Any path that leaves the index cold (this
        return, invalidate()) MUST unbounded-ensure off the RPC
        (deadline None, e.g. _warmup_models / _ensure_vault_engines_unbounded)
        or the new rows are FTS-only until a large-budget search arrives.
        """
        with self._lock:
            if not self._ready:
                return False
            for cid, emb in zip(chunk_ids, embeddings):
                # Idempotent: a retrying caller may race a rebuild that
                # already picked these rows up from the DB.
                if cid in self._reverse_map:
                    continue
                self._add_locked(cid, emb)
            return True

    def remove_batch(self, chunk_ids: List[int]) -> bool:
        """Tombstone several chunk_ids as one atomic operation, only while warm.

        Same contract as add_batch: warm/invalidated is re-checked under the
        lock, and the whole multi-id operation holds it.
        """
        with self._lock:
            if not self._ready:
                return False
            for cid in chunk_ids:
                if cid in self._reverse_map:
                    idx = self._reverse_map.pop(cid)
                    self._id_map.pop(idx, None)
            return True

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
        # Held for the whole read: the index and the id maps must be observed
        # as one consistent snapshot, or a concurrent invalidate()/rebuild
        # resolves internal indices against maps that no longer match them.
        with self._lock:
            return self._search_locked(query_embedding, top_k)

    def _search_locked(
        self,
        query_embedding: np.ndarray,
        top_k: int = 20,
    ) -> List[Tuple[int, float]]:
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

    def _reconstruct_vectors_from_index(self) -> bool:
        """Recover raw vectors from the live FAISS index. Returns True on success.

        GA4-4: load_from_disk restores the FAISS index but sets _vectors = [],
        and every rebuild path is guarded on _vectors being non-empty — so after
        a warm start rebuild() silently did nothing and chunks removed since the
        snapshot stayed searchable. FAISS can hand the vectors back via
        reconstruct_n, which makes the warm-start index rebuildable again.

        Best-effort by contract: a quantized index reconstructs lossily and some
        index types cannot reconstruct at all. Callers must treat False as "no
        raw vectors", never as "no vectors exist".
        """
        if self._index is None or not self._chunk_ids:
            return False
        try:
            count = int(self._index.ntotal)
            if count <= 0:
                return False
            restored = self._index.reconstruct_n(0, count)
            vectors = [np.array(restored[i], dtype=np.float32) for i in range(count)]
            if len(vectors) != len(self._chunk_ids):
                logger.warning(
                    "FAISS reconstruct returned %d vectors for %d chunk ids; "
                    "leaving raw vectors empty rather than mispairing them",
                    len(vectors), len(self._chunk_ids),
                )
                return False
            self._vectors = vectors
            return True
        except Exception as exc:
            logger.debug("FAISS vector reconstruction unavailable: %s", exc)
            return False

    def rebuild(self) -> None:
        """Force a full rebuild from stored vectors (cleans up deletions)."""
        with self._lock:
            self._rebuild_locked()

    def _rebuild_locked(self) -> None:
        if self._chunk_ids and not self._vectors:
            # GA4-4: a disk-restored index has chunk ids but no raw vectors.
            # Try to recover them rather than falling through to a silent
            # no-op that leaves removed chunks searchable.
            if not self._reconstruct_vectors_from_index():
                logger.warning(
                    "FAISS rebuild skipped: %d chunk id(s) but no raw vectors, "
                    "and the index could not reconstruct them. Removals will "
                    "not be compacted until the index is rebuilt from the DB.",
                    len(self._chunk_ids),
                )
                return
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
                self._ready = False
                self._chunk_ids = []
                self._vectors = []
                self._id_map = {}
                self._reverse_map = {}
                self._index = None

    def invalidate(self) -> None:
        """Drop the in-memory index so the next search reloads it from the DB.

        Not a delete: ``chunk_embeddings`` is untouched. This clears ``ready``
        and advances the generation, which is what
        ``RetrievalEngine._ensure_faiss_loaded`` gates its rebuild on — so an
        out-of-process writer that added chunk rows can make a warm engine
        pick them up without a restart. The gate is READINESS, not count>0:
        a partially re-populated index must never read as warm.

        Taken under the lock, and as a single rebind of each attribute rather
        than in-place mutation, so a concurrent search sees either the old
        index or an empty one -- never a partly cleared one.
        """
        with self._lock:
            self._invalidated = True
            self._ready = False
            self._generation += 1
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
        explicit_manifest = getattr(self.config, "faiss_manifest_path", None)
        if explicit_manifest:
            return os.fspath(explicit_manifest)

        from minni.faiss_persist import _faiss_dir_for_db
        faiss_dir = _faiss_dir_for_db(self.config.db_path)
        return os.path.join(faiss_dir, "index.manifest.json")

    def try_load_from_disk(self, db_conn=None) -> bool:
        """
        Attempt to load the FAISS index from disk cache.

        Returns True if the cache was valid and loaded, False on miss.
        Call this before building from DB on cold start.
        """
        from minni.faiss_persist import compute_db_checksum, load

        manifest = self._manifest_path()

        # Compute current DB checksum (requires a live connection)
        if db_conn is None:
            logger.debug("No DB connection for checksum; skipping disk load")
            return False

        generation = self.generation
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

        # Hold the lock for the whole restore: concurrent searches (e.g. after
        # invalidate() triggers a reload) must never observe a half-applied
        # index/id-map state. _lock is an RLock, so build_from_vectors below
        # re-acquiring it is fine.
        with self._lock:
            # Fence: the cache was validated against the checksum sampled
            # BEFORE the slow disk read. If the DB moved or an invalidate()
            # advanced the generation since, applying would clear the cold
            # state on data that is already stale — warm-and-wrong, which
            # count/ready gates then protect. Miss instead; the caller falls
            # through to a DB rebuild that sees the current rows.
            if (self._generation != generation
                    or compute_db_checksum(db_conn) != checksum):
                logger.info(
                    "FAISS disk cache discarded: DB or generation moved "
                    "during the load"
                )
                return False
            if faiss_index is not None:
                # Restore from FAISS index: rebuild id maps from chunk_id_order
                self._invalidated = False
                self._ready = True
                self._index = faiss_index
                self._chunk_ids = list(chunk_ids)
                self._id_map = {i: cid for i, cid in enumerate(chunk_ids)}
                self._reverse_map = {cid: i for i, cid in enumerate(chunk_ids)}
                # GA4-4: this used to set _vectors = [] and leave it there, which
                # made the warm-started index permanently un-rebuildable — every
                # rebuild path is guarded on _vectors, so removals were never
                # compacted and deleted chunks stayed searchable. Recover the raw
                # vectors from the index itself; if this index type cannot
                # reconstruct, rebuild() now says so out loud instead of no-opping.
                self._vectors = []
                self._reconstruct_vectors_from_index()
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

    def save_to_disk(self, db_conn=None, db_checksum: str = None) -> bool:
        """
        Save the current index to disk.

        Args:
            db_conn: An open sqlite3.Connection for computing the DB checksum.
            db_checksum: Precomputed checksum to record instead. Callers that
                built the index from a known DB snapshot pass the checksum of
                THAT snapshot, so the manifest can never claim a checksum the
                saved vectors do not correspond to.

        Returns:
            True on success, False if save is skipped or fails.
        """
        from minni.faiss_persist import compute_db_checksum, save

        # Held across the whole save: _index/_vectors/_chunk_ids must be
        # written as one consistent snapshot. Atomic rename prevents torn
        # files, not a consistent-but-wrong one assembled while a concurrent
        # invalidate/build/add was replacing the state mid-save.
        with self._lock:
            if self.count == 0 or not self._ready:
                logger.debug("Skipping FAISS save: index is empty or not validated")
                return False

            checksum = db_checksum
            if checksum is None:
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

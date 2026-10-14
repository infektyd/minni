"""FAISS vector search, Cross-Encoder reranking, RRF fusion, and backend resolution."""

import logging
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from minni.request_deadline import RequestDeadlineExceeded

logger = logging.getLogger("sovereign.retrieval")


class SemanticRerankMixin:
    """FAISS semantic search, Cross-Encoder reranking, RRF fusion, and vector backends."""

    _CPU_PREDICT_BATCH_CAP = 8
    _KNOWN_BACKENDS = ("faiss-disk", "faiss-mem")
    _MAX_BACKENDS = 2

    def _semantic_search(
        self,
        query: str,
        limit: int,
        agent_filter: Optional[Sequence[str]] = None,
    ) -> List[Dict]:
        """Semantic search via FAISS index. Returns best-chunk-per-doc."""
        import minni.retrieval as _ret

        deadline_monotonic = self._current_deadline()
        query_emb = self._encode_query(query, deadline_monotonic=deadline_monotonic)
        if query_emb.size == 0:
            return []
        if _ret.past_search_deadline(deadline_monotonic):
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return []

        search_limit = limit * 5
        faiss_results = self.faiss_index.search(query_emb, top_k=search_limit)

        if not faiss_results:
            if _ret.past_search_deadline(deadline_monotonic):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return []
            self._ensure_faiss_loaded()
            if _ret.past_search_deadline(deadline_monotonic) or (
                not self.faiss_index.ready
                and (
                    _ret.should_skip_faiss_rebuild(deadline_monotonic)
                    or self._deadline_skipped_vector()
                )
            ):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return []
            faiss_results = self.faiss_index.search(query_emb, top_k=search_limit)

        if not faiss_results:
            return []

        chunk_ids = [cid for cid, _ in faiss_results]
        score_map = {cid: score for cid, score in faiss_results}

        doc_best: Dict[int, Dict] = {}
        agent_scope = self._normalize_agent_filter(agent_filter)

        with self.db.cursor() as c:
            placeholders = ",".join("?" * len(chunk_ids))
            agent_clause = ""
            params: list = list(chunk_ids)
            if agent_scope:
                agent_clause = f" AND d.agent IN ({','.join('?' * len(agent_scope))})"
                params.extend(agent_scope)
            c.execute(
                f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id IN ({placeholders})
                {agent_clause}
                """,
                params,
            )

            for row in c.fetchall():
                cid = row["chunk_id"]
                did = row["doc_id"]
                sim = score_map.get(cid, 0.0)

                if did not in doc_best or sim > doc_best[did]["similarity"]:
                    doc_best[did] = {
                        "doc_id": did,
                        "chunk_id": cid,
                        "path": row["path"],
                        "agent": row["agent"],
                        "sigil": row["sigil"],
                        "similarity": sim,
                        "chunk_text": row["chunk_text"],
                        "heading_context": row["heading_context"] or "",
                        "decay_score": row["decay_score"] or 1.0,
                        "page_status": row["page_status"] or "candidate",
                        "privacy_level": row["privacy_level"] or "safe",
                        "page_type": row["page_type"],
                        "evidence_refs": row["evidence_refs"],
                        "indexed_at": row["indexed_at"],
                        "layer": row["layer"] or "knowledge",
                    }

        results = sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)
        return results[: limit * 3]

    def _ensure_faiss_loaded(self) -> None:
        """Load FAISS index from disk cache or rebuild from DB if cold."""
        import minni.retrieval as _ret

        if self.faiss_index.ready:
            return
        if _ret.past_search_deadline(self._current_deadline()):
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return

        timeout = _ret.faiss_load_lock_timeout(self._current_deadline())
        if timeout is None:
            acquired = self._faiss_load_lock.acquire()
        else:
            acquired = self._faiss_load_lock.acquire(timeout=timeout)
        if not acquired:
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return
        try:
            if self.faiss_index.ready:
                return
            if _ret.past_search_deadline(self._current_deadline()):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return

            try:
                conn = self.db._get_conn()
                if self.faiss_index.try_load_from_disk(db_conn=conn):
                    return
            except RequestDeadlineExceeded:
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return
            except Exception as e:
                logger.debug("Disk cache load failed (non-fatal): %s", e)

            if _ret.should_skip_faiss_rebuild(self._current_deadline()):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return

            from minni.faiss_persist import compute_db_checksum

            conn = self.db._get_conn()

            for _ in range(3):
                generation = self.faiss_index.generation
                checksum_before = compute_db_checksum(conn)

                chunk_ids = []
                embeddings = []
                with self.db.cursor() as c:
                    c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
                    for row in c.fetchall():
                        vec = np.frombuffer(row["embedding"], dtype=np.float32)
                        if vec.shape[0] == self.config.embedding_dim:
                            chunk_ids.append(row["chunk_id"])
                            embeddings.append(vec)

                if not chunk_ids:
                    self.faiss_index.invalidate()
                    return

                all_vecs = np.array(embeddings, dtype=np.float32)
                staged = self.faiss_index.stage_build(chunk_ids, all_vecs)

                if compute_db_checksum(conn) != checksum_before:
                    logger.debug("DB changed during FAISS rebuild; retrying")
                    continue

                if not self.faiss_index.commit_staged(staged, generation):
                    logger.debug("FAISS invalidated during rebuild; retrying")
                    continue

                logger.info("FAISS index loaded from DB: %d vectors", len(chunk_ids))
                try:
                    self.faiss_index.save_to_disk(
                        db_conn=conn, db_checksum=checksum_before
                    )
                except Exception as e:
                    logger.debug("FAISS disk save failed (non-fatal): %s", e)
                return

            logger.warning(
                "FAISS rebuild could not get a stable DB snapshot; leaving the "
                "index cold for the next search"
            )
            self.faiss_index.invalidate()
        finally:
            self._faiss_load_lock.release()

    def _remove_live_faiss(self, chunk_ids: List[int]) -> None:
        """Tombstone superseded chunk_ids out of the live FAISS index."""
        if not chunk_ids:
            return
        try:
            self.faiss_index.remove_batch(chunk_ids)
        except Exception as exc:
            logger.debug("durable-index: live FAISS remove skipped: %s", exc)

    def _refresh_live_faiss(
        self, chunk_ids: List[int], vectors: List[np.ndarray]
    ) -> bool:
        """Make new chunks searchable in the live FAISS index without restart."""
        if not chunk_ids:
            return True
        try:
            if not self.faiss_index.add_batch(chunk_ids, vectors):
                with self._faiss_load_lock:
                    self.faiss_index.add_batch(chunk_ids, vectors)
            return self.faiss_index.ready
        except Exception as exc:
            logger.warning(
                "durable-index: live FAISS refresh failed (%s) — invalidating "
                "so next search reloads from DB",
                exc,
            )
            try:
                self.faiss_index.invalidate()
            except Exception:
                pass
            try:
                self._set_current_deadline(None)
                self._ensure_faiss_loaded()
            except Exception as reload_exc:
                logger.debug(
                    "durable-index: unbounded FAISS reload after refresh "
                    "failure skipped: %s",
                    reload_exc,
                )

        return self.faiss_index.ready

    def _apply_correction_rerank_boost(self, candidates: List[Dict]) -> None:
        """Propagate correction salience boost sign-safely into cross-encoder logits."""
        boost = float(self.config.correction_salience_boost)
        if boost <= 0:
            return
        for c in candidates:
            page_type = str(c.get("page_type") or "").lower()
            if page_type not in self._correction_types:
                continue
            score = float(c.get("rerank_score") or 0.0)
            if score > 0:
                c["rerank_score"] = score * (1.0 + boost)
            elif score < 0:
                c["rerank_score"] = score / (1.0 + boost)
            else:
                c["rerank_score"] = boost
            c["salience_boost"] = boost

    def _apply_decay_rerank_attenuation(self, candidates: List[Dict]) -> None:
        """Propagate memory decay sign-safely into cross-encoder logits."""
        floor = float(self.config.correction_decay_floor)
        for c in candidates:
            decay = c.get("decay_score")
            if decay is None:
                decay = 1.0
            decay = float(decay)
            page_type = str(c.get("page_type") or "").lower()
            if page_type in self._correction_types:
                decay = max(decay, floor)
            decay = max(0.0, min(1.0, decay))
            c["decay_applied"] = decay
            if decay == 1.0:
                continue
            score = float(c.get("rerank_score") or 0.0)
            if score > 0:
                c["rerank_score"] = score * decay
            elif score < 0:
                c["rerank_score"] = score / decay if decay > 0 else score * 2.0
            else:
                c["rerank_score"] = decay - 1.0

    def _apply_rerank_score_adjustments(self, candidates: List[Dict]) -> None:
        """Preserve model-pure raw_rerank_score, then apply correction boost and decay."""
        for c in candidates:
            c["raw_rerank_score"] = float(c.get("rerank_score") or 0.0)
        self._apply_correction_rerank_boost(candidates)
        self._apply_decay_rerank_attenuation(candidates)

    def _pinned_cpu_cross_encoder_predict_kwargs(self) -> dict:
        """Kwargs for CrossEncoder.predict on the verified pinned-CPU path."""
        kwargs = {"show_progress_bar": False}
        raw = getattr(self.config, "reranker_cpu_predict_batch_size", 8)
        if type(raw) is not int or raw < 1:
            return kwargs
        kwargs["batch_size"] = min(raw, self._CPU_PREDICT_BATCH_CAP)
        return kwargs

    def _rerank(self, query: str, candidates: List[Dict]) -> List[Dict]:
        """Re-rank candidates using the cross-encoder with generation-scoped caching."""
        import minni.retrieval as _ret

        reranker = self.reranker
        if not reranker or not candidates:
            return candidates

        model_name, model_version = self._reranker_identity(reranker)
        try:
            from minni.rerank_cache import GLOBAL_RERANK_CACHE

            cache = GLOBAL_RERANK_CACHE
        except Exception:
            cache = None

        missing = []
        missing_indexes = []
        all_scores = [None] * len(candidates)
        corpus = self._rerank_corpus()
        generation = cache.generation if cache is not None else None
        if cache is not None:
            for i, c in enumerate(candidates):
                chunk_id = c.get("chunk_id")
                if chunk_id is None:
                    missing.append(c)
                    missing_indexes.append(i)
                    continue
                cached = cache.get(
                    model_name,
                    model_version,
                    query,
                    int(chunk_id),
                    corpus=corpus,
                    passage=self._rerank_passage(c),
                )
                if cached is None:
                    missing.append(c)
                    missing_indexes.append(i)
                else:
                    all_scores[i] = cached
        else:
            missing = candidates
            missing_indexes = list(range(len(candidates)))

        if not missing:
            for i, c in enumerate(candidates):
                c["rerank_score"] = float(all_scores[i])
            self._apply_rerank_score_adjustments(candidates)
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
            return candidates

        pairs = [[query, self._rerank_passage(c)] for c in missing]

        from minni.models import (
            cross_encoder_unlocked_predict_safe,
            get_cross_encoder_lock,
        )

        try:
            if _ret.past_search_deadline(self._current_deadline()):
                self.last_rerank_degraded = "search deadline; skipped rerank"
                return candidates
            if cross_encoder_unlocked_predict_safe():
                scores = reranker.predict(
                    pairs, **self._pinned_cpu_cross_encoder_predict_kwargs()
                )
                if _ret.past_search_deadline(self._current_deadline(), min_remaining=0):
                    self.last_rerank_degraded = (
                        "search deadline exceeded during nonpreemptible rerank"
                    )
            else:
                with _ret.search_model_lock(
                    get_cross_encoder_lock(), self._current_deadline()
                ) as acquired:
                    if not acquired or _ret.past_search_deadline(self._current_deadline()):
                        self.last_rerank_degraded = "search deadline; skipped rerank"
                        return candidates
                    scores = reranker.predict(pairs, show_progress_bar=False)
                    if _ret.past_search_deadline(
                        self._current_deadline(), min_remaining=0
                    ):
                        self.last_rerank_degraded = (
                            "search deadline exceeded during nonpreemptible rerank"
                        )

            for score_index, c, score_value in zip(missing_indexes, missing, scores):
                score = float(score_value)
                all_scores[score_index] = score
                chunk_id = c.get("chunk_id")
                if cache is not None and chunk_id is not None:
                    cache.set(
                        model_name,
                        model_version,
                        query,
                        int(chunk_id),
                        score,
                        corpus=corpus,
                        passage=self._rerank_passage(c),
                        expected_generation=generation,
                    )

            for i, c in enumerate(candidates):
                c["rerank_score"] = float(all_scores[i] or 0.0)

            self._apply_rerank_score_adjustments(candidates)
            candidates.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        except Exception as e:
            logger.warning("Re-ranking failed: %s — falling back to RRF scores", e)
            self._current_state().rerank_degraded = str(e)

        return candidates

    def _rerank_corpus(self) -> str:
        db_path = getattr(self.config, "db_path", None)
        if not db_path or str(db_path) == ":memory:":
            db = getattr(self, "db", None)
            return f"memory:{id(db if db is not None else self)}"
        return str(Path(db_path).expanduser().resolve())

    @staticmethod
    def _rerank_passage(candidate: Dict) -> str:
        passage = candidate.get("chunk_text", "") or ""
        heading = candidate.get("heading_context", "")
        return f"[{heading}] {passage}" if heading else passage

    def _reranker_identity(self, reranker) -> tuple[str, str]:
        model_name = getattr(self.config, "reranker_model", None) or "unknown"
        for attr in ("model_name", "name"):
            value = getattr(reranker, attr, None)
            if value:
                model_name = str(value)
                break
        model_version = "unknown"
        for attr in ("model_version", "version", "revision"):
            value = getattr(reranker, attr, None)
            if value:
                model_version = str(value)
                break
        return model_name, model_version

    def _score_merged_doc(self, d: Dict) -> None:
        """Compute final_score for one RRF-merged doc with correction salience and decay floor."""
        boost = 0.0
        decay = d.get("decay_score")
        if decay is None:
            decay = 1.0
        page_type = str(d.get("page_type") or "").lower()
        if page_type in self._correction_types:
            boost = float(self.config.correction_salience_boost)
            floor = float(self.config.correction_decay_floor)
            decay = max(decay, floor)
        d["salience_boost"] = boost
        decay = max(0.0, min(1.0, float(decay)))
        d["decay_applied"] = decay
        d["final_score"] = d["rrf_score"] * decay * (1.0 + boost)

    def _rrf_merge(
        self,
        fts_results: List[Dict],
        semantic_results: List[Dict],
        limit: int,
    ) -> List[Dict]:
        """Reciprocal Rank Fusion of FTS5 and semantic results with FTS slot reservation."""
        k = self.config.rrf_k
        doc_scores: Dict[int, Dict] = {}

        for rank, r in enumerate(fts_results, start=1):
            did = r["doc_id"]
            if did not in doc_scores:
                doc_scores[did] = {
                    "doc_id": did,
                    "path": r["path"],
                    "agent": r["agent"],
                    "sigil": r["sigil"],
                    "rrf_score": 0.0,
                    "decay_score": r.get("decay_score", 1.0),
                    "fts_rank": rank,
                    "sem_rank": None,
                    "chunk_text": r.get("chunk_text", ""),
                    "heading_context": r.get("heading_context", ""),
                    "page_status": r.get("page_status", "candidate"),
                    "privacy_level": r.get("privacy_level", "safe"),
                    "page_type": r.get("page_type"),
                    "evidence_refs": r.get("evidence_refs"),
                    "indexed_at": r.get("indexed_at"),
                    "layer": r.get("layer", "knowledge"),
                }
            doc_scores[did]["rrf_score"] += self.config.fts_weight / (k + rank)

        for rank, r in enumerate(semantic_results, start=1):
            did = r["doc_id"]
            if did not in doc_scores:
                doc_scores[did] = {
                    "doc_id": did,
                    "path": r["path"],
                    "agent": r["agent"],
                    "sigil": r["sigil"],
                    "rrf_score": 0.0,
                    "decay_score": r.get("decay_score", 1.0),
                    "fts_rank": None,
                    "sem_rank": rank,
                    "chunk_text": r.get("chunk_text", ""),
                    "heading_context": r.get("heading_context", ""),
                    "page_status": r.get("page_status", "candidate"),
                    "privacy_level": r.get("privacy_level", "safe"),
                    "page_type": r.get("page_type"),
                    "evidence_refs": r.get("evidence_refs"),
                    "indexed_at": r.get("indexed_at"),
                    "layer": r.get("layer", "knowledge"),
                }
            doc_scores[did]["rrf_score"] += self.config.semantic_weight / (k + rank)
            if doc_scores[did]["sem_rank"] is None:
                doc_scores[did]["sem_rank"] = rank
            if (
                r.get("similarity") is not None
                and doc_scores[did].get("similarity") is None
            ):
                doc_scores[did]["similarity"] = r["similarity"]
            if r.get("chunk_text"):
                doc_scores[did]["chunk_text"] = r["chunk_text"]
                doc_scores[did]["heading_context"] = r.get("heading_context", "")
                if r.get("chunk_id") is not None:
                    doc_scores[did]["chunk_id"] = r["chunk_id"]
                else:
                    doc_scores[did].pop("chunk_id", None)
            if not doc_scores[did].get("page_type") and r.get("page_type"):
                doc_scores[did]["page_type"] = r["page_type"]
            if not doc_scores[did].get("evidence_refs") and r.get("evidence_refs"):
                doc_scores[did]["evidence_refs"] = r["evidence_refs"]
            if not doc_scores[did].get("indexed_at") and r.get("indexed_at"):
                doc_scores[did]["indexed_at"] = r["indexed_at"]
            if not doc_scores[did].get("layer") and r.get("layer"):
                doc_scores[did]["layer"] = r["layer"]

        for d in doc_scores.values():
            self._score_merged_doc(d)

        ranked = sorted(
            doc_scores.values(), key=lambda x: x["final_score"], reverse=True
        )
        return self._reserve_top_fts_slots(ranked, fts_results, doc_scores, limit)

    def _note_vector_model_down(self) -> None:
        """Raise the P0-B degradation flag, logging once per outage."""
        if not self.vector_model_down:
            logger.warning(
                "semantic leg DOWN: embedding model unavailable — recall "
                "degraded to lexical (FTS) only until the encoder loads"
            )
        self.vector_model_down = True
        self._current_state().vector_degraded = (
            "embedding model unavailable; lexical (FTS) only"
        )

    def _reset_encode_ms(self) -> None:
        local = getattr(self, "_degradation_local", None)
        if local is None:
            local = threading.local()
            self._degradation_local = local
        local.encode_ms = 0.0

    def _add_encode_ms(self, ms: float) -> None:
        local = getattr(self, "_degradation_local", None)
        if local is None:
            local = threading.local()
            self._degradation_local = local
        local.encode_ms = float(getattr(local, "encode_ms", 0.0) or 0.0) + ms

    def _take_encode_ms(self) -> float:
        local = getattr(self, "_degradation_local", None)
        if local is None:
            return 0.0
        ms = float(getattr(local, "encode_ms", 0.0) or 0.0)
        local.encode_ms = 0.0
        return ms

    def _lookup_query_embedding(self, query: str):
        import minni.retrieval as _ret

        embeddings = getattr(
            getattr(self, "_degradation_local", None), "query_embeddings", None
        )
        if embeddings is not None and query in embeddings:
            return embeddings[query].copy()
        memo = _ret.current_query_embed_cache()
        if memo is None:
            return None
        cached = memo.get(query)
        if cached is None:
            return None
        if embeddings is not None:
            embeddings[query] = cached.copy()
        return cached

    def _store_query_embedding(self, query: str, vec: np.ndarray) -> None:
        import minni.retrieval as _ret

        if vec is None or vec.size == 0:
            return
        embeddings = getattr(
            getattr(self, "_degradation_local", None), "query_embeddings", None
        )
        if embeddings is not None:
            embeddings[query] = vec.copy()
        memo = _ret.current_query_embed_cache()
        if memo is not None:
            memo.set(query, vec)

    def _encode_query(
        self,
        query: str,
        deadline_monotonic: Optional[float] = None,
    ) -> np.ndarray:
        """Encode ``query``, raising the P0-B flag when the encoder is down."""
        import minni.retrieval as _ret

        if deadline_monotonic is None:
            deadline_monotonic = self._current_deadline()
        if _ret.past_search_deadline(deadline_monotonic):
            self.last_vector_degraded = "search deadline; lexical (FTS) only"
            return np.array([], dtype=np.float32)
        cached = self._lookup_query_embedding(query)
        if cached is not None:
            return cached
        try:
            from minni.models import get_embedder, get_embedder_lock

            if _ret.should_skip_cold_model_load(deadline_monotonic, get_embedder):
                self.last_vector_degraded = "search deadline; lexical (FTS) only"
                return np.array([], dtype=np.float32)

            with _ret.search_model_lock(
                get_embedder_lock(), deadline_monotonic
            ) as acquired:
                if not acquired or _ret.past_search_deadline(deadline_monotonic):
                    self.last_vector_degraded = "search deadline; lexical (FTS) only"
                    return np.array([], dtype=np.float32)
                if _ret.should_skip_cold_model_load(deadline_monotonic, get_embedder):
                    self.last_vector_degraded = "search deadline; lexical (FTS) only"
                    return np.array([], dtype=np.float32)
                cached = self._lookup_query_embedding(query)
                if cached is not None:
                    return cached
                if not self.model:
                    self._note_vector_model_down()
                    return np.array([], dtype=np.float32)
                started = time.perf_counter()
                try:
                    vec = self.model.encode(
                        query, show_progress_bar=False
                    ).astype(np.float32)
                finally:
                    self._add_encode_ms((time.perf_counter() - started) * 1000)
                if _ret.past_search_deadline(deadline_monotonic, min_remaining=0):
                    self.last_vector_degraded = (
                        "search deadline exceeded during nonpreemptible encode"
                    )
        except Exception as exc:
            if not self.vector_model_down:
                logger.warning(
                    "semantic leg DOWN: embedding encode failed (%s) — "
                    "recall degraded to lexical (FTS) only",
                    exc,
                )
            self.vector_model_down = True
            self._current_state().vector_degraded = (
                f"embedding encode failed: {exc}"[:200]
            )
            return np.array([], dtype=np.float32)
        self.vector_model_down = False
        self._store_query_embedding(query, vec)
        return vec

    def _normalize_backend_names(self, backend_names: list) -> list:
        """Dedup (order-preserving), cap length, and reject unknown members."""
        seen: set = set()
        deduped: list = []
        for raw in backend_names:
            name = str(raw)
            if name not in self._KNOWN_BACKENDS:
                raise ValueError(
                    f"unknown backend {name!r}; known backends: "
                    f"{list(self._KNOWN_BACKENDS)}"
                )
            if name not in seen:
                seen.add(name)
                deduped.append(name)
        if len(deduped) > self._MAX_BACKENDS:
            raise ValueError(
                f"too many backends ({len(deduped)}); max {self._MAX_BACKENDS}"
            )
        return deduped

    def _resolve_backends(self, backend_names: list) -> list:
        """Resolve a list of backend name strings to VectorBackend instances."""
        resolved = []
        for name in self._normalize_backend_names(backend_names):
            if name == "faiss-disk":
                from minni.backends.faiss_disk import FaissDiskBackend

                resolved.append(FaissDiskBackend(self.config, self.db))
            elif name == "faiss-mem":
                from minni.backends.faiss_mem import FaissMemBackend

                resolved.append(FaissMemBackend(self.config))
        return resolved

    def _hits_to_dicts(self, hits, query_emb: np.ndarray) -> List[Dict]:
        """Convert VectorHit list to the dict format used by _rrf_merge."""
        if not hits:
            return []

        chunk_ids = [h.chunk_id for h in hits]
        score_map = {h.chunk_id: (h.score, h.backend) for h in hits}
        doc_best: Dict[int, Dict] = {}

        with self.db.cursor() as c:
            placeholders = ",".join("?" * len(chunk_ids))
            c.execute(
                f"""
                SELECT ce.chunk_id, ce.doc_id, ce.chunk_text, ce.heading_context,
                       d.path, d.agent, d.sigil, d.decay_score,
                       d.page_status, d.privacy_level, d.page_type,
                       d.evidence_refs, d.indexed_at,
                       COALESCE(ce.layer, d.layer, 'knowledge') AS layer
                FROM chunk_embeddings ce
                JOIN documents d ON d.doc_id = ce.doc_id
                WHERE ce.chunk_id IN ({placeholders})
                """,
                chunk_ids,
            )

            for row in c.fetchall():
                cid = row["chunk_id"]
                did = row["doc_id"]
                sim, bname = score_map.get(cid, (0.0, "unknown"))

                if did not in doc_best or sim > doc_best[did]["similarity"]:
                    doc_best[did] = {
                        "doc_id": did,
                        "chunk_id": cid,
                        "path": row["path"],
                        "agent": row["agent"],
                        "sigil": row["sigil"],
                        "similarity": sim,
                        "chunk_text": row["chunk_text"],
                        "heading_context": row["heading_context"] or "",
                        "decay_score": row["decay_score"] or 1.0,
                        "page_status": row["page_status"] or "candidate",
                        "privacy_level": row["privacy_level"] or "safe",
                        "page_type": row["page_type"],
                        "evidence_refs": row["evidence_refs"],
                        "indexed_at": row["indexed_at"],
                        "layer": row["layer"] or "knowledge",
                        "backend_name": bname,
                    }

        return sorted(doc_best.values(), key=lambda x: x["similarity"], reverse=True)

    def _backend_search(
        self,
        query_emb: np.ndarray,
        limit: int,
        backend,
    ) -> List[Dict]:
        """Search using an explicit VectorBackend instance."""
        if query_emb.size == 0:
            return []
        hits = backend.search(query_emb, k=limit * 5, filter=None)
        if not hits:
            return []
        return self._hits_to_dicts(hits, query_emb)[: limit * 3]

    def _rrf_merge_multi(
        self,
        fts_results: List[Dict],
        semantic_results: List[Dict],
        extra_backend_results: List[List[Dict]],
        limit: int,
    ) -> List[Dict]:
        """RRF merge: FTS + semantic + Nth backend stream(s)."""
        k = self.config.rrf_k
        doc_scores: Dict[int, Dict] = {}

        def _add_stream(ranked_list, weight, stream_label):
            for rank, r in enumerate(ranked_list, start=1):
                did = r["doc_id"]
                if did not in doc_scores:
                    doc_scores[did] = {
                        "doc_id": did,
                        "path": r.get("path", ""),
                        "agent": r.get("agent", ""),
                        "sigil": r.get("sigil", ""),
                        "rrf_score": 0.0,
                        "decay_score": r.get("decay_score", 1.0),
                        "fts_rank": None,
                        "sem_rank": None,
                        "chunk_text": r.get("chunk_text", ""),
                        "heading_context": r.get("heading_context", ""),
                        "page_status": r.get("page_status", "candidate"),
                        "privacy_level": r.get("privacy_level", "safe"),
                        "page_type": r.get("page_type"),
                        "evidence_refs": r.get("evidence_refs"),
                        "indexed_at": r.get("indexed_at"),
                        "layer": r.get("layer", "knowledge"),
                        "backend_name": r.get("backend_name", "faiss-disk"),
                    }
                doc_scores[did]["rrf_score"] += weight / (k + rank)
                if stream_label == "fts":
                    doc_scores[did]["fts_rank"] = rank
                elif stream_label == "sem" and doc_scores[did]["sem_rank"] is None:
                    doc_scores[did]["sem_rank"] = rank
                if r.get("chunk_text") and not doc_scores[did].get("_sem_chunk"):
                    doc_scores[did]["chunk_text"] = r["chunk_text"]
                    doc_scores[did]["heading_context"] = r.get("heading_context", "")
                    if stream_label != "fts":
                        doc_scores[did]["_sem_chunk"] = True
                        if r.get("chunk_id") is not None:
                            doc_scores[did]["chunk_id"] = r["chunk_id"]
                if not doc_scores[did].get("page_type") and r.get("page_type"):
                    doc_scores[did]["page_type"] = r["page_type"]
                if not doc_scores[did].get("evidence_refs") and r.get("evidence_refs"):
                    doc_scores[did]["evidence_refs"] = r["evidence_refs"]
                if not doc_scores[did].get("indexed_at") and r.get("indexed_at"):
                    doc_scores[did]["indexed_at"] = r["indexed_at"]
                if not doc_scores[did].get("layer") and r.get("layer"):
                    doc_scores[did]["layer"] = r["layer"]

        _add_stream(fts_results, self.config.fts_weight, "fts")
        _add_stream(semantic_results, self.config.semantic_weight, "sem")
        for extra in extra_backend_results:
            _add_stream(extra, self.config.semantic_weight, "extra")

        for d in doc_scores.values():
            d.pop("_sem_chunk", None)
            self._score_merged_doc(d)

        ranked = sorted(
            doc_scores.values(), key=lambda x: x["final_score"], reverse=True
        )
        return self._reserve_top_fts_slots(ranked, fts_results, doc_scores, limit)

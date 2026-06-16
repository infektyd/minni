"""``naive_rag`` — the textbook chunk -> embed -> top-k FAISS baseline (§4).

The plain vector-store baseline, with NO governance and NO refusal. It exists to
answer *"isn't Minni just RAG?"* by being exactly RAG, run under identical
fairness controls.

Pipeline (all deterministic):
- **ingest**: decode the frozen corpus, chunk each doc with the SHARED fixed
  chunking (``config.CHUNK_SIZE_WORDS`` / ``CHUNK_OVERLAP_WORDS``), embed every
  chunk with the SHARED pinned embedder (``config.EMBEDDER_MODEL_ID``, loaded via
  the public ``sentence-transformers`` library — never engine internals, §7.2/
  §7.5), and build a FAISS inner-product index over the L2-normalised vectors
  (inner product on normalised vectors == cosine similarity).
- **query**: embed the query with the same model, inner-product top-k over the
  chunk index at the CHUNK level (``config.CHUNK_RETRIEVE_K``), then collapse
  chunks -> whole-file doc-ids with first-hit dedup capped at ``budget.max_docs``
  (== K), build a content-only ``context_string`` within the token budget.
  **Threshold refusal (fix 4, §6.5):** when the TOP cosine score is below the
  pinned ``config.REFUSAL_SCORE_THRESHOLD`` the query is a clear non-match and the
  adapter HONESTLY returns an empty ranked list + ``refused=True``. This is the
  textbook RAG similarity-floor behaviour (NOT governance), so correct-refusal is
  an earnable axis for an ungoverned baseline — not a Minni-only one. A genuine
  positive scores well above the floor and is still answered (no false-refusal
  regression on positives).

FAIRNESS: this adapter and every other vector adapter use the IDENTICAL embedder
id and the IDENTICAL k (both from ``config``). The fairness-conformance test
(test_adapter_fairness.py) asserts that equality across all vector adapters.

Determinism: chunks are built in sorted doc-id order; FAISS ``IndexFlatIP`` is an
exact brute-force index (no ANN randomness); ties in the score sort are broken by
(score desc, chunk_index asc) so the ranking is reproducible bit-for-bit.
"""

from __future__ import annotations

import time

from .. import config
from ..contract import (
    FrozenCorpus,
    IngestReport,
    PreIngestError,
    QueryResult,
    TeardownError,
    TokenBudget,
)
from . import _shared


class NaiveRagAdapter:
    """Plain vector-store RAG baseline (§4). No governance, never refuses."""

    name = "naive_rag"

    def __init__(self) -> None:
        self.config_hash = "naive_rag-s3"
        self._docs: dict[str, str] = {}
        self._index = None
        # Parallel arrays: chunk i has parent doc-id _chunk_docs[i].
        self._chunk_docs: list[str] = []
        self._torn_down = False
        self._ingested = False

    # -- introspection used by the fairness-conformance test --------------
    @property
    def embedder_id(self) -> str:
        return _shared.embedder_id()

    @property
    def retrieval_k(self) -> int:
        return config.K

    # -- contract --------------------------------------------------------
    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        self._require_live()
        import faiss
        import numpy as np

        np.random.seed(config.INGEST_SEED)
        start = time.perf_counter()
        new_docs = _shared.load_docs(corpus)

        pairs = _shared.chunk_corpus(new_docs)  # deterministic, sorted order
        # Build ALL new state into locals first; only assign to self after every
        # fallible step (embed/index build) has succeeded, so a failure on a
        # SECOND ingest() leaves the prior index/_chunk_docs intact and in sync
        # (atomic swap — never a new _chunk_docs paired with a stale _index).
        new_chunk_docs = [doc_id for doc_id, _ in pairs]
        index_bytes = 0
        if pairs:
            vecs = _shared.embed([chunk for _, chunk in pairs])
            dim = vecs.shape[1]
            new_index = faiss.IndexFlatIP(dim)  # exact brute-force; no ANN randomness
            new_index.add(vecs)
            index_bytes = int(vecs.nbytes)
        else:
            new_index = None
        # Atomic commit: _docs, _chunk_docs and _index all describe the SAME
        # corpus together (or none of them are touched on failure).
        self._docs = new_docs
        self._chunk_docs = new_chunk_docs
        self._index = new_index

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._ingested = True
        # doc_count == source FILE count (one per doc_ids() entry), NOT chunks.
        return IngestReport(
            build_wall_clock_ms=elapsed_ms,
            doc_count=len(self._docs),
            index_size_bytes=index_bytes,
            ingest_tokens_used=0,  # embedding is not generation
        )

    def rank_candidates(self, q: str, max_docs: int) -> list:
        """Raw pre-refusal retrieval ranking for INDEX-INTEGRITY introspection.

        Returns the collapsed whole-file ``RankedDoc`` list the vector index
        produces for ``q``, BEFORE the query-time threshold-refusal floor is
        applied. The §9.5 over-count cross-check probes this — it asks "did the
        adapter actually index the contents?" (an INGEST property), which is
        independent of whether the adapter chooses to SURFACE or REFUSE that
        retrieval at query time (a query-time POLICY). Exact-token lookups (a
        random UUID) embed poorly and legitimately fall under the cosine floor,
        so the integrity check must read the raw ranking, not the gated result.
        Not part of the MemoryAdapter contract; used only by the harness's own
        integrity tests.
        """
        self._require_live()
        if not self._ingested:
            raise PreIngestError("rank_candidates() before ingest()")
        return self._rank(q, max_docs)

    def _rank(self, q: str, max_docs: int) -> list:
        ranked_chunks: list[tuple[str, float]] = []
        if self._index is not None and self._chunk_docs:
            qvec = _shared.embed([q])
            top = min(config.CHUNK_RETRIEVE_K, len(self._chunk_docs))
            scores, idxs = self._index.search(qvec, top)
            # Pair (parent doc-id, score, chunk_index) and sort deterministically:
            # score desc, then chunk_index asc to break ties reproducibly.
            hits = [
                (self._chunk_docs[int(ci)], float(s), int(ci))
                for s, ci in zip(scores[0], idxs[0])
                if int(ci) >= 0
            ]
            hits.sort(key=lambda h: (-h[1], h[2]))
            ranked_chunks = [(doc_id, score) for doc_id, score, _ in hits]
        return _shared.collapse_chunks_to_docs(ranked_chunks, max_docs)

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        self._require_live()
        if not self._ingested:
            raise PreIngestError("query() before ingest()")
        start = time.perf_counter()

        ranked = self._rank(q, budget.max_docs)

        # THRESHOLD REFUSAL (§6.5, fix 4): this is NOT governance — it is the
        # standard RAG "best similarity below the confidence floor -> return
        # nothing" behaviour. When the TOP cosine score is below the pinned
        # ``config.REFUSAL_SCORE_THRESHOLD``, the query is a clear non-match: the
        # adapter HONESTLY returns an empty ranked list AND sets refused=True (the
        # §6.5 predicate requires BOTH). On a genuine positive the top cosine is
        # well above the floor, so positives are still answered (no false-refusal
        # regression). This makes correct-refusal an EARNABLE axis for an
        # ungoverned baseline, not a Minni-only one.
        top_score = ranked[0].score if ranked else float("-inf")
        if not ranked or top_score < config.REFUSAL_SCORE_THRESHOLD:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            return QueryResult(
                ranked_results=[],
                context_string="",
                wall_clock_ms=elapsed_ms,
                refused=True,
            )

        context = _shared.build_context(
            [rd.doc_id for rd in ranked], self._docs, budget
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return QueryResult(
            ranked_results=ranked,
            context_string=context,
            wall_clock_ms=elapsed_ms,
            refused=False,
        )

    def teardown(self) -> None:
        self._torn_down = True
        self._index = None
        self._docs = {}
        self._chunk_docs = []
        self._ingested = False

    # -- helpers ---------------------------------------------------------
    def _require_live(self) -> None:
        if self._torn_down:
            raise TeardownError("adapter used after teardown() (§9.4)")

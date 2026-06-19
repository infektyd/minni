"""``sanity_random`` — the negative-control broken adapter (§9.7).

A deliberately near-random retriever. It exists to PROVE the Layer-1 metrics are
not trivially satisfiable: on a sufficiently large fixture (≥50 docs, ≥25
labeled queries, ≤3 gold/query) a random pick must score below the §9.7 floors
(``recall@k < 0.10``, ``ndcg@k < 0.05``, ``mrr < 0.05``). If a random adapter
clears those, the metric is mis-wired.

Determinism: the "randomness" is a FIXED-seed deterministic permutation derived
from a per-query hash, so two Layer-1 runs are byte-identical (§3.2). It is
random *with respect to relevance* — the ranking has no relationship to gold —
but reproducible. No governance, never refuses.
"""

from __future__ import annotations

import hashlib
import time

from ..contract import (
    FrozenCorpus,
    IngestReport,
    PreIngestError,
    QueryResult,
    RankedDoc,
    TeardownError,
    TokenBudget,
)
from . import _shared


class SanityRandomAdapter:
    """Negative-control: returns relevance-random (but deterministic) docs."""

    name = "sanity_random"

    def __init__(self) -> None:
        self.config_hash = "sanity_random-s4"
        self._docs: dict[str, str] = {}
        self._doc_ids: list[str] = []
        self._ingested = False
        self._torn_down = False

    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        self._require_live()
        start = time.perf_counter()
        self._docs = _shared.load_docs(corpus)
        self._doc_ids = sorted(self._docs)
        self._ingested = True
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return IngestReport(
            build_wall_clock_ms=elapsed_ms,
            doc_count=len(self._docs),
            index_size_bytes=0,
            ingest_tokens_used=0,
        )

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        self._require_live()
        if not self._ingested:
            raise PreIngestError("query() before ingest()")
        start = time.perf_counter()
        # Deterministic relevance-random order: sort doc-ids by a per-(query,doc)
        # hash. This has no relationship to which docs are gold, so recall/ndcg/
        # mrr stay at random-baseline — yet it is byte-reproducible across runs.
        def _key(doc_id: str) -> str:
            return hashlib.sha256(f"{q}\x00{doc_id}".encode()).hexdigest()

        ordered = sorted(self._doc_ids, key=_key)
        top = ordered[: budget.max_docs]
        ranked = [RankedDoc(doc_id=d, score=1.0) for d in top]
        context = _shared.build_context(top, self._docs, budget)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        # No governance layer -> never an explicit refusal (§6.5).
        return QueryResult(
            ranked_results=ranked,
            context_string=context,
            wall_clock_ms=elapsed_ms,
            refused=False,
        )

    def teardown(self) -> None:
        self._torn_down = True
        self._docs = {}
        self._doc_ids = []
        self._ingested = False

    def _require_live(self) -> None:
        if self._torn_down:
            raise TeardownError("adapter used after teardown() (§9.4)")

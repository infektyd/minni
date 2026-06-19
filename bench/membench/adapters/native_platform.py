"""``native_platform`` — the "just use the platform's built-in memory" baseline.

HONEST STATUS: this is a DEGRADED, FIDELITY-LIMITED adapter. Per the design
spec's resolution of open question §10.4 (§8.2 Threats to Validity), Claude Code
session-memory / context-compaction is built for an INTERACTIVE session and
cannot be faithfully driven headlessly to expose a clean ``query()``. Rather than
fake a parity it does not have, this adapter is a best-effort stand-in and SAYS
SO, loudly, via a machine-readable capability annotation (``capability`` /
``degraded`` / ``fidelity_note``) that the report carries into its
threats-to-validity section.

WHAT IT ACTUALLY DOES (deterministic, defensible):
  It mimics the platform pattern of *"stuff the most recent notes into the
  context window until it is full."* On ``query()`` it orders the corpus docs by
  a RECENCY PROXY (see below), optionally promotes docs that share a query term
  to the front of the recency order (mimicking the platform surfacing a recent
  note that mentions the topic), then performs WHOLE-FILE truncation-to-budget:
  it fills ``context_string`` with the most-recent docs, whole-file, until the
  token budget is reached. ``ranked_results`` are the docs that made it into the
  context, in recency order.

WHAT IT DOES *NOT* FAITHFULLY REPRESENT (carried into §8.2 threats-to-validity):
  - It does NOT run Claude Code's real session memory, summarisation, or
    compaction heuristics. Those are interactive, model-driven, and not
    reproducible headlessly; this adapter substitutes a deterministic
    recency+truncation rule for them.
  - The RECENCY PROXY is itself degraded: the frozen corpus is content-hashed,
    not mtime-preserving, and the synthetic fixture carries no per-doc dates, so
    "recency" here is the CANONICAL SORTED DOC-ID ORDER (lexicographically-last
    doc-id == "most recent"). On a real numbered/dated vault this approximates
    chronology; in general it is a PROXY, not true recency. This is disclosed,
    not hidden.
  - It has NO governance / provenance gate, so it NEVER refuses (§6.5). A miss is
    a miss, never a refusal.
  Because of all this, the report flags this adapter's numbers as
  "fidelity-limited / lowest-confidence" and does not let them ground a headline
  claim. It satisfies the MemoryAdapter contract and the conformance tests; it
  does not pretend to capabilities it lacks.

Determinism: recency order is the deterministic sorted doc-id order; the optional
lexical promotion is a stable partition (term-matching docs first, each group
keeping recency order). No RNG, no timestamps, no dict-order reliance.
"""

from __future__ import annotations

import re
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

_WORD = re.compile(r"[A-Za-z0-9]+")


def _terms(text: str) -> set[str]:
    return {w.lower() for w in _WORD.findall(text)}


class NativePlatformAdapter:
    """Degraded, honestly-annotated platform-memory baseline (recency+truncation)."""

    name = "native_platform"

    # -- machine-readable capability annotation (feeds §8.2 report section) ----
    # The report reads these fields verbatim. They are the structural proof that
    # this adapter does NOT claim parity it lacks.
    capability: str = "degraded"
    degraded: bool = True
    fidelity_note: str = (
        "native_platform is a FIDELITY-LIMITED stand-in: it does NOT drive Claude "
        "Code's real interactive session-memory/compaction (not reproducible "
        "headlessly). It substitutes a deterministic recency-ordered whole-file "
        "truncation-to-budget rule, with recency approximated by canonical "
        "sorted doc-id order (content-hashed corpus carries no mtime/dates). No "
        "governance gate; never refuses. Lowest-confidence adapter; numbers are "
        "flagged and do not ground headline claims (§8.2)."
    )

    def __init__(self) -> None:
        self.config_hash = "native_platform-s3-degraded"
        self._docs: dict[str, str] = {}
        # Recency order: lexicographically-last doc-id == most recent (proxy).
        self._recency: list[str] = []
        self._torn_down = False
        self._ingested = False

    # -- contract --------------------------------------------------------
    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        self._require_live()
        start = time.perf_counter()
        self._docs = _shared.load_docs(corpus)
        # Most-recent-first recency proxy: reverse of the canonical sorted order.
        self._recency = sorted(self._docs, reverse=True)
        index_bytes = sum(len(t.encode("utf-8")) for t in self._docs.values())
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._ingested = True
        return IngestReport(
            build_wall_clock_ms=elapsed_ms,
            doc_count=len(self._docs),  # whole files == doc_ids()
            index_size_bytes=index_bytes,
            ingest_tokens_used=0,
        )

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        self._require_live()
        if not self._ingested:
            raise PreIngestError("query() before ingest()")
        start = time.perf_counter()

        qterms = _terms(q)
        # Stable partition: docs that mention a query term keep recency order and
        # come first (the platform surfacing a recent topical note); the rest
        # follow in recency order. Deterministic, no scoring.
        matched = [d for d in self._recency if qterms & _terms(self._docs[d])]
        rest = [d for d in self._recency if d not in set(matched)]
        ordered = matched + rest

        # Whole-file truncation-to-budget: fill context with most-recent docs
        # until the budget is reached; ranked_results == docs that fit, capped at
        # max_docs. Recency-proxy score: higher for more-recent (earlier) docs.
        selected: list[str] = []
        used = 0
        sep_tokens = _shared.count_tokens("\n\n")
        for doc_id in ordered:
            if len(selected) >= budget.max_docs:
                break
            body = self._docs[doc_id].strip()
            if not body:
                continue
            add_sep = sep_tokens if selected else 0
            cost = _shared.count_tokens(body)
            if used + add_sep + cost > budget.max_tokens:
                break  # whole-file: do not split a note; stop at the first overflow
            selected.append(doc_id)
            used += add_sep + cost

        n = len(selected)
        ranked = [
            RankedDoc(doc_id=d, score=float(n - i)) for i, d in enumerate(selected)
        ]
        context = _shared.build_context(selected, self._docs, budget)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return QueryResult(
            ranked_results=ranked,
            context_string=context,
            wall_clock_ms=elapsed_ms,
            refused=False,  # no governance gate; degraded baseline never refuses
        )

    def teardown(self) -> None:
        self._torn_down = True
        self._docs = {}
        self._recency = []
        self._ingested = False

    # -- helpers ---------------------------------------------------------
    def _require_live(self) -> None:
        if self._torn_down:
            raise TeardownError("adapter used after teardown() (§9.4)")

"""``markdown_grep`` — the Obsidian-vault-as-agent-memory archetype (§4.1).

The dominant local-first agent-memory pattern in 2026 is a plain-Markdown vault
the agent reads and searches directly. Its explicit selling point against vector
stores is *"no embedding cliff, no provider lock-in"* — i.e. LEXICAL / file-based
search over real files. This adapter reconstructs exactly that: the frozen corpus
treated as on-disk markdown files, answered by **BM25 lexical retrieval** (Okapi
BM25 via ``rank_bm25``) over WHOLE files. No embeddings (this archetype is
lexical, matching how people actually search an Obsidian vault). No governance —
but it DOES honestly refuse when its lexical search finds ZERO matching notes
(threshold refusal, fix 4 / §6.5): a query with no shared term against any note
returns nothing AND ``refused=True``. This is the lexical analogue of "no result
above the confidence floor", not a governance gate, and it makes correct-refusal
an EARNABLE axis for this baseline rather than a Minni-only one.

Why whole-file BM25 (not chunked): an Obsidian user greps/searches files and
opens the matching NOTE, not a sub-window. Scoring at the file granularity is
both faithful to the archetype AND already the canonical retrieval unit (§3.1),
so there is no chunk->doc collapse to do — every ranked unit is already a
whole-file doc-id, trivially unique.

Determinism: the BM25 corpus is built in sorted doc-id order; tokenization is a
fixed regex; ties in the score sort are broken by doc-id asc. ``rank_bm25`` is a
pure-numpy scorer with no RNG, so scores are reproducible bit-for-bit.

NOTE on subprocess-safety (§3.1/§9.10): this adapter does its lexical search
IN-PROCESS with BM25 and invokes NO subprocess at all, so there is no shell-
injection surface. A ripgrep-backed variant would have to pass ``q`` positionally
after ``--`` via list-form ``subprocess`` (never ``shell=True``); BM25 sidesteps
that entirely.
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


def _tokenize(text: str) -> list[str]:
    """Fixed lexical tokenization: lowercased alphanumeric runs (deterministic)."""
    return [w.lower() for w in _WORD.findall(text)]


class MarkdownGrepAdapter:
    """Obsidian-vault-as-memory archetype: whole-file BM25 lexical retrieval."""

    name = "markdown_grep"

    def __init__(self) -> None:
        self.config_hash = "markdown_grep-s3"
        self._docs: dict[str, str] = {}
        self._doc_ids: list[str] = []
        self._bm25 = None
        self._torn_down = False
        self._ingested = False

    # -- contract --------------------------------------------------------
    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        self._require_live()
        from rank_bm25 import BM25Okapi

        start = time.perf_counter()
        new_docs = _shared.load_docs(corpus)
        # Sorted doc-id order -> deterministic BM25 corpus indexing.
        new_doc_ids = sorted(new_docs)
        tokenized = [_tokenize(new_docs[d]) for d in new_doc_ids]
        # Build ALL new state into locals; only assign to self after the fallible
        # BM25Okapi build succeeds, so a failed SECOND ingest() leaves the prior
        # _docs/_doc_ids/_bm25 intact and in sync (atomic swap — never a new
        # _doc_ids paired with a stale _bm25). Mirrors naive_rag's pattern.
        # BM25Okapi requires at least one document; guard the empty corpus.
        new_bm25 = BM25Okapi(tokenized) if tokenized else None
        # Atomic commit: _docs, _doc_ids and _bm25 all describe the SAME corpus.
        self._docs = new_docs
        self._doc_ids = new_doc_ids
        self._bm25 = new_bm25
        index_bytes = sum(len(t.encode("utf-8")) for t in self._docs.values())

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self._ingested = True
        return IngestReport(
            build_wall_clock_ms=elapsed_ms,
            doc_count=len(self._docs),  # whole files, == doc_ids()
            index_size_bytes=index_bytes,
            ingest_tokens_used=0,
        )

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        self._require_live()
        if not self._ingested:
            raise PreIngestError("query() before ingest()")
        start = time.perf_counter()

        from .. import config

        ranked: list[RankedDoc] = []
        n_hits = 0
        if self._bm25 is not None:
            qtokens = _tokenize(q)
            scores = self._bm25.get_scores(qtokens)
            # Pair (doc_id, score); keep only positive lexical matches (a 0 score
            # means no shared term — a miss, not a hit). Sort score desc, then
            # doc-id asc for a deterministic tie-break.
            hits = [
                (self._doc_ids[i], float(s))
                for i, s in enumerate(scores)
                if s > 0.0
            ]
            n_hits = len(hits)
            hits.sort(key=lambda h: (-h[1], h[0]))
            ranked = [
                RankedDoc(doc_id=d, score=s) for d, s in hits[: budget.max_docs]
            ]

        # THRESHOLD REFUSAL (§6.5, fix 4): an Obsidian-style lexical searcher that
        # finds ZERO matching notes (no shared term with ANY doc, < the pinned
        # ``config.LEXICAL_REFUSAL_MIN_HITS``) HONESTLY surfaces nothing — it
        # returns an empty ranked list AND refused=True (both fields, §6.5). This
        # is the lexical analogue of "no hit above the confidence floor", NOT
        # governance, and it makes correct-refusal earnable for this baseline too.
        # Any query that lexically matches a note is answered, so positives (which
        # the synthetic fixture builds to be lexically findable) never false-refuse.
        if n_hits < config.LEXICAL_REFUSAL_MIN_HITS:
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
        self._bm25 = None
        self._docs = {}
        self._doc_ids = []
        self._ingested = False

    # -- helpers ---------------------------------------------------------
    def _require_live(self) -> None:
        if self._torn_down:
            raise TeardownError("adapter used after teardown() (§9.4)")

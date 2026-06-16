"""Deterministic in-memory stub adapter (slice s1).

This adapter exists to prove the §3.1 contract, the corpus loader, and the
harness-owned token-budget enforcement end-to-end WITHOUT any external service —
so the suite is green even when an isolated Minni daemon cannot be stood up in
this environment (the spec-sanctioned fallback, S1 scope item 7).

It does deterministic lexical scoring (token-overlap) over the frozen corpus,
collapses to whole-file doc-ids with first-hit dedup, and builds a content-only
``context_string``. It also provides negative variants the conformance suite
uses to prove the guards actually trip.
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

_WORD = re.compile(r"[A-Za-z0-9]+")


def _terms(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text)]


class StubAdapter:
    """A faithful, deterministic in-memory MemoryAdapter (§3.1).

    NOTE: the stub has NO governance layer — it does pure lexical retrieval — so
    it never explicitly refuses. ``query()`` always returns ``refused=False``; an
    empty ``ranked_results`` is a plain retrieval miss, not a governance refusal.
    """

    name = "stub"

    def __init__(self) -> None:
        self.config_hash = "stub-v1"
        self._corpus: FrozenCorpus | None = None
        self._docs: dict[str, str] = {}
        self._torn_down = False

    # -- contract --------------------------------------------------------
    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        self._require_live()
        start = time.perf_counter()
        self._corpus = corpus
        self._docs = {}
        for doc_id in corpus.doc_ids():
            self._docs[doc_id] = corpus.read(doc_id).decode("utf-8", "replace")
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        index_bytes = sum(len(t.encode("utf-8")) for t in self._docs.values())
        # doc_count == source FILE count (one per doc_ids() entry), NOT chunks.
        return IngestReport(
            build_wall_clock_ms=elapsed_ms,
            doc_count=len(self._docs),
            index_size_bytes=index_bytes,
            ingest_tokens_used=0,
        )

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        self._require_live()
        if self._corpus is None:
            # Contract: query() before ingest() RAISES — consistent with the
            # MinniAdapter, so query-before-ingest is never a silent empty
            # result that masks a harness wiring bug (§9.4). Distinct from
            # TeardownError so the two failure modes are never conflated.
            raise PreIngestError("query() before ingest()")
        start = time.perf_counter()
        qterms = set(_terms(q))

        scored: list[tuple[float, str]] = []
        for doc_id, text in self._docs.items():
            dterms = _terms(text)
            if not dterms:
                continue
            overlap = sum(1 for t in dterms if t in qterms)
            if overlap == 0:
                continue
            score = overlap / len(dterms)
            scored.append((score, doc_id))

        # Deterministic ordering: score desc, then doc_id asc for tie-break.
        scored.sort(key=lambda t: (-t[0], t[1]))
        top = scored[: budget.max_docs]

        ranked = [RankedDoc(doc_id=doc_id, score=score) for score, doc_id in top]
        # §3.1: `refused` is True ONLY on an EXPLICIT governance decline. The stub
        # has NO governance layer — it is pure lexical retrieval — so it can never
        # explicitly refuse. An empty result is a plain retrieval miss, NOT a
        # refusal; equating the two would mis-code misses as governance refusals
        # and inflate false_refusal_rate. Always report refused=False.
        refused = False
        context = self._build_context([d.doc_id for d in ranked], budget)

        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return QueryResult(
            ranked_results=ranked,
            context_string=context,
            wall_clock_ms=elapsed_ms,
            refused=refused,
        )

    def teardown(self) -> None:
        self._torn_down = True
        self._corpus = None
        self._docs = {}

    # -- helpers ---------------------------------------------------------
    def _require_live(self) -> None:
        if self._torn_down:
            raise TeardownError("adapter used after teardown() (§9.4)")

    def _build_context(self, doc_ids: list[str], budget: TokenBudget) -> str:
        """Concatenate retrieved doc bodies, trimmed to fit the token budget.

        The harness owns the AUTHORITATIVE budget enforcement; this trim is a
        cooperative best-effort so a well-behaved adapter does not trivially
        trip the abort. (The over-budget NEGATIVE adapter below deliberately
        ignores the budget to prove the harness abort fires.)
        """
        from ..tokenizer import count_tokens

        parts: list[str] = []
        for doc_id in doc_ids:
            parts.append(self._docs[doc_id].strip())
        ctx = "\n\n".join(parts)
        if count_tokens(ctx) <= budget.max_tokens:
            return ctx
        # Coarse character-level trim until under budget (deterministic).
        # Guard the fixed point: a single Python char can tokenize to >1 token
        # (e.g. '😀' -> 2 cl100k tokens), so len(ctx)//2 can stop shrinking the
        # string. Break when the halving no longer makes progress so we never
        # spin forever — the harness owns the AUTHORITATIVE abort regardless.
        while ctx and count_tokens(ctx) > budget.max_tokens:
            new_ctx = ctx[: max(1, len(ctx) // 2)]
            if new_ctx == ctx:
                # Fixed point: halving no longer shrinks the string (a single
                # multi-byte char can tokenize to >1 token). Return EMPTY rather
                # than the still-over-budget string — a well-behaved adapter must
                # never hand back an over-budget context_string. (The harness
                # still owns the AUTHORITATIVE abort; this just keeps a legitimate
                # StubAdapter run from tripping it.)
                return ""
            ctx = new_ctx
        return ctx


# ---------------------------------------------------------------------------
# Negative variants — used by the conformance suite to prove guards trip.
# ---------------------------------------------------------------------------
class OverBudgetStubAdapter(StubAdapter):
    """Ignores the budget and returns a deliberately over-budget context.

    Proves the harness token-budget abort fires (§3.1/§9.4): an adapter cannot
    smuggle extra context past the cap.
    """

    name = "stub_overbudget"

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        self._require_live()
        if self._corpus is None:
            # Same query-before-ingest guard as the parent: never silently
            # swallow a harness wiring bug as a valid empty result (§9.4).
            raise PreIngestError("query() before ingest()")
        # Build a context far larger than any reasonable budget, ignoring it.
        big = " ".join(self._docs.values()) * 50
        ranked = [
            RankedDoc(doc_id=did, score=1.0)
            for did in list(self._docs)[: budget.max_docs]
        ]
        return QueryResult(
            ranked_results=ranked,
            context_string=big,
            wall_clock_ms=0.1,
            refused=False,
        )


class RoleMarkerStubAdapter(StubAdapter):
    """Prepends a banned role marker. Proves the content-only check trips."""

    name = "stub_rolemarker"

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        result = super().query(q, budget)
        return QueryResult(
            ranked_results=result.ranked_results,
            context_string="Assistant: " + result.context_string,
            wall_clock_ms=result.wall_clock_ms,
            refused=result.refused,
        )


class MiscountStubAdapter(StubAdapter):
    """Self-reports a wrong ``doc_count``. Proves the runner's doc-count abort.

    A lying adapter that misreports its source-file count (here, +1) must abort
    the run (§9.5) — the harness never trusts a self-reported doc_count.
    """

    name = "stub_miscount"

    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        report = super().ingest(corpus)
        return IngestReport(
            build_wall_clock_ms=report.build_wall_clock_ms,
            doc_count=report.doc_count + 1,  # deliberate over-count
            index_size_bytes=report.index_size_bytes,
            ingest_tokens_used=report.ingest_tokens_used,
        )


class DuplicateDocStubAdapter(StubAdapter):
    """Returns a duplicate doc-id. Proves the dedup/uniqueness check trips."""

    name = "stub_dup"

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        result = super().query(q, budget)
        if result.ranked_results:
            dup = result.ranked_results[0]
            ranked = [dup, dup]
        else:
            ranked = []
        return QueryResult(
            ranked_results=ranked,
            context_string=result.context_string,
            wall_clock_ms=result.wall_clock_ms,
            refused=result.refused,
        )

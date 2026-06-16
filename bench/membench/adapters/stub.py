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
        from ._shared import neutralize_banned_markers

        parts: list[str] = []
        for doc_id in doc_ids:
            # Neutralize banned role markers via the SAME shared routine every
            # other adapter uses (fairness/uniformity, BUG 1): a legitimate
            # transcript-style doc must not abort assert_well_formed on this
            # adapter either. The nonce envelope remains the injection floor.
            parts.append(neutralize_banned_markers(self._docs[doc_id]).strip())
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
    """Prepends a banned role marker. Proves the content-only check trips.

    The injected prefix is configurable (default ``"Assistant: "``) so a test can
    drive the backstop with ANY banned form — including the bracketed/xml marker
    variants (``<retrieved_context``/``</retrieved_context``) that the word-colon
    default would not exercise (review finding #7). The injection BYPASSES the
    shared builder on purpose, so it proves the structural backstop still fires.
    """

    name = "stub_rolemarker"

    #: Class-level injected prefix; subclass/override or set per-instance to drive
    #: the backstop with a specific banned marker form.
    injected_prefix = "Assistant: "

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        result = super().query(q, budget)
        return QueryResult(
            ranked_results=result.ranked_results,
            context_string=self.injected_prefix + result.context_string,
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


class PartialIngestStubAdapter(StubAdapter):
    """A DISCLOSED partial ingest: skips one doc but accounts for it (§9.5).

    Models the minni single-RPC oversize case: it indexes all but the LAST corpus
    doc (so that doc is genuinely not retrievable) and reports the shortfall via
    ``skipped_doc_count`` / ``skipped_doc_ids`` so ``doc_count + skipped ==
    corpus``. The §9.5 gate must ACCEPT this (fully accounted) and the adapter is
    scored only on what it ingested — never gamed by inflating skipped, because a
    skipped doc is dropped from the retrievable index here.
    """

    name = "stub_partial"

    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        report = super().ingest(corpus)
        ids = list(corpus.doc_ids())
        if not ids:
            return report
        skipped_id = ids[-1]
        # Genuinely drop the skipped doc from the retrievable index so it cannot
        # be retrieved — a skipped doc must never score (no gaming the gate).
        self._docs.pop(skipped_id, None)
        return IngestReport(
            build_wall_clock_ms=report.build_wall_clock_ms,
            doc_count=report.doc_count - 1,  # one fewer PROMOTED
            index_size_bytes=report.index_size_bytes,
            ingest_tokens_used=report.ingest_tokens_used,
            skipped_doc_count=1,
            skipped_doc_ids=(skipped_id,),
            skip_reason="oversize for single-RPC daemon cap (test stub)",
        )


class LeakyReasonSkipStubAdapter(StubAdapter):
    """A DISCLOSED partial ingest whose free-form ``skip_reason`` embeds an
    absolute local path (§9.5 redaction).

    Models a careless adapter that leaks an operator-specific temp path into the
    open ``skip_reason`` string. The harness MUST redact it via ``_redact_str``
    before it reaches results.json / report.md. The accounting stays honest
    (doc_count + skipped == corpus) and the skipped id stays a real corpus member,
    so the §9.5 gate PROCEEDS — proving redaction runs on a PASSING run, not only
    on the error-isolation path. (Note: a leaked absolute path in the skipped ID
    itself can never reach _normalize_skip_id through orchestration — the gate's
    corpus-subset check rejects a non-corpus id first; _normalize_skip_id is
    unit-tested directly as defence-in-depth.)
    """

    name = "stub_leaky_skip"

    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        report = super().ingest(corpus)
        ids = list(corpus.doc_ids())
        if not ids:
            return report
        skipped_id = ids[-1]
        # Genuinely drop the doc so it cannot be retrieved (no gaming).
        self._docs.pop(skipped_id, None)
        return IngestReport(
            build_wall_clock_ms=report.build_wall_clock_ms,
            doc_count=report.doc_count - 1,
            index_size_bytes=report.index_size_bytes,
            ingest_tokens_used=report.ingest_tokens_used,
            skipped_doc_count=1,
            skipped_doc_ids=(skipped_id,),  # a REAL corpus member (passes gate)
            skip_reason=(
                "oversize at /var/folders/x/tmp/docs/huge.md for single-RPC cap"
            ),
        )


class FullySkippedStubAdapter(StubAdapter):
    """A degenerate but FULLY ACCOUNTED partial: indexes nothing, skips all (§9.5).

    ``doc_count == 0`` and ``skipped_doc_count == corpus`` so ``doc_count +
    skipped == corpus`` — fully accounted, NOT a silent undercount. The §9.5 gate
    ALLOWS it (only over-count and silent undercount abort). It drops every doc
    from the retrievable index, so it scores recall 0 on every query — the report
    states plainly that it ingested 0 docs. Pins the doc_count=0 edge.
    """

    name = "stub_fully_skipped"

    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        report = super().ingest(corpus)
        ids = tuple(corpus.doc_ids())
        # Drop EVERY doc so nothing is retrievable (no gaming: indexed nothing).
        self._docs = {}
        return IngestReport(
            build_wall_clock_ms=report.build_wall_clock_ms,
            doc_count=0,  # indexed nothing
            index_size_bytes=report.index_size_bytes,
            ingest_tokens_used=report.ingest_tokens_used,
            skipped_doc_count=len(ids),
            skipped_doc_ids=ids,
            skip_reason="fully-skipped test stub (indexed nothing)",
        )


class SilentUndercountStubAdapter(StubAdapter):
    """Under-reports doc_count WITHOUT accounting for the missing docs (§9.5).

    ``doc_count`` is short by one and ``skipped_doc_count`` stays 0, so
    ``doc_count + skipped < corpus`` — a SILENT undercount (docs unaccounted
    for). The §9.5 gate MUST abort this adapter: a partial run that silently
    omits docs can never look complete.
    """

    name = "stub_undercount"

    def ingest(self, corpus: FrozenCorpus) -> IngestReport:
        report = super().ingest(corpus)
        return IngestReport(
            build_wall_clock_ms=report.build_wall_clock_ms,
            doc_count=report.doc_count - 1,  # short, with NO skip accounting
            index_size_bytes=report.index_size_bytes,
            ingest_tokens_used=report.ingest_tokens_used,
        )


class GatedStubAdapter(StubAdapter):
    """A stub WITH a governance gate that refuses on a retrieval miss (§6.5).

    Unlike the plain StubAdapter (which never refuses), this variant models a
    provenance gate: when lexical retrieval finds NO overlapping doc, it returns
    ``refused=True`` with an empty ranked list — the §6.5 refusal predicate. On a
    well-built negative query (no doc legitimately matches) this earns a correct
    refusal; on a positive query where it DID find docs it answers normally. Used
    by the §9.7 refusal-pair test to show correct_refusal_rate rewards correct
    refusals without gaming.
    """

    name = "stub_gated"

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        result = super().query(q, budget)
        if not result.ranked_results:
            # Retrieval miss -> explicit governance refusal (refused AND empty).
            return QueryResult(
                ranked_results=[],
                context_string="",
                wall_clock_ms=result.wall_clock_ms,
                refused=True,
            )
        return result


class RefuseEverythingStubAdapter(StubAdapter):
    """Refuses on EVERY query (§6.5 gaming exposure).

    Sets ``refused=True`` with an empty ranked list unconditionally. It maxes
    correct_refusal_rate (1.0 on negatives) but the SAME predicate fires on
    positives, so false_refusal_rate is 1.0 and recall@k is 0 across all positive
    bands — the pair of rates exposes the gaming (§6.5). Used by the §9.7
    refusal-pair test.
    """

    name = "stub_refuse_all"

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        self._require_live()
        if self._corpus is None:
            raise PreIngestError("query() before ingest()")
        return QueryResult(
            ranked_results=[],
            context_string="",
            wall_clock_ms=0.1,
            refused=True,
        )


class FakeRefuseStubAdapter(StubAdapter):
    """Sets ``refused=True`` but STILL returns docs (§6.5 credit-farming hole).

    Per §6.5 the ``refused`` flag is only honored when ranked_results is also
    empty, so this adapter earns NO correct-refusal credit despite the flag. Used
    to prove the refusal predicate reads BOTH fields.
    """

    name = "stub_fake_refuse"

    def query(self, q: str, budget: TokenBudget) -> QueryResult:
        result = super().query(q, budget)
        return QueryResult(
            ranked_results=result.ranked_results,
            context_string=result.context_string,
            wall_clock_ms=result.wall_clock_ms,
            refused=True,  # claims refusal while returning docs -> NOT a refusal
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

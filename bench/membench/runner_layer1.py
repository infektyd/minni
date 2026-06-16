"""Layer-1 runner — deterministic offline scoring loop (slice s1, minimal).

Full metric scoring (recall@k / nDCG@k / refusal / Wilcoxon) is slice s4. What
s1 needs from the runner is the LOAD-BEARING fairness/safety machinery:

1. Harness-OWNED token counting (§3.1): after ``query()`` returns, compute
   ``harness_tokens = count_tokens(context_string)`` — the adapter supplies no
   token field, so it cannot under-count.
2. Token-budget ENFORCEMENT (§3.1/§9.4): immediately after computing
   ``harness_tokens`` assert ``harness_tokens <= budget.max_tokens`` and ABORT
   the run (raise :class:`BudgetExceeded`) on violation — never silently
   truncate the over-budget context to fit.
3. Structural conformance of each QueryResult (§3.1) via
   ``contract.assert_well_formed``.

The runner drives ONE adapter over the fixture corpus and emits a list of
scored records (one per query). This is enough to prove the contract round-trip
and the budget abort; metric columns land in s4.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import CONTEXT_LOG_TRUNCATE
from .contract import (
    FrozenCorpus,
    MemoryAdapter,
    QueryResult,
    TokenBudget,
    assert_well_formed,
    validate_query,
)
from .tokenizer import count_tokens

# Suffix appended when context_string is truncated for emission (§5.1).
_ELIDED_SUFFIX = "…[elided]"


def _truncate_context(ctx: str) -> str:
    """Truncate context to the log-exposure cap, appending the elided marker."""
    if len(ctx) > CONTEXT_LOG_TRUNCATE:
        return ctx[:CONTEXT_LOG_TRUNCATE] + _ELIDED_SUFFIX
    return ctx


class BudgetExceeded(RuntimeError):
    """Raised when a query result exceeds ``budget.max_tokens`` (§3.1/§9.4).

    The runner ABORTS the whole run on this — the over-budget result is never
    silently truncated to fit, so an adapter cannot smuggle extra context past
    the cap.
    """


@dataclass
class ScoredRecord:
    """One query's harness-side record (s1 shape; metrics added in s4)."""

    query_index: int
    query: str
    adapter: str
    doc_ids: list[str]
    refused: bool
    harness_tokens: int
    wall_clock_ms: float
    # ALREADY truncated at construction (§5.1) — storing the full corpus text
    # here would create a log-exposure trap for any caller that serialises the
    # record directly (dataclasses.asdict / json.dumps) bypassing record_to_json.
    context_string: str

    def __post_init__(self) -> None:
        # Enforce the truncation invariant at construction so it cannot be
        # bypassed by a future direct-serialisation path.
        max_len = CONTEXT_LOG_TRUNCATE + len(_ELIDED_SUFFIX)
        if len(self.context_string) > max_len:
            raise ValueError(
                f"ScoredRecord.context_string len={len(self.context_string)} "
                f"exceeds truncation cap {max_len}; truncate before construction."
            )


def score_query(
    adapter: MemoryAdapter,
    corpus: FrozenCorpus,
    q: str,
    budget: TokenBudget,
    query_index: int,
) -> ScoredRecord:
    """Run one query through the contract with full harness-side enforcement."""
    q = validate_query(q)
    result: QueryResult = adapter.query(q, budget)

    # Structural conformance (membership, dedup, max_docs, content-only).
    assert_well_formed(result, corpus, budget)

    # Harness-OWNED token count + budget abort (load-bearing).
    harness_tokens = count_tokens(result.context_string)
    if harness_tokens > budget.max_tokens:
        raise BudgetExceeded(
            f"adapter {adapter.name!r} returned {harness_tokens} tokens > "
            f"budget.max_tokens={budget.max_tokens} on query #{query_index} "
            "— aborting run (no silent truncation)."
        )

    return ScoredRecord(
        query_index=query_index,
        query=q,
        adapter=adapter.name,
        doc_ids=[rd.doc_id for rd in result.ranked_results],
        refused=result.refused,
        harness_tokens=harness_tokens,
        wall_clock_ms=result.wall_clock_ms,
        # Truncated at construction — the full context is consumed above for the
        # authoritative token count and is never stored on the record (§5.1).
        context_string=_truncate_context(result.context_string),
    )


def run_layer1(
    adapter: MemoryAdapter,
    corpus: FrozenCorpus,
    queries: list[str],
    budget: TokenBudget,
) -> list[ScoredRecord]:
    """Ingest the frozen corpus, then score every query (s1 minimal loop).

    Returns scored records. Raises :class:`BudgetExceeded` (aborting the run)
    the moment any adapter result exceeds the token budget. The caller tears the
    adapter down.
    """
    ingest_report = adapter.ingest(corpus)
    if ingest_report.doc_count != len(corpus.doc_ids()):
        raise RuntimeError(
            f"ingest doc_count={ingest_report.doc_count} != "
            f"len(corpus.doc_ids())={len(corpus.doc_ids())} — aborting (§9.5)."
        )
    records: list[ScoredRecord] = []
    for i, q in enumerate(queries):
        records.append(score_query(adapter, corpus, q, budget, i))
    return records


def record_to_json(rec: ScoredRecord) -> dict:
    """Serialize a scored record.

    ``context_string`` is ALREADY truncated at record construction (§5.1), so
    this is a straight ``asdict`` — there is no untruncated context anywhere on
    the record to leak.
    """
    return asdict(rec)

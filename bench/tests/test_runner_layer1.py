"""Layer-1 runner round-trip + token-budget enforcement (§3.1, §9.4)."""

import pytest

from membench import config
from membench.adapters.stub import (
    MiscountStubAdapter,
    OverBudgetStubAdapter,
    StubAdapter,
)
from membench.contract import TokenBudget
from membench.runner_layer1 import (
    _ELIDED_SUFFIX,
    BudgetExceeded,
    ScoredRecord,
    record_to_json,
    run_layer1,
)

_QUERIES = [
    "What is the Aurora Protocol witness phase?",
    "Who leads the Lindgren team?",
    "What is the seal timeout?",
    "completely unrelated nonsense xyzzy plugh",  # likely refusal
]


def test_runner_roundtrip_emits_scored_records(corpus, budget):
    adapter = StubAdapter()
    try:
        records = run_layer1(adapter, corpus, _QUERIES, budget)
        assert len(records) == len(_QUERIES)
        for rec in records:
            assert rec.harness_tokens <= budget.max_tokens
            assert isinstance(rec.refused, bool)
            json_rec = record_to_json(rec)
            # Context is truncated for emission (§5.1) — tight bound, and when
            # truncation fires the elided suffix MUST be present.
            ctx = json_rec["context_string"]
            max_len = config.CONTEXT_LOG_TRUNCATE + len(_ELIDED_SUFFIX)
            assert len(ctx) <= max_len
            if ctx.endswith(_ELIDED_SUFFIX):
                # Truncated form: exactly CONTEXT_LOG_TRUNCATE chars + suffix.
                assert len(ctx) == max_len
    finally:
        adapter.teardown()


def test_runner_aborts_on_doc_count_mismatch(corpus, budget):
    """A lying adapter that misreports doc_count must abort the run (§9.5)."""
    adapter = MiscountStubAdapter()
    try:
        with pytest.raises(RuntimeError):
            run_layer1(adapter, corpus, _QUERIES, budget)
    finally:
        adapter.teardown()


def test_scored_record_rejects_untruncated_context():
    """ScoredRecord.__post_init__ enforces the truncation cap (finding #7).

    A context_string one char past CONTEXT_LOG_TRUNCATE + len(suffix) must RAISE
    ValueError at construction — the truncation invariant cannot be bypassed by
    a direct-serialisation path.
    """
    over = "x" * (config.CONTEXT_LOG_TRUNCATE + len(_ELIDED_SUFFIX) + 1)
    with pytest.raises(ValueError):
        ScoredRecord(
            query_index=0,
            query="q",
            adapter="stub",
            doc_ids=[],
            refused=False,
            harness_tokens=0,
            wall_clock_ms=0.0,
            context_string=over,
        )


def test_runner_aborts_on_over_budget(corpus):
    budget = TokenBudget(max_tokens=32, max_docs=10)
    adapter = OverBudgetStubAdapter()
    try:
        with pytest.raises(BudgetExceeded):
            run_layer1(adapter, corpus, _QUERIES, budget)
    finally:
        adapter.teardown()

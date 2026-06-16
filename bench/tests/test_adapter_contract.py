"""Adapter-contract conformance suite for slice s1 (§9.4).

Proven here against the deterministic stub adapter (the in-memory adapter that
makes the suite green regardless of whether an isolated Minni daemon can be
stood up):
- well-formed QueryResult shape (no adapter token field; harness token count
  equals len(canonical_tokenizer.encode(context_string)));
- ranked_results drawn only from corpus.doc_ids(), with UNIQUE doc-ids;
- content-only context_string (banned-role-marker negative trips);
- token-budget abort fires on an over-budget adapter;
- teardown() then query() RAISES.
"""

import pytest

from membench import tokenizer
from membench.adapters.stub import (
    DuplicateDocStubAdapter,
    OverBudgetStubAdapter,
    RoleMarkerStubAdapter,
    StubAdapter,
)
from membench.contract import (
    ContractError,
    IngestReport,
    PreIngestError,
    QueryResult,
    RankedDoc,
    TeardownError,
    TokenBudget,
    assert_well_formed,
    validate_query,
)
from membench.runner_layer1 import BudgetExceeded, score_query

_Q = "What is the Aurora Protocol witness phase?"


def test_query_result_shape_and_harness_token_count(corpus, budget):
    adapter = StubAdapter()
    try:
        report = adapter.ingest(corpus)
        # IngestReport type + all fields (§3.1), not just doc_count.
        assert isinstance(report, IngestReport)
        assert report.doc_count == len(corpus.doc_ids())
        assert isinstance(report.build_wall_clock_ms, float)
        assert report.build_wall_clock_ms >= 0
        assert isinstance(report.index_size_bytes, int)
        assert report.index_size_bytes >= 0
        assert report.ingest_tokens_used >= 0
        result = adapter.query(_Q, budget)
        assert isinstance(result, QueryResult)
        # No adapter-supplied token field exists on QueryResult.
        assert not hasattr(result, "tokens_used")
        assert isinstance(result.wall_clock_ms, float)
        assert result.wall_clock_ms >= 0
        assert isinstance(result.refused, bool)
        # Harness-owned token count is authoritative.
        harness_tokens = tokenizer.count_tokens(result.context_string)
        assert harness_tokens == len(tokenizer.encode(result.context_string))
        assert harness_tokens <= budget.max_tokens
        assert_well_formed(result, corpus, budget)
    finally:
        adapter.teardown()


def test_ranked_results_membership_and_uniqueness(corpus, budget):
    adapter = StubAdapter()
    try:
        adapter.ingest(corpus)
        result = adapter.query(_Q, budget)
        valid = set(corpus.doc_ids())
        ids = [rd.doc_id for rd in result.ranked_results]
        assert all(i in valid for i in ids)
        assert len(ids) == len(set(ids)), "ranked_results must be deduplicated"
        assert len(ids) <= budget.max_docs
        # ranked_results must be ordered highest-score-first (§3.1 ordering).
        scores = [rd.score for rd in result.ranked_results]
        assert scores == sorted(scores, reverse=True), "scores must be descending"
    finally:
        adapter.teardown()


def test_query_before_ingest_raises(corpus, budget):
    """query() before any ingest() must RAISE PreIngestError (NOT TeardownError).

    query-before-ingest is a distinct failure mode from use-after-teardown; it
    must raise its own exception so a harness wiring bug can never masquerade as
    a legitimate refusal NOR be conflated with teardown. We also assert the
    adapter has NOT been torn down — proving this is the never-ingested path.
    """
    adapter = StubAdapter()
    try:
        assert adapter._torn_down is False
        with pytest.raises(PreIngestError):
            adapter.query(_Q, budget)
        # PreIngestError must NOT be a TeardownError (disjoint modes).
        assert not issubclass(PreIngestError, TeardownError)
    finally:
        # Don't leave the adapter alive (finding #10).
        adapter.teardown()


def test_stub_no_match_is_not_a_refusal(corpus, budget):
    """A zero-hit lexical query is a plain MISS, never a governance refusal.

    The stub has no governance layer, so refused must be False even when nothing
    matches — equating empty-results with refusal would mis-code retrieval
    misses as refusals (review finding #2).
    """
    adapter = StubAdapter()
    try:
        adapter.ingest(corpus)
        result = adapter.query("xyzzy plugh frobnicate qwxz zzqq", budget)
        assert result.ranked_results == []
        assert result.refused is False
    finally:
        adapter.teardown()


def test_overbudget_adapter_trips_runner_abort(corpus):
    """An over-budget context_string must ABORT the run (§3.1/§9.4)."""
    budget = TokenBudget(max_tokens=64, max_docs=10)
    adapter = OverBudgetStubAdapter()
    try:
        adapter.ingest(corpus)
        with pytest.raises(BudgetExceeded):
            score_query(adapter, corpus, _Q, budget, 0)
    finally:
        adapter.teardown()


def test_role_marker_adapter_fails_content_only(corpus, budget):
    adapter = RoleMarkerStubAdapter()
    try:
        adapter.ingest(corpus)
        with pytest.raises(ContractError):
            score_query(adapter, corpus, _Q, budget, 0)
    finally:
        adapter.teardown()


def test_duplicate_doc_adapter_fails_uniqueness(corpus, budget):
    adapter = DuplicateDocStubAdapter()
    try:
        adapter.ingest(corpus)
        with pytest.raises(ContractError):
            score_query(adapter, corpus, _Q, budget, 0)
    finally:
        adapter.teardown()


def test_teardown_then_query_raises(corpus, budget):
    adapter = StubAdapter()
    adapter.ingest(corpus)
    adapter.query(_Q, budget)
    adapter.teardown()
    with pytest.raises(TeardownError):
        adapter.query(_Q, budget)


def test_teardown_then_ingest_raises(corpus):
    """teardown() must render the adapter inoperable for ingest() too (§9.4).

    Without this, a torn-down adapter could be silently re-ingested and reused,
    defeating the teardown contract.
    """
    adapter = StubAdapter()
    adapter.ingest(corpus)
    adapter.teardown()
    with pytest.raises(TeardownError):
        adapter.ingest(corpus)


def test_validate_query_empty_raises():
    with pytest.raises(ContractError):
        validate_query("")


def test_validate_query_null_byte_raises():
    with pytest.raises(ContractError):
        validate_query("hello\x00world")


def test_validate_query_oversized_raises():
    # One byte over the MAX_QUERY_BYTES cap (ASCII -> 1 byte/char).
    from membench.contract import MAX_QUERY_BYTES

    with pytest.raises(ContractError):
        validate_query("a" * (MAX_QUERY_BYTES + 1))


def test_validate_query_accepts_valid():
    assert validate_query(_Q) == _Q


def test_max_docs_cap_enforced(corpus):
    """An adapter returning more than max_docs is rejected (§3.1)."""
    budget = TokenBudget(max_tokens=4096, max_docs=2)
    # Build an over-cap result by hand and assert the structural check trips.
    over = QueryResult(
        ranked_results=[RankedDoc(d, 1.0) for d in corpus.doc_ids()[:5]],
        context_string="ok",
        wall_clock_ms=1.0,
    )
    with pytest.raises(ContractError):
        assert_well_formed(over, corpus, budget)

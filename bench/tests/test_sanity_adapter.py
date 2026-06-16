"""Negative-control sanity adapter floors (§9.7).

A relevance-random adapter MUST score near-floor on the synthetic fixture:
``recall@k < 0.10``, ``ndcg@k < 0.05``, ``mrr < 0.05``. If a random pick clears
those, a metric is mis-wired (e.g. counting any returned doc as relevant). The
fixture MUST have >= 50 docs and >= 25 labeled queries with <= 3 gold/query so
the floors hold in EXPECTATION, not by accident on a tiny corpus — the 10-doc
fixture is deliberately NOT used here (with k=10 over 10 docs a random pick
returns every doc and trivially scores recall 1.0).
"""

from membench import config
from membench.adapters.sanity_random import SanityRandomAdapter
from membench.contract import TokenBudget
from membench.fixtures.synthetic import build_synthetic_corpus
from membench.metrics import strip_excluded_fields
from membench.runner_layer1 import canonical_json, run_layer1_gold, scorecard

# §9.7 thresholds — well above pure-random expectation for this fixture's label
# density but far below any real adapter, giving an unambiguous oracle.
RECALL_FLOOR = 0.10
NDCG_FLOOR = 0.05
MRR_FLOOR = 0.05


def test_synthetic_fixture_meets_size_minimums():
    """The fixture itself must clear the §9.7 size floors (not vacuous)."""
    corpus, gold = build_synthetic_corpus(n_docs=200)
    assert len(corpus.doc_ids()) >= 50
    labeled = [g for g in gold]
    assert len(labeled) >= 25
    positives = [g for g in gold if g.band != "negative"]
    assert len(positives) >= 25
    for g in positives:
        assert len(g.gold_doc_ids) <= 3  # <= 3 gold/query (§9.7)


def test_sanity_adapter_scores_below_floors():
    """Relevance-random adapter must score below the §9.7 quality floors."""
    corpus, gold = build_synthetic_corpus(n_docs=200)
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)
    adapter = SanityRandomAdapter()
    try:
        records = run_layer1_gold(adapter, corpus, gold, budget)
        card = scorecard(adapter.name, records, config.K)
    finally:
        adapter.teardown()
    overall = card["overall"]
    assert overall["recall_at_k"] < RECALL_FLOOR, overall
    assert overall["ndcg_at_k"] < NDCG_FLOOR, overall
    assert overall["mrr"] < MRR_FLOOR, overall


def test_sanity_adapter_is_deterministic():
    """The sanity adapter's randomness is fixed-seed -> byte-stable scores.

    Compare CANONICAL JSON after the determinism strip (NIT c) — consistent with
    the determinism gate (§3.2), which strips machine-dependent timing fields and
    then requires byte-identity. Dict equality would ignore key-ordering and pass
    on a card that fails the real byte-level gate, so we match the gate exactly.
    """
    corpus, gold = build_synthetic_corpus(n_docs=200)
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)
    cards = []
    for _ in range(2):
        adapter = SanityRandomAdapter()
        try:
            records = run_layer1_gold(adapter, corpus, gold, budget)
            cards.append(scorecard(adapter.name, records, config.K))
        finally:
            adapter.teardown()
    a = canonical_json(strip_excluded_fields(cards[0]))
    b = canonical_json(strip_excluded_fields(cards[1]))
    assert a == b

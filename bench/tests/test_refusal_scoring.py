"""Correct/false refusal scoring exposes gaming (§6.5 / §9.7).

(a) A gated stub that refuses correctly on negatives scores HIGH
    correct_refusal_rate and 0 false_refusal_rate.
(b) A refuse-everything stub scores HIGH correct_refusal_rate (1.0) AND HIGH
    false_refusal_rate (1.0) AND recall 0 on positives — the PAIR of rates
    exposes the gaming; correct-refusal alone would flatter it.
(c) A fake-refuser (refused=True but still returns docs) earns NO correct-refusal
    credit — the predicate reads BOTH fields (§6.5).
"""

from membench import config
from membench.adapters.stub import (
    FakeRefuseStubAdapter,
    GatedStubAdapter,
    RefuseEverythingStubAdapter,
)
from membench.contract import TokenBudget
from membench.fixtures.synthetic import build_synthetic_corpus
from membench.runner_layer1 import run_layer1_gold, scorecard

# Use the synthetic corpus: its negatives target ABSENT topics with no lexical
# overlap, so a lexical gate genuinely refuses on them (the 10-doc fixture's
# negatives share common words like "team"/"Aurora" with the corpus, which would
# make a lexical gate answer rather than refuse — a real-corpus hazard, not what
# the refusal-SCORING math is being tested for here).
def _score(adapter):
    corpus, gold = build_synthetic_corpus(n_docs=150)
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)
    try:
        records = run_layer1_gold(adapter, corpus, gold, budget)
        return scorecard(adapter.name, records, config.K)
    finally:
        adapter.teardown()


def test_gated_stub_refuses_negatives_correctly():
    """Gated stub: high correct_refusal on negatives, low false_refusal."""
    card = _score(GatedStubAdapter())["overall"]
    # The synthetic negatives ask about absent facts with vocab DISJOINT from the
    # corpus -> every one is a lexical miss -> refusal. All four refuse, so the
    # rate is EXACTLY 1.0; a weaker >0.5 bound would mask a 3-of-4 regression.
    assert card["correct_refusal_rate"] == 1.0, card
    # It answers every positive it can retrieve (the synthetic corpus is built so
    # each positive's gold doc is lexically findable), so false_refusal is 0.0.
    assert card["false_refusal_rate"] == 0.0, card
    # It must ALSO correctly retrieve positives (item 10): a bug that made the
    # gated stub refuse everything would leave false_refusal_rate==0.0 only if it
    # answered nothing — but recall would then collapse. Mirror the FakeRefuse
    # test's positive-retrieval check so the "answers positives" claim is pinned.
    assert card["recall_at_k"] > 0.5, card


def test_refuse_everything_is_exposed_by_the_pair():
    """Refuse-everything: correct_refusal 1.0 BUT false_refusal 1.0, recall 0."""
    card = _score(RefuseEverythingStubAdapter())["overall"]
    assert card["correct_refusal_rate"] == 1.0, card
    # The SAME predicate fires on positives -> maximal false-refusal.
    assert card["false_refusal_rate"] == 1.0, card
    # And it retrieves nothing, so recall is floor across positives.
    assert card["recall_at_k"] == 0.0, card
    assert card["ndcg_at_k"] == 0.0, card
    assert card["mrr"] == 0.0, card


def test_fake_refuser_gets_no_credit_when_it_returns_docs():
    """refused=True with NON-EMPTY ranked is NOT a refusal (§6.5 both fields).

    Observable only when the adapter actually returns docs (a non-empty ranked
    list), i.e. on POSITIVE queries here: the fake-refuser sets refused=True yet
    returns the right docs, so it is NOT scored as a false refusal AND its recall
    stays high — the predicate reads BOTH fields, not the flag alone. (The
    field-level "refused+non-empty != refusal" case is also covered directly in
    test_metrics.test_correct_refusal_fake_refusal_not_credited.)
    """
    card = _score(FakeRefuseStubAdapter())["overall"]
    # On positives it returns the correct doc -> NOT counted as a false refusal
    # despite refused=True, and recall is high (the flag did not suppress credit).
    assert card["false_refusal_rate"] == 0.0, card
    assert card["recall_at_k"] > 0.5, card
    # IMPORTANT (item 7): on the SYNTHETIC NEGATIVES the base StubAdapter finds no
    # lexical overlap and returns an empty ranked list, so FakeRefuseStubAdapter's
    # refused=True + empty list VACUOUSLY satisfies the §6.5 predicate and earns
    # full correct-refusal credit (== 1.0). This is NOT the predicate failing —
    # the adapter genuinely returns nothing on those negatives, so it is a
    # legitimate refusal there. We assert the observed value so the gap is
    # documented rather than hidden behind the test's name.
    assert card["correct_refusal_rate"] == 1.0, card


def test_fake_refuser_with_nonempty_negative_results_earns_no_credit():
    """The credit-farming case the name promises: refused=True + NON-EMPTY ranked.

    The §6.5 predicate (refused AND ranked==[]) must DENY correct-refusal credit
    when an adapter claims refusal yet still returns docs. The FakeRefuseStub on
    the synthetic negatives returns empty (a genuine refusal), so to exercise the
    BOTH-fields predicate directly we drive a hand-built negative case through the
    metric: a query that refused but returned a doc earns ZERO correct-refusal.
    """
    from membench.metrics import correct_refusal_rate, is_refusal

    # refused=True but ranked is non-empty -> NOT a refusal (both fields read).
    assert is_refusal(True, ["doc-a"]) is False
    assert is_refusal(True, []) is True
    # Over a population of negatives, a fake-refuser that returns docs scores 0.0.
    fake_refuser_negatives = [(True, ["doc-a"]), (True, ["doc-b"])]
    assert correct_refusal_rate(fake_refuser_negatives) == 0.0


# ── Fix 4: correct-refusal is an EARNABLE axis, NOT Minni-only ───────────────
def test_at_least_two_distinct_adapters_can_refuse_on_negatives():
    """≥ TWO distinct ungoverned baselines produce is_refusal==True on the
    negative band (fix 4) — the correct-refusal axis is earnable, not Minni-only.

    naive_rag refuses when top cosine < REFUSAL_SCORE_THRESHOLD; markdown_grep
    refuses on zero lexical hits. The synthetic negatives have vocabulary DISJOINT
    from every corpus doc, so both honestly refuse on them. We assert BOTH earn a
    POSITIVE correct_refusal_rate AND a ZERO false_refusal_rate (no regression on
    positives — the panel's exact target)."""
    from membench.adapters.markdown_grep import MarkdownGrepAdapter
    from membench.adapters.naive_rag import NaiveRagAdapter

    refusers = []
    for factory in (NaiveRagAdapter, MarkdownGrepAdapter):
        card = _score(factory())["overall"]
        # Earns correct-refusal credit on the negative band. The negatives have
        # vocabulary DISJOINT from every corpus doc, so a correct adapter refuses
        # on ALL of them — assert == 1.0 (review fix: > 0.0 would let a partial-
        # refusal regression of 1/N pass unnoticed; mirrors the gated-stub test).
        assert card["correct_refusal_rate"] == 1.0, (factory.__name__, card)
        # ...WITHOUT refusing on positives (no false-refusal regression — the
        # positives are built to be retrievable, so tau never fires on them).
        assert card["false_refusal_rate"] == 0.0, (factory.__name__, card)
        # ...and still retrieves positives well (tau is not set so high it
        # suppresses real answers).
        assert card["recall_at_k"] > 0.5, (factory.__name__, card)
        refusers.append(factory.__name__)

    # The load-bearing claim: at least TWO DISTINCT adapters can refuse.
    assert len(set(refusers)) >= 2, refusers


def test_threshold_refusal_does_not_fire_on_positives():
    """The panel's regression target: tau must NOT make a baseline refuse a
    POSITIVE it can retrieve. Drive a single clear-positive query and assert
    neither threshold-refuser refuses it."""
    from membench.adapters.markdown_grep import MarkdownGrepAdapter
    from membench.adapters.naive_rag import NaiveRagAdapter

    corpus, _ = build_synthetic_corpus(n_docs=150)
    budget = TokenBudget(max_tokens=config.DEFAULT_MAX_TOKENS, max_docs=config.K)
    # A query that names a real corpus topic -> a clear positive.
    q = "What document is exclusively about mineral0007xyz?"
    for factory in (NaiveRagAdapter, MarkdownGrepAdapter):
        a = factory()
        try:
            a.ingest(corpus)
            r = a.query(q, budget)
            assert r.refused is False, (factory.__name__, "refused a clear positive")
            assert r.ranked_results, (factory.__name__, "returned nothing on a positive")
        finally:
            a.teardown()

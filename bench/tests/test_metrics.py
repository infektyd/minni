"""Hand-worked metric unit tests (§9.2).

Every expected value below is computed BY HAND in the test (the comment shows the
arithmetic), so a formula deviation from §6 is caught, not rubber-stamped. Covers
the spec's named edge cases: empty gold (negatives), gold larger than k, zero
relevant retrieved, adapter returns fewer than k, and the false-refusal-on-
positive case (§9.2).
"""

import math

import pytest

from membench import metrics


# ── recall@k (§6.1) ──────────────────────────────────────────────────────────
def test_recall_all_gold_retrieved():
    # gold={A,B}, ranked top-k contains both -> 2/2 = 1.0
    assert metrics.recall_at_k(["A", "B", "C"], {"A", "B"}, k=10) == 1.0


def test_recall_partial():
    # gold={A,B,C}, ranked has A,B only -> 2/3
    assert metrics.recall_at_k(["A", "B", "X"], {"A", "B", "C"}, k=10) == pytest.approx(
        2 / 3
    )


def test_recall_gold_larger_than_k_ceiling():
    # gold has 4 docs, k=2, ranked top-2 = [A,B] both gold -> 2/4 = 0.5
    # (§6.1 ceiling: with |G|>k recall maxes at k/|G| = 2/4 = 0.5)
    assert metrics.recall_at_k(["A", "B", "C", "D"], {"A", "B", "C", "D"}, k=2) == 0.5


def test_recall_zero_relevant_retrieved():
    # ranked has none of gold -> 0/2 = 0.0
    assert metrics.recall_at_k(["X", "Y"], {"A", "B"}, k=10) == 0.0


def test_recall_empty_ranked_is_zero():
    # adapter returned fewer than k (here zero) -> 0 relevant -> 0/1
    assert metrics.recall_at_k([], {"A"}, k=10) == 0.0


def test_recall_negative_query_raises():
    with pytest.raises(ValueError):
        metrics.recall_at_k(["A"], set(), k=10)


# ── precision@k (§6.2) — denominator is k, NOT |R_k| ─────────────────────────
def test_precision_denominator_is_k_not_returned_count():
    # ranked = [A] (fewer than k), A is gold. precision = 1 hit / k=10 = 0.1
    # NOT 1/1 — returning fewer docs must NOT inflate precision (§6.2).
    assert metrics.precision_at_k(["A"], {"A"}, k=10) == pytest.approx(0.1)


def test_precision_two_hits_over_k():
    # ranked top-5 = [A,B,X,Y,Z], gold={A,B} -> 2 hits / k=5 = 0.4
    assert metrics.precision_at_k(["A", "B", "X", "Y", "Z"], {"A", "B"}, k=5) == pytest.approx(
        0.4
    )


def test_precision_negative_query_raises():
    with pytest.raises(ValueError):
        metrics.precision_at_k(["A"], set(), k=10)


def test_precision_gold_beyond_k_not_counted(__import_check=None):
    """|ranked| > k, with a gold doc at position k+1 — it must NOT count (item 4).

    ranked = [X,Y,A] (3 items), k=2 so R_k=[X,Y]. gold={A} sits at position 3
    (k+1), OUTSIDE the top-k truncation, so precision@2 = 0 hits / 2 = 0.0. This
    proves _top_k truncates at k, not at |ranked|.
    """
    assert metrics.precision_at_k(["X", "Y", "A"], {"A"}, k=2) == 0.0
    # Sanity: the SAME gold doc inside the top-k (k=3) WOULD count -> 1/3.
    assert metrics.precision_at_k(["X", "Y", "A"], {"A"}, k=3) == pytest.approx(1 / 3)


def test_precision_k_zero_is_zero(__import_check=None):
    """precision_at_k(..., k=0) == 0.0 — the k==0 guard, no ZeroDivisionError
    (item 6). The denominator is k, so k=0 would divide by zero without the
    explicit guard."""
    assert metrics.precision_at_k(["A", "B"], {"A"}, k=0) == 0.0


# ── nDCG@k (§6.3) — binary rel, zero-fill, 0/0=0 ─────────────────────────────
def test_ndcg_perfect_ranking_is_one():
    # gold={A,B}, ranked=[A,B,...]. DCG = 1/log2(2) + 1/log2(3).
    # IDCG (2 ideal hits) = 1/log2(2) + 1/log2(3). Equal -> 1.0.
    assert metrics.ndcg_at_k(["A", "B", "C"], {"A", "B"}, k=10) == pytest.approx(1.0)


def test_ndcg_one_relevant_at_rank_three():
    # gold={A}, ranked=[X,Y,A]. DCG = 1/log2(3+1)=1/log2(4)=1/2=0.5.
    # IDCG (1 ideal hit) = 1/log2(2)=1.0. nDCG = 0.5/1.0 = 0.5.
    got = metrics.ndcg_at_k(["X", "Y", "A"], {"A"}, k=10)
    assert got == pytest.approx(0.5)


def test_ndcg_zero_fill_fewer_than_k():
    # adapter returned fewer than k: ranked=[A] only, gold={A,B}.
    # DCG sums only existing positions: 1/log2(2)=1.0 (B never appears -> rel 0).
    # IDCG (2 ideal hits) = 1/log2(2)+1/log2(3) = 1 + 0.63093 = 1.63093.
    # nDCG = 1.0 / 1.63093 = 0.613147...
    expected = 1.0 / (1.0 / math.log2(2) + 1.0 / math.log2(3))
    assert metrics.ndcg_at_k(["A"], {"A", "B"}, k=10) == pytest.approx(expected)


def test_ndcg_zero_relevant_is_zero():
    assert metrics.ndcg_at_k(["X", "Y"], {"A"}, k=10) == 0.0


def test_ndcg_negative_query_raises():
    with pytest.raises(ValueError):
        metrics.ndcg_at_k(["A"], set(), k=10)


def test_ndcg_relevant_beyond_k_does_not_contribute(__import_check=None):
    """A relevant doc at position k+1 contributes NOTHING to DCG (item 5).

    ranked=[X,Y,A], k=2 -> R_k=[X,Y] (A truncated). gold={A}. DCG over R_k = 0
    (no relevant in top-2), so nDCG@2 = 0/IDCG = 0.0 — proving truncation at k,
    NOT at |R(q)|. (IDCG with 1 ideal hit and k=2 is 1/log2(2)=1.0, non-zero, so
    a wrong DCG-over-all-ranked would give 1/log2(4)=0.5/1.0=0.5 != 0.0.)
    """
    assert metrics.ndcg_at_k(["X", "Y", "A"], {"A"}, k=2) == 0.0
    # Sanity: extend k to 3 and the same gold doc DOES contribute.
    assert metrics.ndcg_at_k(["X", "Y", "A"], {"A"}, k=3) == pytest.approx(0.5)


# ── MRR (§6.4) ───────────────────────────────────────────────────────────────
def test_rr_first_relevant_at_rank_one():
    assert metrics.reciprocal_rank(["A", "B"], {"A"}, k=10) == 1.0


def test_rr_first_relevant_at_rank_three():
    # first gold at rank 3 -> 1/3
    assert metrics.reciprocal_rank(["X", "Y", "A", "B"], {"A", "B"}, k=10) == pytest.approx(
        1 / 3
    )


def test_rr_no_relevant_is_zero():
    assert metrics.reciprocal_rank(["X", "Y"], {"A"}, k=10) == 0.0


def test_rr_outside_topk_is_zero():
    # gold at rank 3 but k=2 -> not in R_k -> 0
    assert metrics.reciprocal_rank(["X", "Y", "A"], {"A"}, k=2) == 0.0


# ── refusal predicate + rates (§6.5) ─────────────────────────────────────────
def test_is_refusal_requires_both_fields():
    assert metrics.is_refusal(True, []) is True
    # refused=True but non-empty ranked -> NOT a refusal (credit-farming hole).
    assert metrics.is_refusal(True, ["A"]) is False
    # empty ranked but refused=False -> plain miss, NOT a refusal.
    assert metrics.is_refusal(False, []) is False
    assert metrics.is_refusal(False, ["A"]) is False


def test_correct_refusal_rate():
    # 3 negatives, 2 correctly refused (refused AND empty) -> 2/3
    negatives = [(True, []), (True, []), (False, ["A"])]
    assert metrics.correct_refusal_rate(negatives) == pytest.approx(2 / 3)


def test_correct_refusal_fake_refusal_not_credited():
    # refused=True but returned a doc -> NOT a refusal -> 0/2
    negatives = [(True, ["A"]), (True, ["B"])]
    assert metrics.correct_refusal_rate(negatives) == 0.0


def test_false_refusal_rate():
    # 4 positives, 1 refused -> 1/4
    positives = [(True, []), (False, ["A"]), (False, ["B"]), (False, ["C"])]
    assert metrics.false_refusal_rate(positives) == 0.25


def test_false_refusal_on_positive_counts_as_miss(__import_check=None):
    """A refusal on a POSITIVE: false_refusal increments AND recall@k == 0 (§9.2).

    Asserts a refusal on a positive is scored as a miss, never free refusal
    credit. The refusal predicate fires (false_refusal counts it) AND because the
    ranked list is empty, recall@k for that query is 0.
    """
    positive = (True, [])  # refused, empty ranked
    assert metrics.false_refusal_rate([positive]) == 1.0
    # recall for a refused positive: empty ranked, gold non-empty -> 0/|gold|.
    assert metrics.recall_at_k([], {"A", "B"}, k=10) == 0.0
    assert metrics.ndcg_at_k([], {"A", "B"}, k=10) == 0.0
    assert metrics.reciprocal_rank([], {"A", "B"}, k=10) == 0.0


def test_refusal_rates_not_complementary():
    """correct_refusal_rate and false_refusal_rate have DIFFERENT denominators."""
    # 2 negatives both refused -> correct = 1.0
    assert metrics.correct_refusal_rate([(True, []), (True, [])]) == 1.0
    # SAME refuse-everything behavior on 3 positives -> false = 1.0 (not 0.0)
    assert metrics.false_refusal_rate([(True, []), (True, []), (True, [])]) == 1.0


def test_correct_refusal_rate_empty_returns_zero():
    """Empty negative population -> 0.0 (vacuous), not a ZeroDivisionError (item 7).

    Load-bearing: the scorecard calls this even when a gold set has no negatives.
    """
    assert metrics.correct_refusal_rate([]) == 0.0


def test_false_refusal_rate_empty_returns_zero():
    """Empty positive population -> 0.0 (vacuous), not a ZeroDivisionError (item 7)."""
    assert metrics.false_refusal_rate([]) == 0.0


# ── token cost (§6.6) + percentiles ──────────────────────────────────────────
def test_mean_empty_is_zero():
    assert metrics.mean([]) == 0.0


def test_mean_basic():
    assert metrics.mean([10.0, 20.0, 30.0]) == 20.0


def test_percentile_p50_odd():
    # sorted [1,2,3]; p50 -> rank 1.0 -> exactly 2
    assert metrics.percentile([3.0, 1.0, 2.0], 50.0) == 2.0


def test_percentile_p95_interpolates():
    # sorted [0,10,20,30,40]; p95 -> rank 0.95*4=3.8 -> 30 + 0.8*(40-30)=38
    assert metrics.percentile([0.0, 10.0, 20.0, 30.0, 40.0], 95.0) == pytest.approx(38.0)


def test_percentile_single_value():
    assert metrics.percentile([7.0], 95.0) == 7.0


def test_percentile_empty_returns_zero():
    """Empty input -> 0.0 (item 9): the scorecard relies on this when a band has
    no records; a regression here would feed nonsense into the latency block."""
    assert metrics.percentile([], 50.0) == 0.0
    assert metrics.percentile([], 95.0) == 0.0


def test_percentile_out_of_range_raises():
    """p outside [0, 100] must raise (item 9), not silently produce a nonsense
    value that leaks into p50/p95."""
    with pytest.raises(ValueError):
        metrics.percentile([1.0, 2.0, 3.0], -1.0)
    with pytest.raises(ValueError):
        metrics.percentile([1.0, 2.0, 3.0], 101.0)


# ── Determinism strip footgun guard (fix 7, §3.2/§9.1) ───────────────────────
def test_score_field_names_are_disjoint_from_strip_set():
    """Score field names MUST NOT collide with the recursively-stripped timing
    fields — else strip_excluded_fields would silently erase a real score at some
    nesting level and the determinism gate would pass vacuously (fix 7)."""
    collision = set(metrics.REQUIRED_SCORE_FIELDS) & metrics.DETERMINISM_STRIP_FIELDS
    assert collision == set(), (
        "score field(s) collide with stripped timing fields: " f"{sorted(collision)}"
    )


def test_strip_does_not_erase_a_score_nested_under_a_non_timing_key():
    """A score block nested under an ARBITRARY key survives the strip; only the
    explicitly-named timing fields are dropped, at every level."""
    obj = {
        "overall": {
            "recall_at_k": 0.5,
            "latency_ms": {"p50": 1.2, "p95": 3.4},  # stripped (timing)
            "bootstrap": {"recall_at_k": 0.5, "p50": 0.4},  # p50 here IS stripped
        }
    }
    out = metrics.strip_excluded_fields(obj)
    # Score survives at both nesting levels; timing leaves are gone everywhere.
    assert out["overall"]["recall_at_k"] == 0.5
    assert "latency_ms" not in out["overall"]
    assert out["overall"]["bootstrap"]["recall_at_k"] == 0.5
    # p50 is a registered timing field, so it is (correctly) stripped wherever it
    # appears — which is exactly why the disjointness invariant above protects the
    # score names from ever being chosen to collide with it.
    assert "p50" not in out["overall"]["bootstrap"]

"""Hand-checked unit tests for the §6.9 significance machinery (fix 3).

Every assertion here is checked against a value computed BY HAND (or against
scipy's own published reference output for a tiny n), so the Wilcoxon + BH-FDR +
CI plumbing is grounded, not self-confirming. These ground the comparative
"adapter A beats adapter B" claims the report makes.
"""

import math

import pytest

from membench.stats import (
    benjamini_hochberg,
    compare_adapters_task_success,
    task_success_ci,
)


# ── Benjamini-Hochberg FDR (hand-checked step-up) ────────────────────────────
def test_bh_all_equal_spaced_pvalues():
    # p=[0.01,0.02,0.03,0.04,0.05], m=5. raw_(i)=p_(i)*5/i = 0.05 for every rank,
    # so every adjusted value is exactly 0.05 (the classic linear case).
    adj = benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
    assert all(a == pytest.approx(0.05) for a in adj)


def test_bh_step_up_monotone_enforcement():
    # p=[0.005,0.009,0.05,0.1,0.5], m=5.
    # raw_(i) = 0.025, 0.0225, 0.0833.., 0.125, 0.5
    # step-up (monotone from the largest rank down): the rank-2 raw 0.0225 pulls
    # rank-1 down from 0.025 to 0.0225, so the two smallest both become 0.0225.
    adj = benjamini_hochberg([0.005, 0.009, 0.05, 0.1, 0.5])
    assert adj[0] == pytest.approx(0.0225)
    assert adj[1] == pytest.approx(0.0225)
    assert adj[2] == pytest.approx(0.05 * 5 / 3)  # 0.08333..
    assert adj[3] == pytest.approx(0.1 * 5 / 4)   # 0.125
    assert adj[4] == pytest.approx(0.5)           # 0.5 * 5/5
    # Returned in INPUT order and clamped to <= 1.
    assert all(0.0 <= a <= 1.0 for a in adj)


def test_bh_preserves_input_order():
    # Unsorted input: order must be preserved on output.
    adj = benjamini_hochberg([0.5, 0.005, 0.1, 0.009, 0.05])
    # The 0.005 (index 1) and 0.009 (index 3) are the two smallest -> 0.0225 each.
    assert adj[1] == pytest.approx(0.0225)
    assert adj[3] == pytest.approx(0.0225)
    assert adj[0] == pytest.approx(0.5)


def test_bh_empty():
    assert benjamini_hochberg([]) == []


def test_bh_clamps_above_one():
    # A large p with a small family inflates raw beyond 1.0; must clamp.
    adj = benjamini_hochberg([0.9, 0.95])
    assert all(a <= 1.0 for a in adj)


# ── 95% CI on per-adapter task success (Student-t, hand-checked) ─────────────
def test_ci_constant_rates_is_a_point():
    ci = task_success_ci([1.0, 1.0, 1.0, 1.0])
    assert ci.mean == 1.0 and ci.low == 1.0 and ci.high == 1.0 and ci.n == 4


def test_ci_two_episodes_clamped_to_unit_interval():
    # rates [0,1]: mean 0.5, sd(ddof=1)=0.7071, se=0.5, t_975(df=1)=12.706 ->
    # half-width 6.353; clamped to [0,1].
    ci = task_success_ci([0.0, 1.0])
    assert ci.mean == pytest.approx(0.5)
    assert ci.low == 0.0 and ci.high == 1.0


def test_ci_hand_computed_halfwidth():
    # rates [0.4,0.6,0.5,0.5,0.5] (n=5): mean 0.5; deviations sum-sq = 0.01+0.01 =
    # 0.02; var(ddof=1)=0.02/4=0.005; sd=0.070710..; se=sd/sqrt(5)=0.0316227..;
    # t_975(df=4)=2.776 -> half = 0.08777.. -> CI = [0.4122.., 0.5877..].
    ci = task_success_ci([0.4, 0.6, 0.5, 0.5, 0.5])
    sd = math.sqrt(0.005)
    se = sd / math.sqrt(5)
    half = 2.776 * se
    assert ci.mean == pytest.approx(0.5)
    assert ci.low == pytest.approx(0.5 - half)
    assert ci.high == pytest.approx(0.5 + half)


def test_ci_empty_and_single():
    assert task_success_ci([]).n == 0
    one = task_success_ci([0.7])
    assert one.mean == 0.7 and one.low == 0.7 and one.high == 0.7


def test_ci_rejects_non_95_level():
    with pytest.raises(ValueError):
        task_success_ci([0.5, 0.6], level=0.90)


# ── Pairwise Wilcoxon + BH over per-episode paired success rates (§6.9) ───────
def _rates(values):
    return {f"ep-{i}": v for i, v in enumerate(values)}


def test_wilcoxon_clear_winner_is_significant():
    # A strictly dominates B on every one of 8 episodes -> the two-sided Wilcoxon
    # p for all-positive differences over n=8 is 2/2^8 = 0.0078125, well below
    # q=0.05 even after BH on a 1-comparison family. winner == A.
    per_episode = {
        "A": _rates([0.6, 0.7, 0.8, 0.9, 1.0, 0.7, 0.8, 0.9]),
        "B": _rates([0.1, 0.2, 0.3, 0.4, 0.5, 0.2, 0.3, 0.4]),
    }
    comps = compare_adapters_task_success(per_episode, q=0.05)
    assert len(comps) == 1
    c = comps[0]
    assert c.adapter_a == "A" and c.adapter_b == "B"
    assert c.n_episodes == 8
    assert c.p_value == pytest.approx(2 / 2 ** 8)
    # 1-comparison family: BH-adjusted == raw.
    assert c.p_adjusted == pytest.approx(c.p_value)
    assert c.significant is True
    assert c.winner == "A"
    # Nit b: the median per-episode difference (A - B) must be POSITIVE, matching
    # the winner direction — A dominates B on every episode, so median_diff > 0.
    assert c.median_diff > 0


def test_wilcoxon_tie_is_not_significant():
    # Identical per-episode rates -> all-zero diffs -> p=1.0, not significant.
    rates = _rates([0.5, 0.6, 0.7, 0.8])
    comps = compare_adapters_task_success({"A": rates, "B": dict(rates)}, q=0.05)
    c = comps[0]
    assert c.p_value == 1.0
    assert c.significant is False
    assert c.winner == ""


def test_family_size_is_c_n_2():
    # 4 adapters -> C(4,2) = 6 pairwise comparisons, the confirmatory family.
    per = {name: _rates([0.5, 0.5, 0.5, 0.5]) for name in ("a", "b", "c", "d")}
    comps = compare_adapters_task_success(per, q=0.05)
    assert len(comps) == 6


def test_bh_correction_demotes_the_single_smallest_p_in_a_size10_family():
    # The multiplicity-control proof, done directly on a size-10 p-family with
    # exactly ONE small p (rank 1) and nine ties at 1.0. BH at rank 1 multiplies
    # by m/1 = 10: a raw 0.0078125 (the n=8 all-positive Wilcoxon p) becomes
    # 0.078125 > 0.05, so a pair that WOULD be significant raw is DEMOTED to
    # not-significant after correction. This is exactly the family shape §6.9
    # corrects (10 overall pairwise comparisons).
    family = [2 / 2 ** 8] + [1.0] * 9  # one real signal, nine ties
    adj = benjamini_hochberg(family)
    assert adj[0] == pytest.approx(family[0] * 10 / 1)  # 0.078125
    assert adj[0] > 0.05  # demoted: not significant after correction
    assert all(a == 1.0 for a in adj[1:])


def test_bh_does_not_demote_when_several_pairs_genuinely_win():
    # When MANY pairs genuinely differ (here a beats c/d/e and b loses to c/d/e),
    # the smallest p sits at a higher rank, so BH divides by a larger i and the
    # adjusted p stays small -> the win is retained. Verifies the correction does
    # NOT over-penalize a family with real, broad signal (computed, not eyeballed).
    win = _rates([0.6, 0.7, 0.8, 0.9, 1.0, 0.7, 0.8, 0.9])
    lose = _rates([0.1, 0.2, 0.3, 0.4, 0.5, 0.2, 0.3, 0.4])
    base = _rates([0.5] * 8)
    per = {"a": win, "b": lose, "c": dict(base), "d": dict(base), "e": dict(base)}
    comps = compare_adapters_task_success(per, q=0.05)
    assert len(comps) == 10  # C(5,2)
    a_b = next(c for c in comps if {c.adapter_a, c.adapter_b} == {"a", "b"})
    assert a_b.p_value == pytest.approx(2 / 2 ** 8)
    # a beats c/d/e too -> four pairs share the smallest raw p (ranks 1-4), so the
    # BH adjust for them is raw * 10 / 4, still well below 0.05 -> significant.
    assert a_b.p_adjusted == pytest.approx(a_b.p_value * 10 / 4)
    assert a_b.significant is True
    assert a_b.winner == "a"


def test_pairing_is_by_episode_intersection():
    # B is missing one episode; the pair is formed on the INTERSECTION only.
    a = {"ep-0": 0.9, "ep-1": 0.8, "ep-2": 0.7}
    b = {"ep-0": 0.1, "ep-1": 0.2}  # no ep-2
    comps = compare_adapters_task_success({"A": a, "B": b}, q=0.05)
    assert comps[0].n_episodes == 2  # only the shared episodes are paired


def test_zero_shared_episodes_is_included_with_neutral_pvalue():
    # Review fix 7: two adapters with DISJOINT episode sets cannot be paired. The
    # comparison must still be emitted (so the BH family size is correct) with
    # n_episodes=0, p_value=1.0 (neutral — does not distort the FDR family),
    # significant=False, winner=''. p_adjusted comes from the BH pass, so it is a
    # valid number (not the early-branch NaN default).
    a = {"ep-0": 0.9, "ep-1": 0.8}
    b = {"ep-2": 0.1, "ep-3": 0.2}  # fully disjoint keys
    comps = compare_adapters_task_success({"A": a, "B": b}, q=0.05)
    assert len(comps) == 1
    c = comps[0]
    assert c.n_episodes == 0
    assert c.p_value == 1.0
    assert c.significant is False
    assert c.winner == ""
    # p_adjusted is set by the BH correction (the comparison IS in the family).
    assert c.p_adjusted == pytest.approx(1.0)
    assert not math.isnan(c.p_adjusted)


def test_zero_shared_pair_does_not_distort_a_real_winner_in_the_family():
    # The disjoint (p=1.0) comparison sits in the SAME BH family as a real winner;
    # it must not inflate the adjusted p of the genuine signal beyond recognition.
    win = {f"ep-{i}": v for i, v in enumerate(
        [0.6, 0.7, 0.8, 0.9, 1.0, 0.7, 0.8, 0.9])}
    lose = {f"ep-{i}": v for i, v in enumerate(
        [0.1, 0.2, 0.3, 0.4, 0.5, 0.2, 0.3, 0.4])}
    far_a = {"x-0": 0.5, "x-1": 0.5}
    far_b = {"y-0": 0.5, "y-1": 0.5}  # disjoint from far_a -> n_episodes=0
    per = {"win": win, "lose": lose, "fa": far_a, "fb": far_b}
    comps = compare_adapters_task_success(per, q=0.05)
    # C(4,2)=6 comparisons, all present including the disjoint fa/fb pair.
    assert len(comps) == 6
    fa_fb = next(c for c in comps if {c.adapter_a, c.adapter_b} == {"fa", "fb"})
    assert fa_fb.n_episodes == 0 and fa_fb.p_value == 1.0
    win_lose = next(
        c for c in comps if {c.adapter_a, c.adapter_b} == {"win", "lose"}
    )
    # The real signal survives BH over the 6-member family (raw p * 6/1 here,
    # since the others are all 1.0 and rank above it).
    assert win_lose.p_adjusted == pytest.approx(win_lose.p_value * 6 / 1)
    assert win_lose.significant is True
    assert win_lose.winner == "win"


def test_wilcoxon_value_error_fallback_to_approx(monkeypatch):
    # Review fix 9: _wilcoxon_pair has an `except ValueError` fallback to
    # method='approx'. The installed scipy does NOT raise ValueError for the n=2
    # all-same-sign case (it returns a p), so the branch is otherwise untested.
    # Force the primary call to raise ValueError and assert the fallback's result
    # is returned (the approx method is invoked and its p flows through).
    import membench.stats as stats_mod

    calls = {"n": 0}

    class _Res:
        statistic = 3.0
        pvalue = 0.123

    def fake_wilcoxon(a, b, **kwargs):
        calls["n"] += 1
        if kwargs.get("method") == "approx":
            return _Res()  # the fallback path's result
        raise ValueError("forced: simulate scipy raising on the primary call")

    monkeypatch.setattr("scipy.stats.wilcoxon", fake_wilcoxon)
    stat, p = stats_mod._wilcoxon_pair([0.9, 0.8, 0.7], [0.1, 0.2, 0.3])
    assert calls["n"] == 2  # primary (raised) + approx fallback
    assert stat == pytest.approx(3.0)
    assert p == pytest.approx(0.123)


def test_wilcoxon_n2_all_positive_handled_by_primary_path():
    # Documents the empirical behaviour the fallback comment references: with the
    # installed scipy, an n=2 all-positive-diff pair is handled by the PRIMARY
    # wilcoxon call (no ValueError), returning a valid p in [0, 1]. This is why the
    # ValueError fallback above had to be exercised via a forced raise, not real
    # inputs — the branch is defensive against future scipy behaviour changes.
    from membench.stats import _wilcoxon_pair

    stat, p = _wilcoxon_pair([0.9, 0.8], [0.1, 0.2])
    assert 0.0 <= p <= 1.0

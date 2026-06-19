"""Pre-registered comparison statistics for Layer-2 (§6.9) — fix 3.

Reporting per-adapter means + 95% CIs is NOT enough to *claim* one adapter beats
another; with N=5 trials over ~20 episodes the CIs overlap and "eyeballing
intervals" is exactly what a reviewer attacks (§6.9). The spec pre-registers the
comparison procedure; this module IMPLEMENTS it so every comparative claim is
grounded in code:

- **Unit of analysis** (§6.9): per-EPISODE success rates — each episode's MEAN
  success over its N trials — PAIRED by episode across the two adapters compared.
  (Same episodes -> a paired design. This is the per-episode aggregation the
  runner now produces, fix 2.)
- **Test:** the **Wilcoxon signed-rank** test over the per-episode paired
  differences (``scipy.stats.wilcoxon``; non-parametric, robust to the small,
  non-normal, bounded 0-1 episode sample). Authoritative per §6.9.
- **Confirmatory family:** **overall task success only** — up to C(5,2)=10
  pairwise comparisons across <=5 adapters = the entire confirmatory family
  (§6.9). Per-band tests are exploratory and NOT mixed into this family.
- **Multiple-comparison correction:** the family of pairwise p-values is
  corrected with **Benjamini-Hochberg FDR** at ``q`` (default 0.05). A claim of
  "A beats B on task success" is made ONLY when the BH-adjusted Wilcoxon
  ``p < q``; otherwise "no significant difference at the current N" (§6.9).
- **95% CIs** on per-adapter task success, over the per-episode mean success
  rates (the §6.7 / line-320 reporting requirement).

Everything here is PURE / deterministic given its inputs and makes no network
call. ``scipy`` is the only non-stdlib dependency (present in engine/.venv).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Per-adapter 95% CI on task success (over per-episode mean success rates)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfidenceInterval:
    """A two-sided confidence interval on a mean."""

    mean: float
    low: float
    high: float
    n: int
    level: float  # e.g. 0.95


# Student-t critical values t_{0.975, df} for small df (two-sided 95%). For
# df >= 30 we fall back to the normal approximation 1.96 (the t value is within
# ~3% and these are reporting CIs, not gate thresholds). Hand-checkable: each
# entry is the standard table value, so the unit tests can assert exact arithmetic.
_T_975: dict[int, float] = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045,
}


def _t_crit_975(df: int) -> float:
    if df <= 0:
        return 0.0
    if df in _T_975:
        return _T_975[df]
    return 1.96  # df >= 30: normal approximation (documented above)


def task_success_ci(
    per_episode_rates: list[float], *, level: float = 0.95
) -> ConfidenceInterval:
    """95% CI on per-adapter task success over per-episode mean success rates.

    ``per_episode_rates`` is ONE value per episode: that episode's mean success
    over its N trials (the §6.9 unit of analysis). The CI is the Student-t
    interval on the mean of those per-episode rates (n = #episodes, df = n-1),
    clamped to [0, 1] since success is a bounded rate. With a single episode the
    interval is a point (the spread is undefined); with zero, an empty point at 0.

    Only ``level=0.95`` is supported (the spec's reporting level); other levels
    raise so a caller cannot silently get a mislabeled interval.
    """
    if level != 0.95:
        raise ValueError("only level=0.95 is supported (the spec reporting level)")
    n = len(per_episode_rates)
    if n == 0:
        return ConfidenceInterval(mean=0.0, low=0.0, high=0.0, n=0, level=level)
    mean = sum(per_episode_rates) / n
    if n == 1:
        return ConfidenceInterval(mean=mean, low=mean, high=mean, n=1, level=level)
    # Sample standard deviation (ddof=1) -> standard error -> t interval.
    var = sum((r - mean) ** 2 for r in per_episode_rates) / (n - 1)
    sd = math.sqrt(var)
    se = sd / math.sqrt(n)
    half = _t_crit_975(n - 1) * se
    low = max(0.0, mean - half)
    high = min(1.0, mean + half)
    return ConfidenceInterval(mean=mean, low=low, high=high, n=n, level=level)


# ---------------------------------------------------------------------------
# Benjamini-Hochberg FDR correction over a family of p-values
# ---------------------------------------------------------------------------
def benjamini_hochberg(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg STEP-UP adjusted p-values (q-values), input order.

    For ``m`` p-values sorted ascending p_(1)..p_(m), the BH adjusted value at
    rank ``i`` (1-based) is ``p_(i) * m / i``, then made MONOTONE non-decreasing
    from the largest rank down (the standard step-up enforcement so a smaller raw
    p never gets a larger adjusted p), and clamped to <= 1.0. Returned in the
    SAME order as the input. A family is "significant at q" wherever the adjusted
    value is < q. Empty input -> empty output. Hand-checkable on small inputs.
    """
    m = len(pvalues)
    if m == 0:
        return []
    # Sort (value, original_index) ascending by p.
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted_sorted = [0.0] * m
    # Step-up: walk from the LARGEST p (rank m) down to rank 1, carrying the
    # running minimum so the sequence is monotone non-decreasing in rank.
    running_min = 1.0
    for rank in range(m, 0, -1):
        idx = order[rank - 1]
        raw = pvalues[idx] * m / rank
        running_min = min(running_min, raw)
        adjusted_sorted[rank - 1] = min(1.0, running_min)
    # Scatter back to original order.
    out = [0.0] * m
    for rank in range(m):
        out[order[rank]] = adjusted_sorted[rank]
    return out


# ---------------------------------------------------------------------------
# Pairwise Wilcoxon signed-rank over per-episode paired success rates (§6.9)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PairwiseComparison:
    """One A-vs-B confirmatory comparison on overall task success (§6.9)."""

    adapter_a: str
    adapter_b: str
    n_episodes: int  # paired episodes (after dropping any unpaired)
    mean_a: float  # A's mean per-episode success over the paired episodes
    mean_b: float
    median_diff: float  # median of per-episode (a - b)
    wilcoxon_stat: float
    p_value: float  # raw two-sided Wilcoxon p
    p_adjusted: float = float("nan")  # BH-adjusted across the family (filled later)
    significant: bool = False  # p_adjusted < q AND a direction exists
    winner: str = ""  # adapter with the higher mean when significant, else ""


def _wilcoxon_pair(a: list[float], b: list[float]) -> tuple[float, float]:
    """Two-sided Wilcoxon signed-rank (stat, p) over paired samples a,b.

    All-zero differences (the two adapters tied on every episode) have no
    well-defined signed-rank statistic; scipy raises, so we map that to
    (stat=0.0, p=1.0) — "no evidence of a difference", the correct conclusion.
    """
    from scipy.stats import wilcoxon

    diffs = [x - y for x, y in zip(a, b)]
    if all(d == 0 for d in diffs):
        return 0.0, 1.0
    try:
        res = wilcoxon(a, b)  # zero_method='wilcox' default: drops zero diffs
    except ValueError:
        # e.g. every nonzero diff has the same sign with too-few samples; fall
        # back to the asymptotic method which always returns a p.
        res = wilcoxon(a, b, method="approx")
    return float(res.statistic), float(res.pvalue)


def compare_adapters_task_success(
    per_episode_success: dict[str, dict[str, float]],
    *,
    q: float = 0.05,
) -> list[PairwiseComparison]:
    """Confirmatory pairwise comparison of OVERALL task success (§6.9).

    ``per_episode_success`` maps ``adapter -> {episode_id -> mean success rate}``
    (the per-episode aggregation, fix 2). For every unordered adapter pair this:

    1. PAIRS the two adapters by episode id (intersection of their episode ids,
       sorted for determinism) — the §6.9 paired design.
    2. Runs the two-sided **Wilcoxon signed-rank** test on the paired per-episode
       success rates.
    3. After collecting the WHOLE family of raw p-values, corrects them with
       **Benjamini-Hochberg FDR** (the confirmatory family is "overall task
       success", size up to C(5,2)=10).
    4. Marks a pair ``significant`` iff its BH-adjusted p < ``q`` AND the means
       differ; ``winner`` is the higher-mean adapter (else "").

    Returns the comparisons in a deterministic order (sorted adapter pairs).
    Pure/offline aside from scipy.
    """
    adapters = sorted(per_episode_success)
    comparisons: list[PairwiseComparison] = []
    for a_name, b_name in itertools.combinations(adapters, 2):
        a_map = per_episode_success[a_name]
        b_map = per_episode_success[b_name]
        shared = sorted(set(a_map) & set(b_map))
        a_vals = [a_map[e] for e in shared]
        b_vals = [b_map[e] for e in shared]
        if not shared:
            comparisons.append(
                PairwiseComparison(
                    adapter_a=a_name, adapter_b=b_name, n_episodes=0,
                    mean_a=0.0, mean_b=0.0, median_diff=0.0,
                    wilcoxon_stat=0.0, p_value=1.0,
                )
            )
            continue
        mean_a = sum(a_vals) / len(a_vals)
        mean_b = sum(b_vals) / len(b_vals)
        diffs = sorted(x - y for x, y in zip(a_vals, b_vals))
        mid = len(diffs) // 2
        median_diff = (
            diffs[mid]
            if len(diffs) % 2
            else (diffs[mid - 1] + diffs[mid]) / 2.0
        )
        stat, p = _wilcoxon_pair(a_vals, b_vals)
        comparisons.append(
            PairwiseComparison(
                adapter_a=a_name, adapter_b=b_name, n_episodes=len(shared),
                mean_a=mean_a, mean_b=mean_b, median_diff=median_diff,
                wilcoxon_stat=stat, p_value=p,
            )
        )

    # BH-FDR across the WHOLE confirmatory family (§6.9), then mark significance.
    adjusted = benjamini_hochberg([c.p_value for c in comparisons])
    out: list[PairwiseComparison] = []
    for c, padj in zip(comparisons, adjusted):
        significant = bool(padj < q and c.mean_a != c.mean_b)
        winner = ""
        if significant:
            winner = c.adapter_a if c.mean_a > c.mean_b else c.adapter_b
        out.append(
            PairwiseComparison(
                adapter_a=c.adapter_a, adapter_b=c.adapter_b,
                n_episodes=c.n_episodes, mean_a=c.mean_a, mean_b=c.mean_b,
                median_diff=c.median_diff, wilcoxon_stat=c.wilcoxon_stat,
                p_value=c.p_value, p_adjusted=padj,
                significant=significant, winner=winner,
            )
        )
    return out


def comparison_to_dict(c: PairwiseComparison) -> dict:
    """Canonical JSON-able dict for one comparison (sorted-key friendly)."""
    return {
        "adapter_a": c.adapter_a,
        "adapter_b": c.adapter_b,
        "n_episodes": c.n_episodes,
        "mean_a": c.mean_a,
        "mean_b": c.mean_b,
        "median_diff": c.median_diff,
        "wilcoxon_stat": c.wilcoxon_stat,
        "p_value": c.p_value,
        "p_adjusted": c.p_adjusted,
        "significant": c.significant,
        "winner": c.winner,
    }

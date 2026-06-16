"""Exact Layer-1 scoring formulas (design spec §6) + determinism gate (§3.2/§9.1).

Every metric here is a PURE function of (ranked doc-ids, gold doc-ids, refusal
flags, token counts). No LLM, no network, no clock — Layer 1 is the part a
skeptic can run themselves and get byte-identical numbers (§3.2).

Notation (§6): for a query *q*, ``gold`` = set of gold doc-ids G(q), ``ranked`` =
the adapter's ranked list R(q), ``R_k`` = the top-``k`` of ``ranked``. ``k``
defaults to ``config.K``. ``rel(d) = 1`` iff ``d in gold`` else ``0``.

Population conventions (load-bearing — a reviewer will check these):
- recall@k / precision@k / nDCG@k / MRR are defined ONLY on **non-negative**
  queries (``gold != set()``) and reported as the mean over those queries (§6.1,
  §6.2, §6.3, §6.4). Negatives carry G(q)=∅ and are excluded from quality math;
  their behavior is scored exactly once, in the refusal metrics (§6.5).
- correct_refusal_rate is over the NEGATIVE queries only; false_refusal_rate is
  over the POSITIVE (non-negative) queries only (§6.5). The two rates have
  DIFFERENT denominators and are NOT complementary — they are reported side by
  side so a refuse-everything adapter (1.0 correct-refusal) is exposed by a high
  false-refusal rate (§6.5).
- token_cost is the mean ``harness_tokens`` over a precisely-named population:
  ALL scored queries (positives AND negatives), since the harness tokenizes
  ``context_string`` for every query (§6.6). The population is stated once here
  and used consistently by the aggregator.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from . import config

# ---------------------------------------------------------------------------
# Determinism gate excluded fields (§3.2 / §9.1).
#
# The byte-identical Layer-1 score comparison strips EXACTLY these field names
# and asserts every OTHER field byte-identical. They are the wall-clock timing
# fields named in §3.1 — machine-dependent, deliberately excluded from the
# determinism gate (latency is reported as a distribution, never byte-checked).
#
# This is the AUTHORITATIVE copy (config.py mirrors it for the report header per
# the spec's §11 placement; the two are asserted equal by test_determinism).
# Any NEW timing field a future scorecard adds MUST be named here AND in
# config.DETERMINISM_EXCLUDED_FIELDS, or the determinism gate will (correctly)
# flag it as unregistered nondeterminism — which the §9.1 meta-test proves.
# ---------------------------------------------------------------------------
DETERMINISM_EXCLUDED_FIELDS: frozenset[str] = frozenset(
    {"wall_clock_ms", "build_wall_clock_ms"}
)

# The scorecard derives a per-adapter latency DISTRIBUTION (p50/p95) from the
# wall-clock per-record times. Those percentiles are machine-dependent timing —
# they must NOT enter the byte-identity gate (§3.2: "latency is reported as a
# distribution ... excluded from the byte-identical determinism gate"). They are
# named EXPLICITLY here (the task's instruction: "+ any other timing field the
# scorecard adds — name them in the constant") rather than silently dropped.
# ``latency_ms`` is the container key; ``p50``/``p95`` are its leaves. Stripping
# the container removes all of them. These are SCORECARD-level timing fields, kept
# separate from the §9.1 contract-record constant above (which the §9.1 test
# asserts is EXACTLY the two §3.1 record fields) so neither assertion lies.
SCORECARD_TIMING_FIELDS: frozenset[str] = frozenset({"latency_ms", "p50", "p95"})

# The full set the determinism gate strips from a SCORECARD artifact: the two
# §3.1 record timing fields plus the scorecard's own derived latency fields.
DETERMINISM_STRIP_FIELDS: frozenset[str] = (
    DETERMINISM_EXCLUDED_FIELDS | SCORECARD_TIMING_FIELDS
)

# The score fields that MUST survive the determinism strip (§9.1): a lazy
# implementer cannot pass the gate by stripping the whole record. The §9.1 test
# asserts each of these is still present in the stripped per-adapter scorecard.
REQUIRED_SCORE_FIELDS: tuple[str, ...] = (
    "recall_at_k",
    "precision_at_k",
    "ndcg_at_k",
    "mrr",
    "token_cost",
    "correct_refusal_rate",
    "false_refusal_rate",
)


# ---------------------------------------------------------------------------
# Per-query primitives (§6.1 – §6.4). Each is a pure function with a single,
# hand-verifiable formula; the unit tests compute the expected value by hand.
# ---------------------------------------------------------------------------
def _top_k(ranked: Sequence[str], k: int) -> list[str]:
    """The top-k of the ranked doc-id list (R_k(q), §6)."""
    return list(ranked[:k])


def recall_at_k(ranked: Sequence[str], gold: set[str], k: int = config.K) -> float:
    """recall@k(q) = |R_k(q) ∩ G(q)| / |G(q)|  (§6.1; defined for |G(q)| > 0).

    When |G(q)| > k the maximum is k/|G(q)| < 1.0 (the §6.1 ceiling) — this is a
    property of the gold set, not a bug; the formula is unchanged.
    """
    if not gold:
        raise ValueError("recall@k is undefined for a negative query (G(q)=∅)")
    rk = set(_top_k(ranked, k))
    return len(rk & gold) / len(gold)


def precision_at_k(
    ranked: Sequence[str], gold: set[str], k: int = config.K
) -> float:
    """precision@k(q) = |R_k(q) ∩ G(q)| / k  (§6.2; defined for |G(q)| > 0).

    Denominator is k, NOT |R_k| — if the adapter returns fewer than k docs the
    missing slots count as non-relevant, so returning fewer docs cannot inflate
    precision (§6.2).
    """
    if not gold:
        raise ValueError("precision@k is undefined for a negative query (G(q)=∅)")
    if k == 0:
        return 0.0  # explicit 0/0 = 0, consistent with the nDCG convention
    rk = set(_top_k(ranked, k))
    return len(rk & gold) / k


def ndcg_at_k(ranked: Sequence[str], gold: set[str], k: int = config.K) -> float:
    """nDCG@k(q) = DCG@k / IDCG@k  (binary relevance, §6.3).

    DCG@k  = Σ_{i=1..k} rel(R_i) / log2(i+1), positions i > |R(q)| treated as
             rel=0 (the sum runs only over existing ranked positions, zero-fill).
    IDCG@k = Σ_{i=1..min(k,|G(q)|)} 1 / log2(i+1).
    Defined 0 when IDCG@k = 0 (explicit 0/0 = 0).
    """
    if not gold:
        raise ValueError("nDCG@k is undefined for a negative query (G(q)=∅)")
    rk = _top_k(ranked, k)
    # DCG: sum only over positions that actually exist in R_k (zero-fill the
    # rest). Position index i is 1-based, so the discount is log2(i+1).
    dcg = 0.0
    for i, doc_id in enumerate(rk, start=1):
        if doc_id in gold:
            dcg += 1.0 / math.log2(i + 1)
    # IDCG: the best achievable ordering puts min(k, |gold|) relevant docs first.
    ideal_hits = min(k, len(gold))
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0  # explicit 0/0 = 0 (cannot occur once gold != ∅, but pinned)
    return dcg / idcg


def reciprocal_rank(
    ranked: Sequence[str], gold: set[str], k: int = config.K
) -> float:
    """RR(q) = 1 / rank_of_first_relevant(R_k(q)); 0 if no relevant in R_k (§6.4)."""
    if not gold:
        raise ValueError("MRR is undefined for a negative query (G(q)=∅)")
    rk = _top_k(ranked, k)
    for i, doc_id in enumerate(rk, start=1):
        if doc_id in gold:
            return 1.0 / i
    return 0.0


# ---------------------------------------------------------------------------
# Refusal predicate + rates (§6.5). ONE operational definition of refusal,
# drawn straight from the QueryResult contract: refused == True AND
# ranked_results == []. Both required — refused=True with a non-empty ranked
# list is NOT a refusal (closes the credit-farming hole, §6.5).
# ---------------------------------------------------------------------------
def is_refusal(refused: bool, ranked: Sequence[str]) -> bool:
    """Refusal iff ``refused`` AND the ranked list is empty (§6.5, both fields)."""
    return bool(refused) and len(ranked) == 0


def correct_refusal_rate(
    negatives: Sequence[tuple[bool, Sequence[str]]],
) -> float:
    """(# negatives refused) / (# negative queries)  (§6.5).

    ``negatives`` is one ``(refused, ranked_doc_ids)`` pair per NEGATIVE query.
    Returns 0.0 when there are no negatives (vacuous; reported alongside the
    count so the denominator is visible).
    """
    if not negatives:
        return 0.0
    refused = sum(1 for r, ranked in negatives if is_refusal(r, ranked))
    return refused / len(negatives)


def false_refusal_rate(
    positives: Sequence[tuple[bool, Sequence[str]]],
) -> float:
    """(# positive queries refused) / (# positive queries)  (§6.5).

    Same refusal predicate as correct_refusal_rate, applied to POSITIVE queries:
    a refusal on a positive is a miss scored here (and recall@k = 0 for it). The
    denominator is the positives, NOT the negatives — the two rates are reported
    side by side and are NOT complementary.
    """
    if not positives:
        return 0.0
    refused = sum(1 for r, ranked in positives if is_refusal(r, ranked))
    return refused / len(positives)


# ---------------------------------------------------------------------------
# Token cost (§6.6) + latency percentiles (reported, NOT in the determinism
# gate — §3.2). token_cost population: ALL scored queries.
# ---------------------------------------------------------------------------
def mean(values: Sequence[float]) -> float:
    """Arithmetic mean; 0.0 over an empty population (stated, not silent)."""
    if not values:
        return 0.0
    return sum(values) / len(values)


def percentile(values: Sequence[float], p: float) -> float:
    """The ``p``-th percentile (0..100) via linear interpolation on a sorted copy.

    Deterministic (sorted input, pure arithmetic). Used for latency p50/p95 only,
    which are REPORTED but EXCLUDED from the byte-identical determinism gate
    (§3.2) — wall-clock is machine-dependent. Returns 0.0 over empty input.
    """
    if not values:
        return 0.0
    if not 0.0 <= p <= 100.0:
        raise ValueError("percentile p must be in [0, 100]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (p / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    frac = rank - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


# ---------------------------------------------------------------------------
# Determinism strip (§3.2 / §9.1).
# ---------------------------------------------------------------------------
def strip_excluded_fields(obj, strip: frozenset[str] = DETERMINISM_STRIP_FIELDS):
    """Recursively drop the named timing keys from a JSON-able object.

    Used by the determinism gate: strip EXACTLY the named timing fields
    (``DETERMINISM_STRIP_FIELDS`` = the two §3.1 record fields + the scorecard's
    derived ``latency_ms``/``p50``/``p95``), then require the remainder
    byte-identical across two runs (§3.2). A field NOT in the strip set (e.g. a
    jittery ``run_start_epoch``) SURVIVES the strip and therefore makes the
    comparison FAIL — which is precisely what the §9.1 meta-test asserts (the gate
    catches unregistered nondeterminism rather than silently ignoring it).

    The strip set is a parameter so a test can pass the narrower
    ``DETERMINISM_EXCLUDED_FIELDS`` to prove that set alone is insufficient for a
    scorecard that carries latency (the latency block then leaks and the diff
    fails), demonstrating the scorecard-timing fields are load-bearing.
    """
    if isinstance(obj, dict):
        return {
            key: strip_excluded_fields(value, strip)
            for key, value in obj.items()
            if key not in strip
        }
    if isinstance(obj, list):
        return [strip_excluded_fields(item, strip) for item in obj]
    return obj

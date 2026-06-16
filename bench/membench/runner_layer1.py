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

import json
from dataclasses import asdict, dataclass

from . import metrics
from .config import CONTEXT_LOG_TRUNCATE
from .corpus import CorpusHashMismatch
from .contract import (
    FrozenCorpus,
    MemoryAdapter,
    QueryResult,
    TokenBudget,
    assert_well_formed,
    validate_query,
)
from .contract import IngestReport
from .goldset import BAND_NEGATIVE, BANDS, GoldItem
from .tokenizer import count_tokens

# Scorecard float precision (§9.1 determinism note). nDCG/MRR/recall involve
# log2 + float division, and the naive_rag embedder yields float32 scores whose
# bit pattern can vary across BLAS builds; we ROUND every scorecard metric to a
# fixed number of decimal places so the canonical JSON is byte-stable on the
# pinned runtime. Ranking ORDER (which doc-ids are returned) is the determinism-
# load-bearing quantity and is unaffected; this only quantizes the reported
# aggregate floats. Documented here per the task's explicit instruction.
SCORE_PRECISION = 10


def _q(value: float) -> float:
    """Quantize a reported metric to ``SCORE_PRECISION`` places (byte-stability)."""
    return round(float(value), SCORE_PRECISION)

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


def assert_ingest_accounting(
    ingest_report: IngestReport, corpus: FrozenCorpus
) -> None:
    """The §9.5 ingest-accounting gate, shared by EVERY caller of ingest().

    THREE outcomes (identical to run_bench._run_one_adapter so no caller is a
    weaker path):
      • doc_count > corpus -> OVER-COUNT: a bug. ALWAYS abort (never weakened).
      • doc_count + skipped == corpus -> FULLY ACCOUNTED. PROCEED. A shortfall
        is a DISCLOSED partial ingest (scored on what it ingested, penalized on
        gold queries whose doc it skipped). Anti-gaming: skipped_doc_ids MUST be
        real corpus members, so an adapter cannot pad skipped_doc_count with
        fabricated/non-corpus ids to clear the gate. (IngestReport.__post_init__
        already enforces len(skipped_doc_ids) == skipped_doc_count.)
      • doc_count + skipped < corpus -> SILENT UNDERCOUNT: docs unaccounted for.
        Abort.
    """
    corpus_ids = set(corpus.doc_ids())
    corpus_size = len(corpus_ids)
    if ingest_report.doc_count > corpus_size:
        raise RuntimeError(
            f"ingest doc_count={ingest_report.doc_count} EXCEEDS "
            f"len(corpus.doc_ids())={corpus_size} — over-count, aborting "
            "this adapter (§9.5)."
        )
    # Count↔id-list agreement (the primary guard is IngestReport.__post_init__).
    # A path that bypassed the constructor (e.g. object.__setattr__ raising
    # skipped_doc_count above len(skipped_doc_ids)) would let the accounting
    # arithmetic below treat phantom skips as accounted, hiding one unaccounted
    # doc behind the inflated count. Re-assert here before any arithmetic.
    if ingest_report.skipped_doc_count != len(ingest_report.skipped_doc_ids):
        raise RuntimeError(
            f"skipped_doc_count={ingest_report.skipped_doc_count} disagrees with "
            f"len(skipped_doc_ids)={len(ingest_report.skipped_doc_ids)} — "
            "bypassed constructor; aborting"
        )
    if not set(ingest_report.skipped_doc_ids).issubset(corpus_ids):
        raise RuntimeError(
            "ingest skipped_doc_ids contains non-corpus ids — an adapter cannot "
            "pad skipped_doc_count with fabricated ids to clear the §9.5 gate; "
            "aborting this adapter."
        )
    # Defensive secondary check (the primary guard is IngestReport.__post_init__):
    # the subset test above passes for duplicated ids, but the accounting below
    # uses the raw skipped_doc_count, which a duplicate inflates. Catch any path
    # that bypassed the dataclass constructor (e.g. dataclasses.replace).
    if len(set(ingest_report.skipped_doc_ids)) != len(ingest_report.skipped_doc_ids):
        raise RuntimeError(
            "ingest skipped_doc_ids contains duplicate ids — a repeated corpus id "
            "inflates skipped_doc_count and hides a silent undercount; aborting "
            "this adapter (§9.5)."
        )
    accounted = ingest_report.doc_count + ingest_report.skipped_doc_count
    if accounted != corpus_size:
        raise RuntimeError(
            f"ingest accounting mismatch: doc_count={ingest_report.doc_count} + "
            f"skipped_doc_count={ingest_report.skipped_doc_count} = {accounted} "
            f"!= len(corpus.doc_ids())={corpus_size} — silent undercount (docs "
            "unaccounted for), aborting this adapter (§9.5)."
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
    assert_ingest_accounting(ingest_report, corpus)
    records: list[ScoredRecord] = []
    for i, q in enumerate(queries):
        records.append(score_query(adapter, corpus, q, budget, i))
    return records


def assert_corpus_hash_agreement(
    adapter_corpora: dict[str, FrozenCorpus],
) -> str:
    """Assert EVERY adapter ingests the SAME corpus content-hash; abort on drift.

    Spec §7.1 / §9.5(a): "All five ingest the same content-hashed snapshot. The
    hash is asserted equal across adapters at run start; a mismatch aborts the
    run." This is the fairness control that kills *"Minni got a cleaner corpus."*

    ``adapter_corpora`` maps each adapter name to the :class:`FrozenCorpus` it is
    about to ingest. The common case hands the SAME corpus object to all adapters,
    but the check is meaningful precisely because a wiring bug could hand a
    DIFFERENT corpus (different bytes -> different ``content_hash``) to one
    adapter. Returns the single agreed hash on success; raises
    :class:`CorpusHashMismatch` (aborting the run) the moment two adapters report
    different ``content_hash`` values.
    """
    if not adapter_corpora:
        raise CorpusHashMismatch(
            "no adapters supplied — cannot assert corpus-hash agreement (§9.5a)"
        )
    hashes = {name: corpus.content_hash for name, corpus in adapter_corpora.items()}
    distinct = set(hashes.values())
    if len(distinct) != 1:
        # Report the per-adapter hashes so the mismatch is diagnosable.
        detail = ", ".join(f"{n}={h}" for n, h in sorted(hashes.items()))
        raise CorpusHashMismatch(
            "corpus content-hash MISMATCH across adapters — every adapter MUST "
            "ingest identical bytes (§7.1/§9.5a); aborting the run.\n  " + detail
        )
    return next(iter(distinct))


def record_to_json(rec: ScoredRecord) -> dict:
    """Serialize a scored record.

    ``context_string`` is ALREADY truncated at record construction (§5.1), so
    this is a straight ``asdict`` — there is no untruncated context anywhere on
    the record to leak.
    """
    return asdict(rec)


# ===========================================================================
# Slice s4 — scoring + aggregation over the gold set.
#
# The s1 loop above takes a plain list of query STRINGS. Layer-1 scoring needs
# the gold LABELS (band + gold doc-ids) per query, so the functions below drive
# the same contract round-trip but key each record to a GoldItem, then aggregate
# into a per-adapter scorecard (per-band + overall quality metrics, refusal
# rates, token cost, latency percentiles). NO LLM anywhere — pure arithmetic
# over ranked doc-ids and harness token counts.
# ===========================================================================
@dataclass
class GoldScoredRecord:
    """One gold query's scored record: the harness record + its band/gold.

    ``band`` and ``gold_doc_ids`` come from the GoldItem (ground truth);
    everything else is harness-measured. The per-query metric math reads exactly
    these fields, so it is hand-verifiable.
    """

    query_id: str
    band: str
    gold_doc_ids: list[str]
    adapter: str
    ranked_doc_ids: list[str]
    refused: bool
    harness_tokens: int
    wall_clock_ms: float

    @property
    def is_negative(self) -> bool:
        return self.band == BAND_NEGATIVE


def score_gold_query(
    adapter: MemoryAdapter,
    corpus: FrozenCorpus,
    item: GoldItem,
    budget: TokenBudget,
) -> GoldScoredRecord:
    """Run one gold query through the contract and tag it with its gold labels."""
    base = score_query(adapter, corpus, item.question, budget, query_index=-1)
    return GoldScoredRecord(
        query_id=item.id,
        band=item.band,
        gold_doc_ids=list(item.gold_doc_ids),
        adapter=adapter.name,
        ranked_doc_ids=list(base.doc_ids),
        refused=base.refused,
        harness_tokens=base.harness_tokens,
        wall_clock_ms=base.wall_clock_ms,
    )


def run_layer1_gold(
    adapter: MemoryAdapter,
    corpus: FrozenCorpus,
    gold_items: list[GoldItem],
    budget: TokenBudget,
) -> list[GoldScoredRecord]:
    """Ingest the frozen corpus, then score EVERY gold query (s4 loop).

    Same fairness/safety machinery as ``run_layer1`` (the §9.5 ingest-accounting
    gate, per-query budget abort), but produces gold-keyed records the aggregator can
    score against ground truth. The caller tears the adapter down.
    """
    ingest_report = adapter.ingest(corpus)
    assert_ingest_accounting(ingest_report, corpus)
    return [score_gold_query(adapter, corpus, item, budget) for item in gold_items]


def _quality_block(
    records: list[GoldScoredRecord], k: int
) -> dict[str, float]:
    """Mean recall@k/precision@k/nDCG@k/MRR over NON-NEGATIVE records (§6.1–6.4).

    Negatives carry G(q)=∅ and are excluded from quality math (their behavior is
    scored once, in the refusal block). Empty population -> 0.0 means.
    """
    positives = [r for r in records if not r.is_negative]
    recalls, precisions, ndcgs, rrs = [], [], [], []
    for r in positives:
        gold = set(r.gold_doc_ids)
        # A refusal on a positive yields empty ranked_doc_ids, so recall/precision/
        # nDCG/MRR are all 0 for it — scored as a miss, never as free credit (§9.2).
        recalls.append(metrics.recall_at_k(r.ranked_doc_ids, gold, k))
        precisions.append(metrics.precision_at_k(r.ranked_doc_ids, gold, k))
        ndcgs.append(metrics.ndcg_at_k(r.ranked_doc_ids, gold, k))
        rrs.append(metrics.reciprocal_rank(r.ranked_doc_ids, gold, k))
    return {
        "recall_at_k": _q(metrics.mean(recalls)),
        "precision_at_k": _q(metrics.mean(precisions)),
        "ndcg_at_k": _q(metrics.mean(ndcgs)),
        "mrr": _q(metrics.mean(rrs)),
        "n_positive": len(positives),
    }


def scorecard(
    adapter_name: str,
    records: list[GoldScoredRecord],
    k: int,
) -> dict:
    """Aggregate a per-adapter scorecard (§3.2, §6).

    Emits per-band AND overall quality metrics, correct/false refusal rates,
    token_cost mean, and latency p50/p95. Quality metrics are over non-negative
    queries (§6.1–6.4); refusal rates split positives vs negatives (§6.5);
    token_cost is the mean over ALL scored queries (§6.6).

    ``wall_clock_ms``-derived latency lives under the ``latency_ms`` key whose
    field names (``p50``/``p95``) are part of the NON-byte-checked surface — the
    determinism gate strips ``wall_clock_ms`` from the per-record list; latency
    percentiles are reported but a reviewer must verify they are not folded into
    the byte-identity comparison. They are deliberately rounded for readability
    only, NOT for the gate.
    """
    overall = _quality_block(records, k)

    negatives = [
        (r.refused, r.ranked_doc_ids) for r in records if r.is_negative
    ]
    positives = [
        (r.refused, r.ranked_doc_ids) for r in records if not r.is_negative
    ]
    overall["correct_refusal_rate"] = _q(metrics.correct_refusal_rate(negatives))
    overall["false_refusal_rate"] = _q(metrics.false_refusal_rate(positives))
    overall["n_negative"] = len(negatives)

    # token_cost population = ALL scored queries (positives AND negatives), since
    # the harness tokenizes context_string for every query (§6.6).
    token_costs = [float(r.harness_tokens) for r in records]
    overall["token_cost"] = _q(metrics.mean(token_costs))
    overall["n_scored"] = len(records)

    # Per-band quality + refusal (negatives band reports refusal, no quality).
    per_band: dict[str, dict] = {}
    for band in BANDS:
        band_records = [r for r in records if r.band == band]
        if not band_records:
            continue
        block: dict = {"n": len(band_records)}
        if band == BAND_NEGATIVE:
            block["correct_refusal_rate"] = _q(
                metrics.correct_refusal_rate(
                    [(r.refused, r.ranked_doc_ids) for r in band_records]
                )
            )
        else:
            qb = _quality_block(band_records, k)
            # ``n_positive`` is redundant per-band — the block already carries
            # ``n`` (== n_positive for a positive band), so drop it instead of
            # re-listing the quality keys by hand (NIT a, dedupe).
            qb.pop("n_positive", None)
            block.update(qb)
            block["false_refusal_rate"] = _q(
                metrics.false_refusal_rate(
                    [(r.refused, r.ranked_doc_ids) for r in band_records]
                )
            )
        block["token_cost"] = _q(
            metrics.mean([float(r.harness_tokens) for r in band_records])
        )
        per_band[band] = block

    # Latency — REPORTED, never byte-checked (§3.2). Field names live under
    # latency_ms; the per-record wall_clock_ms (the determinism-excluded field)
    # is the source. These floats are machine-dependent and must NOT enter the
    # byte-identity gate.
    latencies = [r.wall_clock_ms for r in records]
    latency_ms = {
        "p50": round(metrics.percentile(latencies, 50.0), 4),
        "p95": round(metrics.percentile(latencies, 95.0), 4),
    }

    return {
        "adapter": adapter_name,
        "k": k,
        "overall": overall,
        "per_band": per_band,
        "latency_ms": latency_ms,
    }


def build_scorecards(
    adapters_records: dict[str, list[GoldScoredRecord]],
    k: int,
) -> dict:
    """Build the full multi-adapter scorecard artifact (sorted-key canonical)."""
    return {
        "k": k,
        "adapters": {
            name: scorecard(name, records, k)
            for name, records in sorted(adapters_records.items())
        },
    }


def canonical_json(scorecards: dict) -> str:
    """Canonical JSON for the byte-identity determinism gate (sorted keys).

    The determinism comparison runs ``strip_excluded_fields`` over the parsed
    structure first (§3.2); the per-adapter scorecards above carry no
    wall_clock_ms per-record, but the latency_ms block is reported. The gate in
    test_determinism strips the EXCLUDED fields and diffs the rest.
    """
    return json.dumps(scorecards, sort_keys=True, ensure_ascii=False, indent=2)


def render_table(scorecards: dict) -> str:
    """Human-readable per-adapter × metric table (overall row per adapter)."""
    cols = [
        ("recall_at_k", "recall@k"),
        ("precision_at_k", "prec@k"),
        ("ndcg_at_k", "ndcg@k"),
        ("mrr", "mrr"),
        ("correct_refusal_rate", "corr_ref"),
        ("false_refusal_rate", "false_ref"),
        ("token_cost", "tok_cost"),
    ]
    header = f"{'adapter':<16} " + " ".join(f"{label:>9}" for _, label in cols)
    header += f" {'p50ms':>8} {'p95ms':>8}"
    lines = [header, "-" * len(header)]
    for name, card in sorted(scorecards["adapters"].items()):
        ov = card["overall"]
        row = f"{name:<16} "
        row += " ".join(f"{ov.get(key, 0.0):>9.4f}" for key, _ in cols)
        lat = card["latency_ms"]
        row += f" {lat['p50']:>8.2f} {lat['p95']:>8.2f}"
        lines.append(row)
    return "\n".join(lines)

"""Metric computation and gate policy for recall evaluation."""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from .retrievers import SearcherProtocol

logger = logging.getLogger("sovereign.eval")

KNOWN_RETRIEVE_KWARGS = {
    "limit", "agent_id", "update_access", "budget_tokens",
    "depth", "include_superseded", "include_rejected", "include_drafts",
    "include_expired",
    "expand", "use_hyde",
}

_warned_unknown_kwargs: set = set()


def _recall_at_k(
    expected_ids: List[int],
    result_doc_ids: List[int],
    k: int,
) -> float:
    """Recall@K: fraction of expected doc_ids found in the top-K results."""
    if not expected_ids:
        return 0.0
    top_k = set(result_doc_ids[:k])
    hits = sum(1 for eid in expected_ids if eid in top_k)
    return hits / len(expected_ids)


def _mrr(
    expected_ids: List[int],
    result_doc_ids: List[int],
) -> float:
    """Mean Reciprocal Rank: 1/rank of the first relevant result, or 0."""
    expected_set = set(expected_ids)
    for rank, did in enumerate(result_doc_ids, start=1):
        if did in expected_set:
            return 1.0 / rank
    return 0.0


def _normalise_relevance(expected: Any) -> Dict[int, float]:
    """
    Convert supported relevance specs into {doc_id: grade}.

    Legacy queries use expected_doc_ids=[...], which are binary relevance.
    Newer adversarial queries may use {"doc_id": grade} maps or
    [{"doc_id": 1, "grade": 3}] lists for graded nDCG.
    """
    if isinstance(expected, dict):
        relevance = {}
        for key, value in expected.items():
            try:
                relevance[int(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return relevance

    if isinstance(expected, list):
        relevance = {}
        for item in expected:
            if isinstance(item, dict):
                doc_id = item.get("doc_id")
                grade = item.get("grade", item.get("relevance", 1.0))
                try:
                    relevance[int(doc_id)] = float(grade)
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    relevance[int(item)] = 1.0
                except (TypeError, ValueError):
                    continue
        return relevance

    return {}


def _dcg(grades: List[float]) -> float:
    """Discounted cumulative gain with gains as 2^grade - 1."""
    import math

    total = 0.0
    for idx, grade in enumerate(grades, start=1):
        gain = (2.0 ** float(grade)) - 1.0
        total += gain / math.log2(idx + 1)
    return total


def _ndcg_at_k(
    expected_relevance: Any,
    result_doc_ids: List[int],
    k: int,
) -> float:
    """Normalized Discounted Cumulative Gain at K."""
    relevance = _normalise_relevance(expected_relevance)
    if not relevance:
        return 0.0

    result_grades = [relevance.get(int(doc_id), 0.0) for doc_id in result_doc_ids[:k]]
    ideal_grades = sorted(relevance.values(), reverse=True)[:k]
    ideal = _dcg(ideal_grades)
    if ideal == 0.0:
        return 0.0
    return _dcg(result_grades) / ideal


def _token_budget_recall_at_k(
    expected_ids: List[int],
    result_doc_ids: List[int],
    result_token_counts: List[int],
    k: int,
    budget_tokens: int,
) -> float:
    """
    Recall@K constrained by cumulative result tokens.

    The first result is always eligible, matching the retrieval packer's
    first-result guarantee for tiny budgets.
    """
    if not expected_ids:
        return 0.0
    expected_set = set(expected_ids)
    seen_ids = []
    total_tokens = 0

    for idx, doc_id in enumerate(result_doc_ids[:k]):
        tokens = int(result_token_counts[idx]) if idx < len(result_token_counts) else 0
        if idx > 0 and budget_tokens > 0 and total_tokens + tokens > budget_tokens:
            break
        seen_ids.append(int(doc_id))
        total_tokens += max(tokens, 0)

    hits = sum(1 for doc_id in expected_set if doc_id in seen_ids)
    return hits / len(expected_set)


def _calibration_error(results: List[Dict[str, Any]], expected_ids: List[int]) -> Optional[float]:
    """Mean absolute difference between confidence and actual relevance."""
    expected_set = set(expected_ids)
    errors = []
    for r in results:
        conf = r.get("confidence")
        if conf is None:
            continue
        did = r.get("doc_id")
        actual = 1.0 if did in expected_set else 0.0
        errors.append(abs(float(conf) - actual))
    if not errors:
        return None
    return sum(errors) / len(errors)


def _safe_search(
    searcher: SearcherProtocol,
    query: str,
    config_name: str,
    config_kwargs: Dict[str, Any],
    limit: int = 10,
    *, strict: bool = False,
) -> Tuple[List[Dict[str, Any]], float]:
    """
    Run search() with the given config, stripping unrecognised kwargs with
    a warning (logged once per config/kwarg pair). Strict quality mode
    rejects unsupported options before calling the retriever.
    """
    safe_kwargs: Dict[str, Any] = {"limit": limit, "update_access": False}
    for k, v in config_kwargs.items():
        if k in KNOWN_RETRIEVE_KWARGS:
            safe_kwargs[k] = v
        else:
            if strict:
                raise RuntimeError("quality evaluation contains unsupported retrieval options")
            key = (config_name, k)
            if key not in _warned_unknown_kwargs:
                logger.warning(
                    "config %r requested unknown kwarg %r, ignoring", config_name, k
                )
                _warned_unknown_kwargs.add(key)

    t0 = time.perf_counter()
    try:
        results = searcher.search(query, **safe_kwargs)
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise RuntimeError("quality evaluation retrieval failed") from exc
        logger.error("search() raised for config %r query %r: %s", config_name, query, exc)
        results = []
    elapsed = time.perf_counter() - t0
    return results, elapsed


def _extract_doc_ids(results: List[Dict[str, Any]], *, strict: bool = False) -> List[int]:
    """Extract doc_ids from a list of search results."""
    if strict:
        if not isinstance(results, list):
            raise RuntimeError("quality retrieval results must be a ranked list")
        ids = []
        for row in results:
            if not isinstance(row, dict):
                raise RuntimeError("quality retrieval result must be an object")
            did = row.get("doc_id")
            if did is None:
                provenance = row.get("provenance")
                did = provenance.get("doc_id") if isinstance(provenance, dict) else None
            if isinstance(did, bool) or not isinstance(did, int):
                raise RuntimeError("quality retrieval requires an exact integer ID at every rank")
            ids.append(did)
        return ids
    ids = []
    for r in results:
        did = r.get("doc_id") or r.get("provenance", {}) and r.get("provenance", {}).get("doc_id")
        if did is not None:
            ids.append(int(did))
    return ids


def _extract_token_counts(results: List[Dict[str, Any]]) -> List[int]:
    """Extract or estimate token counts for token-budget-normalized metrics."""
    counts = []
    for r in results:
        raw = r.get("token_count")
        if raw is None:
            text = r.get("text") or r.get("snippet") or r.get("chunk_text") or ""
            raw = max(1, len(str(text)) // 4) if text else 0
        try:
            counts.append(int(raw))
        except (TypeError, ValueError):
            counts.append(0)
    return counts


def run_eval(
    searcher: SearcherProtocol,
    queries: List[Dict[str, Any]],
    config_name: str,
    config_kwargs: Dict[str, Any],
    ks: Tuple[int, ...] = (1, 3, 5, 10),
    *, strict_search: bool = False,
) -> Dict[str, Any]:
    """Run evaluation over all queries for a single config."""
    per_query = []
    aggregate_r_at_k = {k: [] for k in ks}
    aggregate_ndcg_at_k = {k: [] for k in ks}
    aggregate_token_budget_r_at_k = {k: [] for k in ks}
    aggregate_mrr = []
    aggregate_cal_err = []
    total_latency = 0.0

    for q in queries:
        query_text = q["query"]
        expected_ids = [int(i) for i in q.get("expected_doc_ids", [])]
        expected_relevance = q.get("expected_relevance") or q.get("relevance") or expected_ids
        notes = q.get("notes", "")
        budget_tokens = int(q.get("budget_tokens", 4096))

        results, latency = _safe_search(
            searcher, query_text, config_name, config_kwargs, strict=strict_search
        )
        result_ids = _extract_doc_ids(results, strict=strict_search)
        token_counts = _extract_token_counts(results)

        r_at_k = {k: _recall_at_k(expected_ids, result_ids, k) for k in ks}
        ndcg_at_k = {k: _ndcg_at_k(expected_relevance, result_ids, k) for k in ks}
        token_budget_r_at_k = {
            k: _token_budget_recall_at_k(
                expected_ids,
                result_ids,
                token_counts,
                k,
                budget_tokens,
            )
            for k in ks
        }
        mrr = _mrr(expected_ids, result_ids)
        cal_err = _calibration_error(results, expected_ids)

        for k in ks:
            aggregate_r_at_k[k].append(r_at_k[k])
            aggregate_ndcg_at_k[k].append(ndcg_at_k[k])
            aggregate_token_budget_r_at_k[k].append(token_budget_r_at_k[k])
        aggregate_mrr.append(mrr)
        if cal_err is not None:
            aggregate_cal_err.append(cal_err)
        total_latency += latency

        per_query.append({
            "query": query_text,
            "expected_doc_ids": expected_ids,
            "notes": notes,
            "result_doc_ids": result_ids[:10],
            "recall_at_k": r_at_k,
            "ndcg_at_k": {k: round(ndcg_at_k[k], 4) for k in ks},
            "token_budget_recall_at_k": {
                k: round(token_budget_r_at_k[k], 4) for k in ks
            },
            "budget_tokens": budget_tokens,
            "mrr": round(mrr, 4),
            "calibration_error": round(cal_err, 4) if cal_err is not None else None,
            "latency_s": round(latency, 4),
        })

    n = len(queries) or 1
    summary = {
        "config": config_name,
        "n_queries": len(queries),
        "total_latency_s": round(total_latency, 3),
        "mean_latency_s": round(total_latency / n, 4),
        "recall_at_k": {
            k: round(sum(aggregate_r_at_k[k]) / n, 4) for k in ks
        },
        "ndcg_at_k": {
            k: round(sum(aggregate_ndcg_at_k[k]) / n, 4) for k in ks
        },
        "token_budget_recall_at_k": {
            k: round(sum(aggregate_token_budget_r_at_k[k]) / n, 4) for k in ks
        },
        "mrr": round(sum(aggregate_mrr) / n, 4),
        "mean_calibration_error": (
            round(sum(aggregate_cal_err) / len(aggregate_cal_err), 4)
            if aggregate_cal_err else None
        ),
    }

    return {"summary": summary, "per_query": per_query}


def _metric_value(per_query: Dict[str, Any], metric: str, k: int) -> float:
    values = per_query.get(metric, {})
    if isinstance(values, dict):
        return float(values.get(k, values.get(str(k), 0.0)) or 0.0)
    return 0.0


QUALITY_GATE_DEFAULT_MIN_IMPROVEMENT = 0.05

_FLOAT_TOLERANCE = 1e-9


def _query_class(item: Dict[str, Any]) -> str:
    """Query-class label for the no-regression check (``notes`` field)."""
    return str(item.get("notes", "uncategorized") or "uncategorized")


def _judgment_key(item: Dict[str, Any]) -> Optional[List[int]]:
    """
    Exact integer judgment identity, or None when malformed.

    Only true integers are supported IDs: floats (even integral ones),
    strings, and booleans are malformed evidence and are never coerced,
    so 1.1/1.9 can never collapse onto ID 1.
    """
    raw = item.get("expected_doc_ids")
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    ids = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int):
            return None
        ids.append(entry)
    return sorted(ids)


def _identity_problems(report: Dict[str, Any]) -> List[str]:
    """Duplicate or empty query identities within one report."""
    seen: set = set()
    problems = []
    for item in report.get("per_query", []):
        query = item.get("query")
        if not isinstance(query, str) or not query.strip():
            problems.append("<empty query identity>")
        elif query in seen:
            problems.append(query)
        else:
            seen.add(query)
    return problems


def _strict_metric_value(
    item: Dict[str, Any],
    metric: str,
    k: int,
) -> Tuple[Optional[float], str]:
    """
    Extract a validated bounded-metric value, or (None, error).

    Missing metrics default to nothing: unlike the descriptive
    ``_metric_value`` helper, the gate never treats an absent, non-finite,
    or out-of-range score as zero evidence.
    """
    values = item.get(metric)
    if not isinstance(values, dict):
        return None, f"metric {metric!r} missing"
    if k in values:
        raw = values[k]
    elif str(k) in values:
        raw = values[str(k)]
    else:
        return None, f"{metric}@{k} missing"
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or not 0.0 <= raw <= 1.0
    ):
        return None, f"{metric}@{k} invalid ({raw!r})"
    return float(raw), ""


def evaluate_quality_gate(
    reports: Dict[str, Dict[str, Any]],
    baseline: str = "baseline",
    candidate: Optional[str] = None,
    metric: str = "recall_at_k",
    k: int = 5,
    min_relative_improvement: float = QUALITY_GATE_DEFAULT_MIN_IMPROVEMENT,
) -> Dict[str, Any]:
    """
    Baseline-versus-candidate acceptance gate matching the normative rule:
    at least ``min_relative_improvement`` *relative* gain on ``metric@k``
    (candidate >= baseline * (1 + min_relative_improvement)) with no
    regression on any query class.

    This is separate from :func:`evaluate_gate` (minnid-vs-ripgrep per-query
    loss rate), which is preserved unchanged for backward compatibility.

    Comparability is validated *before* any numerical scoring, and
    incomplete evidence never authorises defaults:
    - invalid parameters (anything but the normative ``recall_at_k``
      metric, non-positive ``k``, negative or non-finite improvement
      threshold) -> ``ok=False``. Graded metrics such as ``ndcg_at_k``
      are rejected: changed grades can move scores without changing IDs,
      and this gate does not compare grade judgments.
    - missing baseline/candidate reports -> ``ok=False`` with a reason.
    - duplicate or empty query identities within a report -> ``ok=False``.
    - different query sets -> ``ok=False`` (incompatible, not comparable).
    - same query with different judgments (``expected_doc_ids``) or class
      metadata (``notes``) -> ``ok=False``; changed expectations are not
      scored against each other.
    - absent, non-finite, or out-of-range ``metric@k`` on either side ->
      ``ok=False``; a missing score is never defaulted to zero.
    - queries without judgments (empty ``expected_doc_ids``) are excluded
      from means and listed under ``unevaluable_queries``; a whole class
      without judged queries fails under ``unevaluable_classes`` instead
      of disappearing from no-regression acceptance. Excluded probes are
      reported as unevaluable, never as passed.
    - zero comparable judged queries -> ``ok=False``.
    - zero baseline mean: relative gain is undefined, so any strict
      improvement on valid finite evidence passes the improvement leg
      (both-zero fails: no evidence).
    - ties count as no regression; float noise below 1e-9 is ignored.
    """
    metric_label = f"{metric}@{k}"

    def _fail(reason: str, **extra: Any) -> Dict[str, Any]:
        report = {
            "ok": False,
            "reason": reason,
            "gate": "quality",
            "baseline": baseline,
            "candidate": candidate,
            "metric": metric_label,
            "min_relative_improvement": min_relative_improvement,
            "baseline_score": None,
            "candidate_score": None,
            "absolute_improvement": None,
            "relative_improvement": None,
            "improvement_ok": False,
            "comparable_queries": 0,
            "unevaluable_queries": [],
            "classes": [],
            "unevaluable_classes": [],
            "regressions": [],
            "label_mismatches": [],
            "incomparable_queries": [],
            "limitations": [],
        }
        report.update(extra)
        return report

    if metric != "recall_at_k":
        return _fail(
            f"unsupported metric={metric!r}: quality gate compares the "
            "normative recall_at_k only"
        )
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        return _fail(f"invalid k={k!r}: must be a positive integer")
    if (
        isinstance(min_relative_improvement, bool)
        or not isinstance(min_relative_improvement, (int, float))
        or not math.isfinite(min_relative_improvement)
        or min_relative_improvement < 0
    ):
        return _fail(
            "invalid min_relative_improvement="
            f"{min_relative_improvement!r}: must be a finite number >= 0"
        )

    if candidate is None:
        others = [name for name in reports if name != baseline]
        candidate = others[0] if len(reports) == 2 and len(others) == 1 else None

    baseline_report = reports.get(baseline) if baseline else None
    candidate_report = reports.get(candidate) if candidate else None
    if not baseline_report or not candidate_report:
        return _fail(
            f"missing reports for baseline {baseline!r} and/or "
            f"candidate {candidate!r}"
        )

    baseline_id_problems = _identity_problems(baseline_report)
    candidate_id_problems = _identity_problems(candidate_report)
    if baseline_id_problems or candidate_id_problems:
        return _fail(
            "duplicate or empty query identities: "
            f"{len(baseline_id_problems)} in baseline, "
            f"{len(candidate_id_problems)} in candidate",
            incomparable_queries=sorted(set(
                baseline_id_problems + candidate_id_problems
            ))[:10],
        )

    baseline_by_query = {
        item.get("query"): item for item in baseline_report.get("per_query", [])
    }
    candidate_by_query = {
        item.get("query"): item for item in candidate_report.get("per_query", [])
    }
    only_in_baseline = sorted(set(baseline_by_query) - set(candidate_by_query))
    only_in_candidate = sorted(set(candidate_by_query) - set(baseline_by_query))
    if only_in_baseline or only_in_candidate:
        return _fail(
            "incompatible query sets: "
            f"{len(only_in_baseline)} only in baseline, "
            f"{len(only_in_candidate)} only in candidate",
            only_in_baseline=only_in_baseline[:10],
            only_in_candidate=only_in_candidate[:10],
            limitations=[
                "reports must cover the same query set before quality "
                "evidence is comparable.",
            ],
        )

    comparable: List[Tuple[str, float, float, str]] = []
    unevaluable_queries = []
    unevaluable_classes: List[str] = []
    label_mismatches = []
    incomparable = []
    for query, base_item in baseline_by_query.items():
        cand_item = candidate_by_query[query]
        base_judgment = _judgment_key(base_item)
        cand_judgment = _judgment_key(cand_item)
        if base_judgment is None or cand_judgment is None:
            incomparable.append({
                "query": query,
                "issue": "malformed expected_doc_ids",
            })
            continue
        if base_judgment != cand_judgment:
            incomparable.append({
                "query": query,
                "issue": "changed judgments",
                "baseline_expected_doc_ids": base_judgment,
                "candidate_expected_doc_ids": cand_judgment,
            })
            continue
        if any(not isinstance(item.get("notes"), str) or not item["notes"].strip()
               for item in (base_item, cand_item)):
            incomparable.append({"query": query, "issue": "missing explicit query class"})
            continue
        base_class = _query_class(base_item)
        cand_class = _query_class(cand_item)
        if cand_class != base_class:
            incomparable.append({
                "query": query,
                "issue": "changed class metadata",
                "baseline_class": base_class,
                "candidate_class": cand_class,
            })
            label_mismatches.append({
                "query": query,
                "baseline_class": base_class,
                "candidate_class": cand_class,
            })
            continue
        if not base_judgment:
            unevaluable_queries.append(query)
            unevaluable_classes.append(base_class)
            continue
        base_score, base_error = _strict_metric_value(base_item, metric, k)
        cand_score, cand_error = _strict_metric_value(cand_item, metric, k)
        if base_error or cand_error:
            incomparable.append({
                "query": query,
                "issue": "; ".join(
                    f"{side}: {error}"
                    for side, error in (
                        ("baseline", base_error),
                        ("candidate", cand_error),
                    )
                    if error
                ),
            })
            continue
        comparable.append((query, base_score, cand_score, base_class))

    if incomparable:
        return _fail(
            f"incomparable evidence for {len(incomparable)} querie(s): "
            "judgments, class metadata, and metric scores must match in "
            "shape before numerical comparison",
            incomparable_queries=incomparable[:10],
            unevaluable_queries=sorted(unevaluable_queries),
            unevaluable_classes=sorted(set(unevaluable_classes)),
            label_mismatches=label_mismatches,
            limitations=[
                "incomplete evidence authorises no defaults: fix judgments, "
                "labels, or metric coverage and re-run.",
            ],
        )

    if not comparable:
        return _fail(
            "no comparable judged queries (all excluded as unevaluable)",
            unevaluable_queries=sorted(unevaluable_queries),
            unevaluable_classes=sorted(set(unevaluable_classes)),
            limitations=[
                "recall is undefined without relevance judgments; judge or "
                "remove unevaluable queries before gating.",
            ],
        )

    baseline_mean = sum(b for _, b, _, _ in comparable) / len(comparable)
    candidate_mean = sum(c for _, _, c, _ in comparable) / len(comparable)
    absolute = candidate_mean - baseline_mean
    if baseline_mean > 0:
        relative: Optional[float] = absolute / baseline_mean
        improvement_ok = candidate_mean + _FLOAT_TOLERANCE >= baseline_mean * (
            1 + min_relative_improvement
        )
    else:
        relative = None
        improvement_ok = candidate_mean > 0

    by_class: Dict[str, List[Tuple[float, float]]] = {}
    for _, b, c, cls in comparable:
        by_class.setdefault(cls, []).append((b, c))
    class_rows = []
    regressions = []
    for cls in sorted(by_class):
        pairs = by_class[cls]
        b_mean = sum(b for b, _ in pairs) / len(pairs)
        c_mean = sum(c for _, c in pairs) / len(pairs)
        regressed = c_mean < b_mean - _FLOAT_TOLERANCE
        if regressed:
            regressions.append(cls)
        class_rows.append({
            "class": cls,
            "n": len(pairs),
            "baseline": round(b_mean, 4),
            "candidate": round(c_mean, 4),
            "delta": round(c_mean - b_mean, 4),
            "regression": regressed,
        })

    missing_classes = sorted(set(unevaluable_classes) - set(by_class))
    if missing_classes:
        return _fail(
            "missing evidence for query class(es): "
            f"{', '.join(missing_classes)} — whole classes without judged "
            "queries cannot pass no-class-regression acceptance",
            baseline_score=round(baseline_mean, 4),
            candidate_score=round(candidate_mean, 4),
            comparable_queries=len(comparable),
            unevaluable_queries=sorted(unevaluable_queries),
            classes=class_rows,
            unevaluable_classes=missing_classes,
            label_mismatches=label_mismatches,
            limitations=[
                "excluded probes are reported as unevaluable, never as "
                "passed; negative/private probes need an explicit "
                "non-recall category contract before they can gate.",
                "judge or remove unevaluable queries before gating.",
            ],
        )

    ok = bool(improvement_ok) and not regressions
    if ok:
        if relative is None:
            reason = (
                f"candidate improves over zero baseline "
                f"({metric_label} 0.0000 -> {candidate_mean:.4f}) "
                f"with no regression on {len(class_rows)} query class(es)"
            )
        else:
            reason = (
                f"candidate shows {relative * 100:.1f}% relative {metric_label} "
                f"gain ({baseline_mean:.4f} -> {candidate_mean:.4f}) "
                f"with no regression on {len(class_rows)} query class(es)"
            )
    elif regressions and not improvement_ok:
        reason = (
            f"candidate fails improvement leg ({metric_label} "
            f"{baseline_mean:.4f} -> {candidate_mean:.4f}) and regresses "
            f"on class(es): {', '.join(sorted(regressions))}"
        )
    elif regressions:
        reason = (
            f"candidate regresses on class(es): {', '.join(sorted(regressions))}"
        )
    elif baseline_mean > 0:
        reason = (
            f"candidate gain below +{min_relative_improvement * 100:.0f}% "
            f"({metric_label} {baseline_mean:.4f} -> {candidate_mean:.4f})"
        )
    else:
        reason = (
            f"no measurable improvement ({metric_label} baseline 0.0000, "
            f"candidate {candidate_mean:.4f})"
        )

    limitations = [
        f"descriptive comparison over {len(comparable)} judged queries; "
        "no significance test or confidence interval is computed.",
        f"{len(unevaluable_queries)} unevaluable querie(s) excluded: recall "
        "is undefined without relevance judgments.",
        "mock or placeholder doc_ids exercise harness plumbing only; gate "
        "evidence requires a reviewed seed set, not a synthetic fixture pass.",
        "per-class means over small samples are noisy; ties count as no "
        "regression and float noise below 1e-9 is ignored.",
        "latency, degradation, and answer quality are reported elsewhere "
        "and are not gated here.",
    ]

    return {
        "ok": ok,
        "reason": reason,
        "gate": "quality",
        "baseline": baseline,
        "candidate": candidate,
        "metric": metric_label,
        "min_relative_improvement": min_relative_improvement,
        "baseline_score": round(baseline_mean, 4),
        "candidate_score": round(candidate_mean, 4),
        "absolute_improvement": round(absolute, 4),
        "relative_improvement": round(relative, 4) if relative is not None else None,
        "improvement_ok": bool(improvement_ok),
        "comparable_queries": len(comparable),
        "unevaluable_queries": sorted(unevaluable_queries),
        "classes": class_rows,
        "unevaluable_classes": sorted(
            set(unevaluable_classes) - set(by_class)
        ),
        "regressions": sorted(regressions),
        "label_mismatches": label_mismatches,
        "incomparable_queries": [],
        "limitations": limitations,
    }


def evaluate_gate(
    reports: Dict[str, Dict[str, Any]],
    primary: str = "minnid",
    baseline: str = "ripgrep",
    max_loss_rate: float = 0.20,
    metric: str = "recall_at_k",
    k: int = 5,
) -> Dict[str, Any]:
    """Fail if the primary retriever loses to baseline on too many queries."""
    primary_report = reports.get(primary)
    baseline_report = reports.get(baseline)
    if not primary_report or not baseline_report:
        return {
            "ok": False,
            "reason": f"missing reports for {primary!r} and/or {baseline!r}",
            "loss_rate": 1.0,
            "losses": [],
        }

    baseline_by_query = {
        item.get("query"): item for item in baseline_report.get("per_query", [])
    }
    losses = []
    comparable = 0
    for item in primary_report.get("per_query", []):
        query = item.get("query")
        other = baseline_by_query.get(query)
        if other is None:
            continue
        comparable += 1
        primary_score = _metric_value(item, metric, k)
        baseline_score = _metric_value(other, metric, k)
        if primary_score < baseline_score:
            losses.append({
                "query": query,
                primary: primary_score,
                baseline: baseline_score,
            })

    loss_rate = (len(losses) / comparable) if comparable else 0.0
    return {
        "ok": loss_rate <= max_loss_rate,
        "primary": primary,
        "baseline": baseline,
        "metric": f"{metric}@{k}",
        "max_loss_rate": max_loss_rate,
        "loss_rate": round(loss_rate, 4),
        "comparable_queries": comparable,
        "losses": losses,
    }

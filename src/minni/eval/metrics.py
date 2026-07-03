"""Metric computation and gate policy for recall evaluation."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from .retrievers import SearcherProtocol

logger = logging.getLogger("sovereign.eval")

KNOWN_RETRIEVE_KWARGS = {
    "limit", "agent_id", "update_access", "budget_tokens",
    "depth", "include_superseded", "include_rejected", "include_drafts",
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
) -> Tuple[List[Dict[str, Any]], float]:
    """
    Run search() with the given config, stripping unrecognised kwargs with
    a warning (logged once per config/kwarg pair).
    """
    safe_kwargs: Dict[str, Any] = {"limit": limit, "update_access": False}
    for k, v in config_kwargs.items():
        if k in KNOWN_RETRIEVE_KWARGS:
            safe_kwargs[k] = v
        else:
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
        logger.error("search() raised for config %r query %r: %s", config_name, query, exc)
        results = []
    elapsed = time.perf_counter() - t0
    return results, elapsed


def _extract_doc_ids(results: List[Dict[str, Any]]) -> List[int]:
    """Extract doc_ids from a list of search results."""
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
            searcher, query_text, config_name, config_kwargs
        )
        result_ids = _extract_doc_ids(results)
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

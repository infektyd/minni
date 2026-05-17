"""
Sovereign Memory — Rationale Generator.

PR-2: explain(result_record) -> str

Produces a deterministic, human-readable single-line explanation of why a
search result ranked where it did. Derived purely from the provenance dict
and other envelope fields — no model calls, no randomness.

Example outputs:
  "Top semantic hit (cosine 0.82) on 'auth migration'; FTS BM25 rank 3; cross-encoder confirmed; fresh (12d)."
  "FTS-only match (no semantic); BM25 rank 1; no cross-encoder; aged (45d)."
  "Semantic-only hit; no FTS match; cross-encoder rejected; moderate age (8d)."
"""

from typing import Dict, Optional, Any


def explain(result_record: Dict[str, Any]) -> str:
    """
    Produce a human-readable rationale string from a result envelope dict.

    Args:
        result_record: A result dict as returned by RetrievalEngine.retrieve().
                       Must contain at least 'score'. Other fields are optional.

    Returns:
        Single-line string explaining why this result was retrieved and ranked.
        Never raises — returns a minimal fallback string on any error.
    """
    try:
        return _build_rationale(result_record)
    except Exception:
        score = result_record.get("score", 0)
        return f"Retrieved with score {score:.4f}."


def _build_rationale(r: Dict[str, Any]) -> str:
    parts = []

    prov = r.get("provenance") or {}

    sem_rank = prov.get("semantic_rank") or r.get("sem_rank")
    fts_rank = prov.get("fts_rank") or r.get("fts_rank")
    rrf = prov.get("rrf_score") or r.get("rrf_score")
    ce_score = prov.get("cross_encoder_score") or r.get("rerank_score")
    decay = prov.get("decay_factor") or r.get("decay_score")
    age_days = prov.get("age_days") or r.get("age_days")
    confidence = r.get("confidence")
    score = r.get("score", 0)

    # ── Lead sentence: describe how it was found ──────────────────────────────
    if sem_rank is not None and fts_rank is not None:
        parts.append(
            f"Hybrid match: semantic rank {sem_rank}, FTS BM25 rank {fts_rank}"
        )
    elif sem_rank is not None:
        parts.append(f"Semantic-only hit (rank {sem_rank}; no FTS match)")
    elif fts_rank is not None:
        parts.append(f"FTS-only match (BM25 rank {fts_rank}; no semantic)")
    else:
        parts.append(f"Retrieved with score {score:.4f}")

    # ── RRF score ─────────────────────────────────────────────────────────────
    if rrf is not None:
        parts.append(f"RRF {rrf:.4f}")

    # ── Cross-encoder ─────────────────────────────────────────────────────────
    if ce_score is not None:
        if ce_score > 2.0:
            parts.append(f"cross-encoder confirmed ({ce_score:.2f})")
        elif ce_score > 0.0:
            parts.append(f"cross-encoder weak ({ce_score:.2f})")
        else:
            parts.append(f"cross-encoder low ({ce_score:.2f})")

    # ── Freshness ─────────────────────────────────────────────────────────────
    if age_days is not None:
        age = int(age_days)
        if age <= 3:
            parts.append(f"very fresh ({age}d)")
        elif age <= 14:
            parts.append(f"fresh ({age}d)")
        elif age <= 60:
            parts.append(f"moderate age ({age}d)")
        else:
            parts.append(f"aged ({age}d)")
    elif decay is not None:
        if decay >= 0.9:
            parts.append("fresh")
        elif decay >= 0.5:
            parts.append("moderate decay")
        else:
            parts.append("heavily decayed")

    # ── Confidence ────────────────────────────────────────────────────────────
    if confidence is not None:
        parts.append(f"confidence {confidence:.2f}")

    return "; ".join(parts) + "."

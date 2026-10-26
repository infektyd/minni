"""Durability predicate, K reservation calibration, noise-free reference, and ρ calibration (Rev 5 §5.2, §5.4)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import math
import re
import statistics
from typing import Any

from minni.afm_passes.harness_envelopes import strip_harness_blocks
from minni.durability import (
    CATEGORY_SYNONYMS,
    CONTENT_TEST_PREFIXES,
    DURABLE_CATEGORIES,
    classify_durability,
    durability_of,
    matches_content_test,
    normalize_category,
)

__all__ = [
    "CATEGORY_SYNONYMS",
    "CONTENT_TEST_PREFIXES",
    "DURABLE_CATEGORIES",
    "calibrate_k",
    "calibrate_rho",
    "classify_durability",
    "durability_of",
    "evaluate_b7_stored_chatter",
    "evaluate_content_test_precision",
    "matches_content_test",
    "normalize_category",
    "run_noise_free_reference",
    "score_ups_gate",
]

_HEADING_LINE_RE = re.compile(r"(?m)^#{1,6}\s+(.+)$")
_TOKEN_RE = re.compile(r"[a-z0-9_]{2,}")

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "are",
        "was",
        "were",
        "have",
        "has",
        "had",
        "not",
        "but",
        "what",
        "when",
        "where",
        "which",
        "who",
        "how",
        "why",
        "can",
        "will",
        "should",
        "would",
        "could",
        "into",
        "about",
        "over",
        "under",
        "then",
        "than",
        "also",
        "only",
    }
)


def evaluate_content_test_precision(sample_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate precision of the content test on a labelled sample of ``general`` rows (§5.2 item 3)."""
    tp = 0
    fp = 0
    fn = 0
    tn = 0
    for row in sample_rows:
        content = str(row.get("content") or "")
        expected = bool(row.get("is_durable"))
        predicted = matches_content_test(content)
        if predicted and expected:
            tp += 1
        elif predicted and not expected:
            fp += 1
        elif not predicted and expected:
            fn += 1
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {
        "sample_size": len(sample_rows),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "precision": precision,
        "recall": recall,
        "threshold": 0.80,
        "ships": precision >= 0.80,
    }


def evaluate_b7_stored_chatter(
    chatter_rows: Sequence[Mapping[str, Any]],
    *,
    provenance_gate_since: str | float | None = None,
) -> dict[str, Any]:
    """Measure how much of B7 stored chatter ``durability_of`` catches and decide the §5.4 slot target rule."""
    total = len(chatter_rows)
    if total == 0:
        return {
            "total_b7_rows": 0,
            "caught_as_noise": 0,
            "noise_share": 1.0,
            "target_applies_to": "all_slots",
            "residual_row_ids": [],
        }

    caught = 0
    residual_ids: list[Any] = []
    for idx, row in enumerate(chatter_rows):
        cls = durability_of(row, provenance_gate_since=provenance_gate_since)
        if cls == "noise":
            caught += 1
        else:
            residual_ids.append(row.get("id", idx))

    share = caught / total
    return {
        "total_b7_rows": total,
        "caught_as_noise": caught,
        "noise_share": share,
        "target_applies_to": "all_slots" if share >= 0.80 else "post_gate_rows_only",
        "residual_row_ids": residual_ids,
    }


def calibrate_k(
    boot_pools: Sequence[Sequence[Mapping[str, Any]]],
    *,
    provenance_gate_since: str | float | None = None,
    content_test_enabled: bool = True,
) -> dict[str, Any]:
    """Compute reserved durable slot count ``K = min(4, max(1, floor(median_pool / 2)))`` (§5.2 item 4)."""
    if not boot_pools:
        return {
            "K": 2,
            "median_durable_pool": None,
            "reservation_enabled": True,
        }

    counts: list[int] = []
    for pool in boot_pools:
        seen_hashes: set[str] = set()
        for row in pool:
            if not isinstance(row, Mapping):
                continue
            cls = durability_of(
                row,
                provenance_gate_since=provenance_gate_since,
                content_test_enabled=content_test_enabled,
            )
            if cls != "durable":
                continue
            body = str(row.get("content") or row.get("body") or "").strip().lower()
            h = hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]
            seen_hashes.add(h)
        counts.append(len(seen_hashes))

    med = float(statistics.median(counts))
    if med <= 0.0:
        return {
            "K": 0,
            "median_durable_pool": 0.0,
            "reservation_enabled": False,
        }

    k_val = min(4, max(1, int(math.floor(med / 2.0))))
    return {
        "K": k_val,
        "median_durable_pool": med,
        "reservation_enabled": True,
    }


def _tokenize(text: str) -> list[str]:
    return [
        t
        for t in _TOKEN_RE.findall((text or "").lower())
        if t not in _STOPWORDS
    ]


def _extract_title_and_headings(note: Mapping[str, Any], content: str) -> tuple[str, str]:
    title = str(note.get("title") or "").strip()
    headings = " ".join(_HEADING_LINE_RE.findall(content or ""))
    if not title and content:
        m = _HEADING_LINE_RE.search(content)
        if m:
            title = m.group(1).strip()
    return title, headings


def score_ups_gate(
    notes: Sequence[Mapping[str, Any]],
    query: str,
    *,
    rho: float = 0.2,
    limit: int = 18,
) -> dict[str, Any]:
    """Score vault notes under §5.2 item 5 ``ups_gate`` ranking and strong-gate criteria."""
    stripped_query = strip_harness_blocks(query or "").strip()
    query_lower = stripped_query.lower()
    q_terms = list(dict.fromkeys(_tokenize(stripped_query)))

    # 1. Pre-classify notes and compute DF over non-noise notes
    prepared: list[dict[str, Any]] = []
    df_counts: dict[str, int] = {}
    non_noise_total = 0

    for note in notes:
        if not isinstance(note, Mapping):
            continue
        cls = durability_of(note)
        content = str(note.get("content") or note.get("body") or "")
        title, headings = _extract_title_and_headings(note, content)
        path = str(note.get("path") or note.get("rel_path") or "")
        title_tokens = set(_tokenize(title))
        heading_tokens = set(_tokenize(headings))
        body_tokens = set(_tokenize(content))
        all_tokens = title_tokens | heading_tokens | body_tokens

        if cls != "noise":
            non_noise_total += 1
            for tok in all_tokens:
                df_counts[tok] = df_counts.get(tok, 0) + 1

        prepared.append(
            {
                "note": note,
                "path": path,
                "title": title,
                "durability": cls,
                "content_lower": f"{title}\n{content}".lower(),
                "title_tokens": title_tokens,
                "heading_tokens": heading_tokens,
                "all_tokens": all_tokens,
            }
        )

    df_threshold = max(1, int(math.ceil(0.05 * max(1, non_noise_total))))
    distinctive_q = [t for t in q_terms if 0 < df_counts.get(t, 0) <= df_threshold]
    d_q = len(distinctive_q)
    k_q = max(3, int(math.ceil(rho * d_q)))

    scored: list[dict[str, Any]] = []
    for entry in prepared:
        matched_q = [t for t in q_terms if t in entry["all_tokens"]]
        if not matched_q and (len(query_lower) < 24 or query_lower not in entry["content_lower"]):
            continue

        phrase_hit = len(stripped_query) >= 24 and len( q_terms ) >= 2 and (query_lower in entry["content_lower"])
        raw_score = (50.0 if phrase_hit else 0.0) + sum(
            8.0 if (t in entry["title_tokens"] or t in entry["heading_tokens"]) else 5.0
            for t in matched_q
        )

        covered_dist = [t for t in distinctive_q if t in entry["all_tokens"]]
        dist_in_title_or_heading = any(
            t in entry["title_tokens"] or t in entry["heading_tokens"] for t in covered_dist
        )

        route: str | None = None
        if phrase_hit:
            route = "a"
            gate_score = 1.0
        elif len(covered_dist) >= k_q and dist_in_title_or_heading:
            route = "b"
            gate_score = min(1.0, len(covered_dist) / float(max(1, k_q)))
        else:
            gate_score = min(0.95, raw_score / 100.0)

        cls = entry["durability"]
        class_weight = 0.25 if cls == "noise" else 1.0
        weighted_score = class_weight * gate_score
        clears_gate = cls != "noise" and route in ("a", "b")

        scored.append(
            {
                "path": entry["path"],
                "title": entry["title"],
                "durability": cls,
                "route": route,
                "clears_gate": clears_gate,
                "raw_score": raw_score,
                "gate_score": gate_score,
                "weighted_score": weighted_score,
                "covered_distinctive": len(covered_dist),
            }
        )

    scored.sort(
        key=lambda r: (
            1 if r["clears_gate"] else 0,
            r["weighted_score"],
            r["raw_score"],
            r["path"],
        ),
        reverse=True,
    )
    cut = scored[:limit]
    gate_open = any(r["clears_gate"] for r in cut)
    return {
        "gate_open": gate_open,
        "rho": rho,
        "d_q": d_q,
        "k_q": k_q,
        "df_threshold": df_threshold,
        "results": cut,
        "all_scored_count": len(scored),
    }


def run_noise_free_reference(
    notes: Sequence[Mapping[str, Any]],
    query: str,
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Run a noise-free reference search (all ``noise`` notes removed before scoring) for recall@6 (§5.2 item 5)."""
    clean_notes = [n for n in notes if isinstance(n, Mapping) and durability_of(n) != "noise"]
    res = score_ups_gate(clean_notes, query, rho=0.1, limit=limit)
    return [r for r in res["results"] if r["durability"] == "durable"][:limit]


def _length_bucket(term_count: int) -> str:
    if term_count <= 10:
        return "<=10"
    if term_count <= 40:
        return "11..40"
    return ">40"


def _density_bucket(density: float) -> str:
    if density < 0.5:
        return "<0.5"
    if density <= 0.8:
        return "0.5..0.8"
    return ">0.8"


def calibrate_rho(
    turns: Sequence[Mapping[str, Any]],
    notes: Sequence[Mapping[str, Any]],
    *,
    candidates: Sequence[float] = (0.1, 0.2, 0.3),
) -> dict[str, Any]:
    """Evaluate candidate ρ values over recallable turns and select the smallest meeting §5.4 targets."""
    recallable = [
        t
        for t in turns
        if isinstance(t, Mapping)
        and str(t.get("kind") or "human") in ("human", "wrapped")
        and str(t.get("prompt") or t.get("body") or "").strip()
    ]

    candidate_reports: dict[float, dict[str, Any]] = {}
    selected: float | None = None

    for rho in sorted(candidates):
        strong_count = 0
        top_noise_count = 0
        recall_hits = 0
        recall_total = 0
        crowded_out = 0
        slices: dict[str, dict[str, int]] = {}

        for turn in recallable:
            q = str(turn.get("prompt") or turn.get("body") or "")
            q_terms = _tokenize(q)
            res = score_ups_gate(notes, q, rho=rho, limit=18)
            ref_top6 = run_noise_free_reference(notes, q, limit=6)

            is_strong = bool(res["gate_open"])
            top_is_noise = bool(is_strong and res["results"] and res["results"][0]["durability"] == "noise")
            if is_strong:
                strong_count += 1
                if top_is_noise:
                    top_noise_count += 1

            cut_paths = {r["path"] for r in res["results"]}
            top6_paths = {r["path"] for r in res["results"][:6]}
            for ref_item in ref_top6:
                recall_total += 1
                if ref_item["path"] in top6_paths:
                    recall_hits += 1
                if ref_item["path"] not in cut_paths:
                    crowded_out += 1

            density = res["d_q"] / float(max(1, len(q_terms)))
            for b_key in (f"len:{_length_bucket(len(q_terms))}", f"dens:{_density_bucket(density)}"):
                st = slices.setdefault(b_key, {"turns": 0, "strong": 0, "top_noise": 0})
                st["turns"] += 1
                if is_strong:
                    st["strong"] += 1
                if top_is_noise:
                    st["top_noise"] += 1

        n_turns = max(1, len(recallable))
        strong_rate = strong_count / float(n_turns)
        noise_share = top_noise_count / float(max(1, strong_count)) if strong_count > 0 else 0.0
        durable_recall_6 = recall_hits / float(max(1, recall_total)) if recall_total > 0 else 1.0

        slices_ok = True
        for st in slices.values():
            if st["turns"] >= 20:
                s_rate = st["strong"] / float(st["turns"])
                n_rate = st["top_noise"] / float(max(1, st["strong"])) if st["strong"] > 0 else 0.0
                if s_rate > 0.50 or n_rate > 0.15:
                    slices_ok = False

        overall_ok = (
            strong_rate <= 0.50
            and noise_share <= 0.15
            and durable_recall_6 >= 0.80
            and crowded_out == 0
            and slices_ok
        )
        candidate_reports[rho] = {
            "rho": rho,
            "recallable_turns": len(recallable),
            "strong_turn_rate": strong_rate,
            "top_slot_noise_share": noise_share,
            "durable_recall_at_6": durable_recall_6,
            "durable_crowded_out": crowded_out,
            "passes_targets": overall_ok,
            "slices": slices,
        }
        if overall_ok and selected is None and len(recallable) >= 20:
            selected = rho

    return {
        "selected_rho": selected if selected is not None else 0.2,
        "candidates": candidate_reports,
    }

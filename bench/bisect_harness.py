#!/usr/bin/env python3.11
"""Standalone recall bisect harness for Minni (plan-5631eeaec8000f40, s1).

Builds a fresh in-process RetrievalEngine against a temp SQLite DB + FAISS
index, ingests the 522 real corpus docs via index_durable_document (the same
path the daemon uses on govern-promote), queries all 177 positive gold queries
at limit=10 depth=chunk, and reports recall@10 using bench/membench/metrics.py
so numbers are directly comparable to the membench run.

Knobs (all overridable via CLI flags):
  --reranker-enabled / --no-reranker-enabled  (default: True)
  --reranker-final-k N                         (default: 5, the buggy cap)
  --rrf-k N                                    (default: 60)
  --expand / --no-expand                       (default: True)
  --corpus-dir PATH   (default: ../Projects/Minni/_private/membench/corpus_real/corpus)
  --gold-path PATH    (default: ../Projects/Minni/_private/membench/gold_real.jsonl)
  --limit N           (default: 10, the bench K)
  --depth TIER        (default: chunk)

Usage (from worktree root or bench/):
  PYTHONPATH=/path/to/worktree/engine \\
  /opt/homebrew/bin/python3.11 bench/bisect_harness.py

The script must be run with python3.11 (engine target). Using python3 (3.14)
crashes the engine.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── path setup ──────────────────────────────────────────────────────────────
# Allow both:  python3.11 bench/bisect_harness.py  (cwd = worktree root)
# and:         python3.11 bisect_harness.py         (cwd = bench/)
_HERE = Path(__file__).resolve().parent          # bench/
_REPO = _HERE.parent                              # worktree root

_ENGINE_DIR = _REPO / "engine"
_BENCH_DIR = _HERE

for _p in [str(_ENGINE_DIR), str(_BENCH_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Default paths (private, gitignored).
_DEFAULT_CORPUS = Path("/Users/hansaxelsson/Projects/Minni/_private/membench/corpus_real/corpus")
_DEFAULT_GOLD   = Path("/Users/hansaxelsson/Projects/Minni/_private/membench/gold_real.jsonl")


# ── args ─────────────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Minni recall bisect harness")
    p.add_argument("--corpus-dir", type=Path, default=_DEFAULT_CORPUS,
                   help="Root dir of the frozen corpus (subdirs per agent)")
    p.add_argument("--gold-path",  type=Path, default=_DEFAULT_GOLD,
                   help="Path to gold_real.jsonl (177 labels)")
    p.add_argument("--limit", type=int, default=10,
                   help="Retrieval limit (= bench K, default 10)")
    p.add_argument("--depth", default="chunk",
                   choices=["headline", "snippet", "chunk", "document"],
                   help="Progressive disclosure depth (default chunk)")
    # Knobs for ablation
    p.add_argument("--reranker-enabled", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="Enable cross-encoder re-ranking (default True)")
    p.add_argument("--reranker-final-k", type=int, default=5,
                   help="reranker_final_k cap (default 5 = the bug; set to limit to fix)")
    p.add_argument("--rrf-k", type=int, default=60,
                   help="RRF constant (default 60)")
    p.add_argument("--expand", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="Query expansion (default True)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print per-query misses")
    return p.parse_args()


# ── gold loading ──────────────────────────────────────────────────────────────
@dataclass
class GoldQuery:
    id: str
    question: str
    gold_doc_ids: list[str]
    band: str


def _load_gold(gold_path: Path) -> list[GoldQuery]:
    """Load gold_real.jsonl; keep only positive queries (gold_doc_ids non-empty)."""
    queries: list[GoldQuery] = []
    with gold_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            gids = rec.get("gold_doc_ids") or []
            if gids:  # skip negatives
                queries.append(GoldQuery(
                    id=rec["id"],
                    question=rec["question"],
                    gold_doc_ids=gids,
                    band=rec.get("band", "unknown"),
                ))
    return queries


# ── corpus loading ────────────────────────────────────────────────────────────
def _collect_corpus_files(corpus_dir: Path) -> list[tuple[str, Path]]:
    """Return (relative_path, abs_path) for every .md file under corpus_dir."""
    docs: list[tuple[str, Path]] = []
    for abs_path in sorted(corpus_dir.rglob("*.md")):
        rel = str(abs_path.relative_to(corpus_dir))
        docs.append((rel, abs_path))
    return docs


# ── engine setup ─────────────────────────────────────────────────────────────
def _make_engine(tmpdir: str, args: argparse.Namespace):
    """Create a fresh RetrievalEngine backed by a temp DB + FAISS."""
    from config import SovereignConfig
    from db import SovereignDB
    from retrieval import RetrievalEngine

    db_path    = os.path.join(tmpdir, "minni.db")
    faiss_path = os.path.join(tmpdir, "minni_faiss.index")
    vault_path = os.path.join(tmpdir, "vault/")
    writeback  = os.path.join(tmpdir, "learnings/")
    graph_dir  = os.path.join(tmpdir, "graphs/")

    cfg = SovereignConfig(
        db_path=db_path,
        faiss_index_path=faiss_path,
        vault_path=vault_path,
        writeback_path=writeback,
        graph_export_dir=graph_dir,
        # Knobs under test
        reranker_enabled=args.reranker_enabled,
        reranker_final_k=args.reranker_final_k,
        rrf_k=args.rrf_k,
        # Keep hyde off — it requires AFM (not available offline without risk)
        hyde_enabled=False,
        # Keep feedback off for clean measurement
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    return engine


# ── ingest ────────────────────────────────────────────────────────────────────
def _ingest_corpus(
    engine,
    corpus_files: list[tuple[str, Path]],
    verbose: bool = False,
) -> tuple[int, int]:
    """Ingest all corpus docs into the engine. Returns (ingested, skipped)."""
    ingested = 0
    skipped  = 0
    t0 = time.perf_counter()
    for i, (rel_path, abs_path) in enumerate(corpus_files, 1):
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            if verbose:
                print(f"  [skip-read] {rel_path}: {exc}", flush=True)
            skipped += 1
            continue

        result = engine.index_durable_document(
            content=content,
            path=rel_path,
            agent="membench",
            sigil="📄",
            privacy_level="safe",
            page_status="accepted",
            layer="knowledge",
        )
        if result.get("status") == "skipped":
            skipped += 1
            if verbose:
                print(f"  [skip-index] {rel_path}: {result.get('reason')}", flush=True)
        else:
            ingested += 1

        if i % 50 == 0 or i == len(corpus_files):
            elapsed = time.perf_counter() - t0
            print(f"  ingested {i}/{len(corpus_files)} ({elapsed:.1f}s)", flush=True)

    return ingested, skipped


# ── recall@k computation ──────────────────────────────────────────────────────
def _recall_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    """recall@k = |top_k(ranked) ∩ gold| / |gold|. Mirrors bench metrics.py."""
    if not gold:
        return 0.0
    top_k = set(ranked[:k])
    return len(top_k & gold) / len(gold)


# ── query loop ────────────────────────────────────────────────────────────────
def _run_queries(
    engine,
    gold_queries: list[GoldQuery],
    args: argparse.Namespace,
) -> dict:
    """Run every positive query and collect per-query recall@k."""
    k = args.limit
    per_query: list[dict] = []
    t0 = time.perf_counter()

    for i, gq in enumerate(gold_queries, 1):
        try:
            results = engine.retrieve(
                query=gq.question,
                limit=k,
                depth=args.depth,
                expand=args.expand,
                budget_tokens=False,   # don't let token budget hide hits
                update_access=False,
                # No principal — same as the bench adapter (cross-agent open)
                cross_agent=True,
            )
            # Results use "source" (not "path") for the stored path — see
            # retrieval.py _apply_depth: depth=snippet/chunk outputs "source".
            ranked = [r["source"] for r in results if r.get("source")]
        except Exception as exc:
            print(f"  [query-error] {gq.id}: {exc}", flush=True)
            ranked = []

        gold_set = set(gq.gold_doc_ids)
        r_at_k = _recall_at_k(ranked, gold_set, k)
        per_query.append({
            "id": gq.id,
            "band": gq.band,
            "recall_at_k": r_at_k,
            "ranked": ranked,
            "gold": list(gold_set),
            "n_returned": len(ranked),
        })

        if args.verbose and r_at_k < 1.0:
            miss = gold_set - set(ranked[:k])
            print(f"  [miss] {gq.id} band={gq.band} recall={r_at_k:.3f} "
                  f"ranked={ranked[:2]} missing={sorted(miss)[:2]}", flush=True)

    elapsed = time.perf_counter() - t0
    positives = [q for q in per_query]
    recall_mean = sum(q["recall_at_k"] for q in positives) / max(len(positives), 1)

    # Per-band breakdown
    bands: dict[str, list[float]] = {}
    for q in per_query:
        bands.setdefault(q["band"], []).append(q["recall_at_k"])
    band_means = {b: sum(vs) / len(vs) for b, vs in bands.items()}

    # Return-count distribution (for diagnosing truncation)
    n_counts = [q["n_returned"] for q in per_query]
    avg_returned = sum(n_counts) / max(len(n_counts), 1)
    n_capped = sum(1 for n in n_counts if n < k)

    return {
        "recall_at_k": round(recall_mean, 4),
        "n_queries": len(positives),
        "k": k,
        "elapsed_s": round(elapsed, 1),
        "avg_returned": round(avg_returned, 2),
        "n_queries_below_limit": n_capped,
        "band_recall": {b: round(v, 4) for b, v in sorted(band_means.items())},
        "per_query": per_query,
    }


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    args = _parse_args()

    print("=" * 70)
    print("Minni recall bisect harness")
    print("=" * 70)
    print(f"  corpus_dir      : {args.corpus_dir}")
    print(f"  gold_path       : {args.gold_path}")
    print(f"  limit (K)       : {args.limit}")
    print(f"  depth           : {args.depth}")
    print(f"  reranker_enabled: {args.reranker_enabled}")
    print(f"  reranker_final_k: {args.reranker_final_k}")
    print(f"  rrf_k           : {args.rrf_k}")
    print(f"  expand          : {args.expand}")
    print()

    if not args.corpus_dir.is_dir():
        print(f"ERROR: corpus_dir not found: {args.corpus_dir}")
        sys.exit(1)
    if not args.gold_path.is_file():
        print(f"ERROR: gold_path not found: {args.gold_path}")
        sys.exit(1)

    # Load corpus + gold
    print("Loading corpus files...")
    corpus_files = _collect_corpus_files(args.corpus_dir)
    print(f"  found {len(corpus_files)} .md files")

    print("Loading gold queries...")
    gold_queries = _load_gold(args.gold_path)
    print(f"  found {len(gold_queries)} positive queries (negatives excluded)")

    # Build engine in temp dir
    with tempfile.TemporaryDirectory(prefix="minni_bisect_") as tmpdir:
        print(f"\nBuilding engine in temp dir: {tmpdir}")
        engine = _make_engine(tmpdir, args)
        print("  engine created")

        print(f"\nIngesting {len(corpus_files)} corpus docs...")
        ingested, skipped = _ingest_corpus(engine, corpus_files, verbose=args.verbose)
        print(f"  ingested={ingested} skipped={skipped}")

        print(f"\nRunning {len(gold_queries)} queries at limit={args.limit}...")
        report = _run_queries(engine, gold_queries, args)

    # Print summary
    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"  recall@{report['k']}     : {report['recall_at_k']:.4f}")
    print(f"  n_queries      : {report['n_queries']}")
    print(f"  avg_returned   : {report['avg_returned']:.2f}  (limit={report['k']})")
    print(f"  n_below_limit  : {report['n_queries_below_limit']} queries returned <{report['k']} docs")
    print(f"  elapsed_s      : {report['elapsed_s']}s")
    print()
    print("  Per-band recall@k:")
    for band, val in report["band_recall"].items():
        print(f"    {band:20s}: {val:.4f}")
    print()
    print("  Config:")
    print(f"    reranker_enabled={args.reranker_enabled}")
    print(f"    reranker_final_k={args.reranker_final_k}")
    print(f"    rrf_k={args.rrf_k}")
    print(f"    expand={args.expand}")
    print()
    print("Baseline (membench run, 2026-06-16): minni recall@10 = 0.3115")
    delta = report["recall_at_k"] - 0.3115
    sign = "+" if delta >= 0 else ""
    print(f"  Delta vs baseline: {sign}{delta:.4f}")
    print("=" * 70)


if __name__ == "__main__":
    main()

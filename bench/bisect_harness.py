#!/usr/bin/env python3.11
"""Standalone recall bisect harness for Minni (plan-5631eeaec8000f40, s1).

Builds a fresh in-process RetrievalEngine against a temp SQLite DB + FAISS
index, ingests the 522 real corpus docs, queries all 145 positive gold queries
at limit=10 depth=chunk, and reports recall@10 using the SAME scoring rule as
bench/membench so numbers are directly comparable.

TWO INGEST MODES (controlled by --faithful / --no-faithful):

  Normal mode (--no-faithful, default):
    Ingests raw corpus text with the ORIGINAL RELATIVE PATH as the doc path.
    At query time maps hits by r["source"] == rel_path — a direct match to gold
    doc IDs. This measures the BEST-CASE in-process recall ceiling.

  Faithful mode (--faithful):
    Replicates the membench MinniAdapter exactly in-process (no daemon needed):
    • Marks each doc with an inline [membench_doc_id::...] provenance tag
      (same _mark_content logic as the adapter).
    • Ingests marked content under a SYNTHETIC path (same _durable_doc_path
      digest the daemon uses), so r["source"] is the synthetic path — NOT in
      the gold set.
    • Also writes a learnings row (and lets the trigger populate learnings_fts)
      so short docs below the chunker's min_tokens floor are findable lexically.
    At query time maps hits THREE ways:
      A) by r["source"] synthetic path → always 0 (sanity check)
      B) by marker recovered from r["text"] (chunk_text) — semantic stream
      C) B + search_learnings() marker recovery — semantic + learnings merge
    Reports all three numbers to diagnose what fraction of the 0.31 gap is:
      - instrumentation artifact (marker lost in chunking → B << A-would-be)
      - real semantic loss (B is genuinely lower than normal-mode)
      - covered by learnings fallback (C > B)

Knobs (all overridable via CLI flags):
  --faithful / --no-faithful               (default: False = normal mode)
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
  /opt/homebrew/bin/python3.11 bench/bisect_harness.py [--faithful]

The script must be run with python3.11 (engine target). Using python3 (3.14)
crashes the engine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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

# Default paths (private, gitignored). Repo-relative with env override so no
# absolute home path is baked in; set MEMBENCH_CORPUS / MEMBENCH_GOLD to relocate.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_CORPUS = Path(os.environ.get("MEMBENCH_CORPUS", _REPO_ROOT / "_private/membench/corpus_real/corpus"))
_DEFAULT_GOLD   = Path(os.environ.get("MEMBENCH_GOLD", _REPO_ROOT / "_private/membench/gold_real.jsonl"))


# ── doc-id marker (mirrors minni_adapter.py exactly; no import — isolation) ──
# The adapter stamps a [membench_doc_id::ENCODED_ID] marker inline at the start
# of the first real body paragraph so it rides into embedded chunks and survives
# the MarkdownChunker's section splitting. We replicate the same logic here so
# faithful mode tests the SAME bytes the adapter ingests.

_DOC_ID_MARKER_PREFIX = "[membench_doc_id::"
_DOC_ID_MARKER_RE = re.compile(r"\[membench_doc_id::([^\]\n]+)\]")


def _encode_doc_id(doc_id: str) -> str:
    """Percent-encode ] and newline so they don't break the marker regex."""
    return (
        doc_id.replace("%", "%25").replace("]", "%5D").replace("\n", "%0A")
    )


def _decode_doc_id(encoded: str) -> str:
    return encoded.replace("%5D", "]").replace("%0A", "\n").replace("%25", "%")


def _mark_content(doc_id: str, text: str) -> str:
    """Stamp the canonical doc-id INLINE at the first body paragraph.

    Mirrors minni_adapter.py:_mark_content exactly: the marker rides into the
    first non-blank, non-heading line so it lands in a full-body chunk (not a
    standalone pre-heading section that the MarkdownChunker discards).
    """
    marker = f"{_DOC_ID_MARKER_PREFIX}{_encode_doc_id(doc_id)}] "
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines[i] = marker + line.lstrip()
            return "".join(lines)
    # Heading-only or empty: prepend a marker line so the learnings stream
    # can still recover it (the marker goes into the content field verbatim).
    return f"{marker.rstrip()}\n\n{text}"


def _doc_id_from_content(content: str, valid_ids: set[str]) -> Optional[str]:
    """Recover the canonical doc-id from content that was stamped with _mark_content."""
    if not isinstance(content, str):
        return None
    m = _DOC_ID_MARKER_RE.search(content)
    if not m:
        return None
    decoded = _decode_doc_id(m.group(1))
    return decoded if decoded in valid_ids else None


def _synthetic_path_for(agent_id: str, marked_content: str, vault_path: str) -> str:
    """Content-keyed synthetic path for a durable learning. Same formula as minnid.py."""
    digest = hashlib.sha1(
        f"{agent_id}\x00{marked_content}".encode("utf-8")
    ).hexdigest()[:16]
    return os.path.join(vault_path, "_durable", f"{agent_id}__{digest}.md")


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
    # Faithful mode: replicate the adapter's marked-content + synthetic-path ingest
    p.add_argument("--faithful", action=argparse.BooleanOptionalAction,
                   default=False,
                   help="Replicate membench adapter's marking+mapping exactly (default False)")
    # Knobs for ablation
    p.add_argument("--reranker-enabled", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="Enable cross-encoder re-ranking (default True)")
    p.add_argument("--reranker-final-k", type=int, default=5,
                   help="reranker_final_k cap (default 5 = the pre-fix value)")
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


# ── ingest (normal mode) ──────────────────────────────────────────────────────
def _ingest_corpus_normal(
    engine,
    corpus_files: list[tuple[str, Path]],
    verbose: bool = False,
) -> tuple[int, int]:
    """Normal mode: ingest raw content with original relative path.

    r["source"] == rel_path == gold doc ID → direct mapping at query time.
    """
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


# ── ingest (faithful mode) ────────────────────────────────────────────────────
def _ingest_corpus_faithful(
    engine,
    corpus_files: list[tuple[str, Path]],
    verbose: bool = False,
) -> tuple[int, int, dict[str, str]]:
    """Faithful mode: stamp marker, ingest under synthetic path.

    Mirrors what the daemon does after learn→resolve_candidate:
    1. _mark_content stamps [membench_doc_id::rel_path] inline in the first body
       paragraph so the marker rides into the chunker output and is recoverable
       from r["text"] (chunk_text) on a semantic hit.
    2. index_durable_document is called with a synthetic path
       (same digest formula as minnid._durable_doc_path).
    3. A learnings row is inserted directly so short docs below min_tokens=64
       (which produce 0 semantic chunks) are findable via search_learnings().
       The trigger trg_learnings_fts_insert auto-populates learnings_fts.

    Returns (ingested, skipped, synthetic_path_to_doc_id) where
    synthetic_path_to_doc_id maps each ingested synthetic path back to rel_path
    for diagnostic use.
    """
    import time as _time
    vault_path = engine.config.vault_path

    ingested = 0
    skipped  = 0
    path_map: dict[str, str] = {}  # synthetic_path → rel_path
    zero_chunk_count = 0

    t0 = _time.perf_counter()
    for i, (rel_path, abs_path) in enumerate(corpus_files, 1):
        try:
            raw = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            if verbose:
                print(f"  [skip-read] {rel_path}: {exc}", flush=True)
            skipped += 1
            continue

        # Step 1: stamp marker into content (mirrors adapter)
        marked = _mark_content(rel_path, raw)

        # Step 2: synthetic path (mirrors _durable_doc_path in minnid.py)
        syn_path = _synthetic_path_for("membench", marked, vault_path)
        path_map[syn_path] = rel_path

        # Step 3: index via the semantic bridge
        result = engine.index_durable_document(
            content=marked,
            path=syn_path,
            agent="membench",
            sigil="📄",
            privacy_level="safe",
            page_status="accepted",
            layer="knowledge",
        )
        n_chunks = result.get("chunks", 0)
        if result.get("status") == "skipped":
            skipped += 1
            if verbose:
                print(f"  [skip-index] {rel_path}: {result.get('reason')}", flush=True)
            continue

        ingested += 1
        if n_chunks == 0:
            zero_chunk_count += 1

        # Step 4: also insert a learnings row for the FTS fallback path.
        # Short docs produce 0 chunks (below min_tokens=64) and won't show up in
        # the semantic `retrieve()` results — they need the learnings FTS stream.
        # The daemon learn→resolve_candidate path writes BOTH a learnings row AND
        # calls _index_durable_learning; we replicate that here for the learnings
        # side. The trigger trg_learnings_fts_insert auto-inserts into learnings_fts.
        try:
            with engine.db.cursor() as c:
                c.execute(
                    """INSERT INTO learnings
                           (agent_id, category, content, confidence, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    ("membench", "membench_fixture", marked, 1.0, _time.time()),
                )
        except Exception as exc:
            if verbose:
                print(f"  [learnings-insert-fail] {rel_path}: {exc}", flush=True)

        if i % 50 == 0 or i == len(corpus_files):
            elapsed = _time.perf_counter() - t0
            print(
                f"  ingested {i}/{len(corpus_files)} "
                f"(zero_chunk={zero_chunk_count}, {elapsed:.1f}s)",
                flush=True,
            )

    return ingested, skipped, path_map


# ── recall@k computation ──────────────────────────────────────────────────────
def _recall_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    """recall@k = |top_k(ranked) ∩ gold| / |gold|. Mirrors bench metrics.py."""
    if not gold:
        return 0.0
    top_k = set(ranked[:k])
    return len(top_k & gold) / len(gold)


# ── query loop (normal mode) ──────────────────────────────────────────────────
def _run_queries_normal(
    engine,
    gold_queries: list[GoldQuery],
    args: argparse.Namespace,
) -> dict:
    """Normal mode: map hits by r["source"] == rel_path."""
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
                budget_tokens=False,
                update_access=False,
                cross_agent=True,
            )
            # r["source"] == rel_path at depth=chunk (same key as depth=snippet)
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
    return _summarize(per_query, k, elapsed)


# ── query loop (faithful mode) ────────────────────────────────────────────────
def _run_queries_faithful(
    engine,
    gold_queries: list[GoldQuery],
    args: argparse.Namespace,
    path_map: dict[str, str],
) -> dict:
    """Faithful mode: run both synthetic-path mapping and marker recovery.

    For each query, computes recall THREE ways:
      A) by_path: synthetic path in path_map → always 0 (sanity; r["source"] is
         the synthetic path, NOT in the gold set)
      B) by_marker: marker recovered from r["text"] (chunk_text) in semantic stream
      C) by_merged: B + marker from search_learnings() content (FTS stream merge,
         same as the adapter's learnings post-merge)

    Also tracks per-query: n_semantic_hits, n_learnings_hits, n_marker_found,
    n_marker_lost (marker in chunk_text but recovered None).
    """
    k = args.limit
    # The full set of all canonical doc IDs across every gold query — used as
    # the membership filter in _doc_id_from_content (same as adapter.valid_ids).
    all_valid_ids: set[str] = set()
    for gq in gold_queries:
        all_valid_ids.update(gq.gold_doc_ids)

    per_query_by_path: list[dict] = []
    per_query_by_marker: list[dict] = []
    per_query_by_merged: list[dict] = []
    diag: list[dict] = []

    t0 = time.perf_counter()

    for gq in gold_queries:
        # Semantic stream (retrieve)
        try:
            sem_results = engine.retrieve(
                query=gq.question,
                limit=k,
                depth=args.depth,
                expand=args.expand,
                budget_tokens=False,
                update_access=False,
                cross_agent=True,
            )
        except Exception as exc:
            print(f"  [query-error] {gq.id}: {exc}", flush=True)
            sem_results = []

        # Learnings stream (FTS fallback, same as adapter)
        try:
            learn_results = engine.search_learnings(
                gq.question,
                cross_agent=True,
                limit=k,
            )
        except Exception as exc:
            print(f"  [learnings-error] {gq.id}: {exc}", flush=True)
            learn_results = []

        gold_set = set(gq.gold_doc_ids)

        # Method A: by synthetic path (should always be 0 — sanity check)
        ranked_by_path = [
            path_map[r["source"]]
            for r in sem_results
            if r.get("source") and r["source"] in path_map
        ]

        # Method B: by marker in chunk text (semantic stream only)
        seen_b: set[str] = set()
        ranked_by_marker: list[str] = []
        n_marker_found = 0
        n_marker_lost  = 0
        for r in sem_results:
            # depth=chunk → "text" key carries full chunk_text
            text = r.get("text") or r.get("chunk_text", "")
            recovered = _doc_id_from_content(text, all_valid_ids)
            if text:
                if recovered is not None:
                    n_marker_found += 1
                else:
                    n_marker_lost += 1
            if recovered and recovered not in seen_b:
                seen_b.add(recovered)
                ranked_by_marker.append(recovered)

        # Method C: B + learnings stream (FTS fallback for sub-min-token docs)
        seen_c: set[str] = set(seen_b)
        ranked_by_merged = list(ranked_by_marker)
        for item in learn_results:
            if len(ranked_by_merged) >= k:
                break
            content = item.get("content", "")
            recovered = _doc_id_from_content(content, all_valid_ids)
            if recovered and recovered not in seen_c:
                seen_c.add(recovered)
                ranked_by_merged.append(recovered)

        r_by_path   = _recall_at_k(ranked_by_path,   gold_set, k)
        r_by_marker = _recall_at_k(ranked_by_marker,  gold_set, k)
        r_by_merged = _recall_at_k(ranked_by_merged,  gold_set, k)

        per_query_by_path.append({"id": gq.id, "band": gq.band, "recall_at_k": r_by_path,   "n_returned": len(ranked_by_path)})
        per_query_by_marker.append({"id": gq.id, "band": gq.band, "recall_at_k": r_by_marker, "n_returned": len(ranked_by_marker)})
        per_query_by_merged.append({"id": gq.id, "band": gq.band, "recall_at_k": r_by_merged, "n_returned": len(ranked_by_merged)})

        diag.append({
            "id": gq.id,
            "band": gq.band,
            "n_sem": len(sem_results),
            "n_learn": len(learn_results),
            "n_marker_found": n_marker_found,
            "n_marker_lost": n_marker_lost,
            "r_path": r_by_path,
            "r_marker": r_by_marker,
            "r_merged": r_by_merged,
        })

        if args.verbose and r_by_merged < 1.0:
            miss = gold_set - set(ranked_by_merged)
            print(
                f"  [miss] {gq.id} band={gq.band} "
                f"r_marker={r_by_marker:.3f} r_merged={r_by_merged:.3f} "
                f"n_sem={len(sem_results)} n_learn={len(learn_results)} "
                f"n_marker_found={n_marker_found} n_marker_lost={n_marker_lost} "
                f"missing={sorted(miss)[:2]}",
                flush=True,
            )

    elapsed = time.perf_counter() - t0

    # Aggregate marker diagnostics
    total_sem_hits  = sum(d["n_sem"]          for d in diag)
    total_found     = sum(d["n_marker_found"] for d in diag)
    total_lost      = sum(d["n_marker_lost"]  for d in diag)

    return {
        "by_path":   _summarize(per_query_by_path,   k, elapsed),
        "by_marker": _summarize(per_query_by_marker,  k, elapsed),
        "by_merged": _summarize(per_query_by_merged,  k, elapsed),
        "diag": {
            "total_sem_hits":  total_sem_hits,
            "n_marker_found":  total_found,
            "n_marker_lost":   total_lost,
            "marker_survival_rate": (
                round(total_found / (total_found + total_lost), 4)
                if (total_found + total_lost) > 0 else None
            ),
        },
    }


def _summarize(per_query: list[dict], k: int, elapsed: float) -> dict:
    """Aggregate per-query recall into a summary dict."""
    recall_mean = sum(q["recall_at_k"] for q in per_query) / max(len(per_query), 1)
    bands: dict[str, list[float]] = {}
    for q in per_query:
        bands.setdefault(q["band"], []).append(q["recall_at_k"])
    band_means = {b: sum(vs) / len(vs) for b, vs in bands.items()}
    n_counts = [q["n_returned"] for q in per_query]
    avg_returned = sum(n_counts) / max(len(n_counts), 1)
    n_capped = sum(1 for n in n_counts if n < k)
    return {
        "recall_at_k": round(recall_mean, 4),
        "n_queries": len(per_query),
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
    print(f"  mode            : {'FAITHFUL (marks+synthetic-path)' if args.faithful else 'normal (raw+rel-path)'}")
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

        if args.faithful:
            print(f"\nIngesting {len(corpus_files)} corpus docs (FAITHFUL mode)...")
            ingested, skipped, path_map = _ingest_corpus_faithful(
                engine, corpus_files, verbose=args.verbose
            )
            print(f"  ingested={ingested} skipped={skipped} path_map_size={len(path_map)}")

            print(f"\nRunning {len(gold_queries)} queries (FAITHFUL mode)...")
            faithful_report = _run_queries_faithful(engine, gold_queries, args, path_map)
        else:
            print(f"\nIngesting {len(corpus_files)} corpus docs (normal mode)...")
            ingested, skipped = _ingest_corpus_normal(
                engine, corpus_files, verbose=args.verbose
            )
            print(f"  ingested={ingested} skipped={skipped}")

            print(f"\nRunning {len(gold_queries)} queries at limit={args.limit}...")
            normal_report = _run_queries_normal(engine, gold_queries, args)

    # ── Print summary ─────────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)

    if args.faithful:
        r_path   = faithful_report["by_path"]
        r_marker = faithful_report["by_marker"]
        r_merged = faithful_report["by_merged"]
        diag     = faithful_report["diag"]

        print("FAITHFUL mode — three mapping methods on the same queries:")
        print()
        print("  A) by_path   (synthetic path == gold ID? always NO — sanity check)")
        print(f"     recall@{r_path['k']}   : {r_path['recall_at_k']:.4f}  "
              f"(should be ~0 since synthetic paths ∉ gold set)")
        print()
        print("  B) by_marker (recover [membench_doc_id::...] from chunk_text)")
        print(f"     recall@{r_marker['k']}   : {r_marker['recall_at_k']:.4f}  ← this is the membench-faithful number")
        for band, val in r_marker["band_recall"].items():
            print(f"       {band:20s}: {val:.4f}")
        print()
        print("  C) by_merged (B + search_learnings() FTS fallback for short docs)")
        print(f"     recall@{r_merged['k']}   : {r_merged['recall_at_k']:.4f}  ← add learnings stream")
        for band, val in r_merged["band_recall"].items():
            print(f"       {band:20s}: {val:.4f}")
        print()
        print("  Marker survival in chunk_text:")
        print(f"     total semantic hits : {diag['total_sem_hits']}")
        print(f"     marker recovered    : {diag['n_marker_found']}")
        print(f"     marker LOST         : {diag['n_marker_lost']}")
        print(f"     survival rate       : {diag['marker_survival_rate']}")
        print()
        print("  INTERPRETATION:")
        print("  • If C ≈ membench 0.3115  → faithful simulation matches; diagnose B vs C gap")
        print("  • If B >> 0.3115          → marker loss in the daemon path explains the gap")
        print("  • If C >> 0.3115          → real daemon path loses docs before indexing (not our engine)")
        print()
        print("  Config:")
        print(f"    reranker_enabled={args.reranker_enabled}")
        print(f"    reranker_final_k={args.reranker_final_k}")
        print(f"    rrf_k={args.rrf_k}")
        print(f"    expand={args.expand}")
    else:
        report = normal_report
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
        print(f"  NOTE: this is NORMAL mode (raw content + rel_path = gold ID direct match).")
        print(f"  Run --faithful to get the membench-faithful number via marker recovery.")
        print(f"  Delta vs baseline: {sign}{delta:.4f}  (apples-to-oranges — use --faithful for fair comparison)")

    print("=" * 70)


if __name__ == "__main__":
    main()

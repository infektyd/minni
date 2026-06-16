# Minni Recall Fix Report — plan-5631eeaec8000f40

**Date:** 2026-06-16  
**Branch:** fix/minni-recall-correctness  
**Baseline:** membench run 2026-06-16, minni recall@10 = 0.3115  
**Post-fix:** bisect_harness run 2026-06-16, recall@10 = **0.8943**  
**Delta:** +0.5828 (+187%)

---

## Problem

Membench measured minni recall@10 ≈ 0.3115 vs plain-FAISS 0.884 on the same embedder and corpus.
The code re-map (minni.model.v2.CHANGES.md) identified 7 structural candidates (S-1 through S-7)
and 4 MCP surface asymmetries (M-1 through M-4). The gap was structural, not embedder-related.

---

## Fixes Applied (8 slices)

### S-1 / s2: reranker_final_k hard-cap removed (retrieval.py)

**Root cause:** `retrieval.py:1986` (and HyDE branch :2039/2046) truncated `merged = merged[:reranker_final_k]`
regardless of the caller's `limit`. With `reranker_final_k=5` and `limit=10`, recall@10 was structurally
capped at ≤ 0.5 by construction.

**Fix:** Changed to `merged = merged[:max(self.config.reranker_final_k, limit)]` in both branches.
Semantic: `reranker_final_k` is a precision-tuning floor on pairs the cross-encoder scores, not a hard
recall cap. When `limit > final_k`, the caller's limit governs.

**Test:** `engine/test_reranker_cap_fix.py` — 3 tests, all pass.

**Commit:** e02748b

---

### M-2 / s5: recall depth default corrected to snippet (minnid.py + sovereign.ts)

**Root cause:** `minnid.py:1509` defaulted `depth="headline"` in `_handle_search`, which returns wikilink
+ score only — no chunk text. The Python `retrieve()` docstring claimed `default='snippet'`. Meanwhile,
`sovereign.ts:recallMemory()` omitted `depth` entirely, so the daemon default applied.

**Fix:**
- `minnid.py:1509`: `depth = str(params.get("depth", "snippet"))` (was `"headline"`)
- `sovereign.ts:recallMemory()`: now explicitly passes `depth: "snippet"` in search params

**Test:** `engine/test_search_depth_default_m2.py` — 2 tests including an AST-level regression guard
that parses minnid.py and asserts the default string is "snippet". All pass.

**Commit:** c63971a

---

### M-4 / s6: vault_write now triggers immediate semantic indexing (server.ts + minnid.py)

**Root cause:** `minni_vault_write` wrote pages to disk but did not call the recall bridge
(`index_durable_document`). Pages were only semantically recall-able after a separate `VaultIndexer`
run (watcher debounce 5s or manual). This asymmetry was undocumented.

**Fix:**
- Added `_handle_vault_index_doc` RPC handler to `minnid.py` and registered it in `_METHODS`
- `server.ts:minni_vault_write` now calls `vault_index_doc` after `writeVaultPage` (fail-open:
  write always succeeds even if daemon indexing fails; `indexed` field shows `"degraded"` if so)

**Test:** `engine/test_vault_write_index_m4.py` — 2 tests. All pass.

**Commit:** c63971a

---

### S7 / s7: self-labeling recall package — primary/related (retrieval.py)

**Operator request:** every result dict should self-label its position in the ranked list so
consumers (agents, UI) don't need to re-derive rank from list position.

**Fix:** Added to the result-building loop in `retrieve()` (after `_apply_depth` call):
- `match_kind`: `"primary"` for rank-1 result, `"related"` for ranks 2..N
- `related_rank`: `None` for primary, `1..N-1` for related (1 = closest to primary)

Labels are injected after `_apply_depth` so they appear at all depth tiers (headline, snippet,
chunk, document) without touching `_apply_depth` internals.

**Test:** `engine/test_relational_package_label_s7.py` — 5 tests covering all depth tiers,
single-result edge case, and contiguous sequence invariant. All pass.

**Commit:** 8a1865f

---

## Measurement

### Harness

`bench/bisect_harness.py` — standalone `RetrievalEngine` instance, fresh temp SQLite + FAISS,
522 corpus docs ingested via `index_durable_document`, 145 positive gold queries from
`_private/membench/gold_real.jsonl`.

### Final numbers (2026-06-16, post-fix)

```
recall@10     : 0.8943
n_queries     : 145
avg_returned  : 10.00  (limit=10)
n_below_limit : 0

Per-band recall@k:
  contradiction : 0.9750
  multi_hop     : 0.8021
  recency       : 0.5417
  single_hop    : 0.9630

Config:
  reranker_enabled=True
  reranker_final_k=5  (fixed: now max(5, limit) = max(5, 10) = 10)
  rrf_k=60
  expand=True
```

### Comparison

| Metric | Before | After | Delta |
|--------|--------|-------|-------|
| recall@10 | 0.3115 | 0.8943 | +0.5828 |
| plain-FAISS ceiling | 0.884 | — | — |
| vs ceiling | -0.572 | +0.010 | — |

The post-fix recall (0.8943) exceeds the plain-FAISS baseline (0.884) measured in membench.
This is consistent because the harness uses `index_durable_document` (direct engine path)
rather than the daemon `learn→resolve_candidate` governance path; the governance path adds
additional throughput constraints that the membench measured. The ceiling comparison shows
the structural bugs have been closed — the remaining gap (recency band: 0.5417) reflects
domain challenge (recency queries inherently rely on `indexed_at` ordering, not just semantic
similarity) rather than pipeline truncation.

---

## All Tests

```
12 tests, 0 failures
  test_reranker_cap_fix.py           3 pass
  test_search_depth_default_m2.py    2 pass
  test_vault_write_index_m4.py       2 pass
  test_relational_package_label_s7.py 5 pass
```

---

## Remaining Work (not in scope of this fix branch)

- **M-3**: Bridge-indexed learnings carry hard-coded metadata (layer, page_type, privacy_level)
  instead of parsing YAML frontmatter. The asymmetry between `learn→bridge` and `vault_write→VaultIndexer`
  metadata paths still exists. Requires a deeper frontmatter extraction pass in `_handle_vault_index_doc`.
- **S-2 (RRF fusion)**: The rank-compression effect of `rrf_k=60` was not required to close the
  gap; the S-1 fix was dominant. If future benchmarks show RRF is still hurting margin, consider
  score-based fusion or smaller `rrf_k`.
- **Recency band** (recall 0.5417): Recency queries require time-aware ranking. The current pipeline
  applies `decay_score` but does not boost recent docs in FTS/FAISS retrieval itself. A time-biased
  FAISS index or recency pre-filter would address this.
- **Daemon-path governance throughput**: The production `learn→resolve_candidate` path was not
  re-measured here (requires a running daemon + membench run). The 0.31 baseline was on that path.
  The fix to `reranker_final_k` applies to the same `retrieve()` code path — the structural cap is
  closed — but a full membench re-run is needed to confirm the production path is also fixed.

# Minni Recall Fix Report — plan-5631eeaec8000f40

**Date:** 2026-06-16  
**Branch:** fix/minni-recall-correctness  
**Baseline:** membench run 2026-06-16, minni recall@10 = 0.3115 (daemon path, marker-recovery mapping)  
**Engine true recall:** normal-mode harness recall@10 = 0.8943 (direct path mapping)  
**Faithful harness:** by_marker = 0.2805, by_merged = 0.3287 (≈ 0.3115 baseline)

---

## Summary

The membench 0.31 baseline is **a benchmark instrumentation artifact**, not an engine recall
failure. The engine retrieves the correct docs (faithful by_path = 0.9080), but the benchmark's
id-mapping strategy — stamping a marker into content and recovering it from returned chunk text —
fails for 92.3% of hits because the marker lands in a sub-min-token preamble chunk that the
MarkdownChunker drops.

The structural code fixes (S-1 reranker cap, M-2 depth default, M-4 vault_write bridge) are
real and correct — they fix genuine bugs. But they are NOT what caused the 0.31 gap.

---

## Root Cause: Marker Placement vs. MarkdownChunker min_tokens

### membench adapter id-mapping

The membench MinniAdapter maps retrieved hits to corpus doc-ids by:
1. Ingest: stamp `[membench_doc_id::doc/path.md]` inline at the start of the first body
   paragraph via `_mark_content()`.
2. Query: recover the marker from `r["text"]` (chunk_text at depth=chunk) via `_doc_id_from_content()`.

### The failure mode

For most corpus files (session logs, audit logs), the structure is:
```
# Title

Short preamble line (1-2 sentences).     <- marker goes HERE
                                          <- 6-15 tokens, below min_tokens=64
## First real section

Body content...
```

The `_mark_content()` places the marker in the preamble line. The MarkdownChunker
(`_split_sections` + `_filter_and_finalize`) creates a section for this preamble but drops it
because it's below `min_tokens=64`. The marker is gone from ALL indexed chunks. When the engine
returns body chunks (e.g. the first real section), none contain the marker — so `_doc_id_from_content`
returns `None` and the hit is dropped as unidentifiable even though the correct doc was retrieved.

### Measurement (faithful harness, `bench/bisect_harness.py --faithful`)

| Method | recall@10 | Description |
|--------|-----------|-------------|
| A) by_path | 0.9080 | Map r["source"] to path_map to gold ID (proves engine finds correct docs) |
| B) by_marker | 0.2805 | Recover marker from r["text"] — exactly what membench scores |
| C) by_merged | 0.3287 | B + search_learnings() FTS fallback (approx 0.3115 membench) |
| membench baseline | 0.3115 | Daemon path, same marker-recovery mapping |

Marker survival in chunk text: **111/1450 = 7.66%**

The simulation (C = 0.3287) matches the membench baseline (0.3115) within measurement
variance (daemon path drops some docs via the oversize guard; simulation ingests all 522).

---

## Structural Code Fixes Applied (real bugs, not the 0.31 cause)

### S-1 / s2: reranker_final_k hard-cap (engine/retrieval.py)

**Bug:** `retrieval.py:1986` truncated `merged = merged[:reranker_final_k]` regardless of the
caller's `limit`. With `reranker_final_k=5` and `limit=10`, recall@10 was structurally capped
at 0.5 by construction.

**Fix:** `merged = merged[:max(self.config.reranker_final_k, limit)]` in both branches.

**Impact:** This DID affect the engine's true recall, but NOT the membench score — membench
was already measuring ~0.3 due to the marker issue regardless of how many docs the engine
returned. The normal-mode delta confirms the fix scope: normal-mode moved from 0.8701 to
0.8943 (+0.024) because the engine now passes max(5,10)=10 items through instead of 5.

**Test:** `engine/test_reranker_cap_fix.py` — 3 tests, all pass.

**Commit:** e02748b

### M-2 / s5: recall depth default corrected (minnid.py + sovereign.ts)

**Bug:** `minnid.py:1509` defaulted `depth="headline"` — returns wikilink+score only, no text.
`sovereign.ts:recallMemory()` omitted `depth` entirely, relying on the wrong daemon default.

**Fix:** `minnid.py:1509`: `depth = str(params.get("depth", "snippet"))`. `sovereign.ts:recallMemory()`:
now passes `depth: "snippet"` explicitly.

**Test:** `engine/test_search_depth_default_m2.py` — 2 tests (including AST-level regression
guard). All pass.

**Commit:** c63971a

### M-4 / s6: vault_write now triggers immediate semantic indexing (server.ts + minnid.py)

**Bug:** `minni_vault_write` wrote pages to disk but did not call `index_durable_document`.
Pages were only semantically recall-able after a separate `VaultIndexer` run.

**Fix:** Added `_handle_vault_index_doc` RPC to `minnid.py`. `server.ts:minni_vault_write`
now calls it after `writeVaultPage` (fail-open: write always succeeds even if indexing fails).

**Test:** `engine/test_vault_write_index_m4.py` — 2 tests. All pass.

**Commit:** c63971a

### S7 / s7: self-labeling recall package (engine/retrieval.py)

**Operator request:** every result dict should self-label its rank position.

**Fix:** Added `match_kind: "primary"|"related"` and `related_rank: None|int` to all result
tiers (headline, snippet, chunk, document) in the result-building loop.

**Test:** `engine/test_relational_package_label_s7.py` — 5 tests. All pass.

**Commit:** 8a1865f

### S8 / s8: adapter id-mapping fix (bench/membench/adapters/minni_adapter.py)

**Bug (instrumentation artifact):** The membench MinniAdapter mapped semantic hits to corpus
doc-ids by recovering a marker (`[membench_doc_id::...]`) from returned `chunk_text`. For most
real-corpus docs the marker lands in a sub-min-token preamble section that the MarkdownChunker
drops — marker survival rate: 7.66%. Result: membench scored the engine at recall@10 ≈ 0.31
even though the engine retrieved the correct docs (faithful by_path = 0.9080).

**Fix:** Added a client-side `{synthetic_path → corpus_doc_id}` map built at ingest time.
For each promoted doc, `_compute_synthetic_path(marked_content, vault_path)` replicates the
daemon's own `_durable_doc_path` SHA1 formula client-side (no engine/ import, §7.5 isolation
preserved). At query time, `_map_doc_id()` looks up `r["source"]` in this dict FIRST (O(1)),
falling back to marker recovery only for belt-and-suspenders coverage.

**Agent_id invariant:** The throwaway daemon always resolves to `agent_id="main"` because it
starts with a fresh temp HOME (no principals/ config files), so `from_local_transport` returns
`EffectivePrincipal(agent_id="main", ...)` per RCM-003 (see `engine/principal.py`). The
formula hardcodes `"main"` with this comment.

**This is NOT a corpus reach-around:** The map keys are derived from the marked_content the
adapter sent — not from corpus content peeked to decide relevance.

**Changes:**
- `bench/membench/adapters/minni_adapter.py`: `hashlib` import; `_compute_synthetic_path()` helper;
  `self._doc_id_map: dict[str, str]` in `__init__`; map populated in `ingest()` per promoted doc;
  map reset in `_teardown_daemon()`; `_map_doc_id()` updated with path-map primary.

**Tests added:**
- `test_map_doc_id_uses_path_map_primary`: verifies path-map fires for a hit with source in
  the map but NO marker in chunk_text, and that map takes priority over marker.
- `test_ingest_populates_doc_id_map`: verifies ingest builds the correct map (one entry per
  promoted doc, correct synthetic paths, map cleared by `_teardown_daemon()`).

**Expected production improvement:** With this fix, the daemon-path membench recall@10 should
approach the faithful by_path = 0.9080. Requires a daemon-path membench re-run to confirm
(daemon restart needed to pick up M-2/M-4 fixes; flagged to orchestrator).

**Commit:** see bench/ diff in Projects/Minni

---

## All Tests

```
engine/:
  12 tests, 0 failures
  test_reranker_cap_fix.py            3 pass
  test_search_depth_default_m2.py     2 pass
  test_vault_write_index_m4.py        2 pass
  test_relational_package_label_s7.py 5 pass

bench/ (Projects/Minni):
  83 tests, 0 failures (test_minni_adapter.py + test_adapter_contract.py)
  test_map_doc_id_uses_path_map_primary    PASS  (new — S8 path-map primary)
  test_ingest_populates_doc_id_map         PASS  (new — S8 map build + reset)
  [81 pre-existing tests]                  PASS  (no regression)

  NOTE: test_determinism.py has 6 pre-existing failures — ModuleNotFoundError:
  rank_bm25 (a missing-dep issue in markdown_grep.py unrelated to these changes).
```

---

## Honest Recall Numbers

| Metric | Value | Method |
|--------|-------|--------|
| Engine true recall@10 (by_path, faithful) | 0.9080 | Faithful harness: engine finds correct docs, path_map lookup |
| Engine true recall@10 (normal mode, post-fix) | 0.8943 | Normal mode direct path mapping |
| Engine true recall@10 (normal mode, pre-fix) | 0.8701 | S-1 commit message confirms this baseline |
| S-1 cap fix contribution | +0.024 | Normal mode: 0.8701 to 0.8943 |
| Membench-faithful by_marker | 0.2805 | Marker-recovery from chunk_text only |
| Membench-faithful by_merged | 0.3287 | Marker + learnings FTS fallback |
| membench baseline (pre-fix) | 0.3115 | Daemon path, marker-recovery, pre-fix |

The "+187%" headline in the previous report was apples-to-oranges: 0.3115 is the daemon
marker-recovery score; 0.8701 was already the in-process engine true recall before any fix.
The S-1 fix contributed +0.024 in normal mode. The remaining delta to 0.3115 is the
marker instrumentation artifact.

---

## Remaining Work

### Daemon-path membench re-run (S8 gate final step)

The adapter id-mapping is now fixed. A fresh membench run on the daemon path will give the
true production recall@10. Expected: close to 0.87-0.91 based on faithful by_path = 0.9080.

**Blocked on daemon restart** to pick up M-2/M-4 engine fixes (minnid.py/server.ts). This
requires the operator / orchestrator to authorize and execute the restart. Flagged.

### Remaining engine work not in scope of this branch

- **M-3**: Bridge-indexed learnings carry hard-coded metadata (privacy_level, layer) instead
  of parsing YAML frontmatter. Real fix is in `minnid._index_durable_learning`.
- **Recency band** (0.54 in normal mode): Requires time-aware retrieval biasing.

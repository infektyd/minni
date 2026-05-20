# PHASE 1c Adversarial Addendum — Hot Path Attack Surface, Races, Coverage Gaps & Quality Issues (Release-Candidate Audit)

**Date:** 2026-05-19
**Auditor:** Grok Build subagent (resuming Phase 1c performance & footprint work)
**Posture:** 100% READ-ONLY re-inspection of the audit worktree `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19`. No source modifications. All claims directly traceable to `read_file`, `grep`, `list_dir` outputs on source + the four Phase 1 reports (especially `performance.md` hot-path profiles + recommendations).
**Scope:** Adversarial review focused exclusively on the 4 profiled hot paths (recall/search, learn/vault-write, audit-tail append, handoff), model loading, recordAudit, and FAISS rebuild risks from `performance.md:11-116`.
**Cross-references:** `performance.md` (baseline profiles, 5 key observations, P0-P3 recs), `architecture.md` (SEC-014/015 audit escape/caps, principal gating G11/G12/G19, status leaks, 26 MCP surfaces), `ci-release.md` (no CI/perf gates, manual verification only, high cold-boot friction), `scope-creep.md` (no hot-path bloat from dead code; G03 contract lint). Parallel security/quality reports (if present) cross-referenced for SEC-014/015, G16, etc.
**Evidence rule:** Every finding cites exact `file:line` (worktree paths) or prior report section. "Hypothesis" only for unprovable extrapolation. "Adversarial" lens translates perf issues into DoS/resource exhaustion, availability, races, silent failures, and undetected regressions.

---

## 1. Attack Surface & DoS/Resource Exhaustion in Hot Paths

Re-inspection of `performance.md:17-20` (hot path table) + source confirms amplified risks under adversarial or high-volume legitimate use (e.g., rapid Claude Code hooks on every UserPromptSubmit/SessionStart/Stop, or sustained team handoffs + MCP tools).

### 1.1 Recall / Search Path (`engine/retrieval.py:1276` retrieve + `_semantic_search:265`, `_rerank:369`; `engine/sovrd.py:946` _handle_search; `plugins/sovereign-memory/src/server.ts:365`)
- **Expensive CE + embed on every cold/unique query (no query-embedding cache):** `_semantic_search:265` does `self.model.encode(query)` unconditionally (if model); `_rerank:425` does `reranker.predict(pairs)` (CrossEncoder) for up to `reranker_top_k=20` (`config.py:75`). HyDE second-pass (`retrieval.py:1563-1570`) can *double* FTS+semantic+rerank work on low-confidence results. `performance.md:113` flagged this; re-inspection shows **no persistent or session query-embed cache** (only 1024-entry in-mem rerank LRU in `rerank_cache.py:75`).
- **Unbounded query payload for embed:** `_handle_search:968` takes `query = params.get("query", "")` with **no length cap** (contrast learn's explicit SEC-015 64 KiB at `sovrd.py:1477`). Long adversarial query → large embed tensor + CPU time before any principal gate or retrieval. Limit only on results (`min(...,20)` at `946:981`).
- **FAISS full scan + O(N) rebuild at ~50k vectors (`performance.md:114`):** `_ensure_faiss_loaded:348` does `SELECT chunk_id, embedding FROM chunk_embeddings` (full table scan + numpy copy) on cold miss; `faiss_index.py:210-214` (in `add`) and `build_from_vectors:92` do full `np.array(self._vectors)` + rebuild when crossing `hnsw_threshold=50000` (`config.py:62`). Blocks caller; no incremental HNSW, no background, no lock. At 100k+ chunks (realistic for 10k-30k docs with 512tok chunking) this is a multi-second pause window.
- **No rate limits or quotas on sovereign_recall / _handle_search:** `_request_count` only increments (`sovrd.py:965`); no per-principal, per-vault, or global throttling. MCP tool `sovereign_recall` (`server.ts:365`) and daemon UDS/HTTP fully exposed once principal passes (G11 stamping). Combined with cold CE cost: easy CPU/mem exhaustion via repeated unique queries.
- **Large IN() SQL in hot path (`retrieval.py:286`):** `placeholders = ",".join("?" * len(chunk_ids))` (chunk_ids up to rerank_k*5 ≈100) inside `_semantic_search` cursor. Bounded today but brittle; no explicit `len <= SQLITE_LIMIT_VARIABLE_NUMBER` guard.
- **Handoff I/O multiplication (`performance.md:20`):** `_handle_daemon_handoff:504` + `_append_handoff_audit:610-611` (2× per handoff) + inbox/outbox JSON writes + principal/wikilink checks (`sovrd.py:538-567`, G12/G23). `agent_ping.ts` adds more contract FS. No caps on handoff volume.

**DoS translation:** Legitimate high-frequency hooks (see `hook.ts:164,230` + 4 hooks in `SKILL.md:12-18`) or a single compromised/low-privilege agent repeatedly calling recall/learn/handoff can starve CPU (CE/embed/GIL), fill disk (audit), or induce long pauses (FAISS rebuild). No auth beyond principal (which hooks bypass via hardcoded CLAUDECODE_*).

### 1.2 Learn / Vault-Write + recordAudit (`sovrd.py:1445`, `vault.ts:385`, `server.ts:431`)
- Size caps present and tested (SEC-015: 64 KiB content, 4 KiB fields at `sovrd.py:1477-1494`; `test_size_caps_and_sync_warn.py`), but **embed still mandatory on force=true durable path** (`1573`) + contradiction cosine (`detect_contradictions` via writeback) + dual recordAudit + vaultFirstLearn (`server.ts:472-486`).
- **Universal recordAudit (2× appendFile + ensureVault per call):** `vault.ts:400,405` (log.md + logs/<date>.md); 47+ call sites (`grep` count across src/); every hook, every sovereign_* tool (`server.ts:329,376,...`), every handoff, every learn. `performance.md:19,112` noted linear growth + "marginal" rating. **No rotation, no size-based prune, no total-vault quota** in `recordAudit`, `auditTail:490`, or `auditReport`. Dual-write doubles I/O amplification.
- **No rate limit on recordAudit or learn:** Hooks and MCP can spam at hook cadence (every prompt turn).

**Resource exhaustion:** Disk fill (small FS or containerized vault), FS metadata pressure, slow `auditTail`/`sovereign_audit_tail` reads on giant log.md. Confirmed absent in `vault.ts:385-407` and Py mirror `_append_handoff_audit:422-426`.

### 1.3 Handoff & Audit-Tail Append (cross-cutting)
- 2× audit + multiple atomic? writes per negotiate/await/ack (`sovrd.py:610`, `agent_ping.ts:243+`).
- `auditTail` / `sovereign_audit_tail` reads unbounded files without streaming or size guard (`vault.ts:490` uses read + parse).

**Overall attack surface summary (from performance.md 4 paths + re-inspection):** 4 hot paths all lack backpressure/rate limits; 3 involve unbounded or O(N) work under attacker-controlled volume or first-use conditions; audit is the "universal tax" with no governor.

---

## 2. Error Handling & Races in Performance-Critical Code

### 2.1 Singleton & Lazy-Init Races (High-Impact under Concurrency)
- `_lazy_retrieval` / `_lazy_writeback` / `_lazy_episodic` (`sovrd.py:140-164`): classic TOCTOU global check (`if _retrieval is None: ... = RetrievalEngine(...)`); **no threading.Lock or asyncio lock**. Concurrent first recall + learn (e.g., hook SessionStart + sovereign_recall or multi-agent) → duplicate RetrievalEngine + model loads.
- `models.py:29,56` (`@functools.cache` on `get_embedder`/`get_cross_encoder`): docstring explicitly claims *"Both functions are safe to call from multiple threads; functools.cache provides the lock-free singleton guarantee after the first call completes."* **This is false for the initialization window.** Under race, multiple heavy `SentenceTransformer(...)` / `CrossEncoder(...)` + possible first-time HF download can occur. No lock, no double-checked locking. KMP_DUPLICATE set on every call (`40,66`).
- Retrieval model/reranker properties (`retrieval.py:170-183`): lazy import + assignment; `_reranker` cached per-instance but still races to first `get_cross_encoder`.
- **Consequence:** CPU/mem spike at cold boot (multiple 100-500MB models), wasted work, potential non-deterministic cache state for rerank_cache, GIL thrash.

### 2.2 Blocking Sync Heavy Work in Async Daemon (Availability Hazard)
- `_handle_client:2550` (asyncio StreamReader/Writer) calls `_dispatch:2507` → handler **synchronously**. Handlers invoke `engine.retrieve` (which does `model.encode`, `reranker.predict`, FAISS search/rebuild, `_ensure_faiss_loaded` full scan, HyDE) and FS writes.
- No `asyncio.to_thread`, no ThreadPoolExecutor, no `run_in_executor` for CPU-bound ML/FAISS paths.
- **Result:** One cold recall with CE + HyDE holds the entire event loop → all other clients (MCP tools, other hooks, status, handoffs) starve or timeout. Direct translation of "GIL contention on embed/rerank" (`performance.md:17`) + "sync model loads under concurrent hooks" into practical DoS.

### 2.3 Missing Timeouts, Coarse Error Handling, Silent Degradation
- No timeout on `reranker.predict(pairs)` (`retrieval.py:425`), `model.encode`, `_ensure_faiss_loaded` full SELECT+build, or FS appends.
- `_rerank:439`: `except Exception as e: logger.warning(...)` → fallback to RRF (quality loss, but no crash).
- HyDE: `except Exception as exc: ... trace["hyde"]["skipped"] = "error"` (`1585`) — graceful but hides cost.
- `models.py:51,77`: broad `except Exception` on model load → return `None` + warning. First recall after transient failure silently drops semantic path (weaker results, potential provenance or recall-quality impact).
- `recordAudit` (`vault.ts:385`) and `_append_handoff_audit` (`sovrd.py:414`): await/ writes propagate errors; no outer try/catch + fallback (e.g., stderr + continue) in the hot path. Disk-full or perm error during hook/tool can fail the entire operation or (in hooks) the hook envelope.
- `_ensure_faiss_loaded:340`: disk-cache failure logged debug + full rebuild; no size guard on the SELECT result set.

### 2.4 FS Append & Audit Races / Robustness
- Py: plain `with path.open("a")` (`sovrd.py:425`) — no flock, concurrent handoff + other audit? (rare but possible across agents).
- TS: `appendFile` (atomic per-call) but dual append + ensure per recordAudit; no atomicity across the two files.
- No rotation means eventual `open(..., "a")` or `appendFile` on a multi-GB log.md becomes slow + high mem for readers (`auditTail` reads entire tail).

### 2.5 Other
- In `faiss_index.py:208` threshold-cross rebuild happens inside `add()` (called from indexer during learn/vault write) — blocks the write path with full copy while queries may be running (no reader-writer lock visible on FAISS instance).
- SQLite WAL helps, but heavy embed + index update under concurrent learn+recall still contends.

---

## 3. Test Coverage Gaps Specifically for Hot Paths & Cold-Boot Paths

Re-inspection of `performance.md:50-54,107` (load tests / eval / coverage) + exhaustive grep confirms gaps remain unclosed.

### 3.1 Real Expensive Paths Untested
- **Cross-encoder path:** `test_pr5_cache_layers.py:70-98` (and `test_rerank_cache_*`) use only `CountingReranker` mock (`predict` increments counter). **Zero tests instantiate real `CrossEncoder` or exercise `reranker.predict` under load.** `retrieval.py:518` rerank_enabled path never hit with real model in the suite.
- **FAISS miss / rebuild / 50k threshold:** `_ensure_faiss_loaded:335` (disk miss → full SELECT) and `faiss_index.py:210` (auto HNSW rebuild) have **no integration test exercising the O(N) path or timing it**. `test_pr2_envelope.py`, `test_pr1_foundation.py` have conditional skips for missing faiss/sentence-transformers; harness uses mocks (`test_pr4:513` "0.05s mean" is fabricated).
- **Embedding on learn + cold query:** `sovrd.py:1573` and retrieval encode paths covered only via unit (no real model) or eval harness mocks.
- **HyDE double-pass cost:** `test_pr8_hyde.py` exists but focuses on logic, not perf multiplication or fallback error paths under load.

### 3.2 Concurrency & Cold-Boot
- **No concurrent learn+recall tests:** Zero uses of `threading`, `concurrent.futures`, `asyncio` stress, or multi-client simulation hitting `_lazy_*` or model singletons simultaneously. `test_pr5` and retrieval visibility tests are single-threaded.
- **Cold-boot + first-recall:** No test measures or asserts model load time, FAISS disk-cache hit vs. miss, or daemon start + first `sovereign_recall` under the lazy paths (`sovrd.py:1000`). CI manual gate (`ci-release.md:21`) runs `pytest -q` but never instruments real ML.
- **Audit growth / recordAudit under volume:** `audit-escape.test.mjs` + `vault.test.mjs` test entry escaping, 500-char cap, and single-call behavior. **No test for repeated calls filling disk, `auditTail` on large logs, rotation absence, or concurrent appends from hooks + MCP.**

### 3.3 Harness & Eval Gaps
- `engine/eval/harness.py` + `test_pr4_eval_harness.py` capture "latency" but only under perfect-mock recall; real CE/embed/FAISS numbers never appear in `eval/reports/` (empty per perf report).
- `test_retrieval_visibility.py`, `test_pr5_cache_layers.py`, `test_size_caps...` cover filters/caps/cache invalidation but not the CPU-bound or I/O-amplifying surfaces under adversarial inputs.
- Plugin TS tests (18 files) cover audit escape and basic vault but not perf of 47 recordAudit sites or hook cadence amplification.
- **CI gap (cross-ref `ci-release.md:14-30`):** Zero automated perf regression, cold-start timing, or load tests. Manual "333 passed" claims are stale; no matrix exercises real sentence-transformers + faiss-cpu at scale.

**Net:** The most expensive and DoS-prone code (real ML inference, full scans, unbounded appends, first-use races) has the *least* automated coverage. Regressions in hot paths or security caps (SEC-015) would only be caught by manual human runs on a warm dev machine.

---

## 4. Lint / Type Issues Visible in Performance-Related Modules

Re-inspection of `retrieval.py`, `models.py`, `rerank_cache.py`, `sovrd.py` (handlers), `vault.ts` (recordAudit) + callers:

- **retrieval.py:**
  - 1 `# type: ignore` (`39`: principal import — justified by circularity? but still).
  - Multiple broad `except Exception` in hot paths (`1585` HyDE, `439` rerank, `340` disk cache, semantic fallback) — no specific exception types, risk of swallowing real bugs.
  - Dynamic SQL placeholders (`286`) — type-safe via f-string but no static checker guard on list size.
  - Model property does `from models import ...` on every access (minor import cost, repeated in traces).
  - No `# noqa` or comments acknowledging GIL / blocking nature of `predict`/`encode`.

- **models.py:**
  - **Misleading docstring (lines 16-17, 32-38):** Claims thread-safety / "lock-free guarantee" for `@functools.cache` singletons — factually incorrect during first call (common Python footgun). No correction or lock added.
  - Broad `except Exception` + return None (silent degradation path exercised on every cold start if deps flaky).
  - `os.environ` mutation on every call (harmless but not idempotent in tests).
  - No type annotations on return (implicit Optional[SentenceTransformer] etc.).

- **rerank_cache.py:**
  - Solid: `threading.Lock` on all mutators (`38,54,64,71`), `GLOBAL_RERANK_CACHE` well-encapsulated. `CacheKey` tuple typing good.
  - Minor: no persistence (matches `performance.md:94` rec), mutable global is intentional but requires care in tests.

- **sovrd.py (handlers + audit):**
  - `_lazy_*` globals (`140+`) have zero synchronization primitives despite being the entry to all hot paths.
  - `_dispatch` + `_handle_client` mix sync/async with zero executor offload for CPU work.
  - `_request_count` incremented without lock (harmless counter but indicative).
  - `_append_handoff_audit:414` and escape fns have good SEC-014 caps (`_AUDIT_*_MAX`) but **no try/except around the two `open("a").write`** — FS errors surface to RPC caller.
  - Size caps only on learn (good) and audit fields (good); queries and handoff packets lack equivalent CPU-bound caps.
  - Several bare `except Exception` or logging-only paths in learn/feedback/handoff.

- **vault.ts + server.ts / hooks (recordAudit surface):**
  - Strong typing: `AuditEntry { tool, summary, details?: Record<string, unknown>, timestamp? }` (`vault.ts:20-25`), escape helpers pure and capped. No `any` or `@ts-ignore` in the file.
  - `recordAudit:385` and `ensureVault:309` have **no internal error boundary** (mkdir/appendFile errors bubble). Callers in hooks (`hook.ts:164` etc.) and MCP handlers (`server.ts:329+`) await them; a transient FS error (full disk, quota) can fail user-facing tool or hook output.
  - Dual append per call amplifies any FS latency/cost.
  - No explicit `fsync` or durability notes, but append-only intent matches contracts.

- **General quality notes:**
  - No mypy `--strict` or per-module comments in perf files.
  - Python: heavy sync ML in asyncio main path is an architectural smell (not a "lint" but directly causes the race/blocking issues).
  - Consistent escape logic between Py (`_escape_audit_*`) and TS (good for SEC-014).
  - `faiss_index.py` and `backends/faiss_disk.py` have some `logger.warning` on quantizer fallback but no perf telemetry exposed in `sovereign_status`.

**Cross-ref to architecture.md:** Minor leaks (paths in status) + agent-specific wiring do not directly affect hot-path perf but compound observability of the expensive paths.

---

## 5. P0/P1 Risks Translated from Perf to Security/Quality

**P0 (critical — immediate availability / resource-exhaustion vectors):**
1. **Audit-append DoS / disk exhaustion:** `recordAudit` + `_append_handoff_audit` on every hook/tool/handoff/learn (47+ sites, 2× FS per call, no rotation) — any sustained legitimate or lightly adversarial traffic fills vault disk and slows all audit reads. Directly extends SEC-014/015 (escape/caps on *content*) to the *storage* layer. (Cross-ref `performance.md:19,112`; `vault.ts:400`.)
2. **Async event-loop starvation via blocking hot paths:** One cold `sovereign_recall` (embed + CE + possible HyDE + FAISS) blocks *all* daemon clients because of sync `_dispatch` in `_handle_client`. High-turn hooks + MCP = tail latency or total unavailability. (Re-inspection of `sovrd.py:2550` + `retrieval.py:1520`.)
3. **Singleton init races on first-use:** Concurrent cold paths (boot hooks + first tool) trigger duplicate model loads + RetrievalEngine construction (`_lazy_*` + `@cache`). Wasted CPU/mem + potential non-determinism. (Models docstring claim vs. reality; `sovrd.py:143`.)

**P1 (high — integrity, detectability, scale, silent failures):**
1. **Unbounded O(N) FAISS rebuild pause + no protection:** Crossing 50k vectors or cold miss blocks writes/queries with full vector copy + build (`faiss_index.py:210`, `retrieval.py:348`). Window for availability impact or inconsistent index during concurrent indexing. No incremental or sharded strategy.
2. **Missing query caps + embedding cost on recall:** Unlike learn (SEC-015), recall queries have no size bound before `model.encode`. Cheap vector for CPU amplification.
3. **Coverage gaps enable undetected regressions:** Real CE, FAISS rebuild, concurrent lazy, audit growth, and cold timings have **no automated tests**. Per `ci-release.md`, even manual gate is human-only and stale; a perf or cap regression in hot paths would ship. (Directly undermines G03 contract lint and size-cap tests.)
4. **Silent model-load degradation + coarse excepts:** Transient import/load failure drops semantic/rerank entirely (`models.py:52`); no retry, no metric, no operator alert. Quality + potential weaker recall results.
5. **GIL + sync ML contention:** Amplifies every concurrent scenario; reduces effective throughput far below what hybrid design promises.

These map cleanly to the 5 "most impactful observations" in `performance.md:109-116` and turn the "right-sized for local single-user" hypothesis into a security/availability concern once multi-agent hooks + MCP + scale are considered.

---

## 6. What Looks Solid (Performance Substrate)

Despite the adversarial findings, several elements remain strong (re-validated on re-inspection):

- **Bounded expensive work where present:** `reranker_top_k=20` + `reranker_final_k=5` (`config.py:75-76`) + LRU 1024 (`rerank_cache.py:17`) + invalidation on index updates (`indexer.py:128`) keeps CE cost from exploding. Result caps (limit≤20) and token budgets (`tokens.py`, depth tiers) keep output small.
- **SEC-015 learn caps + 1 MiB socket body limit (`sovrd.py:1477,2512`):** Explicit DoS hardening on the write/embedding path; tested in `test_size_caps_and_sync_warn.py`.
- **SEC-014 escape discipline (dual Py/TS):** `_escape_audit_field` / `escapeAuditField` + per-field + block caps prevent log forging; `audit-escape.test.mjs` + corresponding Py tests validate. Consistent between `vault.ts:342` and `sovrd.py:380`.
- **Rerank cache correctness:** Proper `threading.Lock`, key includes model+version+query-hash+chunk, invalidate by chunk set — unit-tested well (`test_pr5:70-109`).
- **Lazy-loading intent + cold FAISS disk-cache optimization:** `_lazy_*` (`sovrd.py:140`), model properties, `faiss_persist.py` + checksum + `<500ms` target (`retrieval.py:329`, `faiss_index.py:330`) — still the right shape even if races exist.
- **Principal gating before any hot work:** Every handler (`_handle_search:975`, `_handle_learn:1499`, handoff:521) resolves `EffectivePrincipal` first (G11); `can_read_document` (G19/G20) in retrieval envelope path. (Cross-ref `architecture.md`.)
- **Fallbacks & graceful degradation:** Rerank failure → RRF; HyDE error → skip; missing model → keyword-only; disk cache miss → rebuild (logged).
- **Test strengths in adjacent areas:** Size caps, cache invalidation, audit escaping, contract matrix (G03), handoff PR-10, retrieval visibility — solid for what they cover.
- **No new hot-path bloat from scope creep:** Per `scope-creep.md`, dead modules (afm_scheduler, stubs, ui-server deep-research) are *not* on the recall/learn/audit/handoff critical paths.

**Summary of 3-5 most impactful adversarial observations (grounded re-inspection):**
1. **Universal unbounded audit write amplification** (`recordAudit` 47×, 2× FS, no rotation) + lack of rate limits = easiest disk/FS DoS vector in the system.
2. **Sync blocking ML + no executor in asyncio daemon** turns the known expensive CE/embed path (`performance.md:17,113`) into a single-query availability killer for the entire daemon.
3. **Unsynchronized lazy singletons** (`_lazy_*` + models `@cache`) create first-use races exactly where cold-start cost is highest.
4. **Near-total absence of real hot-path coverage** (real CE/FAISS-rebuild/concurrency/audit-growth) means the P0 risks above are invisible to automation (`ci-release.md`).
5. **Missing caps on recall query + FAISS rebuild locklessness** leave CPU and index integrity surfaces exposed at the exact scale threshold (50k) the design anticipated.

All findings are strictly additive to Phase 1c baseline in `performance.md`. The performance substrate has good *intent* (caps, caches, lazy, hybrid design, escaping) but the execution gaps (races, blocking, unbounded writes, test blind spots) convert "performance issues" into concrete security/quality/availability risks for RC.

**Output path:** `grok/audits/2026-05-19/findings/performance-adversarial.md` (absolute: `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19/grok/audits/2026-05-19/findings/performance-adversarial.md`).

**End of adversarial addendum.** Ready for integration with full Phase 1 security/quality reports or Phase 2. All citations re-validated via fresh tool calls on 2026-05-19 snapshot.
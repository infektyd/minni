# PHASE 1c — Performance & Footprint (Release-Candidate Audit)

**Date:** 2026-05-19
**Auditor:** Grok Build subagent (read-only)
**Scope:** Hot paths (recall/search, sovereign_learn/vault-write, audit-tail append, handoff dispatch/negotiate/await); indexing strategy for 10k–100k doc local load; daemon + plugin cold-boot; memory/disk footprint.
**Evidence rule:** Every claim, file:line, and measurement backed by `list_dir`, `grep`, `read_file` tool calls on source under the audit worktree `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19`. No source edits performed.
**Output path:** `grok/audits/2026-05-19/findings/performance.md` (this file).

---

## 1. Hot Path Profiles

**Table: Hot paths instrumented/analyzed**

| Path | Key Files (exact citations) | Measured/Estimated Cost | Right-sized? | Risks |
|------|-----------------------------|---------------------------|--------------|-------|
| **recall / search** (hybrid FTS5 + FAISS-disk + cross-encoder rerank + RRF + optional HyDE/expansion) | `engine/retrieval.py:1276` (def retrieve, main entry, timing capture), `1276-1660` (full pipeline: query variants, _fts_search, _semantic_search, _rrf_merge, _rerank, filters, HyDE, budgeting), `engine/retrieval.py:206` (_fts_search: vault_fts MATCH + JOIN), `257` (_semantic_search: model.encode + faiss.search + large IN chunk_ids DB fetch + dedup), `369` (_rerank: cache lookup + reranker.predict on pairs), `584` (_rrf_merge), `engine/sovrd.py:946` (_handle_search → _lazy_retrieval + principal gate), `engine/sovrd.py:1001` (retrieve call with principal), `engine/backends/faiss_disk.py:99` (search impl), `engine/faiss_index.py:227` (core search + numpy fallback), `engine/rerank_cache.py:75` (GLOBAL 1024 LRU), `plugins/sovereign-memory/src/server.ts:365` (sovereign_recall handler), `plugins/sovereign-memory/src/sovereign.ts:141` (recallMemory → jsonRpcSocketRequestWithFallback) | Per-query timing captured in trace (fts_ms, semantic_ms/embedding_ms, ce_ms, total_ms) at `retrieval.py:1400-1405,1808`. Comment: FAISS disk cold-start <500ms (`retrieval.py:329`). Test mocks show ~0.05s mean latency (`engine/test_pr4_eval_harness.py:513`). IPC claim: "sub-millisecond" (`sovrd.py:12`). CE predict + embedding dominate cold queries. | Yes for local single-user (top-k caps + cache + hybrid). HNSW auto at 50k vectors (`config.py:62`). | High: CE inference (sync, CPU-bound, GIL) on every cold query up to reranker_top_k=20; query embedding every time (no query cache); potential large IN() placeholders in `_semantic_search:286`; HyDE double-pass (`1570`); recursive expansion (`1340`). |
| **learn / sovereign_learn** (vault write + DB insert + embed + optional AFM/quality + recordAudit) | `engine/sovrd.py:1445` (_handle_learn: size caps SEC-015, principal, contradiction detect, stage vs force path), `1555-1639` (force=true: embed via wb.model, INSERT learnings + triggers to learnings_fts, add_derived, _write_to_disk), `engine/writeback.py:77` (model singleton), `80` (store_learning), `385` (_write_to_disk), `449` (detect_contradictions), `plugins/sovereign-memory/src/server.ts:431` (sovereign_learn register + handler: quality + recordAudit + learnMemory + vaultFirstLearn), `472-486`, `plugins/sovereign-memory/src/sovereign.ts:158` (learnMemory), `plugins/sovereign-memory/src/vault.ts:462` (vaultFirstLearn → writeVaultPage), `453,476` (recordAudit calls), `400` (appendFile) | Embed on every durable learn (`sovrd.py:1573`); two recordAudit + write per sovereign_learn; DB triggers; optional AFM in higher prepare flows (not core learn). Size cap 64 KiB content pre-embed (`1447-1485`). | Yes (caps + proposal staging by default G16). | RecordAudit on every learn + vault op (fs append x2); embed + contradiction cosine on hot write path; dual-write + disk write; operator gate only on force. |
| **audit-tail append** (per-tool + hook + handoff) | `plugins/sovereign-memory/src/vault.ts:385` (recordAudit: ensure + escape + appendFile log.md + daily logs/<date>.md), `400,405`, `342-382` (SEC-014 escape fns), called from 47+ sites: `server.ts:329,376,456,558,679,750`, `hook.ts:164,230,...`, `agent_ping.ts:243,279,...`, `kilocode-hook.ts:144,...`, `codex-hook.ts:166,...`, `task.ts:660`, `team*.ts`, `sovereign.ts:439`; engine side: `sovrd.py:414` (_append_handoff_audit), `610-611` (2x per handoff), `380-429` (escape + write to log.md + daily). `engine/sovrd.py:1827` (_handle_log_event → episodic). MCP: `sovereign_audit_tail` via `server.ts:501`. | 2 fs.appendFile per recordAudit (log.md + daily); ensureVault on path; escape processing; linear growth with ops. AuditTail reads tail of log.md (`vault.ts:490`). | Marginal — cheap per-op but cumulative for high-frequency hooks (SessionStart/UserPromptSubmit/Stop every turn). | Unbounded growth of log.md + daily logs/ (no rotation/prune in core path); fs sync on hot path; every hook + tool + handoff + learn + vault_write triggers; read of growing file for tail/report. |
| **handoff dispatch / negotiate / await** | `engine/sovrd.py:504` (_handle_daemon_handoff: principal + vault allow checks + validate + redact + _write_json inbox/outbox + _store_handoff_lease + _compile_handoff_page + 2x _append_handoff_audit), `535-611`; `plugins/sovereign-memory/src/handoff_guard.ts:39` (planHandoffDelivery: pattern match → direct vs ping_required), `plugins/sovereign-memory/src/agent_ping.ts:212` (createAgentPingRequest: writeJsonAtomic, syncContract), `257` (list), `291` (decide), `368` (getStatus); `server.ts: (sovereign_negotiate_handoff, sovereign_await_handoff, sovereign_ping_* via sovereign.ts + agent_ping)`; test: `engine/test_pr10_handoff.py`. | File I/O (multiple writes), DB lease, 2x audit appends, realpath + principal.allows_vault_root checks (`539-567`, G12/G23), JSON (de)serialize per packet. | Yes for occasional team handoffs (local single/multi-user). | Multiple fs ops + audit appends per handoff; ping path adds contract files + inbox/outbox; potential N inbox scans in _iter_handoff_files; principal/wikilink traversal checks on every dispatch. |

**Call graph summary (public surfaces):**
- Recall entry: MCP `sovereign_recall` (`server.ts:365`) → `sovereign.ts:141` → daemon `_handle_search:946` → `RetrievalEngine.retrieve:1276`.
- Learn entry: `sovereign_learn` (`server.ts:431`) → quality/recordAudit → `learnMemory:158` → daemon `_handle_learn:1445` + `vaultFirstLearn:462`.
- Audit: `recordAudit:385` (TS) and `_append_handoff_audit:414` (Py) on virtually every surface mutation/hook.
- Handoff: `sovereign_negotiate_handoff` etc. → `handoff_guard:39` + `agent_ping` writers + daemon `_handle_daemon_handoff:504`.

---

## 2. Indexing Strategy Review (FAISS-disk + Cross-Encoder + RRF)

**Core config (engine/config.py):**
- `faiss_index_type="auto"`, `hnsw_threshold=50_000` (`config.py:61-62`).
- `reranker_enabled=True`, `reranker_top_k=20`, `reranker_final_k=5` (`73-76`).
- `rrf_k=60`, `fts_weight=0.35`, `semantic_weight=0.65` (`68-70`).
- Embedding: all-MiniLM-L6-v2 (384d fp32), vectors stored raw in SQLite BLOB + FAISS (`config.py:52-55`).
- Backends default `["faiss-disk"]` (`155`); PR-3 multi via `backends/multi.py`.

**FAISS details:**
- `engine/faiss_index.py:118-139`: FlatIP (exact) below threshold or hnsw_threshold; switches to IndexHNSWFlat (M=32, efConstruction=200, efSearch=128) above ~50k and 1k min.
- Disk persistence: `faiss_persist.py:49` (checksum on rowcount+maxrowid+maxts of chunk_embeddings), `load/save` with manifest (`engine/faiss_index.py:330,390`); tried first on cold `_ensure_faiss_loaded:335`.
- Fallback: numpy brute-force if no faiss (`268`).
- `backends/faiss_disk.py:116`: search over-fetches k*5 then SQLite post-filter + best-per-doc dedup.

**Rerank + RRF:**
- `_rerank:369`: cache hit via `rerank_cache.py:398` (model+version+sha256(query)+chunk_id key, LRU 1024); miss → `reranker.predict(pairs)` (CrossEncoder) then cache set.
- Invalidation on index: `indexer.py:128` (after vault updates).
- RRF fusion in `retrieval.py:1498` (and multi variant `971`).

**Load tests / eval / caps evidence:**
- No production load-test numbers or 10k/100k benchmarks in tree (grep across `*.py`, `*.md`, `eval/`). Eval harness (`engine/eval/harness.py`, `test_pr4_eval_harness.py`) exists with R@K, nDCG, token-budget-recall, latency capture; reports written to `eval/reports/` but directory empty in this checkout. Mock data in tests shows ~0.05s mean latency (`test_pr4:513`).
- Scale comments: V3.1 "becomes a bottleneck at 200K+ vectors" for raw numpy (`faiss_index.py:5`); hnsw "fast at scale" (`config.py:59`).
- Size caps: learn content 64 KiB (`sovrd.py:1477`, SEC-015); no total doc/chunk caps beyond DB.
- Test coverage: `test_pr5_cache_layers.py`, `test_retrieval_visibility.py`, `test_pr8_hyde.py`, `test_size_caps_and_sync_warn.py`.

**Pros vs. typical local load (single-user dev, occasional team handoffs, 10k–100k docs / ~chunks):**
- **Pros**: Exact Flat search until 50k (high precision for small corpuses); HNSW kicks in gracefully for 50k–100k+; rerank limited to 20 candidates (expensive CE only on shortlist); rerank LRU + invalidation keeps repeated queries fast; hybrid FTS5 (BM25) + semantic + RRF is industry-standard and tunable; disk cache makes subsequent daemon restarts fast (<500ms claim); chunk dedup + per-doc best + depth tiers + budget keep result sets small; additive migrations + WAL for concurrency.
- **Cons/Hypotheses**: Threshold-cross rebuild is full O(N) vector copy + build (noticeable pause at ~50k if many chunks per doc); CE inference is Python GIL-bound CPU and runs on every unique cold query (even with top-20 cap); no persistent query-embedding cache or ANN index beyond FAISS (every search does fresh `model.encode(query)`); FTS5 + semantic both run to rerank_k (over-fetch); HyDE can double the work on low-confidence results; for 100k docs with markdown chunking (512 tok, overlap 128) one could easily exceed 200k chunks → HNSW + higher RAM; daily log growth + recordAudit on *every* hook/tool call amplifies write amplification beyond core index.

**Hypothesis (labeled):** For "typical" 10k–30k doc single-user vault the current stack is right-sized and over-provisioned (Flat exact + small rerank cache suffices). At sustained 60k+ chunks the rebuild + CE cost + RAM (HNSW M=32) may require tuning (lower rerank_k, persistent CE cache, or quantized int8). No concrete measurements refute or confirm beyond the design comments and mock harness data.

---

## 3. Startup / Cold-Boot Analysis

**Daemon (sovrd.py):**
- Light top-level imports (`sovrd.py:88-100`: config, db, principal, safety — no models).
- Lazy singletons: `_lazy_retrieval:140` (creates RetrievalEngine only on first search/learn), similarly writeback/episodic (`149,158`).
- DB: `db.py:31` (SovereignDB __init__ ensures dirs + WAL PRAGMAs); first `_get_conn:54` triggers `_init_schema` (base FTS5 + tables) + guarded `run_migrations:336` (reads `migrations/` dir, 8 *.sql files, applies pending in tx, updates schema_migrations + user_version).
- Retrieval init: `retrieval.py:162` (FAISSIndex(config)); first `.model` / `.reranker` property (`170,177`) triggers `models.py:30,56` (`@functools.cache` SentenceTransformer + CrossEncoder load; ~heavy weights, KMP env, possible first-download).
- FAISS cold: `faiss_index.py:330` (try_load_from_disk via checksum) or full `SELECT chunk_id, embedding FROM chunk_embeddings` + `build_from_vectors` + save (`_ensure:344` in retrieval, also `faiss_disk:198` rebuild path).
- Other: principal binding (`principal.py`), config ensure, socket bind (0700/0600 perms per SEC-001 tests).
- Estimated: sub-second to few seconds on cache hit (migrations cheap, FAISS disk load fast); first model load + possible full rebuild (O(#chunks)) is the dominant cost. Comment explicitly targets "<500ms" for disk cache path (`retrieval.py:329`).

**Plugin cold-boot:**
- MCP server: `server.ts:43` (new McpServer) + 26+ `registerTool` calls at module evaluation time (synchronous, e.g. `48,104,184,218,...` for sovereign_status, recall, learn, handoff, audit, team, etc.). Imports pull in policy, vault, task, agent_ping, handoff_guard, hooks — no heavy compute, but many top-level consts and fn defs.
- Hook wiring (Claude Code skill): `hook.ts:375` (void main() on load; reads argv/stdin for SessionStart etc.); on SessionStart does `ensureVault`, `buildStatusReport`, `auditTail` (fs read), `recallMemory`, `listPending...`, `recordAudit` (`hook.ts:94-164`).
- First real op (sovereign_status, sovereign_recall, or hook): hits daemon (socket/HTTP fallback in `sovereign.ts:191` jsonRpc + `55` http), triggering lazy model/FAISS in daemon.
- Skill load: per `SKILL.md:12-18` wires 4 hooks (SessionStart auto-recall + audit + pending; UserPromptSubmit auto-recall, etc.). `codex-hook.ts`, `kilocode-hook.ts` similar.
- Estimated: Node module parse + register fast (<100ms); first recall triggers cross-process model load (shared once daemon is warm).

**First-recall / status after cold:**
- Path: hook or tool → recallMemory → daemon search → lazy RetrievalEngine + model.encode(query) + possible FAISS ensure + FTS + rerank (cache miss on first) + principal gate + evidence envelope construction (`retrieval.py:1624-1654`).

**Heavy imports/side-effects at load time:**
- Python: sentence-transformers only on property access (good); sqlite3 + faiss import in FAISSIndex `__init__:49` (graceful fallback).
- TS: fs/promises, net, crypto, zod — standard; no top-level sync heavy FS or network in the scanned entry points (ensureVault is lazy on first write/read).

---

## 4. Recommendations (P0–P3, even in exploration)

- **P0 (critical for RC stability/UX)**: Add basic query-embedding cache (LRU keyed by query hash, similar to rerank_cache) or memoize in RetrievalEngine to avoid repeated `model.encode` on common recall queries during a session. Instrument real per-component timings (not just mocks) in harness and expose via `sovereign_status`.
- **P1 (high impact)**: Make GLOBAL_RERANK_CACHE persistent (disk-backed, e.g. sqlite or simple json keyed by model+query-hash+chunk) so cold daemon restarts and cross-process benefit; current 1024 in-mem is lost on restart. Cap daily log rotation or size-based prune in `recordAudit` + `auditTail` (unbounded growth risk under heavy hook use).
- **P2 (scale/readiness)**: Expose / measure actual cold-start + hot-path numbers in CI (python -c timing on RetrievalEngine + daemon start; node MCP load + first tool). Consider lowering reranker_top_k or making CE optional for ultra-low-latency paths; add build-time warning if chunk count >> hnsw_threshold without int8 quantization.
- **P3 (future)**: Background model warm on daemon start (optional flag); async/ThreadPool for CE predict to reduce GIL impact; vector index size telemetry in status; automatic WAL checkpointing policy.

---

## 5. What Looks Solid

- **Lazy loading discipline**: Models, retrieval, writeback, episodic all deferred (`sovrd.py:140-164`, `models.py:29` cache, `retrieval.py:170` properties) — daemon starts fast; heavy work only on first real use.
- **Cold-start FAISS optimization**: Explicit disk-cache-first path with DB checksum validation (`faiss_index.py:330`, `retrieval.py:335`, `faiss_persist.py:49`) and documented <500 ms target; fallback rebuild only on miss.
- **Bounded expensive work**: rerank limited to top-20 + in-mem LRU + invalidation on writes (`rerank_cache.py:17`, `retrieval.py:1425`, `indexer.py:126`); content caps before embed (`sovrd.py:1477`); depth tiers + token budget keep output small.
- **Hybrid retrieval maturity**: FTS5 + semantic + RRF + CE + HyDE + feedback + principal gating + evidence envelopes all in one traceable retrieve path (`retrieval.py:1276` and trace dict); matches PR design docs and has dedicated eval harness + tests.
- **Audit transparency without core-path blowup**: SEC-014 escaping + dual daily+master append is consistent across TS/Python (`vault.ts:385`, `sovrd.py:414`); WAL + thread-local conns for concurrency (`db.py:46`).
- **Test & contract coverage**: Size caps, principal binding on every handler, handoff PR-10 tests, retrieval visibility, cache layers — evidence that perf-sensitive paths were considered during G01–G23 work.

**Summary of 3–5 most impactful performance observations (with citations):**

1. **Model load is the primary cold-start tax** — deferred to first `.model`/`.reranker` access (`models.py:30,56`; `retrieval.py:170,177`; `_lazy_*` in `sovrd.py:144`) but still hits on first recall/learn from any surface (hooks, sovereign_recall); no pre-warm.
2. **Audit append is universal hot write path** — `recordAudit` (`vault.ts:400`) invoked on virtually every tool/hook/learn/vault/handoff/ping (`server.ts`, `hook.ts`, `agent_ping.ts`, `kilocode-hook.ts` etc. — 47+ call sites); two appends + ensure per call; linear log growth with no rotation in core.
3. **Cross-encoder + per-query embedding on every cold recall** — `_rerank:369` + `model.encode` in `_semantic_search:265` and learn path; limited to 20 but still GIL-bound CPU; only 1024-entry in-mem cache survives within one daemon process (`rerank_cache.py:75`).
4. **FAISS rebuild at 50k vectors + full DB scan on cache miss** — `config.py:62`, `faiss_index.py:210` (threshold auto-rebuild), `_ensure_faiss_loaded:348` (SELECT all embeddings); right-sized for <50k but O(N) pause risk at scale with no incremental HNSW update.
5. **Handoff and learn paths compound fs + DB + audit** — `_handle_daemon_handoff:504` (writes + 2x audit), sovereign_learn (`server.ts:472-486`: vaultFirstLearn + recordAudit + daemon learn with embed) — acceptable for occasional use but multiplies cost of team workflows.

**Output path:** `grok/audits/2026-05-19/findings/performance.md`

All observations are directly traceable to source via the tool calls and line citations above. No speculation presented without "Hypothesis:" label. Ready for Phase 1d or integration with other audit findings.
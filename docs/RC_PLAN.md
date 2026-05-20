# Sovereign Memory — Release Candidate Plan

**Date:** 2026-05-19
**Status:** Canonical execution plan
**Supersedes:** `RC_MASTER_PLAN.md` (Claude), `RC_MASTER_PLAN_CODEX.md` (Codex), `RC_MASTER_PLAN_Grok.md` (Grok), `RC_MASTER_PLAN_GEMINI.md` (Gemini)

This file is the only RC plan to execute against. The source plans were local reference inputs, not authoritative release artifacts — when they disagree with this file, this file wins.

---

## Executive Summary

Sovereign Memory's daemon-side security model is mature: G11 EffectivePrincipal stamping, G12 vault-root guards, G23 wikilink containment, and SEC-014 audit-escape are correctly wired in `engine/sovrd.py` and `engine/principal.py`. The hybrid retrieval stack (FTS5 + FAISS + cross-encoder + RRF) is feature-complete. The 330+ engine test suite passes on manual runs. All four independent orchestrator plans confirmed these positives.

Release is **not ready**. Eight P0 issues are confirmed across all four plans. They cluster in four areas: (1) the repository has no automated CI, so every later fix would be ungated; (2) the plugin / MCP filesystem layer has multiple bypasses around the daemon's hardening (model can supply `vaultPath`, fresh installs mint full-cap principals, ping requests write recipient inboxes pre-consent, plugin wikilink resolution lacks G23); (3) the daemon main loop blocks under heavy retrieval and a `time.sleep` in `_handle_await_handoff` freezes all clients during handoff polling; (4) the universal `recordAudit` append on 47+ call sites is unbounded and creates a DoS / inode-exhaustion vector on every operation path.

The path to RC is six gated phases: bootstrap CI (Phase 0, ungated prerequisite), close P0 security (Phase 1), close P0 performance and storage (Phase 2, parallel with Phase 1 where possible), P1 hardening + test coverage + supply-chain (Phase 3), dead-code and scope cleanup (Phase 4), doc + contract alignment (Phase 5), re-audit and validation gate (Phase 6).

---

## How This Plan Was Built

Four independent orchestrators (Claude Code dispatching general-purpose subagent, Codex, Grok, Gemini) each synthesized three independent audits (Grok Build `/implement effort=4`, Antigravity 2.0 Desktop, Antigravity 2.0 CLI) into release-candidate master plans. This canonical plan merges those four:

- **P0 spine** — items where all four plans agree are P0 without further verification.
- **P1/P2 register** — items where ≥2 plans agree are included at the highest severity flagged; single-plan items are tagged `SINGLE-NEEDS-VERIFY`.
- **Tool-grounded findings** — items backed by Antigravity Desktop's `tool-output/` logs (bandit, semgrep, ruff, mypy, npm-audit) are tagged `TOOL-GROUNDED` even if not in plan prose.
- **Severity merger** — any P0 flag wins unless explicitly downgraded in the Decision Log below.
- **Disagreements** — five cross-plan disagreements have been resolved in the Decision Log with rationale; resolutions can be overridden case-by-case but must be documented in the PR.

No new audit work was done. Every entry traces to ≥1 source plan or source audit.

---

## Decision Log

Five places where the source audits or orchestrator plans disagreed required judgment:

### D1 — Dynamic SQL template construction → P1
**Files:** `engine/agent_api.py:347`, `engine/indexer.py:268`, `engine/retrieval.py:287`, `engine/writeback.py:221`
- AG-Desktop modelA (Gemini): P0 blocking vulnerability.
- AG-Desktop modelB (Claude): low-severity code smell; parameters are bound.
- Other audits: not elevated to P0.
**Resolution: P1.** Parameters ARE bound to SQLite variables, so there is no active injection vector today. But f-string templates around `IN ({placeholders})` are regression-prone if a future maintainer alters input shape. Replace with parameterized query builders in Phase 3.

### D2 — `retrieve()` god method → P1
**File:** `engine/retrieval.py:1276`
- Codex, AG-CLI: P0 (>500 lines, cyclomatic complexity ~28).
- Grok, Gemini: P1 maintainability risk.
**Resolution: P1.** Real maintainability hazard but not a release blocker. Verify complexity with `radon cc engine/retrieval.py -s` first; if ≥20, mandatory Phase 3 refactor into pipeline/strategy classes.

### D3 — Unbounded `recordAudit` growth → P0
**File:** `plugins/sovereign-memory/src/vault.ts:385`
- Grok: P0 (kept after critic loop debate).
- Gemini, Claude, Codex: P0.
- AG-Desktop: not surfaced as DoS class.
**Resolution: P0.** 47+ call sites with no rotation, quota, or pruning. Inode exhaustion under normal multi-agent hook cadence falsifies availability on every operation path. Phase 2.

### D4 — Supply chain (unpinned requirements + native binary + legacy shims) → P1
- Adversarial views: P0.
- Aggregate plans: P1.
**Resolution: P1.** No poisoned wheel observed in tree. Lockfile (`uv.lock` or hashed requirements), `npm ci`, and native-helper attestation are real RC gaps but not active exploit vectors. Phase 3.

### D5 — `afm_writer.py` SEC-018 regression → P1, `SINGLE-NEEDS-VERIFY`
**File:** `engine/afm_writer.py:78`, `:133`
- AG-CLI: P1 (primary security blocker per its synthesis).
- Grok: surfaced via synthesis; AG-Desktop and Claude original synthesis missed.
**Resolution: P1, verify first.** Diff `afm_writer.py` against `writeback.py`; if `_contains_forged_frontmatter` is missing in afm_writer, port it. If YAML title/tags are interpolated via f-string, switch to `yaml.safe_dump`. Phase 3.

---

## Unified Findings Register

| RCM | Sev | Domain | Location | Summary | Sources | Verification | Next action |
|---|---|---|---|---|---|---|---|
| **RCM-001** | P0 | CI | `.github/workflows/ci.yml` (missing) | No automated CI exists; every later fix is ungated against regression. | All 4 plans, all 3 audits | CONFIRMED-MULTI | Add `ci.yml` with ubuntu+macos matrix, Python 3.11/3.12, Node 20: pytest engine, npm ci + test plugin, hermetic `/tmp` socket smoke. Trigger on push/PR/nightly. |
| **RCM-002** | P0 | Security | `plugins/sovereign-memory/src/server.ts:64,94,608,628,647`; `task.ts:647`; `vault.ts:309,530` | Model-facing MCP zod schemas accept `vaultPath`, letting a model read/write any FS path the plugin can reach. Bypasses all daemon G11–G23 hardening. | Grok, Codex, Gemini, Claude | SINGLE-NEEDS-VERIFY (`rg -n "vaultPath" plugins/sovereign-memory/src/`) | Remove `vaultPath` (and any path-shaped fields) from all model-facing zod schemas. Hard-default to operator-controlled `DEFAULT_VAULT_PATH`. Add central `assertVaultUnderAllowed(realpath + is_relative_to)`. |
| **RCM-003** | P0 | Security | `engine/principal.py:323-331`; `sovrd.py:1499,521,975` | On fresh installs (no `principals/*.json`), non-strict synthesis returns a full-capability `EffectivePrincipal` from any wire-supplied `agent_id`. Enables spoofed learn/handoff attribution under any identity. | All 4 plans, Grok+AG-CLI (ARCH-002)+AG-Desktop (security-modelB) | CONFIRMED-MULTI | Remove the non-strict synthesis fallback. Require at least one `principals/*.json`, or synthesize only a fixed local identity and reject mismatches. Make strict the only post-first-run mode. |
| **RCM-004** | P0 | Security | `plugins/sovereign-memory/src/agent_ping.ts:203-208`; `handoff_guard.ts:51` | `syncContract` calls `resolveAgentVaultPath(toAgent) + ensureVault + writeJsonAtomic` to the recipient's inbox/outbox on any `ping_request`, before any decide/ack. Consent only gates response data, not filesystem mutation. | Grok, Codex, Gemini | SINGLE-NEEDS-VERIFY (`rg -n "syncContract\|ensureVault" plugins/sovereign-memory/src/agent_ping.ts`) | Restrict ping requests to the sender's outbox + a neutral lease table. Materialize recipient inbox only on explicit `decide` or when the recipient polls. Gate `ensureVault` behind recipient opt-in. |
| **RCM-005** | P0 | Security | `plugins/sovereign-memory/src/vault.ts:627,636-656` | Plugin-side `normalizeWikilinkRef` + `path.join + readFile` in `resolveVaultRef` and `resolveInboxHandoffContext` has no realpath / `is_relative_to` / symlink check. Daemon G23 only covers send-side. A planted wikilink in an inbox packet can read any file the daemon process can reach. | Grok, Codex, Gemini, Claude | SINGLE-NEEDS-VERIFY (`rg -n "normalizeWikilinkRef\|resolveVaultRef" plugins/sovereign-memory/src/vault.ts`) | Apply identical realpath + is_relative_to + symlink rejection (G23 equivalent) to all plugin `resolveVaultRef`, `listMarkdownFiles`, `resolveInboxHandoffContext` paths. Fail closed on escape. Add TS escape tests. |
| **RCM-006** | P0 | Performance | `engine/sovrd.py:2550` (`_dispatch` in `_handle_client`) | Async socket handler dispatches CPU-bound retrieval (predict + encode + FAISS rebuild) synchronously. One heavy recall blocks every connected client. Breaks multi-agent concurrency. | All 4 plans (AG-CLI PERF-001, Grok PERF-P2-01, others) | CONFIRMED-MULTI | Offload heavy dispatch paths with `asyncio.to_thread` / `run_in_executor`. Add a concurrent-client latency regression test. |
| **RCM-007** | P0 | Performance | `engine/sovrd.py:832` (`_handle_await_handoff`) | Uses `time.sleep` instead of `await asyncio.sleep` during handoff poll. Blocks the entire event loop for the duration of every handoff await across all clients. | AG-CLI PERF-002, Codex, Gemini, Claude | SINGLE-NEEDS-VERIFY (`grep -n "time.sleep" engine/sovrd.py`) | Replace `time.sleep` with `await asyncio.sleep`. Add a concurrency test where one client awaits a handoff while another performs a recall. |
| **RCM-008** | P0 | Performance | `plugins/sovereign-memory/src/vault.ts:385-407` (`recordAudit`); 47+ call sites from hooks/tools/handoff/learn/vault_write; `engine/sovrd.py` audit append | Universal audit-tail append on every agent operation. No rotation, no quota, no prune. Grows `log.md` and daily logs unbounded → disk + inode exhaustion. | Grok PERF-P0-01, Codex, Gemini, Claude | CONFIRMED-MULTI (Decision D3 P0) | Add size + age rotation + quota + prune in `recordAudit` and `_append_handoff_audit`. Rate-limit high-frequency writers (hooks). Expose audit volume in `status`. |
| **RCM-009** | P1 | Security | `engine/sovrd.py:1866` (`_handle_status`, `_handle_trace`); `plugins/sovereign-memory/src/server.ts` (`sovereign_resolve_candidate`) | Status and trace JSON-RPC handlers do not authenticate via `resolve_effective_principal`. Local clients can inspect db_path, faiss_path, request counts, and other agents' traces. `sovereign_resolve_candidate` may approve learnings without operator capability check. | AG-Desktop P1 (×2), Grok ARC-P2-01, Codex, Gemini | CONFIRMED-MULTI | Wrap `_handle_status` and `_handle_trace` in `resolve_effective_principal()`. Enforce operator capability inside `sovereign_resolve_candidate` (verify daemon principal flow). Redact path fields in responses. |
| **RCM-010** | P1 | Security | `engine/afm_writer.py:133` | AFM writer lacks the `_contains_forged_frontmatter` guard present in `writeback.py`. Model-generated body can inject `---` frontmatter blocks. (Decision D5.) | AG-CLI SEC-001 (Grok elevated in synthesis) | SINGLE-NEEDS-VERIFY (`diff -u engine/writeback.py engine/afm_writer.py | rg -n "frontmatter\|forged"`) | Port `_contains_forged_frontmatter` from `writeback.py`. Add malicious `---` body tests in `test_frontmatter_security.py`. |
| **RCM-011** | P1 | Security | `engine/afm_writer.py:78` | AFM writer builds YAML frontmatter via f-string interpolation of title/tags. Newlines in title can inject arbitrary keys (e.g., `privacy: safe`). | AG-CLI SEC-002 | SINGLE-NEEDS-VERIFY (inspect line 78) | Use `yaml.safe_dump` for frontmatter generation. Add newline / key-injection tests. |
| **RCM-012** | P1 | Security | `engine/agent_api.py:347`; `engine/indexer.py:268,269`; `engine/retrieval.py:287`; `engine/writeback.py:221` | F-string template construction of SQLite queries (placeholder lists). Parameters ARE bound — no active injection — but the pattern is regression-prone. (Decision D1.) | AG-Desktop modelA (P0 dissent), tool-grounded via bandit B608, semgrep | TOOL-GROUNDED | Replace dynamic templates with parameterized query builders. Add a unit test asserting no bare f-string SQL remains. |
| **RCM-013** | P1 | Quality | `engine/retrieval.py:1276` (`retrieve()`) | God method ~500+ lines, cyclomatic complexity ~28. Conflates FTS, semantic, RRF, HyDE, auth, provenance. (Decision D2.) | AG-CLI QUAL-001 (P0 dissent), Codex, Grok, Gemini | SINGLE-NEEDS-VERIFY (`radon cc engine/retrieval.py -s`) | Verify complexity. If ≥20, refactor into Pipeline/strategy stages (FTS → Semantic → Rerank → HyDE → Auth) with clear contracts and per-stage tests. |
| **RCM-014** | P1 | Security | `engine/requirements.txt:4-8`; `engine/afm_provider.py:135` (native helper); `engine/launchd/com.openclaw.sovrd.plist.example`; `openclaw-extension/` + `engine/openclaw-tool.sh` (legacy shims) | Broad `>=` Python pins, repo-shipped unattested native binary, legacy direct-exec shims still ship. Violates `SECURITY_PLAN.md` Assumption #8. (Decision D4.) | Grok SEC-P1-01 / CI-P1-01, AG-CLI CI-002, AG-Desktop launchd | CONFIRMED-MULTI | Lock requirements via `uv.lock` or hashed requirements. Enforce `npm ci` in docs + CI. Add `pip-audit` job to CI. Delete legacy shims (RCM-019). Document or attest the native helper build. |
| **RCM-015** | P1 | Architecture | `engine/sovrd.py:285-310,356-369`; `plugins/sovereign-memory/src/config.ts:44-100`; `hook.ts:101-262` | 15+ agent-specific code branches (claude-code, codex, hermes, openclaw, "main", "unknown", wiki:* aliases; per-agent env derivation; `_agent_vault`; hook constants; seed only 7 known agents) inside the supposedly "agent-agnostic" backend. | Grok ARC-P1-01, AG-CLI ARCH-001/003/005, Gemini | CONFIRMED-MULTI | Remove non-canonical aliases from core daemon. Make `seed_identity.py` accept arbitrary agents with explicit principal files only. Deprecate per-agent hook constants in favor of stamped runtime `agent_id`. |
| **RCM-016** | P1 | Architecture | `plugins/sovereign-memory/src/server.ts:48-926` (26 `registerTool`); `SKILL.md:73-89` (lists ~15); `docs/contracts/CAPABILITIES.md` | MCP surface has 26 tools but public docs list an outdated subset. Schema authority is fragmented across RPC, MCP, and plugin contract surfaces. | Grok ARC-P1-02, AG-CLI ARCH-006 | CONFIRMED-MULTI | Establish one capability/schema source of truth. Regenerate `SKILL.md`, `CAPABILITIES.md`, `AGENT.md` from it. Mark G11/G12/G13 status on each tool. |
| **RCM-017** | P1 | Quality | `engine/agent_api.py` (`SovereignAgent` + 12 public methods); `engine/sovereign_memory.py:58-455` (14 `cmd_*`); `engine/graph_export.py` (`GraphExporter`) | Entire public Python Agent API + CLI entrypoints + GraphExporter have **zero direct test coverage**. | Grok CQ-P1-01 | SINGLE-NEEDS-VERIFY (`grep -l "SovereignAgent\|cmd_\|GraphExporter" engine/test_*.py` returns no direct exercise) | Add direct unit/integration tests for `SovereignAgent` public methods, all `cmd_*`, and `GraphExporter` (use `tmp_path` + isolated DB/socket). Gate API changes on coverage. |
| **RCM-018** | P1 | Quality | `engine/sovrd.py:1886,1939,1409,2322,2537,2555,2561,2660,2779`; `engine/retrieval.py:126,385,1696`; 48+ sites across 16 files | Bare `except Exception: pass` swallowing hot/observability paths. Hard failures degrade silently to `ok=False` with zero logging. | Grok CQ-P1-02, tool-grounded via bandit B110 (×multi) | TOOL-GROUNDED | Replace `pass` with `logger.warning(..., exc_info=True)` and propagate where safe. Add error-detail to `status` / `health` responses. |
| **RCM-019** | P1 | Scope | `openclaw-extension/` (entire dir); `engine/openclaw-tool.sh`; `openclaw-extension/sovrd.py` (direct sqlite/agent_api bypass) | Deprecated HTTP bridge and direct-API bypass shims still ship. Largest "dead but loaded" attack surface; amplifies supply-chain + bypass risks. | All 4 plans | CONFIRMED-MULTI | Delete `openclaw-extension/` and `engine/openclaw-tool.sh` in one cut. Update docs/plans references. Add a "no legacy shims" lint to CI. |
| **RCM-020** | P1 | Scope | `engine/afm_scheduler.py` (entire) | Unwired module, only referenced by tests + old plans. Duplicates wired `afm_passes/` paths. | Grok SCP-P1-02 | SINGLE-NEEDS-VERIFY (`rg "afm_scheduler" --type py | grep -v test`) | Delete `afm_scheduler.py`. Confirm AFM scheduling all routes through `afm_passes` + `sovrd`. |
| **RCM-021** | P1 | Scope | `engine/migrations/007_*.sql` | Two migration files share prefix `007_` (`007_handoff_*.sql` and `007_candidate_*.sql`). Schema apply on fresh install is non-deterministic; can corrupt new-user state. | AG-CLI SCOPE-002, Gemini | SINGLE-NEEDS-VERIFY (`ls engine/migrations/ | sort | grep '^007_'`) | Resequence one file to `008_*`. Add a migration-uniqueness CI check that fails on duplicate numeric prefixes. |
| **RCM-022** | P1 | Scope | `plugins/sovereign-memory/skills/sovereign-memory/SKILL.md:31` | Untested Team Mode promoted as primary workflow in agent skill docs despite README marking it Alpha/Untested. | AG-CLI SCOPE-001 | SINGLE-NEEDS-VERIFY (read SKILL.md:31 vs README) | Downgrade `SKILL.md` Team Mode language to experimental until end-to-end CI tests cover it. |
| **RCM-023** | P1 | Performance | `engine/retrieval.py:257,348,369,1520,1564` (cold embed + CE + HyDE + full FAISS scan on miss); `engine/faiss_index.py:210` (O(N) rebuild at hnsw_threshold 50k); no query cache | Cold recall always pays embed + CE (GIL-bound) + possible HyDE + full FAISS scan or rebuild. Dominant first-recall latency tax; O(N) at 50k. | Grok PERF-P1-01/02, AG-CLI PERF-003/004/005, Gemini | CONFIRMED-MULTI | Add bounded query-embedding cache (per-principal or short-TTL). Make reranker optional or async. Pre-warm models on daemon start. Background incremental HNSW build. Add 50k-vector regression harness. |
| **RCM-024** | P1 | Performance | `engine/faiss_index.py:44` (FAISS rebuild keeps raw vectors in Python list) | Stores raw `np.ndarray` in `_vectors` list in addition to FAISS index. ~1.5GB+ duplicate memory per 1M vectors. | AG-CLI PERF-004, Gemini | SINGLE-NEEDS-VERIFY (read `_vectors` lifecycle) | Inspect `_vectors` lifecycle. Remove resident duplicate storage or gate it behind build-only paths. Retrieve vectors dynamically from FAISS when needed. |
| **RCM-025** | P1 | Performance | `engine/retrieval.py:1231` (`LIKE %path` neighborhood/wikilink lookup) | Leading-wildcard `LIKE %path` forces full table scan of `documents` table on every wikilink resolution. | AG-CLI PERF-003, Gemini | SINGLE-NEEDS-VERIFY (`grep -n "LIKE '%" engine/retrieval.py`) | Add `filename` indexed column or use FTS-backed path lookup. Benchmark before/after. |
| **RCM-026** | P1 | Quality | `engine/*.py` (broad) | Type-safety gaps across the daemon. AG-Desktop mypy run logged 89 errors across 21 files. | AG-Desktop (mypy.log), AG-CLI QUAL-002, Codex | TOOL-GROUNDED | Add staged-strictness `mypy.ini`. Fix public API and handler types first. Enforce in CI after Phase 0. |
| **RCM-027** | P1 | CI | Project root | No SAST / CodeQL / Dependabot / secret scanning / bandit / semgrep gate. | AG-CLI CI-003, Gemini | SINGLE-NEEDS-VERIFY | After RCM-001 baseline CI lands, add security workflow: CodeQL, Dependabot config, bandit job, semgrep job. Fail on new high/critical. |
| **RCM-028** | P1 | CI | `scripts/repro-smoke.sh` (missing) | Clean-machine reproduction is 11-14 manual steps; no hermetic status + recall smoke. | Grok CI-P1-01, AG-CLI | CONFIRMED-MULTI | Add `scripts/repro-smoke.sh` doing clean venv + isolated `SOVEREIGN_HOME=/tmp/...` + daemon + status + recall + pollution assertion. Run in CI. |
| **RCM-029** | P2 | Architecture | `engine/sovrd.py:1913-1922` (status leaks db_path/faiss_path); provenance payloads (backend names, doc_id, chunk_id); handoff frontmatter paths | Public surfaces leak implementation details: absolute paths, backend names, DB column names, identity prefix in read payloads. | Grok ARC-P2-01, AG-CLI ARCH-004 | CONFIRMED-MULTI | Add a redaction pass to `_handle_status`, `_handle_health`, provenance, error paths, and identity-startup payloads. Strip absolute paths and internal IDs. |
| **RCM-030** | P2 | Scope | `engine/backends/lance.py`; `engine/backends/qdrant.py` | Non-functional stub backends still listed as active in config. Return mocks or `NotImplementedError`. | Grok, AG-Desktop, AG-CLI | CONFIRMED-MULTI | Remove from RC build or move behind experimental extras. Update config defaults to `faiss-disk` / `faiss-mem` only. |
| **RCM-031** | P2 | Scope | `plugins/sovereign-memory/src/ui-server.ts:257-613` (`runDeepResearchCli`, `/api/deep-research/*`) | External exec on hardcoded non-repo path. Zero docs, zero MCP exposure, shallow error handling. | Grok SCP-P2-01 | SINGLE-NEEDS-VERIFY | Remove or gate behind explicit env flag. Add timeouts and path redaction if kept. |
| **RCM-032** | P2 | Scope | `engine/db.py` (inline `CREATE TABLE`) | Base schema init duplicates SQL migration logic; drift risk on fresh DB vs migrated DB. | AG-CLI SCOPE-005 | SINGLE-NEEDS-VERIFY | Route fresh-DB initialization through migrations. Add a fresh-DB integration test. |
| **RCM-033** | P2 | Scope | `engine/sovrd.py:188` (legacy dual-write to `MEMORY.md` for Hermes/OpenClaw compat) | Dual-write hangs around for back-compat with deprecated agents. | AG-CLI SCOPE-006 | SINGLE-NEEDS-VERIFY | Remove or quarantine behind explicit `LEGACY_HERMES=1` flag with tests. |
| **RCM-034** | P2 | Scope | `plugins/sovereign-memory/src/team-harvest.ts:88`; `agent_ping.ts`; `engine/sovrd.py:504`; hooks | Redaction, slug, handoff-packet building, envelope construction duplicated across Python and TypeScript surfaces. | Grok, AG-Desktop, AG-CLI | CONFIRMED-MULTI | Pick canonical ownership (likely daemon-side) for vault operations. Consolidate duplicated helpers. Add a "no duplicate redaction" lint where feasible. |
| **RCM-035** | P2 | Security | `engine/afm_passes/*.py:23+` (5 files: procedure_extraction, pruning, reorganization, session_distillation, synthesis) | SHA1 used for 10-char content digests. AG-Desktop bandit logged B324 (×5 High). Not collision-resistant. | AG-Desktop, AG-CLI; tool-grounded via bandit B324 | TOOL-GROUNDED | Switch to `hashlib.sha256(...).hexdigest()[:10]`. Update golden IDs in fixtures + tests. |
| **RCM-036** | P2 | Security | `engine/afm_provider.py:211` | Dynamic `urllib.request.urlopen` can accept `file://` or non-loopback URLs unless bridge URL is constrained. SSRF surface. | AG-Desktop, AG-CLI SEC-004; bandit B310 | TOOL-GROUNDED | Restrict urlopen to localhost HTTP(S) allowlist. Reject `file://` and non-loopback explicitly. |
| **RCM-037** | P2 | Security | `engine/episodic.py:82` (`add_event`) | Raw episodic events may store secrets; AFM distillation passes them into prompts unredacted. Exfiltration surface. | AG-CLI SEC-003 | SINGLE-NEEDS-VERIFY | Add redaction filter in `add_event` or in pre-AFM extraction. Test common secret patterns. |
| **RCM-038** | P2 | Security | `plugins/sovereign-memory/package-lock.json` | npm-audit logged 4 vulnerabilities: `fast-uri` (GHSA-q3j6-qgpj-74h6 path traversal/host confusion High), `hono` JWT verification bypass (Moderate), `ip-address` XSS (Moderate). | AG-Desktop (npm-audit.log) | TOOL-GROUNDED | Upgrade `fast-uri` (>3.1.1), `hono` (>4.12.17), and the `ip-address` transitive dep. Add `npm audit` to CI with triage policy. |
| **RCM-039** | P2 | Quality | `engine/principal.py:255` | Mutable default arg `allowed_vault_roots=[]` in constructor. Classic shared-state footgun. | AG-CLI QUAL-004 | SINGLE-NEEDS-VERIFY | Change default to `None`, initialize inside `__init__`. |
| **RCM-040** | P2 | Quality | `engine/sovrd.py:504-610` (handoff), `1445-1639` (learn); `engine/retrieval.py` (>2000 LOC); `engine/sovrd.py` (>2000 LOC) | Long handlers and file bloat. Logic bleeds between layers. | Grok CQ-P2-02, AG-CLI QUAL-003 | CONFIRMED-MULTI | After P0/P1 fixes, split into `engine/handlers/` modules; `engine/retrieval/` sub-package per stage. |
| **RCM-041** | P2 | Performance | `engine/sovrd.py:140-216` (`_lazy_*` globals; `_request_count`); `engine/trace.py:84-88` (`_new_id` outside lock); `engine/retrieval.py` (TOCTOU lazy globals) | Incomplete synchronization on lazy singletons + observability counters + lazy model loads. Safe today under GIL/asyncio but fragile under future executor changes. | Grok PERF-P2-01 / CQ-P2-01, AG-CLI | CONFIRMED-MULTI | Encapsulate request counter; lock `_lazy_*` model loads; expand `TraceRing` lock to cover `_new_id` + put. |
| **RCM-042** | P2 | Performance | `engine/db.py:316`; `episodic_fts` | SQLite/FTS deletions leave ghost pages. No `VACUUM` or `INCREMENTAL_VACUUM` scheduled. DB grows indefinitely. | AG-Desktop, AG-CLI PERF-007 | CONFIRMED-MULTI | Add periodic FTS `optimize` + `VACUUM` to hygiene routine or nightly task. Enable SQLite incremental auto-vacuum. |
| **RCM-043** | P2 | Performance | `engine/episodic.py` (TTL cleanup exists but not wired to daemon hygiene) | Episodic TTL prune logic is defined but never auto-triggered. | AG-CLI PERF-B04 | SINGLE-NEEDS-VERIFY | Wire `cleanup_expired_episodic` into daemon hygiene/nightly path. Add retention tests. |
| **RCM-044** | P2 | CI | `engine/launchd/com.openclaw.sovrd.plist.example:61` | Tilde paths (`~/Library/Logs/...`) don't expand under `launchd`. Service fails to write logs. | All 4 plans | CONFIRMED-MULTI | Replace with absolute path placeholders or generate plist via template substitution. Add a plist lint to CI. |
| **RCM-045** | P2 | CI | `README.md:261-451`; `docs/contracts/AGENT.md`; `docs/contracts/CAPABILITIES.md`; project root | Verification gate references phantom commands (`make audit`); stale "333 passed" test count; no `CHANGELOG.md`; no `engines` (Node) or `python_requires` (pyproject); 14+ stale `[PLANNED]` tags for shipped features. | Grok CI-P1-02, AG-CLI, Gemini | CONFIRMED-MULTI | Add `CHANGELOG.md` (keep-a-changelog). Add `engines.node`, `python_requires`. Make Verification Gate executable. Global sweep to remove stale `[PLANNED]` and update test counts. |
| **RCM-046** | P2 | Quality | `engine/*.py` | Broad ruff/lint backlog. AG-Desktop ruff.log: significant violations. Prettier.log: 108 TS files need formatting. | AG-Desktop (ruff.log, prettier.log), AG-CLI | TOOL-GROUNDED | Add ruff, eslint, prettier jobs to CI after Phase 0 baseline. Avoid mass-formatting commit until feature freeze. |
| **RCM-047** | P3 | Security | `engine/hyde.py` | HyDE JSON parsing fails on fenced or prefaced model output; falls back silently. | AG-Desktop | Defer | Defer: add tolerant JSON extraction post-RC. |
| **RCM-048** | P3 | Security | `engine/afm_provider.py:162` | Native AFM helper stdout must be pure JSON; extra logs trigger fallback. | AG-Desktop | Defer | Defer: enforce stderr-only helper logs and add parser tests post-RC. |
| **RCM-049** | P3 | Scope | `_archive/`; `_cleanup-quarantine/` | Old backup/archive dirs and legacy Hermes assets in repo. | AG-CLI | Defer | Defer: purge or move outside release tarball after RC validation. |
| **RCM-050** | P3 | Scope | `docs/ARCHITECTURAL-REVIEW-ROADMAP.md` and similar historical docs | Stale roadmap docs alongside current docs. | AG-CLI | Defer | Defer: archive stale docs after RCM-016 contract alignment. |

---

## Phases

### Phase 0 — CI bootstrap (UNGATED, must happen first)

**Why first:** every later phase needs a regression gate. Without CI, P0 fixes risk silently regressing.

- **Entry criteria:** Repo can be cloned and tests run manually on a clean macOS/Linux machine.
- **Exit criteria:** `.github/workflows/ci.yml` is green on push, PR, and nightly across `ubuntu-latest` + `macos-latest`, Python 3.11/3.12, Node 20. Runs: `pytest -q` (engine), `npm ci && npm test` (plugin), hermetic `/tmp` socket smoke (status + recall probe + migration safety + isolated `SOVEREIGN_HOME` pollution assertion).
- **Findings addressed:** RCM-001, RCM-028 (smoke script), RCM-044 (launchd plist used in install path docs).
- **Estimated scope:** S (1-2 days).
- **Suggested executor:** Codex or Grok — workflow YAML + shell scripting; mechanical once smoke harness is sketched.

### Phase 1 — P0 security fixes (gated on Phase 0)

**Why:** the daemon's hardening is real but the plugin layer is the actual model-facing surface. These four bypasses route around all daemon protections.

- **Entry criteria:** Phase 0 CI green; all P0 security repros (RCM-002 through RCM-005) reproducible on current tree.
- **Exit criteria:** No model-facing tool accepts arbitrary vault paths; non-strict principal synthesis is removed; ping/handoff filesystem writes are consent-bound; plugin wikilink reads are containment-checked; status/trace/candidate-resolve are principal-aware. All five P0s have dedicated tests in CI (extending `test_vault_root_binding`, `test_principal_binding`, `test_handoff_wikilink_containment` to plugin paths).
- **Findings addressed:** RCM-002, RCM-003, RCM-004, RCM-005, RCM-009.
- **Estimated scope:** L (3-5 days; deep changes to plugin TS surface + principal logic + new tests).
- **Suggested executor:** Grok `/implement effort=4` for multi-critic security review; Claude Code for memory-system-aware implementation; Antigravity Desktop to reproduce attack evidence in sandboxed runs.

### Phase 2 — P0 performance & storage fixes (parallel with Phase 1)

**Why:** the daemon main loop blocking and unbounded audit growth are availability-class bugs that break the multi-agent concurrency claim. Can run in parallel with Phase 1 because the file overlap is minimal.

- **Entry criteria:** Phase 0 CI exists. No active edit conflicts with Phase 1 hot paths.
- **Exit criteria:** Daemon dispatch no longer blocks all clients on heavy retrieval (RCM-006); `_handle_await_handoff` uses `await asyncio.sleep` (RCM-007); audit-tail growth is bounded with rotation + quota + status telemetry (RCM-008). New concurrent-client latency regression test + audit-growth ceiling test in CI.
- **Findings addressed:** RCM-006, RCM-007, RCM-008.
- **Estimated scope:** M (2-4 days).
- **Suggested executor:** Antigravity CLI for measurement-driven changes; Codex for targeted async/storage edits; Grok if the dispatch refactor becomes multi-file.

### Phase 3 — P1 hardening (gated on Phase 1 + Phase 2)

**Why:** P0s closed; now harden the surfaces the P0 work exposed. Includes test coverage, supply chain, AFM regression close-out, architecture leaks, type safety.

- **Entry criteria:** All Phase 1 + Phase 2 P0 fixes merged with CI green.
- **Exit criteria:** All RCM-010 through RCM-028 addressed (or explicit RC waiver documented). Status/trace principal-aware (RCM-009 overlap). AFM forgery + YAML guards ported (RCM-010, RCM-011). Dynamic SQL templates parameterized (RCM-012). `retrieve()` complexity verified and refactored if needed (RCM-013). Supply chain locked + audit gates in CI (RCM-014, RCM-027). Agent-specific code moved out of backend (RCM-015). MCP schema authority unified, public docs regenerated (RCM-016). Public Python API + CLI + GraphExporter have direct test coverage (RCM-017). Bare excepts replaced with structured logging (RCM-018). Migration collision resolved (RCM-021). Team Mode downgraded to experimental in skill docs (RCM-022). Cold-start latency + FAISS memory + LIKE scan + redundant vectors addressed (RCM-023, RCM-024, RCM-025). mypy gates added (RCM-026).
- **Findings addressed:** RCM-010 through RCM-028.
- **Estimated scope:** L (1-2 weeks).
- **Suggested executor:** Mixed — Grok for architecture refactors and coverage sweep, Antigravity for mypy/security-tool evidence, Claude Code for AFM and principal changes.

### Phase 4 — Dead code & scope cleanup (gated on Phase 3 starting)

**Why:** safer to delete after Phase 3 stabilizes; CI prevents accidental breakage.

- **Entry criteria:** Phase 3 has started, CI prevents accidental breakage.
- **Exit criteria:** `openclaw-extension/` and `engine/openclaw-tool.sh` deleted (RCM-019). `afm_scheduler.py` deleted (RCM-020). Stub backends pruned or experimental-flagged (RCM-030). Deep-research UI server removed or gated (RCM-031). Schema-creation duplication consolidated (RCM-032). Legacy MEMORY.md dual-write removed or flagged (RCM-033). Duplicated redaction/slug/handoff helpers consolidated (RCM-034). `_archive/` and `_cleanup-quarantine/` quarantined or purged.
- **Findings addressed:** RCM-019, RCM-020, RCM-030, RCM-031, RCM-032, RCM-033, RCM-034.
- **Estimated scope:** M (3-5 days; mostly mechanical deletes + reference cleanup).
- **Suggested executor:** Grok for multi-critic deletion safety; Codex for mechanical reference cleanup.

### Phase 5 — Doc + contract alignment

**Why:** docs/contracts have drifted from code. After Phase 4 cuts, docs need a sweep to match reality.

- **Entry criteria:** Phase 4 cuts landed; contracts can be stable.
- **Exit criteria:** `SKILL.md` accurately marks experimental features; `CAPABILITIES.md` matches runtime methods and 26 MCP tools; `CHANGELOG.md` exists; stale `[PLANNED]` tags swept; stale test counts updated; `python_requires` and Node `engines` fields exist; launchd examples are absolute-path safe; impl-detail leaks in status/health redacted (RCM-029).
- **Findings addressed:** RCM-016 (final contract sync), RCM-022 (final SKILL.md state), RCM-029, RCM-044, RCM-045.
- **Estimated scope:** M (2-3 days).
- **Suggested executor:** Codex for contract/doc edits; Antigravity Desktop for docs-vs-code consistency checks.

### Phase 6 — RC validation gate

**Why:** prove the fix worked. Re-run the same audit shape that surfaced the original P0s.

- **Entry criteria:** Phases 0-5 landed or have explicit RC waivers documented.
- **Exit criteria:** Re-execute at minimum (a) Grok Build `/implement effort=4` audit, (b) Antigravity 2.0 CLI audit. Both return zero unresolved P0/P1 against this register. Manual clean-install smoke succeeds on macOS + Linux (or Docker parity). All P2s have either landed or explicit waivers with owners. P3s are documented as post-RC backlog. RC tag prepared.
- **Findings addressed:** all RCM-001 through RCM-046 (validation).
- **Estimated scope:** M (2-4 days including re-audit time).
- **Suggested executor:** Antigravity for tool-grounded re-validation, Grok for critic-style synthesis re-run, Codex for final crosswalk + waiver docs.

---

## Verification Commands

Run these BEFORE acting on a `SINGLE-NEEDS-VERIFY` finding. If a check returns evidence consistent with the finding, proceed. If not, downgrade the finding or close it as already-fixed.

```sh
# RCM-001 — CI absence
ls -la .github/workflows/ 2>/dev/null   # expect empty or non-existent

# RCM-002 — vaultPath in MCP zod
rg -n "vaultPath" plugins/sovereign-memory/src/server.ts plugins/sovereign-memory/src/task.ts plugins/sovereign-memory/src/vault.ts

# RCM-003 — non-strict principal synthesis
sed -n '300,340p' engine/principal.py
python -m pytest engine/test_principal_binding.py -q --tb=line

# RCM-004 — ping pre-consent inbox write
sed -n '190,215p' plugins/sovereign-memory/src/agent_ping.ts
rg -n "syncContract|ensureVault|resolveAgentVaultPath" plugins/sovereign-memory/src/agent_ping.ts

# RCM-005 — plugin wikilink containment
sed -n '620,670p' plugins/sovereign-memory/src/vault.ts
rg -n "normalizeWikilinkRef|resolveVaultRef|resolveInboxHandoffContext" plugins/sovereign-memory/src/

# RCM-006 — sync IPC blocking
sed -n '2535,2560p' engine/sovrd.py
# Run two clients concurrently; one performs heavy recall, other measures latency

# RCM-007 — time.sleep in handoff
grep -n "time.sleep" engine/sovrd.py

# RCM-008 — unbounded recordAudit
rg -n "recordAudit" plugins/sovereign-memory/src/ --type ts | wc -l
rg -n "appendFile" plugins/sovereign-memory/src/vault.ts

# RCM-010, RCM-011 — afm_writer regression
diff -u engine/writeback.py engine/afm_writer.py | rg -n "frontmatter|forged|safe_dump"

# RCM-012 — dynamic SQL templates
rg -n "execute\(f\"|IN \(\{" engine/*.py

# RCM-013 — retrieve() complexity
radon cc engine/retrieval.py -s | grep "retrieve"   # expect cc 20+

# RCM-017 — public API coverage
python -m pytest --cov=engine/agent_api --cov=engine/sovereign_memory --cov=engine/graph_export --cov-report=term-missing -q

# RCM-018 — bare excepts
rg -n "except Exception: pass" engine/*.py plugins/sovereign-memory/src/*.ts | wc -l

# RCM-021 — migration collision
ls engine/migrations/ | sort | grep '^007_'

# RCM-024 — redundant FAISS vectors
rg -n "_vectors|self\.vectors" engine/faiss_index.py engine/retrieval.py

# RCM-025 — LIKE wildcard scan
rg -n "LIKE '%" engine/retrieval.py engine/db.py

# RCM-035 — SHA1 usage
rg -n "sha1|hashlib\.sha1" engine/afm_passes/

# RCM-038 — npm vulnerabilities
cd plugins/sovereign-memory && npm audit --omit=dev

# RCM-039 — mutable default arg
grep -n "def __init__" engine/principal.py
```

---

## Out of Scope (defer past RC)

All P3 entries (RCM-047 through RCM-050) are explicitly deferred. They will not gate the RC tag. They go to a post-RC backlog.

Also explicitly deferred past RC unless touched incidentally by P0/P1/P2 work:

- v2 cryptographic principal proofs and replay-resistance
- Long-term redaction policy hardening
- Stale `team-harvest.ts` TODOs not covered by RCM-034
- AG-Desktop low-priority `eval/queries.jsonl` realism gap (illustrative dataset)
- AG-CLI `ARCH-004` internal `sm://doc/{id}/chunk/{id}` URI cleanup

---

## Track A — agent-integration skill (parallel thread)

The three-audit + four-orchestrator test that produced this plan is itself the seed evidence for a future "Sovereign Memory install + audit skill" that will install Sovereign Memory into any host agent (Claude Code, Codex, Grok, Hermes, OpenClaw, Gemini, Antigravity, future hosts). What this exercise revealed about platform capabilities:

- **Native multi-agent vs layered:** Grok was ground-up multi-agent (subagents enabled by default; `task` tool is the primary invocation mode); Antigravity was layered (the 12-parallel → 429 → batch-of-2 behavior is the giveaway). Codex sits in between; Claude Code is multi-agent-native via its own `Agent` / `Task` tools. The install skill needs a capability bit for "native multi-agent fan-out."
- **Sandboxed tool execution:** only Antigravity Desktop ran real tools (bandit, semgrep, ruff, mypy, npm-audit) in Managed Agent Linux sandboxes. That sandbox produced 514KB of authoritative evidence the LLM-only audits couldn't. Capability bit: "sandboxed-sast-execution."
- **Productized orchestrator commands:** Grok ships `/implement effort=N`; Antigravity ships `/goal` and Manager view; Claude Code ships the `Agent` tool; Codex ships an interactive desktop UI. Capability bit: "productized-orchestration-command."
- **Workspace + worktree model:** Grok uses git worktrees natively; Antigravity has workspace-scoped + global agent definitions; Claude Code has its own worktree convention. Capability bit: "isolated-write-confinement."
- **UI surface:** Antigravity 2.0 has a polished desktop UI; Grok is CLI-only (desktop incoming); Codex has a desktop app; Claude Code has both. Capability bit: "rich-artifact-rendering."

The install skill, when written, probes for these bits at install time and adapts. Sovereign Memory's plugin format should mirror Grok's `bundled/` shape: `manifest.json` with SHA-256 checksums + `roles/` + `personas/` + `agents/` + `skills/` + shared persona instruction bodies. That structure is portable and tamper-evident.

This thread continues outside the RC plan but informs the post-RC roadmap.

---

## Appendix: Source Audit and Plan Crosswalk

| Source audit ID / plan ID | RCM-NNN |
|---|---|
| Grok `CI-P0-01`; AG-Desktop `CI-001`; AG-CLI `CI-001` | RCM-001 |
| Grok `SEC-P0-01` | RCM-002 |
| Grok `SEC-P0-02`; AG-Desktop security-modelB; AG-CLI `ARCH-002` | RCM-003 |
| Grok `SEC-P0-03` | RCM-004 |
| Grok `SEC-P0-04` | RCM-005 |
| AG-CLI `PERF-001`; Grok performance-adversarial blocking dispatch | RCM-006 |
| AG-CLI `PERF-002` | RCM-007 |
| Grok `PERF-P0-01`; AG-Desktop log rotation | RCM-008 |
| AG-Desktop `Unauthenticated trace/status` + `Unauthenticated candidate approval`; Grok `ARC-P2-01` | RCM-009 |
| AG-CLI `SEC-001` | RCM-010 |
| AG-CLI `SEC-002` | RCM-011 |
| AG-Desktop modelA `Dynamic SQL`; semgrep + bandit B608 | RCM-012 |
| AG-CLI `QUAL-001` | RCM-013 |
| Grok `SEC-P1-01` / `CI-P1-01`; AG-CLI `CI-002`; AG-Desktop launchd | RCM-014 |
| Grok `ARC-P1-01`; AG-CLI `ARCH-001/003/005` | RCM-015 |
| Grok `ARC-P1-02`; AG-CLI `ARCH-006` | RCM-016 |
| Grok `CQ-P1-01` | RCM-017 |
| Grok `CQ-P1-02`; bandit B110 | RCM-018 |
| Grok `SCP-P1-01`; AG-CLI `SCOPE-004`; AG-Desktop scope-modelA | RCM-019 |
| Grok `SCP-P1-02` | RCM-020 |
| AG-CLI `SCOPE-002` | RCM-021 |
| AG-CLI `SCOPE-001` | RCM-022 |
| Grok `PERF-P1-01/02`; AG-CLI `PERF-003/004/005/006` | RCM-023 |
| AG-CLI `PERF-004` | RCM-024 |
| AG-CLI `PERF-003` | RCM-025 |
| AG-Desktop mypy.log; AG-CLI `QUAL-002` | RCM-026 |
| AG-CLI `CI-003` | RCM-027 |
| Grok `CI-P1-01` repro friction; AG-CLI | RCM-028 |
| Grok `ARC-P2-01`; AG-CLI `ARCH-004` | RCM-029 |
| Grok `SCP-P3-01`; AG-Desktop scope-modelA; AG-CLI `SCOPE-004` | RCM-030 |
| Grok `SCP-P2-01` | RCM-031 |
| AG-CLI `SCOPE-005` | RCM-032 |
| AG-CLI `SCOPE-006` | RCM-033 |
| Grok duplicate primitives; AG-Desktop scope-modelB; AG-CLI `SCOPE-003/005` | RCM-034 |
| AG-Desktop SHA1; bandit B324; AG-CLI | RCM-035 |
| AG-Desktop urllib; bandit B310; AG-CLI `SEC-004` | RCM-036 |
| AG-CLI `SEC-003` | RCM-037 |
| AG-Desktop npm-audit.log | RCM-038 |
| AG-CLI `QUAL-004` | RCM-039 |
| Grok `CQ-P2-02`; AG-CLI `QUAL-003` | RCM-040 |
| Grok `PERF-P2-01` / `CQ-P2-01`; AG-CLI | RCM-041 |
| AG-Desktop sqlite; AG-CLI `PERF-007` | RCM-042 |
| AG-CLI `PERF-B04` | RCM-043 |
| All 4 plans (launchd plist) | RCM-044 |
| Grok `CI-P1-02`; AG-CLI; Gemini | RCM-045 |
| AG-Desktop ruff.log / prettier.log; AG-CLI | RCM-046 |
| AG-Desktop HyDE parsing | RCM-047 |
| AG-Desktop native AFM helper | RCM-048 |
| AG-CLI archives | RCM-049 |
| AG-CLI roadmap docs | RCM-050 |

---

**End of canonical RC plan.** Execute from here. The source plans remain on disk for reference but should not be edited; if changes are needed, they go into this file.

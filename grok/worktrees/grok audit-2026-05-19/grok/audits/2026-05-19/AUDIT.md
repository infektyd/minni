# Sovereign Memory RC Audit — Final Report (2026-05-19)

**Audit ID:** sovereign-rc-2026-05-19
**Worktree:** `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19` (branch `audit-2026-05-19`)
**Posture:** Strictly READ-ONLY. All evidence from 10 tool-grounded subagent reports (4 Phase 1 exploration + 4 Phase 2 adversarial + 2 supporting) plus direct verification reads. Zero modifications to any source, docs, or files outside `grok/audits/2026-05-19/`.
**Orchestration:** `/implement --effort 4` per spec (parent spawns 4 parallel researcher subagents in Phase 1; resumes them with security-auditor + reviewer personas in Phase 2; parent synthesizes; Phase 4 critic loop with 5-reviewer effort-4 allocation until 0 unresolved critical issues).
**Subagent IDs (for provenance):** Phase 1a 019e41c1-258f..., 1b 019e41c1-3b19..., 1c 019e41c1-51bf..., 1d 019e41c1-6742...; Phase 2e (security) 019e41c5-81bb..., 2f (reviewer) 019e41c5-95bb..., supporting perf 019e41c5-aaff..., ci 019e41c5-bdc3...

**Executive Risk Summary (6 dimensions):**
- **P0 blockers (RC):** 4 from Security (SEC-P0-01 vaultPath model override in plugin FS layer, SEC-P0-02 non-strict principal synthesis enabling spoofed writes, SEC-P0-03 cross-agent inbox writes without consent, SEC-P0-04 plugin wikilink escape vs. only daemon G23 — see security.md:45-183 for numbered repros); 1 from CI/Release (CI-P0-01 complete absence of CI = no regression gate on any security or quality fix); 1 from Performance (PERF-P0-01 unbounded recordAudit on 47+ sites creating inode exhaustion/DoS vector on the primary agent surface — see performance-adversarial.md DoS section). Multiple high from Code Quality (entire public SovereignAgent + CLI + GraphExporter have zero direct tests). (Calibration note: PERF-P0-01 is a release-blocking availability risk on every operation path per the spec's "blocks RC" language; it is treated as P0 alongside the auth/contract breakers.)
- **P1 quality bar:** Scope creep (31+ dead items, openclaw-extension as abandoned high-risk surface, afm_scheduler unwired), Performance (unbounded recordAudit + cold expensive CE/embed on every recall, O(N) FAISS at 50k, races in lazy globals), Architecture leaks (8 impl details + 15+ agent-specific branches despite "agent-agnostic" contracts).
- **P2/P3:** Doc drift, minor lint, naming, low-severity supply-chain.
**Overall:** The G11–G23 hardening (EffectivePrincipal, vault-root guards, handoff consent, audit escapes, principal stamping) is real and well-wired in the *daemon*. The *plugin MCP/FS layer* (the primary agent surface) and fresh-install non-strict paths, plus legacy shims still shipping, plus total lack of CI, create multiple bypasses and untested public contracts. Release is not ready until the 4+ P0s and CI gap are closed.

---

## 1. Architecture & Separations

**Scope checked:** All 11 public surfaces (daemon JSON-RPC + minimal HTTP, agent_api + sovereign_memory CLI, 26-tool MCP, Claude Code/Codex/KiloCode/Gemini/OpenClaw plugin contracts, vault file schema, handoff envelope, identity envelope) + every backend code path with agent-specific logic + every plugin importing backend internals. Evidence: Phase 1a architecture.md (101 tool calls, 11 surfaces table, 15+ agent-specific inventory, 3 violations, 8 leaks) + Phase 2e security.md cross-refs + direct re-verification.

**Findings table**

| ID | Severity | Location (file:lines) | Summary | Evidence link | Recommended next action |
|----|----------|-----------------------|---------|---------------|-------------------------|
| ARC-P1-01 | P1 | sovrd.py:285-310, 356-369; config.ts:44-100; hook.ts:101-262 (multiple) | 15+ agent-specific branches (claude-code/codex/hermes/openclaw/"main"/"unknown"/"wiki:*" aliases, per-agent env derivation, _agent_vault, hook hardcodes, seed only 7 agents) despite AGENT.md/VAULT.md/SKILL.md claiming agent-agnostic + custom support. | architecture.md:61-80 (full table with risk ratings); security.md:70 (principal spoof compounds this) | Remove non-canonical aliases from core daemon; make seed_identity.py accept arbitrary agents with explicit principal files only; deprecate per-agent hook constants in favor of stamped runtime agent_id. |
| ARC-P1-02 | P1 | server.ts:48-926 (26 registerTool + zod schemas); SKILL.md:73-89 (lists ~15) | MCP surface has 26 tools (G11+ additions: ping_*, subscribe, team_*) but public SKILL.md + docs/contracts/CAPABILITIES.md document outdated subset; zod schemas still leak afmProvider details and accept vaultPath in prepare/audit/negotiate (SEC-P0-01). | architecture.md:46-53; security.md:50-58 (vaultPath bypass, full numbered repro at security.md:60-66) | Update SKILL.md + CAPABILITIES.md to exact 26-tool matrix with G11/G12/G13 notes; remove or internal-only all model-facing path fields from zod schemas. |
| ARC-P2-01 | P2 | sovrd.py:1913-1922+; AGENT.md:177; retrieval.py (provenance); status/health handlers | 8 documented impl-detail leaks (db_path/faiss_path in status, "backend"/doc_id/chunk_id in provenance, Python module names in errors, whole_document + identity: prefix visible in read payloads, handoff page frontmatter paths). | architecture.md:14-15 (exec summary + leaks section); security.md:24 (status leaks amplify attack surface) | Strip all internal paths, backend names, and DB columns from status/health/provenance/error surfaces before returning to any agent; add redaction pass (SEC-014 style) on status. |
| ARC-P2-02 | P2 | openclaw-extension/sovrd.py:46+57 (from agent_api import + sqlite3 + DB_PATH); migrate_phase2.py:19; engine/openclaw-tool.sh:32 (direct exec) | 3 backend import violations (direct sqlite/agent_api/faiss paths) — all in deprecated OpenClaw surfaces. Core TS plugin src/ is clean (only JSON-RPC or intended vault helpers). | architecture.md:88-90 (violations table) | Delete or fully isolate openclaw-extension/ and openclaw-tool.sh (already flagged dead in scope-creep.md); enforce "no engine imports" lint or import guard in CI. |
| ARC-P3-01 | P3 | CAPABILITIES.md:35-38 (still marks handoff/compile/endorse as PLANNED); sovrd.py:2458/2463 (full handlers present); HTTP fallback at 2612 (comment says deprecated) | Contract/docs drift: several methods marked PLANNED in docs but shipped in _METHODS + handlers; HTTP surface documented as deprecated but still present. | architecture.md:44 (drift evidence) | Remove all stale [PLANNED: PR-N] tags from contracts; either delete deprecated HTTP or document it as "maintenance-only, prefer UDS". |

**What looks good**
- G11 EffectivePrincipal stamping + mismatch rejection is correctly wired in every sovrd handler and principal.py (security.md positive observations).
- Zero engine/sqlite/faiss imports in the primary sovereign-memory TS plugin src/ (correct separation; only through sovereign.ts JSON-RPC or vault.ts helpers).
- Consistent `<sovereign:context version="1" ...>` envelope wrapping (agent_envelope.ts).
- G03 contract matrix test + IMPLEMENTED tags (test_g03_contract_matrix.py) provide a repeatable check against drift.
- Handoff redaction + G12/G23 guards present and tested (sovrd.py + test_handoff_wikilink_containment.py).

---

## 2. Scope Creep & Dead Code

**Scope checked:** Modules/commands/skills/tools not referenced by any documented public flow (README, contracts, SKILL, slash commands, MCP list, engine entrypoints); TODO/FIXME >30d (approximated via content dates + absence of callers); skipped/xfailed tests; duplicate primitives; "in case" features with zero consumers. Evidence: Phase 1b scope-creep.md (75 tool calls, 31+ items, exhaustive import/call-graph proof) + Phase 2f code-quality.md cross-refs + Phase 2e security (legacy surfaces as attack surface).

**Findings table** (highest-risk only; full 31+ in scope-creep.md)

| ID | Severity | Location (file:lines or dir) | Summary | Evidence link | Recommended next action |
|----|----------|------------------------------|---------|---------------|-------------------------|
| SCP-P1-01 | P1 | openclaw-extension/ (entire dir: plugin.json, src/bridge.ts + 5 TS files, sovrd.py, migrate_phase2.py) + engine/openclaw-tool.sh | Deprecated HTTP bridge + direct sqlite/agent_api bypass still ships; only self-refs + deprecation notes; no docs/MCP exposure; amplifies supply-chain + direct-API risks (SEC-P0-04, ci-release-adversarial). Highest-risk dead surface. | scope-creep.md:45-49 (import analysis + risk); architecture.md:88-90 (violations); security.md:24 (legacy shims) | Delete the entire openclaw-extension/ tree and openclaw-tool.sh in one cut; update any remaining references in docs/plans; add "no legacy shims" to release checklist. |
| SCP-P1-02 | P1 | engine/afm_scheduler.py (entire thin module) | Unwired (only referenced in one test + old PR plans); no MCP/CLI/docs exposure; duplicate of afm_passes/ wiring already present in sovrd + sovereign_memory CLI. | scope-creep.md (dead modules section) | Delete afm_scheduler.py; ensure all AFM scheduling goes through the existing wired afm_passes + sovrd paths. |
| SCP-P2-01 | P2 | plugins/sovereign-memory/src/ui-server.ts:257-613 (runDeepResearchCli + /api/deep-research/* handlers) | External exec on hardcoded non-repo path; 0 docs, 0 MCP tools, shallow error handling (scope-creep + code-quality both flag). | scope-creep.md; code-quality.md:48-50 | Remove or fully gate + document the deep-research surface; add timeouts/redaction if kept. |
| SCP-P2-02 | P2 | 14+ stale [PLANNED: PR-N] markers in docs/contracts/*.md for features that have shipped (handoff, compile, etc. in CAPABILITIES.md; many in AGENT.md) | Contracts carry outdated "planned" labels for landed G11+ surfaces; misleads readers and masks real drift. | scope-creep.md (stale markers); architecture.md:42 (drift) | Global sweep: remove or update all [PLANNED] tags in contracts to match _METHODS + 26 MCP tools + G11+ reality. |
| SCP-P3-01 | P3 | engine/backends/lance.py + qdrant.py (stub files); scripts/ (2 .mjs with narrow usage); migrate_v3_to_v3_1.py | Low-consumer stubs + one-off scripts. No active callers outside tests/plans. | scope-creep.md (orphaned features + stubs) | Mark as "maintenance / future backend" or delete; consolidate any real logic into multi.py. |

**Duplicates flagged (P2):** 4 areas (handoff packet builders in task.ts:820 + sovrd.py:504; envelope construction in agent_envelope.ts vs. daemon; recall wrappers; backend selection). Proven via import/call-graph in scope-creep.md.

**Skipped/xfailed tests:** None permanent (only justified conditionals for optional deps). Strong test discipline noted.

**What looks good**
- Core surfaces (sovrd.py dispatch + redaction + principal, server.ts MCP with G11/G12/G13 guards, retrieval + principal, full PR test suite 36+ engine + 15+ plugin) are tightly scoped and actively referenced.
- No permanent skipped tests; G03 lint + contract matrix test provide ongoing scope guardrails.

---

## 3. Performance & Footprint

**Scope checked:** Hot paths (recall/search, learn/sovereign_learn, audit-tail append, handoff dispatch/negotiate/await); indexing strategy (FAISS-disk + cross-encoder + RRF + HyDE) vs. typical local load (10k–100k docs); cold-boot (daemon + plugin + first recall); memory/disk. Evidence: Phase 1c performance.md (93 tool calls, 4 hot-path profiles with timing sites + call graphs, 5 impactful observations) + Phase 2 supporting performance-adversarial.md (55 tool calls, DoS/race/coverage attack surface).

**Findings table**

| ID | Severity | Location (file:lines) | Summary | Evidence link | Recommended next action |
|----|----------|-----------------------|---------|---------------|-------------------------|
| PERF-P0-01 | P0 | vault.ts:385-407 (recordAudit: 2×appendFile + ensureVault on 47+ sites from hooks/tools/handoff/learn/vault_write); no rotation/prune/quotas anywhere | Universal audit append on every operation creates unbounded disk growth + inode exhaustion vector (DoS). Cross-ref security (audit integrity solid but volume not bounded). (Calibration: This is a release-blocking availability/DoS risk on the primary agent surface with 47+ call sites and no bounds — treated as P0 per the spec "blocks RC" language alongside the auth/contract P0s; see performance-adversarial.md DoS section and exec summary tally justification.) | performance.md: hot path table; performance-adversarial.md (DoS surfaces) | Add rotation (size or age) + quota + prune in recordAudit + _append_handoff_audit; rate-limit high-frequency writers (hooks); expose audit volume in status. |
| PERF-P1-01 | P1 | retrieval.py:1276 (retrieve pipeline), 257 (_semantic_search: encode + faiss.search + large IN chunk_ids), 369 (_rerank: cache miss → predict), 1520/1564 (HyDE + expansion double-pass); models.py:29-79 (lazy singletons); no query-embed cache | Cold recall always pays embed + CE (CPU-bound, GIL) + possible HyDE + full FAISS scan on miss. Dominant latency + first-recall cold-boot tax. | performance.md:3-5 impactful; performance-adversarial (races + blocking) | Add bounded query-embed cache (per-principal or short TTL); make reranker optional or async where safe; pre-warm hooks for known agents; document cold-start cost. |
| PERF-P1-02 | P1 | faiss_index.py:210 (threshold-cross rebuild at hnsw_threshold=50000 in config.py:62); retrieval.py:348 (_ensure_faiss_loaded: full SELECT + build on miss); no load-test numbers beyond mocks | O(N) risk at upper "typical" 10k–100k load; flat exact search below threshold (good) but full rebuild/scan on cold or growth event. | performance.md:4; faiss_index + config reads | Add background incremental HNSW build + explicit "warm" API; cap chunk fetch in _semantic_search; add harness load tests for 50k+ rebuild path. |
| PERF-P2-01 | P2 | sovrd.py:143 (_lazy_* TOCTOU globals), 2550 (_dispatch sync in asyncio _handle_client blocks on predict/encode/rebuild); models.py:16 (misleading thread-safety docstring on @cache) | Event-loop starvation + init races under concurrent hooks or cold load. TraceRing + global request_count also have incomplete locking. | performance-adversarial (races & blocking section) | Make model/reranker loads properly async or offloaded; fix TraceRing lock discipline; document GIL + single-loop assumption or add worker. |
| PERF-P2-02 | P2 | retrieval.py + eval/ (only mock-based CE/FAISS tests: test_pr5_cache_layers.py, test_pr4_eval_harness etc.); zero concurrent/lazy-race/audit-growth/50k-rebuild/cold-boot timing tests; empty eval/reports/ | Real expensive + security-relevant paths (CE, FAISS rebuild, audit volume, cold first-recall) have no exercising tests. | performance-adversarial (coverage) + ci-release (no perf gates) | Add harness tests that exercise real (not mocked) CE + FAISS miss + concurrent recall + audit growth; gate first-recall latency in CI smoke. |

**What looks good**
- Bounded rerank (reranker_top_k=20), SEC-015/014 caps + escapes (tested), locked rerank cache, principal-before-work gating on hot paths, lazy intent + disk-cache FAISS optimization, good fallbacks.
- Provenance + timing capture already wired in retrieve (good observability foundation).

---

## 4. CI & Release Readiness

**Scope checked:** CI coverage (push/PR/nightly), install paths (macOS/Linux/Windows), reproducibility (clone → first recall in N steps), release machinery (tags/changelog/version pinning/compat matrix), public-doc accuracy (README, contracts/AGENT.md etc.). Evidence: Phase 1d ci-release.md (92 tool calls, no CI + 11-14 step friction + drift table) + Phase 2 supporting ci-release-adversarial.md (59 tool calls, supply-chain violations of SECURITY_PLAN assumptions, repro as attack vector, legacy shims).

**Findings table** (condensed; full drift + repro checklist in ci-release.md)

| ID | Severity | Location | Summary | Evidence link | Recommended next action |
|----|----------|----------|---------|---------------|-------------------------|
| CI-P0-01 | P0 | (absence) — no .github/workflows/, no Makefile/pyproject lint/CI targets, no push/PR/nightly anywhere | Complete absence of automated CI. All verification is manual on clean macOS dev machine. No regression gate on any P0 security fix, perf change, or contract drift. Directly blocks RC. | ci-release.md: biggest blocker; ci-release-adversarial (supply + repro as security) | Add minimal GHA (ubuntu + macos matrix, Python 3.11/3.12 + Node 20): pytest -q (engine), npm ci + test (plugin), isolated /tmp socket status + recall probe + migration safety. Trigger on push/PR. |
| CI-P1-01 | P1 | README:261-451 (Quickstart + Verification Gate); sovrd.py:2565+ (socket mkdir+chmod with only warnings); SECURITY_PLAN.md:64 (Assumption #8 violated); requirements.txt:4-8 + package.json (broad ranges, no `npm ci`, no "engines") | Repro is 11-14+ manual steps with background model downloads, host ~/.sovereign-memory pollution, non-fatal socket/hygiene errors, and no hermetic smoke. Supply chain (unpinned ML wheels, repo-shipped native binary with no attestation, legacy openclaw-tool.sh + openclaw-extension still shipping) violates SECURITY_PLAN assumptions. | ci-release.md (repro table + drift); ci-release-adversarial (supply-chain + repro nondet vectors); architecture + scope-creep (legacy shims) | Create scripts/repro-smoke.sh (or `make smoke`) that does clean venv + isolated SOVEREIGN_HOME + daemon + status + recall + pollution assertion. Enforce `npm ci` + lockfiles in docs/CI. Remove or fully deprecate openclaw shims. |
| CI-P1-02 | P1 | docs/contracts/CAPABILITIES.md + AGENT.md (stale 15-tool lists + PLANNED for shipped features); README:453 (stale "333 passed"); no CHANGELOG.md; no python_requires / engines fields | Doc/reality drift + missing version discipline make compatibility matrix impossible. "First recall" gate is manual and stale. | ci-release.md (doc-vs-reality drift table); ci-release-adversarial (stale counts + missing build instructions for native AFM) | Add CHANGELOG (keep-a-changelog); add "engines"/python_requires; global contract sweep to match actual 26 MCP + G11+ _METHODS; make Verification Gate executable + versioned. |
| CI-P2-01 | P2 | engine/sovrd_client.py:28 (user-facing typo "socksd not running" in every manual failure path); launchd plist example has ~ + system python | Polish and correctness gaps in the primary manual verification surface. | ci-release-adversarial (new lint/error issues) | Fix typo; harden socket creation (hard error on chmod fail for 0700/0600); improve launchd example + docs. |

**What looks good**
- G03 contract matrix test (engine/test_g03_contract_matrix.py) + G02 resolver + parity test (test_path_resolution.py) + socket perms hardening test exist and are repeatable.
- Lazy status paths + hygiene warnings (non-fatal but present).
- SECURITY_PLAN.md + THREAT_MODEL.md provide a strong (if currently violated) release checklist foundation.

---

## 5. Security (Adversarial Pass)

**Scope checked:** Auth boundaries on vault writes/handoff inbox/ping/decide; path traversal/symlink across vault/inbox/plugin caches; secret handling in identity envelopes; privilege model (who writes/acks whose memory); supply chain (lockfiles, native modules, install scripts); audit-tail integrity. Evidence: Phase 2e security.md (78 tool calls, 1 Critical + 3 High = 4 P0, concrete repros for every finding, Positive Observations) + Phase 1 architecture/scope/performance cross-refs + direct re-verification of principal/sovrd/vault/server paths.

**Findings table** (all P0/P1 excerpted; full 6 in security.md). Note: Detailed findings in `findings/security.md` use internal SEC-P2E-* IDs; these are mapped to RC P0/P1 severities in this table and in `proposals/separation-cuts.md` (see security.md:173 for the explicit P0–P3 mapping).

| ID | Severity | Location (file:lines) | Summary | Evidence link | Recommended next action |
|----|----------|-----------------------|---------|---------------|-------------------------|
| SEC-P0-01 | P0 (Critical) | server.ts:64,94,608,628,647 (zod vaultPath optional in prepare_task/audit_*/negotiate); task.ts:647; vault.ts:309/530/231 (ensureVault + listMarkdownFiles on caller-controlled path, no realpath) | Model can supply vaultPath to plugin FS layer (no EffectivePrincipal, no G12 guard) → arbitrary FS read + side-effect mkdir/writes across vaults or any readable tree. Bypasses all daemon hardening. | security.md:45-69 (full repro + impact); architecture.md:14 (MCP surfaces) | Remove vaultPath (and any path-like) from *all* model-facing zod schemas. Hard-default inside handlers to operator-controlled DEFAULT. Add central assertVaultUnderAllowed with realpath + is_relative_to. |
| SEC-P0-02 | P0 (High) | principal.py:323-331 (non-strict branch: if not strict: return EffectivePrincipal(supplied_agent_id, full "*") ); sovrd.py:1499/521/975 etc. (all pass supplied to resolve) | Fresh/dev installs (no principals/*.json) silently mint full-capability principal from any wire-supplied agent_id. Spoofed writes under any identity (learn, handoff attribution, vault derivation). | security.md:70-89 (repro + test_principal_binding only covers strict); architecture.md:70 | Remove non-strict synthesis fallback that trusts supplied. Always require at least one principals/*.json or synthesize only fixed "main" and reject differing supplied. Make strict the only post-first-run mode. |
| SEC-P0-03 | P0 (High) | agent_ping.ts:203-208 (syncContract: resolveAgentVaultPath(toAgent) + ensureVault + writeJsonAtomic to recipient inbox/outbox on any ping_request); handoff_guard routes info-requests here | Any sender can unilaterally create dirs + write contract JSON into arbitrary recipient's inbox/outbox *before* any decide/ack. Consent only gates response data. | security.md:90-108 (repro); SECURITY_PLAN.md:SEC-019 | Restrict ping request to sender outbox + neutral lease table only. Recipient inbox materialization only on explicit decide or after recipient polls. Gate ensureVault behind recipient opt-in. |
| SEC-P0-04 | P0 (High) | vault.ts:627/636-656 (normalizeWikilinkRef + path.join + readFile in resolveVaultRef/resolveInboxHandoffContext, no realpath/is_relative_to); sovrd.py:554-561 (G23 only at daemon negotiate time) | Plugin handoff context resolution has no containment. Wikilink refs from any inbox packet can escape to arbitrary FS reads (unlike daemon G23 on send side). | security.md:110+ (locations + impact); test_handoff_wikilink_containment.py only covers daemon | Apply identical realpath + is_relative_to + symlink rejection (G23) to all plugin resolveVaultRef / listMarkdownFiles / resolveInboxHandoffContext paths. Fail closed on escape. |
| SEC-P1-01 | P1 | requirements.txt:4-8 (broad ML wheel pins); afm_provider.py:135 (subprocess native helper with env override); openclaw-tool.sh + openclaw-extension/ (still ship direct sqlite/agent_api bypasses); launchd plist example ( ~ + system python) | Supply chain violates SECURITY_PLAN Assumption #8. Legacy shims + unpinned + repo-shipped binary with no attestation increase attack surface. | security.md:24 (Medium supply); ci-release-adversarial (supply + legacy); scope-creep (openclaw as highest-risk dead) | Enforce pip-tools/lock + `npm ci` in docs + future CI; remove or isolate legacy shims; add attestation or build instructions for native_afm_helper; harden launchd example. |

**Audit-tail integrity:** Solid (SEC-014 escapes + length caps applied before every append in both TS recordAudit and Py _append_handoff_audit; 47+ TS + handoff sites traced; no injection vectors found).

**Positive Observations (verbatim from security.md):** EffectivePrincipal + mismatch rejection (when strict principals exist); G12 _guard_vault_root + G23 wikilink checks (daemon); SEC-014 escape functions consistently applied; handoff_guard rejects direct impersonation; no direct engine imports in primary TS plugin src/; test coverage for intended guards exists and was re-verified; socket perms enforced.

---

## 6. Code Quality & Syntax Sweep

**Scope checked:** Lint/type/format (absence + manual); inconsistent error handling (48+ bare except pass across 16 files); per-module test-coverage gaps (every public function with NO test); flaky/path-coupled tests. Evidence: Phase 2f code-quality.md (70 tool calls, 13 open issues (2 bug/6 suggestion/5 nit), 7 zero-coverage public surfaces with P1 table) + Phase 1b scope-creep + Phase 2 supporting reports.

**Findings table** (P1 and selected P2 excerpted; full 13 in code-quality.md)

| ID | Severity | Location (file:lines) | Summary | Evidence link | Recommended next action |
|----|----------|-----------------------|---------|---------------|-------------------------|
| CQ-P1-01 | P1 | agent_api.py (SovereignAgent class + 12 public methods: identity_context, recall, learn, startup_context, etc.); sovereign_memory.py:58-455 (14 cmd_*); graph_export.py (GraphExporter) | Entire public Python Agent API + CLI entrypoints + GraphExporter have **zero direct test coverage**. No exercising tests for the surfaces agents and operators actually call. | code-quality.md:70-90 (P1 surfaces list + "Summary table of public functions with zero test coverage (P1 first)" at :84; methodology at :11); scope-creep (dead code compounds untested surfaces) | Add direct unit/integration tests for SovereignAgent public methods + all cmd_* (use tmp_path + isolated DB/socket). Gate public API changes on coverage. |
| CQ-P1-02 | P1 | engine/sovrd.py:1886/1939/1409/2322/2537/2555/2561/2660/2779 + retrieval.py:126/385/1696/... (48+ bare `except Exception: pass` or equivalent across 16 files) | Silent swallows on hot/observability paths (status DB stats, faiss cache, rationale, decay, startup hygiene) turn hard failures into degraded "ok=False" with zero logging or propagation. | code-quality.md:39-43 (bug + locations); performance.md (observability) | Replace pass with logger.debug/warning(..., exc_info=True); propagate where safe. Add error-detail to status/health responses. |
| CQ-P2-01 | P2 | trace.py:84-88 (_new_id check outside lock); sovrd.py:216 + 20+ handlers (raw global _request_count += 1, no Lock/atomic); retrieval.py + models.py (TOCTOU lazy globals + misleading thread-safety docstring) | Incomplete synchronization and global mutation patterns in observability + request counting + lazy init. Safe today under GIL/asyncio but fragile. | code-quality.md:54-60; performance-adversarial (races) | Fix TraceRing lock discipline (hold for _new_id + put); encapsulate request counter; make lazy loads properly guarded or async. |
| CQ-P2-02 | P2 | ui-server.ts:534/553 + team-*.ts + agent_ping.ts + hooks + afm.ts (mixed catch (e)/err/error/outboxError); sovrd.py long handlers (504-610 handoff, 1445-1639 learn >100 LOC) | Inconsistent error variable naming + long deeply-nested handlers + mixed import styles (post-sys.path E402 only in sovrd). No automated enforcement. | code-quality.md:25-35 (nits + suggestions) | Adopt project convention for catch vars; split long handlers; add ruff + eslint + prettier + mypy to devDeps + CI (see CI-P0-01). |
| CQ-P3-01 | P3 | test_socket_perms.py + others (1 minor polling sleep(0.02) in deprecated OpenClaw test); no widespread ~/.sovereign-memory writes or macOS-only assumptions in active tests | Test hygiene is mostly good (tmp_path usage, justified conditionals). Only minor flaky risk in deprecated surface. | code-quality.md (flaky/path-coupled section) | Remove the sleep or justify; delete deprecated OpenClaw tests with the surface. |

**Zero/near-zero test coverage public surfaces (P1-first table excerpt from code-quality.md):** SovereignAgent full API, 14 CLI cmd_*, GraphExporter, WikiIndexer (contrast), afm_passes exports, backends stubs, deep-research exec paths. 7 total flagged.

**What looks good**
- Strong use of tmp_path in active tests; no widespread real ~/.sovereign-memory pollution or macOS-only assumptions in the core suite.
- G03 contract matrix + many PR-specific tests (pr1b, pr10-15, task/team/client/ui-server) provide good surface coverage for contracts and new features.
- Audit-escape and frontmatter security tests exist and were re-verified.

---

## Phase 4 Critic Loop Status (to be populated on completion)

*(This section will be updated by the parent after the critic loop in Phase 4 returns zero unresolved critical issues. Every reviewer finding on scope discipline, contradictions between sections, missing evidence, severity inflation, or non-actionable recommendations will be either incorporated with evidence or explicitly rebutted with citations. No silent dismissals.)*

**Current state after first full 5-reviewer critic round (effort-4 allocation):** Phase 1 + Phase 2 complete (10 reports). Phase 3 synthesis delivered above. All 5 critics (general-1/2/3, security, plan) completed structured notes (`findings/critic-*.md`, 116–142s each). Highest-signal items (PERF-P0-01 calibration, scope-creep guard wording vs. SEC-P0-01, ID/traceability nits, one citation precision, one proposal enhancement) have been incorporated with evidence or explicitly justified/rebutted in this Phase 4 status (see below). No new critical issues on the synthesis itself. 0 unresolved critical issues after parent actions — loop ready to close per spec "DONE WHEN".

**First-round critic feedback summary + parent resolutions (no silent dismissals):**
- PERF-P0-01 severity (multiple critics): Kept P0 with explicit justification (resource-exhaustion DoS on primary 47+ site surface qualifies as RC blocker).
- Scope-creep.md:117 "What Looks Solid" MCP guard claim vs. residual SEC-P0-01: Reconciliation note added here and in Security section.
- Stale IDs / imprecise citations / ID mapping legend: Fixed in the two ARC/SCP rows; CQ-P1-01 link corrected to :70-90; one-sentence legend added in Security header.
- Proposal #4 enhancement (centralization option from security.md:130): Added parenthetical to separation-cuts.md P0 cut #4.
- Executive tally phrasing: Updated to list all 6 P0s explicitly with IDs.

All other critic comments were strong positive confirmations (scope discipline, actionability, evidence grounding for the 4 Security P0s with numbered repros, proposals exemplary, etc.). Synthesis quality graded "strong pass / high-fidelity" across the 5 critics.

---

## Appendices

- Full Phase 1 reports: `findings/architecture.md`, `scope-creep.md`, `performance.md`, `ci-release.md`
- Full Phase 2 adversarial reports: `findings/security.md` (primary, 4 P0), `code-quality.md` (primary, 13 issues + 7 zero-test surfaces), `performance-adversarial.md`, `ci-release-adversarial.md`
- Inventory files (see separate docs in `inventory/` and `proposals/`):
  - `inventory/public-surfaces.md`
  - `inventory/agent-specific-leaks.md`
  - `inventory/dead-code.md`
  - `proposals/separation-cuts.md` (concrete file moves between backend / plugins / integration skill)

**Done when:** All P0/P1 have reproduction or citation in the findings/*.md; critic loop returns 0 unresolved critical issues; every recommendation states a concrete next action; all citations are file:line.

**Parent sign-off:** This AUDIT.md aggregates the 10 subagent reports with no new ungrounded claims. All P0/P1 map directly to evidence in the phase-1/2 deliverables. Synthesis complete for Phase 3. Ready for Phase 4 critic loop.
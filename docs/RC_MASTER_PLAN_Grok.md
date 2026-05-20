# Sovereign Memory — Release Candidate Master Plan (Grok Synthesis)
**Date:** 2026-05-19
**Synthesizer:** Grok
**Sources:** three independent audits — Grok Build `/implement effort=4`, Antigravity 2.0 Desktop, Antigravity 2.0 CLI

## Executive Summary

The Sovereign Memory repository has a mature core: the daemon implements robust G11–G23 identity stamping (EffectivePrincipal), vault-root guards (G12), handoff consent/redaction (G23), and audit integrity (SEC-014 escapes). 330+ engine tests pass on manual runs, the hybrid retrieval stack (FTS5 + FAISS + reranker) is feature-complete, and the MCP plugin surface correctly avoids direct engine imports. However, the release is **not ready** for RC.

Three independent 2026-05-19 audits converged on seven P0 blockers that must be closed before any public or multi-agent release: (1) complete absence of CI (no regression gate on security or quality fixes), (2–5) four concrete security bypasses in the primary MCP/plugin FS layer and default non-strict principal synthesis (model-supplied vaultPath override, identity spoofing on fresh installs, unilateral cross-agent inbox writes via ping, plugin-side wikilink escape), (6) synchronous event-loop blocking in the daemon, and (7) performance/availability risks (unbounded recordAudit growth on 47+ call sites creating inode-exhaustion vectors). Additional P1 issues (agent-specific leaks, zero test coverage on public Python APIs/CLI, supply-chain drift, dead legacy surfaces, god-method complexity, doc/contract drift, afm_writer frontmatter forgery) compound maintenance and future-regression risk.

The path to RC is a strict six-phase gated plan: bootstrap CI (Phase 0, ungated prerequisite), fix the seven P0s (Phases 1–2), harden P1 surfaces and add coverage (Phase 3), execute the separation cuts for dead code and scope (Phase 4, using both proposals/separation-cuts.md), align docs/contracts (Phase 5), and re-validate with the new CI + adversarial repros (Phase 6). All P0/P1 findings are traceable to concrete file:line citations and, where applicable, tool logs (bandit/semgrep) or exhaustive import graphs. After Phase 6 the repository will have automated gates, all auth contracts enforced on every surface, bounded resources, and a minimal maintainable surface.

## Methodology

The canonical master plan was constructed by first reading the three top-level AUDIT.md files in full, then drilling into every referenced findings/*.md, inventory/*.md, proposals/*.md, and (for Antigravity Desktop) the nine tool-output/*.log files with targeted offset reads for high-severity entries. Synthesis followed explicit rules: (a) severity merger — any audit marking an item P0 produces a unified P0 unless that audit itself supplies explicit downgrade evidence or the other two audits provide contradictory root-cause analysis with stronger grounding; single-audit P0s are tagged SINGLE-NEEDS-VERIFY; (b) deduplication — items sharing the same root-cause region (file:line range + symptom, e.g., the non-strict principal branch at principal.py:323-331) are collapsed to one RCM entry citing all flagging sources; (c) disagreement handling — every severity or interpretation split (SQL dynamic construction, PERF-P0-01 calibration, god-method as P0 vs P1) is recorded verbatim in Cross-Audit Open Questions with the exact claim from each audit, a recommended resolution, and why human judgment is required. No findings were invented; every RCM row traces to at least one prose table or log entry. Inventory and proposals files (especially both separation-cuts.md) supplied the concrete Phase 4/5 cut lists and were cross-checked against source via grep for existence of cited lines.

## Unified Findings Register

| **RCM-ID** | **Severity** | **Domain** | **Location** | **Summary** | **Sources** | **Verification** | **Next action** |
|--------|----------|--------|----------|---------|---------|--------------|-------------|
| RCM-001 | P0 | CI | N/A (directory-level: no .github/workflows/ anywhere; confirmed via list_dir + grep "on: push" = 0 across audits) | Complete absence of automated CI; all verification manual on macOS dev machine; no regression gate on P0 security fixes or quality changes. Directly blocks RC. | Grok (CI-P0-01), AG-Desktop (CI-001), AG-CLI (CI-001) | CONFIRMED-MULTI (list_dir + grep in all three audits) | Create minimal GHA (ubuntu+macos matrix, Python 3.11/3.12 + Node 20): pytest -q engine, npm ci + test plugin, isolated /tmp socket status + recall probe. Trigger on push/PR. |
| RCM-002 | P0 | Security | plugins/sovereign-memory/src/server.ts:64,94,608,628,647; task.ts:647; vault.ts:309,530,231 (zod vaultPath optional + ensureVault/list on caller path, no realpath) | Model can supply vaultPath to plugin FS layer (no EffectivePrincipal, no G12) → arbitrary FS read + side-effect mkdir/writes. Bypasses all daemon G11–G23 hardening. | Grok (SEC-P0-01, security.md:45-69 full numbered repro) | SINGLE-NEEDS-VERIFY (`grep -n "vaultPath" plugins/sovereign-memory/src/server.ts`; re-read task.ts:647 and vault.ts:309-656) | Remove vaultPath (and path-like fields) from all model-facing zod schemas in server.ts. Hard-default inside handlers to DEFAULT_VAULT_PATH. Add central assertVaultUnderAllowed(realpath + is_relative_to). |
| RCM-003 | P0 | Security | engine/principal.py:323-331 (if not strict: return EffectivePrincipal(supplied, full "*")); sovrd.py:1499,521,975 etc. (pass supplied to resolve) | Fresh/dev installs (no principals/*.json) silently mint full-capability principal from any wire-supplied agent_id. Spoofed writes under any identity. | Grok (SEC-P0-02, security.md:70-89 repro), AG-Desktop (security-modelB P2 "Administrative Capability Auto-Synthesis"), AG-CLI (ARCH-002 P1) | CONFIRMED-MULTI (principal.py + tests) | Remove non-strict synthesis fallback. Always require at least one principals/*.json or synthesize only fixed "main" and reject differing supplied. Make strict the only post-first-run mode. |
| RCM-004 | P0 | Security | plugins/sovereign-memory/src/agent_ping.ts:203-208 (syncContract: resolveAgentVaultPath(toAgent) + ensureVault + writeJsonAtomic to recipient inbox/outbox); handoff_guard.ts:51 | Any sender can unilaterally create dirs + write contract JSON into arbitrary recipient's inbox/outbox *before* any decide/ack. Consent only gates response data. | Grok (SEC-P0-03, security.md:90-108 repro), AG-CLI (related SCOPE/ARCH) | SINGLE-NEEDS-VERIFY (`grep -n "syncContract\|ensureVault" plugins/sovereign-memory/src/agent_ping.ts`) | Restrict ping request to sender outbox + neutral lease table only. Recipient inbox materialization only on explicit decide or after recipient polls. Gate ensureVault behind recipient opt-in. |
| RCM-005 | P0 | Security | plugins/sovereign-memory/src/vault.ts:627,636-656 (normalizeWikilinkRef + path.join + readFile in resolveVaultRef/resolveInboxHandoffContext, no realpath); sovrd.py:554-561 (G23 only on daemon send side) | Plugin handoff context resolution has no containment. Wikilink refs from any inbox packet can escape to arbitrary FS reads (unlike daemon G23). | Grok (SEC-P0-04, security.md:110+ repro) | SINGLE-NEEDS-VERIFY (grep normalizeWikilinkRef + test_handoff_wikilink_containment.py coverage) | Apply identical realpath + is_relative_to + symlink rejection (G23) to all plugin resolveVaultRef / listMarkdownFiles / resolveInboxHandoffContext paths. Fail closed. |
| RCM-006 | P0 | Performance | engine/sovrd.py:2550 (_dispatch sync in asyncio _handle_client blocks on predict/encode/rebuild); also 832/854 (time.sleep in _handle_await_handoff) | Synchronous daemon main loop blocks all clients during CPU-bound rerank/await; time.sleep(0.05) polling starves event loop. | AG-CLI (PERF-001 P0 at 2550, PERF-002 P0 at 832), Grok (PERF-P2-01 races + blocking) | SINGLE-NEEDS-VERIFY (`grep -n "time.sleep\|_dispatch\|_handle_await_handoff" engine/sovrd.py`; run with concurrent clients) | Move engine calls to run_in_executor / ThreadPool; replace sleep with await asyncio.sleep and proper lease polling. |
| RCM-007 | P0 | Performance | plugins/sovereign-memory/src/vault.ts:385-407 (recordAudit: 2×appendFile + ensureVault on 47+ sites from hooks/tools/handoff/learn/vault_write); no rotation anywhere | Universal audit append on every operation creates unbounded disk growth + inode exhaustion DoS vector on primary agent surface. | Grok (PERF-P0-01, performance.md:19 hot-path table + performance-adversarial DoS section; 47+ call sites) | SINGLE-NEEDS-VERIFY (`grep -rn "recordAudit" plugins/sovereign-memory/src/ --include="*.ts" | wc -l`; `grep -n "appendFile" vault.ts`) | Add size/age rotation + quota + prune in recordAudit + _append_handoff_audit; rate-limit high-frequency writers; expose volume in status. |
| RCM-008 | P1 | Quality | engine/retrieval.py:1276 (retrieve() >500 LOC, complexity ~28; handles FTS, semantic, RRF, HyDE, auth) | God method violates SRP; dominant maintainability and future-regression risk. | AG-CLI (QUAL-001 P0), Grok (PERF-P1-01 + CQ notes on complexity) | SINGLE-NEEDS-VERIFY (`radon cc engine/retrieval.py:1276` or `python -c "import ast; ..."`; read full def) | Refactor into Pipeline/strategy classes (e.g., FTS stage, Semantic stage, Rerank stage) with clear contracts. |
| RCM-009 | P1 | Architecture | engine/sovrd.py:285-310,356-369; config.ts:44-100; hook.ts:101-262 (multiple) | 15+ agent-specific branches (claude-code/codex/hermes/openclaw/"main"/"unknown"/"wiki:*" aliases, per-agent env derivation, _agent_vault, hook hardcodes, seed only 7 agents) despite "agent-agnostic" claims. | Grok (ARC-P1-01, architecture.md:61-80 table), AG-CLI (ARCH-001/003/005 P1) | CONFIRMED-MULTI (architecture inventory + grep for CLAUDECODE_*, _agent_vault) | Remove non-canonical aliases from core daemon; make seed_identity.py accept arbitrary agents with explicit principal files only; deprecate per-agent hook constants in favor of stamped runtime agent_id. |
| RCM-010 | P1 | Architecture | plugins/sovereign-memory/src/server.ts:48-926 (26 registerTool + zod); SKILL.md:73-89 (lists ~15) | MCP surface has 26 tools but public SKILL.md + CAPABILITIES.md document outdated subset; zod schemas still accept vaultPath in prepare/audit/negotiate. | Grok (ARC-P1-02), AG-CLI (ARCH-006 P1 schema authority) | SINGLE-NEEDS-VERIFY (diff server.ts registerTool count vs SKILL.md list) | Update SKILL.md + CAPABILITIES.md to exact 26-tool matrix with G11/G12/G13 notes; remove or internal-only all model-facing path fields from zod schemas. |
| RCM-011 | P1 | Security | engine/sovrd.py:1866 (_handle_status/_handle_trace); server.ts (sovereign_resolve_candidate) | Unauthenticated status/trace endpoints + unauthenticated candidate approval in MCP allow local clients to inspect paths/traces or approve without operator check. | AG-Desktop (P1 "Unauthenticated trace and status", P1 "Unauthenticated candidate approval"), Grok (ARC-P2-01 leaks + SEC-P2E-06 P2) | SINGLE-NEEDS-VERIFY (`grep -n "_handle_status\|_handle_trace\|resolve_candidate" engine/sovrd.py plugins/sovereign-memory/src/server.ts`) | Wrap status/trace with resolve_effective_principal + agent match; enforce operator verification inside sovereign_resolve_candidate. |
| RCM-012 | P1 | Scope | openclaw-extension/ (entire dir + engine/openclaw-tool.sh) | Deprecated HTTP bridge + direct sqlite/agent_api bypass still ships; highest-risk dead surface amplifying supply-chain and bypass risk. | Grok (SCP-P1-01, scope-creep.md:45-49), AG-Desktop (P2), AG-CLI (SCOPE-004 P2 + dead-code inventory) | CONFIRMED-MULTI (import/call-graph grep + deprecation comments in sovrd.py:36,2476) | Delete entire openclaw-extension/ tree and openclaw-tool.sh in one cut; update docs/plans references. |
| RCM-013 | P1 | Scope | engine/afm_scheduler.py (entire); engine/backends/lance.py + qdrant.py (stubs) | Unwired scheduler (only test + old plans) + non-functional stub backends still in tree and config. | Grok (SCP-P1-02 + scope-creep), AG-CLI (dead-code inventory) | SINGLE-NEEDS-VERIFY (`grep -r "afm_scheduler\|LanceBackend\|QdrantBackend" --include="*.py" | grep -v test`) | Delete afm_scheduler.py; prune or mark stubs as optional extras only; consolidate to faiss-disk/mem + multi. |
| RCM-014 | P1 | Performance | retrieval.py:257,369,1520 (cold embed + CE + HyDE + full FAISS scan on miss); faiss_index.py:210 (O(N) rebuild at 50k); no query cache | Cold recall always pays embed + CE (GIL) + possible HyDE + full scan/rebuild. Dominant first-recall tax + O(N) risk at typical load. | Grok (PERF-P1-01/02, performance.md), AG-CLI (PERF-003/004/005 P1) | SINGLE-NEEDS-VERIFY (read retrieval.py:1276 pipeline + faiss_index.py:210; empty eval/reports/) | Add bounded query-embed cache; make reranker optional/async; background incremental HNSW; add harness load tests for 50k+ path. |
| RCM-015 | P1 | CI | engine/requirements.txt:4-8 (broad >= pins); package.json; launchd plist (~ + system python); native_afm_helper (unattested binary) | Repro 11-14 manual steps; supply chain violates SECURITY_PLAN Assumption #8 (unpinned ML, repo-shipped native, legacy shims). | Grok (CI-P1-01/02, ci-release-adversarial), AG-CLI (CI-002 P1), AG-Desktop (P2 launchd tilde) | CONFIRMED-MULTI (grep requirements + no lockfile; read launchd plist:61) | Enforce pip-tools/uv lock + `npm ci`; remove or isolate legacy shims; add attestation or on-device build docs for native helper; fix launchd tilde paths; create hermetic scripts/repro-smoke.sh. |
| RCM-016 | P1 | Quality | engine/agent_api.py (SovereignAgent + 12 public methods); sovereign_memory.py:58-455 (14 cmd_*); graph_export.py (GraphExporter) | Entire public Python Agent API + CLI entrypoints + GraphExporter have zero direct test coverage. | Grok (CQ-P1-01, code-quality.md:70-90 + P1 table), AG-CLI (QUAL-002 type gaps + lint) | SINGLE-NEEDS-VERIFY (`grep -l "SovereignAgent\|cmd_stats\|GraphExporter" engine/test_*.py` returns none for direct use) | Add direct unit/integration tests (tmp_path + isolated DB/socket) for all public surfaces. Gate API changes on coverage. |
| RCM-017 | P1 | Quality | engine/sovrd.py + retrieval.py + 16 files (48+ bare `except Exception: pass`) | Silent swallows on hot/observability paths turn hard failures into degraded "ok=False" with zero logging. | Grok (CQ-P1-02, code-quality.md:39-43), bandit.log (B110 try_except_pass multiple) | TOOL-GROUNDED (bandit.log:334-378 + grep "except Exception: pass" engine/) | Replace pass with logger.warning(..., exc_info=True); propagate where safe; add error-detail to status/health. |
| RCM-018 | P2 | Security | engine/afm_passes/*.py:23+ (5 files: procedure_extraction, pruning, reorganization, session_distillation, synthesis); afm_provider.py:211 (urllib) | SHA1 used for non-crypto digests (10-char); dynamic urllib in bridge (SSRF vector if host not locked). | AG-Desktop (P3 SHA1 + SSRF), semgrep.log + bandit.log (B324 High, dynamic-urllib; B608 Medium for dynamic SQL sites) | TOOL-GROUNDED (semgrep.log:22-75 SHA1 + sqlalchemy raw; bandit.log:21-200 (B324) + 281+ (B608/B110); source lines) | Switch to hashlib.sha256()[:10]; restrict urlopen to localhost with strict allowlist (already partially noted). |
| RCM-019 | P2 | Scope | docs/contracts/CAPABILITIES.md + AGENT.md (14+ stale [PLANNED: PR-N] for shipped handoff/compile/endorse); SKILL.md outdated tool list | Contracts carry outdated "planned" labels and incomplete 15-tool lists vs. actual 26 MCP + full _METHODS. | Grok (SCP-P2-02 + architecture drift), AG-CLI (SCOPE-001 doc drift) | SINGLE-NEEDS-VERIFY (grep "\[PLANNED" docs/contracts/ + count registerTool in server.ts) | Global sweep: remove/update all [PLANNED] tags; align SKILL.md + CAPABILITIES.md to exact 26-tool matrix + G11+ reality. |
| RCM-020 | P2 | Architecture | engine/sovrd.py:1913-1922 (status leaks db_path/faiss_path); provenance + read payloads (backend/doc_id/chunk_id, whole_document, identity:); handoff frontmatter paths | 8+ documented impl-detail leaks in public surfaces (status, provenance, error paths, read payloads). | Grok (ARC-P2-01, architecture.md:14-15 + leaks table), AG-CLI (ARCH-004 P2) | SINGLE-NEEDS-VERIFY (read sovrd.py:1913 status handler + retrieval provenance code) | Strip all internal paths, backend names, and DB columns from status/health/provenance/error surfaces; add redaction pass. |
| RCM-021 | P1 | Security | engine/afm_writer.py:78,133 (frontmatter forgery via missing SEC-018 guard; unescaped title/tags for YAML injection); episodic.py:82 (secret leakage P2) | AG-CLI P1: afm_writer lacks _contains_forged_frontmatter guard from writeback.py (model can forge frontmatter) and uses unsafe YAML for title/tags (metadata override injection); related episodic secret leakage to AFM prompts. Primary security blocker per AG-CLI Dimension 3 / conclusion (Grok/AG-Desktop did not surface at this severity). | AG-CLI (SEC-001/SEC-A01/SEC-VULN-001, SEC-002/SEC-A02/SEC-VULN-002, AUDIT.md Dimension 3, vulnerabilities.md, security-gemini-a) | SINGLE-NEEDS-VERIFY (`grep -n "contains_forged_frontmatter\|_escape_frontmatter\|write_text(body)" engine/afm_writer.py engine/writeback.py; python -m pytest test_frontmatter_security.py -k afm`) | Port _contains_forged_frontmatter guard + safe YAML dumper from writeback.py to afm_writer.py; add redaction to episodic.add_event; extend test_frontmatter_security.py. |

## Phases

### Phase 0 — CI bootstrap (UNGATED, must happen first)
**Entry criteria:** Repository clone on clean macOS/Linux machine; manual pytest + npm test pass today.
**Exit criteria:** `.github/workflows/ci.yml` present and green on push/PR for ubuntu + macos matrix (Python 3.11/3.12, Node 20); runs engine pytest -q, plugin npm ci + test, hermetic /tmp socket status + recall probe + migration safety; no host ~/.sovereign-memory pollution; artifacts uploaded.
**Findings addressed:** RCM-001 (and enables gating of all later fixes).
**Estimated scope:** S (1-2 days for minimal GHA + smoke script).
**Suggested executor:** Grok (or any CI-savvy agent) — needs filesystem + workflow authoring; rationale: purely mechanical once smoke harness exists; can be validated immediately by re-running the new workflow.

### Phase 1 — P0 security fixes
**Entry criteria:** Phase 0 CI green; all four SEC-P0 repros reproducible on current tree.
**Exit criteria:** RCM-002/003/004/005 fixed and covered by new or existing tests (test_vault_root_binding, test_principal_binding, test_handoff_wikilink_containment extended to plugin paths); non-strict mode removed or strictly gated; ping writes restricted; G23 applied to plugin resolve paths; all P0 security tests pass in CI. Spot-check AG-CLI/AG-Desktop security P1 surfaces (e.g., afm_writer frontmatter forgery RCM-021).
**Findings addressed:** RCM-002, RCM-003, RCM-004, RCM-005 (plus RCM-011 unauth status/candidate overlap).
**Estimated scope:** M (core daemon/plugin changes + test extensions).
**Suggested executor:** Grok Build security subagent flow or Claude Code — requires deep principal + vault.ts + server.ts understanding and repro writing; rationale: the numbered attack scenarios in security.md provide exact reproduction steps for verification.

### Phase 2 — P0 performance & storage fixes
**Entry criteria:** Phase 0 CI; ability to run concurrent clients against socket.
**Exit criteria:** RCM-006/007 fixed (no blocking sleeps or sync dispatch on hot paths; audit volume bounded with rotation + status telemetry); load harness in CI exercises 50k+ rebuild and sustained hook traffic without inode growth or starvation.
**Findings addressed:** RCM-006, RCM-007 (plus RCM-014 cold-path as stretch).
**Estimated scope:** M (asyncio executor work + audit prune logic + new tests).
**Suggested executor:** Antigravity CLI or Grok perf subagent — tool-grounded measurement + daemon asyncio changes; rationale: AG-CLI inventory/resource-limits + Grok performance-adversarial already mapped the blocking sites.

### Phase 3 — P1 hardening
**Entry criteria:** All Phase 1/2 P0s closed and gated in CI.
**Exit criteria:** RCM-008–011,014–017, RCM-021 addressed (agent-agnostic wiring cleaned, MCP schemas + docs aligned, unauth surfaces gated, zero-coverage public APIs have exercising tests, bare-excepts replaced with logging, supply-chain locks + repro smoke, launchd hardened, afm_writer frontmatter/YAML guards ported); coverage gates in CI for agent_api/sovereign_memory/graph_export.
**Findings addressed:** RCM-008 (god method), RCM-009 (agent leaks), RCM-010 (MCP doc/schema), RCM-011 (unauth), RCM-014 (perf P1), RCM-015 (supply), RCM-016/017 (coverage + excepts), RCM-021 (afm_writer P1 forgery/injection per AG-CLI).
**Estimated scope:** L (refactors + broad test additions + doc sweep).
**Suggested executor:** Mixed — Grok for architecture/scope (proposals/separation-cuts), Antigravity for inventory-driven lint/quality, Claude Code for doc alignment; rationale: spans multiple dimensions and requires both code + contract work.

### Phase 4 — Dead code & scope cleanup
**Entry criteria:** P0/P1 security & perf stable; CI green.
**Exit criteria:** RCM-012/013 + remaining dead items from both separation-cuts.md executed (openclaw-extension + openclaw-tool.sh deleted, afm_scheduler deleted, lance/qdrant stubs pruned or isolated, ui-server deep-research gated or removed, duplicate handoff/envelope logic consolidated where safe); no self-referential or deprecated references remain in active paths.
**Findings addressed:** RCM-012, RCM-013 (and P2 scope items from inventories).
**Estimated scope:** S (deletions + one or two small refactors).
**Suggested executor:** Any (Grok or Antigravity) — mechanical cuts with clear "delete entire dir" instructions in proposals/separation-cuts.md (both versions); rationale: lowest risk, highest confidence from import/call-graph proofs in scope-creep.md and dead-code inventories.

### Phase 5 — Doc + contract alignment
**Entry criteria:** Phase 4 cuts landed; contracts match code.
**Exit criteria:** RCM-019 + all stale [PLANNED] and outdated tool lists removed/updated; SKILL.md, CAPABILITIES.md, AGENT.md, VAULT.md exactly match 26 MCP tools + _METHODS + G11+ reality; CHANGELOG.md initialized (keep-a-changelog); "engines"/python_requires fields present.
**Findings addressed:** RCM-019 (and ARC-P2-02, CI-P1-02 doc drift).
**Estimated scope:** S.
**Suggested executor:** Claude Code or Antigravity Desktop — strong at contract reading + markdown editing; rationale: purely alignment work once code is stable; proposals/separation-cuts.md item 9 and Grok AUDIT.md drift tables give exact targets.

### Phase 6 — RC validation gate
**Entry criteria:** All prior phases complete; CI green on main.
**Exit criteria:** Re-execution of all P0 repros (from security.md numbered scenarios + AG-Desktop/CLI flagged cases) now fail as expected; full CI matrix green including new coverage/perf harnesses; manual clean-install smoke on macOS + Linux (or Docker parity); critic-style review of final tree shows zero open P0/P1 with evidence; RC tag prepared.
**Findings addressed:** All RCM-001–021 (verification of fixes).
**Estimated scope:** M (validation + any last polish).
**Suggested executor:** Grok (synthesis + critic loop experience) or full multi-orchestrator re-run; rationale: requires cross-audit verification mindset and ability to run the exact commands in Verification Backlog.

## Verification Backlog

All SINGLE-NEEDS-VERIFY or TOOL-GROUNDED items require explicit commands before Phase 6:

- `ls -la .github/workflows/` (expect empty or only the new ci.yml) — RCM-001
- `grep -n "vaultPath" plugins/sovereign-memory/src/server.ts | head -20` + `grep -n "G12: vaultPath removed" plugins/sovereign-memory/src/server.ts` (verify residual vs comments) — RCM-002
- `grep -n "if not strict:" engine/principal.py` + run `python -m pytest engine/test_principal_binding.py -q --tb=line` (strict vs non-strict paths) — RCM-003
- `grep -n "syncContract\|ensureVault" plugins/sovereign-memory/src/agent_ping.ts` + `grep -n "toAgent" server.ts` — RCM-004
- `grep -n "normalizeWikilinkRef\|resolveVaultRef\|resolveInboxHandoffContext" plugins/sovereign-memory/src/vault.ts` + check test_handoff_wikilink_containment.py coverage of plugin paths — RCM-005
- `grep -n "time.sleep\|_handle_await_handoff\|_dispatch" engine/sovrd.py` + concurrent-client smoke — RCM-006
- `grep -rn "recordAudit" --include="*.ts" plugins/sovereign-memory/src/ | wc -l` + `grep -n "appendFile" plugins/sovereign-memory/src/vault.ts` — RCM-007
- `python -c "
import ast, sys
tree=ast.parse(open('engine/retrieval.py').read())
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name=='retrieve': print(node.lineno, ast.unparse(node)[:200] if hasattr(ast,'unparse') else 'len='+str(len(ast.dump(node))))
" ` or `radon cc engine/retrieval.py:1276` — RCM-008
- `grep -E "CLAUDECODE_|_agent_vault|DEFAULT_AGENT_ID" engine/sovrd.py plugins/sovereign-memory/src/{hook.ts,config.ts,server.ts} | wc -l` — RCM-009
- `python -m pytest --cov=engine/agent_api,engine/sovereign_memory,engine/graph_export --cov-report=term-missing -q` (expect 0% on public entrypoints today) — RCM-016
- `grep -n "except Exception: pass" engine/*.py plugins/sovereign-memory/src/*.ts | wc -l` + cross-check bandit.log:334-378 — RCM-017
- `grep -n "sha1" engine/afm_passes/*.py` + read semgrep.log:22-75 and bandit.log:22-58 — RCM-018 (TOOL-GROUNDED)
- `grep -n "contains_forged_frontmatter\|_escape_frontmatter\|write_text(body)" engine/afm_writer.py engine/writeback.py` + `python -m pytest test_frontmatter_security.py -k afm` — RCM-021 (SINGLE-NEEDS-VERIFY)
- `ls engine/migrations/ | grep -E '^007_'` (expect collision) + `grep -n "CREATE TABLE" engine/db.py` — scope/quality P1 items
- Read specific tool logs at cited offsets for any promoted issues (bandit.log:281-378 for B608/B110; semgrep.log for SHA1 + sqlalchemy raw query).

## Cross-Audit Open Questions

1. **Dynamic SQL construction severity (agent_api.py:347, indexer.py:268/269, retrieval.py:287, writeback.py:221, faiss_disk.py:134)**
   - AG-Desktop (Gemini Model A): P0 "Dynamic SQL Construction Code-Smell" — blocking vulnerability, replace with query builders.
   - AG-Desktop (Claude Model B): low-severity code smell (parameters still bound as SQLite vars, no active injection vector).
   - AG-CLI: not flagged as P0 (listed in inventories as logic/low).
   - Grok: surfaced only via bandit.log B608 Medium + semgrep sqlalchemy raw; never elevated to security P0 (treated as CQ/perf hygiene).
   **Recommended resolution:** Treat as P2 hygiene (RCM-018 adjacent). Add query-builder or f-string removal + test that placeholders are always bound. Human call needed because active exploit risk vs. future-regression risk judgment differs by auditor threat model.
   **Why unresolved from text:** No concrete attack input that bypasses the parameter binding was demonstrated in any audit.

2. **PERF-P0-01 unbounded recordAudit (vault.ts:385-407, 47+ sites) as RC blocker**
   - Grok: P0 (resource-exhaustion DoS on primary surface; performance-adversarial DoS section + critic loop kept after debate).
   - AG-CLI: not listed as audit-volume P0 (focus on event-loop blocking + FAISS memory).
   - AG-Desktop: not mentioned.
   **Recommended resolution:** Keep P0 (RCM-007) with explicit justification in Phase 2 exit criteria (inode exhaustion under normal hook cadence falsifies availability claim for the agent surface).
   **Why unresolved:** Spec language "blocks RC" is ambiguous between "auth/contract corruption" vs. "availability on every operation path."

3. **God-method complexity in retrieval.py:1276 as P0 vs P1**
   - AG-CLI: QUAL-001 P0 (complexity ~28, SRP violation, must refactor for RC).
   - Grok: PERF-P1-01 + CQ notes (high impact but not auth/contract breaker).
   - AG-Desktop: not surfaced.
   **Recommended resolution:** P1 (RCM-008) — real maintainability risk but gated behind Phase 3 after P0 stability.
   **Why unresolved:** Different risk tolerance for "unpolished for production RC" vs. "hard blocker."

4. **Line-number and coverage drift across audits** (e.g., AG-CLI cites sovrd.py:832 for sleep, actual ~854; Grok vs AG-Desktop status handler lines)
   - Minor but affects exact Verification Backlog commands.
   **Recommended:** Always re-verify with live `grep -n` in final tree before implementation; treat cited lines as "region anchors."

5. **Supply-chain (unpinned reqs + native binary + legacy shims) as P0 (ci-release-adversarial) vs P1 (Grok synthesis)**
   - Adversarial view elevates to P0 for RC; aggregate treats as P1 (SEC-P1-01 / CI-P1-01) because no poisoned wheel observed in tree.
   **Recommended:** Keep P1 but mandate lockfile + attestation in Phase 3 exit; human call on whether "violates SECURITY_PLAN Assumption #8" alone blocks tag.

6. **AG-CLI afm_writer P1 (frontmatter forgery / YAML injection at afm_writer.py:78,133) omission from initial synthesis**
   - AG-CLI AUDIT Dimension 3 + vulnerabilities.md + security-gemini-a explicitly flags SEC-001/002 / SEC-VULN-001/002 as High/P1 "primary security blocker" (missing SEC-018 guard from writeback.py allowing model frontmatter forgery; unescaped YAML title/tags allowing metadata override injection; episodic leakage P2).
   - Grok and AG-Desktop did not surface afm_writer at P1 (Grok focused on MCP/plugin G11-G23 bypasses and daemon principal; AG-Desktop had separate P0 SQL/unauth and P3 SHA1).
   **Recommended resolution:** Added as RCM-021 P1 Security (SINGLE-NEEDS-VERIFY) per "any audit P1" merger rule and inventory evidence. Included in Phase 3 addressed + Verification + Appendix + Cross-Audit note. Not elevated to P0 because no multi-audit convergence or repro showing active bypass of the G11+ daemon model (unlike the 4 Grok SEC-P0s).
   **Why it was initially omitted:** Strict "evidence in the three audits" + focus on Grok's detailed numbered repros for the plugin/daemon surface; afm_writer was AG-CLI-specific P1 not cross-flagged at that severity. Now corrected for completeness.

## Out of Scope (defer past RC)

All P3 / low-severity items explicitly excluded from RC work (will not be part of the six phases):

- RCM-018 (SHA1 digests + urllib — P3/P2 hygiene; switch to sha256 is trivial post-RC).
- Minor test hygiene (polling sleep(0.02) in deprecated OpenClaw tests).
- Stale _archive/ and _cleanup-quarantine/ directories.
- Early G0x env tests and self-only coverage files.
- Future v2 cryptographic principal / replay hardening (explicitly P3 in Grok security.md).
- Any findings labeled P3 in original audits (e.g., AG-Desktop P3 SHA1, AG-CLI SCOPE-007 stale artifacts, Grok ARC-P3-01 contract drift that is not blocking).

P2 items (RCM-018 partial, RCM-019/020, some lint nits) may slip into Phase 3/4 only if they block a P0/P1 cut; otherwise deferred.

## Track A Note (the agent-integration skill)

This three-orchestrator experiment (Grok Build native multi-agent /implement effort=4 with 5-critic Phase-4 loop + parallel researchers, Antigravity 2.0 Desktop layered Gemini-3.5-Flash + Claude-Sonnet-4.6 with full sandboxed tool exec producing 514 KB bandit.log + semgrep etc., Antigravity 2.0 CLI Gemini-3.5-Flash-High producing the richest 7-file inventory + per-dimension -gemini-a/b splits) revealed platform capability deltas that directly inform a future "sovereign-memory install / audit skill" bitmask. Native multi-agent (Grok) excels at long-horizon synthesis, critic loops that force evidence grounding, and proposal generation (separation-cuts.md); layered + productized command (Antigravity) produces immediately executable tool-grounded artifacts (real bandit/semgrep runs against the live tree, exhaustive import graphs turned into dead-code tables); CLI-style thoroughness yields the best raw inventories for Phase 4 cuts. The install skill must expose: (a) ability to spawn parallel researcher personas with distinct effort/read-only worktrees, (b) direct sandboxed SAST/DAST tool execution (bandit/semgrep/ruff/mypy/pip-audit) with log ingestion, (c) inventory vs. prose vs. critic reconciliation primitives, (d) Manager-view aggregation that surfaces "Cross-Audit Open Questions" for human escalation, and (e) write confinement so audit work never touches the target repo. These map to capability bits "parallel-tool-audit", "sandbox-sast", "dedup-severity-merger", "critic-loop", and "read-only-proposal-cutlist". The experiment succeeded because each orchestrator's blind spots were covered by the others; a single skill must replicate that multi-orchestrator redundancy.

## Appendix: Source Audit Crosswalk

| Original ID(s) | RCM-NNN | Notes |
|----------------|---------|-------|
| CI-P0-01, CI-001, CI-001 | RCM-001 | All three |
| SEC-P0-01 / SEC-P2E-01 | RCM-002 | Grok primary (AG-Desktop principal P2 overlaps RCM-003) |
| SEC-P0-02 / principal non-strict (Grok/AG-Desktop/AG-CLI ARCH-002) | RCM-003 | Merged |
| SEC-P0-03 | RCM-004 | Grok |
| SEC-P0-04 | RCM-005 | Grok |
| PERF-001/002 (AG-CLI), PERF-P2-01 (Grok) | RCM-006 | Blocking |
| PERF-P0-01 (Grok) | RCM-007 | Audit volume |
| QUAL-001 (AG-CLI), retrieval complexity (Grok) | RCM-008 | God method |
| ARC-P1-01, ARCH-001/003/005 (AG-CLI) | RCM-009 | Agent-specific |
| ARC-P1-02, ARCH-006 | RCM-010 | MCP surface |
| AG-Desktop P1 status/trace + candidate, Grok ARC-P2-01/SEC-P2E-06 | RCM-011 | Unauth surfaces |
| SCP-P1-01, SCOPE-004, P2 openclaw (AG-Desktop) | RCM-012 | OpenClaw |
| SCP-P1-02, dead-code stubs (AG-CLI) | RCM-013 | Scheduler + backends |
| PERF-P1-01/02 (Grok), PERF-003/004/005 (AG-CLI) | RCM-014 | Cold / O(N) |
| CI-P1-01/02, CI-002, P2 launchd (AG-Desktop) | RCM-015 | Supply + repro |
| CQ-P1-01 (Grok), QUAL-002 (AG-CLI) | RCM-016 | Zero coverage |
| CQ-P1-02 + bandit B110 | RCM-017 | Bare excepts |
| P3 SHA1 (AG-Desktop/AG-CLI SEC-VULN) + semgrep/bandit | RCM-018 | TOOL-GROUNDED P2 |
| AG-CLI SEC-001/002 / SEC-VULN-001/002 (afm_writer frontmatter/YAML) | RCM-021 | AG-CLI P1 (Grok/AG-Desktop did not elevate) |
| SCP-P2-02 + SCOPE-001 doc drift | RCM-019 | Stale planned |
| ARC-P2-01 + ARCH-004 leaks | RCM-020 | Impl leaks |

(Reverse lookup: every original ID from the three AUDIT.md tables and inventories appears exactly once; P3-only items omitted from RCM register per Out of Scope.)

**End of RC Master Plan.** All evidence grounded in the three 2026-05-19 audits and their artifacts. Ready for human review and Phase 0 execution.
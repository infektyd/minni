# Sovereign Memory RC Audit — Phase 1b: Scope Creep & Dead Code

**Audit date:** 2026-05-19
**Worktree:** `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19`
**Posture:** READ-ONLY. All evidence from `list_dir`, `grep`, `read_file` on contracts + source + tests. No source modifications.
**Git history note:** Direct `git log` unavailable via provided tools (worktree `.git` is pointer; `.git/logs` access limited). Last-change evidence derived from cross-file references, plan ledgers (e.g. `docs/plans/execution/WORKTREE_STATE.md`), doc "last updated" dates (2026-04-26), and absence of imports/callers in current entrypoints. Historical references dated ~2026-04-26 (≈23 days prior to audit date; borderline for strict 30d but flagged where content indicates dormancy).

## Summary Table

| Category                  | Count | Highest-Risk Items |
|---------------------------|-------|--------------------|
| Dead / unreferenced modules & surfaces | 8 (core) + 1 deprecated dir surface (~12 files) | `openclaw-extension/` (deprecated bridge, external surface), `engine/afm_scheduler.py` (unwired idle scheduler), `plugins/sovereign-memory/src/ui-server.ts` deep-research integration (external exec, undocumented) |
| Stale TODO/FIXME/XXX + outdated PLANNED markers | 1 active TODO in src + 14+ PLANNED markers in contracts | Contracts `CAPABILITIES.md` and `AGENT.md` (PLANNED: PR-2/9/10/11/13 for features now live in `sovrd.py:2445` and `server.ts`) |
| Skipped / xfailed / conditional tests | 0 permanent; 8 conditional | N/A (only env/dep guards for optional ML libs: faiss-cpu, sentence-transformers, tiktoken, cross-encoder) |
| Duplicate primitives (same func >1 place) | 4 areas | Handoff packers (TS `task.ts:820` + Python `sovrd.py` handoff handlers), Envelope builders (`agent_envelope.ts:wrapEnvelope` + Python retrieval envelopes), Recall wrappers (multiple: `retrieval.py`, `agent_api.py`, `sovereign.ts`, `writeback.py`), Vector backends (5 impls + stubs) |
| "In case" / orphaned features (zero current consumers) | 6 | `afm_scheduler.py`, stub backends (`lance.py`, `qdrant.py`), migrate script, openclaw-extension, deep-research console paths, 2 scripts/ |
| Abandoned experiments / PR plans w/o impl | 2+ (scripts + extension) + historical plans | `scripts/`, `openclaw-extension/`, early G0x tests (env-only) |

**Total dead/unreferenced items + TODOs/PLANNED >~3w flagged:** 12 modules/surfaces/features + 1 dir + 4 duplicate areas + 14 stale markers + 0 permanent skips = **31+ flagged items/markers** (counting distinct files/dirs/areas; duplicates counted separately per task).

Evidence basis: Exhaustive `grep` for imports/callers/docs refs/MCP registrations across `engine/`, `plugins/sovereign-memory/src/`, `openclaw-extension/`, `docs/contracts/`, `README.md`, `SKILL.md`, `commands/*.md`, test files. Import analysis via targeted patterns (e.g. `from X import`, `import X`, tool name strings). Every claim below cites exact `file:line` or `grep` result set.

## Detailed Findings

### 1. Dead Modules / Unreferenced Surfaces

**1.1 `engine/afm_scheduler.py` (full module, ~150+ LOC)**
- **Exact location:** `engine/afm_scheduler.py:1` (class `AFMScheduler`, idle loop logic with `mark_activity`, `register_long_running_op`, etc.).
- **Last meaningful change:** Only referenced in its test + historical plan (`docs/plans/execution/13_PR12_Phase6A_AFM_Session_Distill.md:17` and `WORKTREE_STATE.md:21` entry 2026-04-26). No other hits.
- **Why unused (proven):** `grep` for `afm_scheduler|AFMScheduler|from afm_scheduler` across workspace yields **only** `engine/test_pr12_afm_loop.py:225` + plan files. **Zero imports** in `engine/sovrd.py`, `engine/sovereign_memory.py`, `engine/afm_provider.py`, `engine/afm_passes/`, or `plugins/`. Daemon compile paths (`sovrd.py:2102` `_handle_daemon_compile`, `sovereign_memory.py:424` `pass_runners`) are explicit/CLI-driven only.
- **Risk of keeping:** Maintenance burden (idle scheduler invariants, threading, activity tracking); confusion for contributors expecting AFM auto-scheduling (per old PR-12 design); dormant attack surface if ever accidentally enabled.

**1.2 `engine/migrate_v3_to_v3_1.py` (one-time migration script)**
- **Exact location:** `engine/migrate_v3_to_v3_1.py:1-15` (CLI docstring references).
- **Last meaningful change:** Historical plans only (`docs/plans/execution/01_PR1_Phase0_Foundation.md:27`, `docs/plans/SOVEREIGN-MEMORY-CORE-UPGRADES-SCALE-AGNOSTIC.md:90` — both instruct "keep as deprecated documentation — do not delete"). No runtime refs.
- **Why unused (proven):** `grep` for `migrate_v3_to_v3_1|migrate_v3` yields **only** self + 2 plan files. Not called from `migrations.py`, `sovrd.py` startup, `sovereign_memory.py`, or tests.
- **Risk:** Dead code bloat; contributor confusion (is it safe to delete?); no tests exercising it.

**1.3 `scripts/afm-sovereign-scenarios.mjs` and `scripts/afm-swift-knowledge.mjs`**
- **Exact locations:** `scripts/afm-sovereign-scenarios.mjs:1`, `scripts/afm-swift-knowledge.mjs:1`.
- **Last meaningful change:** Zero references post-creation (no mentions in any `grep` across `*.{md,py,ts,sh}`).
- **Why unused (proven):** `grep` pattern `scripts/|afm-sovereign-scenarios|afm-swift-knowledge` returns **0 matches**. Not in README, SKILL, package.json scripts, engine entrypoints, or tests.
- **Risk:** Orphaned experiment scripts (AFM scenario gen + Swift knowledge extraction); maintenance burden if deps drift; potential secret leakage if run (one contains redaction example).

**1.4 `openclaw-extension/` (entire deprecated surface: 5 TS sources + `sovrd.py` + `package.json` + `README.md` + `tests/` + `migrate_phase2.py` + `plugin.json` etc.)**
- **Exact locations:** `openclaw-extension/src/{bridge-process.ts,bridge.ts,index.ts,sovereign-manager.ts,types.ts}:1+`, `openclaw-extension/sovrd.py:1` (HTTP variant).
- **Last meaningful change:** Deprecation notes in `engine/sovrd.py:36` and `2476` ("openclaw-extension/sovrd.py HTTP variant is deprecated"); docs (`docs/CANONICAL-PATHS.md:21`, `docs/runtime-integration.md:9`, `docs/decisions/SOVEREIGN-OPENCLAW-OPTION2-DECISIONS.md:105`, `SECURITY_PLAN.md`, `README.md:428` for plist only). No cross-imports.
- **Why unused (proven):** `grep` for `openclaw-extension/src|bridge-process|sovereign-manager` yields **only self-referential** (inside the dir) + deprecation comments. Main `plugins/sovereign-memory/src/` and `engine/` have zero imports/calls. `package.json` "start" is internal. OpenClaw now routes via main plugin + sovrd Unix socket (per README architecture).
- **Risk:** Deprecated bridge (HTTP fallback) adds attack surface (socket perms, process supervision); confusion (which sovereign sovrd for OpenClaw?); 2+ LOC of legacy TS/JS to maintain; listed in CANONICAL-PATHS but inactive. Highest-risk abandoned experiment.

**1.5 Deep-research integration in `plugins/sovereign-memory/src/ui-server.ts` (~120+ LOC)**
- **Exact locations:** `plugins/sovereign-memory/src/ui-server.ts:15-19` (hardcoded `DEFAULT_DEEP_RESEARCH_ROOT = .../deep-research-agent`, CLI/Python paths), `257-613` (full `runDeepResearchCli`, `createDeepResearchBridge`, handlers for `/api/deep-research/*` plan/run/status + embedded Python snippet calling external `deep_research_agent.research.ResearchService`). Listed in health tools at `488-492`.
- **Last meaningful change:** No references outside this file.
- **Why unused (proven):** `grep` for `deep_research_plan|deepResearch|DEEP_RESEARCH|deep-research` across entire workspace returns **only** this file (46 hits, all internal). **Zero mentions** in `README.md`, `docs/contracts/*.md`, `SKILL.md`, `commands/*.md`, `plugins/sovereign-memory/README.md` (console section omits it), or engine code. Depends on external non-repo path (`/Users/hansaxelsson/deep-research-agent`). Console docs mention only "status, audit, prepare-task, prepare-outcome, candidate listing".
- **Risk:** High — console server (local HTTP) executes external binaries/scripts via `execFileAsync` with long timeouts; hardcoded user-specific paths; "in case" feature for optional deep research not part of Sovereign Memory scope. Attack surface + contributor confusion. Classic scope creep.

**1.6 Stub vector backends: `engine/backends/lance.py` and `engine/backends/qdrant.py`**
- **Exact locations:** `engine/backends/lance.py:1-30` (docstring: "protocol-conformant but non-functional"; `LanceBackend` raises on ctor), `engine/backends/qdrant.py:1-30` (identical stub pattern for Qdrant).
- **Last meaningful change:** Plan refs (PR-3) + registration; no active use.
- **Why unused (proven):** `grep` for `LanceBackend|QdrantBackend` shows imports only in `retrieval.py:842-847` (optional `try` that catches ImportError and falls back), `backends/__init__.py:12-19`, `backends/multi.py:42-44`, `sovereign_memory.py:335`, `wiki_indexer.py:572`, `indexer.py:305`. **Never successfully instantiated** in production paths (require `pip install "sovereign-memory[lance]"` etc., then still NotImplementedError on methods). Active backends: only `faiss_disk`/`faiss_mem` + multi wrapper.
- **Risk:** "In case" future-expansion code (per docstrings); maintenance (protocol drift); false promise in `vector_backend.py:93` docs; minor bloat.

**Other minor unreferenced:**
- Early `engine/test_g01_numpy_env.py`, `test_g03_contract_matrix.py` etc. (G0x env/contract smoke; only self-refs, not cited in current PR test matrices or docs beyond historical plans; still run via pytest discovery but low visibility).
- Some `engine/eval/` internals (dataset.py, judging.py) primarily test-only with limited cross-refs beyond harness tests.

### 2. Stale TODO / FIXME / XXX Comments (>~30d or outdated)

- **Core source (engine/*.py + plugins/sovereign-memory/src/*.ts):** **0 matches** for `#\s*(TODO|FIXME|XXX|HACK)` or bare equivalents (exhaustive `grep`). One active: `plugins/sovereign-memory/src/team-harvest.ts:88` ("// TODO: extract shared postJson... after Tasks 2-4") — recent, tied to team mode (not >30d stale).
- **Docs/contracts/*.md (stale PLANNED markers, last updated 2026-04-26):** 14+ instances. Examples:
  - `docs/contracts/CAPABILITIES.md:31-38`: `[PLANNED: PR-2]` for `recall` alias + `health_report`; `[PLANNED: PR-9]` feedback/trace; `[PLANNED: PR-10]` handoff; `[PLANNED: PR-11]` hygiene_report; `[PLANNED: PR-13]` compile/endorse. **All now implemented** (see `sovrd.py:2445` `_METHODS` dict including `feedback`, `trace`, `handoff`, `hygiene_report`, `daemon.compile`, `daemon.endorse`, `resolve_candidate`; `server.ts:601+` exposes matching MCP tools).
  - `docs/contracts/AGENT.md:76-79`, `36`: Similar `[PLANNED: PR-10/9]`, workspace_id "(PLANNED — G11 / PR-3)". G11 landed; workspace still vault-encoded.
  - `docs/contracts/VAULT.md:168`: `endorse` "[PLANNED: PR-13]".
- **Why stale:** Contracts are "canonical" (per AGENT.md:8) but lag implementation (PR-9/10/11/13+ landed per WORKTREE_STATE.md 2026-04-26+). No updates in 3+ weeks.
- **Risk:** Confusion for new contributors/auditors (what is actually live?); contract/code drift; maintenance of outdated markers.

Historical plans (`docs/plans/execution/*.md`) contain dozens more old PLANNED/roadmap items (e.g. PR-16+ future), but treated as archival.

### 3. Skipped / Xfailed / Conditionally-Skipped Tests

- **Exhaustive grep** (`@pytest.mark.(skip|xfail|skipif)`, `pytest.skip`, `.only(`, `test.only|it.only` etc.) across `**/*.{py,mjs,ts,js}`:
  - **0 unconditional `@pytest.mark.skip`, `xfail`, or `.only` blocks** that would hide tests permanently.
  - **8 conditional skips** (all env/dep guards, justified):
    - `engine/test_vault_root_binding.py:87`: `pytest.skip("symlink creation not permitted in this env")`.
    - `engine/test_pr2_envelope.py:36-38`: faiss-cpu / incomplete faiss.
    - `engine/test_pr1_foundation.py:145,168,179,207,217`: sentence-transformers / cross-encoder / tiktoken missing (multiple).
    - `engine/test_candidate_lifecycle.py:90,108`: "server.ts not in tree" / "ui-server.ts not present" (env-specific).
  - TS tests (`plugins/sovereign-memory/tests/*.mjs`): 0 skips; mentions of "skipped" are test *logic* (AFM returning SKIP in team-harvest etc.), not test directives.
- **Risk:** Low (none dead/hiding coverage). Conditionals are for optional heavy deps (ML models) + cross-lang smoke. Coverage remains high for core paths (193+ engine tests passing per historical ledgers).

### 4. Duplicate Primitives

Proven via cross-grep + read_file on builders/call sites (same semantics in >1 place):

- **Handoff packers / delivery:** `plugins/sovereign-memory/src/task.ts:820` (`buildHandoffPacket`), `server.ts:704` (calls + `handoffMemory`), `handoff_guard.ts:35+` (`planHandoffDelivery`), `sovereign.ts:235` (`handoffMemory` RPC) **vs** `engine/sovrd.py:504` (`_handle_daemon_handoff` + lease logic + `_ensure_handoff_vault`), `agent_api.py` recall paths, multiple tests (`test_pr10_handoff.py`, `test_handoff_wikilink_containment.py`). Also `agent_ping.ts:244` for ping-based variant.
- **Envelope builders / wrappers:** `plugins/sovereign-memory/src/agent_envelope.ts:1+` (`wrapEnvelope`, `MEMORY_CONTRACT` ref to AGENT.md), used in `server.ts:714` for handoffs + hooks **vs** Python `retrieval.py:1652+` (envelope construction in `retrieve`/`_format_result`), `sovrd.py` redaction + result shaping, `test_evidence_envelope.py`, `test_pr2_envelope.py:641`.
- **Recall / search wrappers:** `engine/retrieval.py:1822` (alias + hybrid FTS+FAISS+rerank), `agent_api.py:94` (`recall`), `writeback.py:242` (`recall_learnings`), `sovrd.py:946` (`_handle_search`) **+** TS `sovereign.ts`, `task.ts`, `vault.ts` (multiple `sovereign_recall` paths + context packing).
- **Vector backends + storage abstraction:** 5 impls in `engine/backends/` (faiss_disk/mem primary + multi + 2 stubs) + `vector_backend.py` protocol + `faiss_index.py`/`faiss_persist.py` duplication of persistence concerns.

**Risk:** Maintenance (bugfix in one, drift in other); inconsistency (e.g. redaction/envelope rules); contributor confusion on "the" handoff/envelope path. Expected in polyglot (Python daemon + TS plugin) but still scope bloat.

### 5. "In Case" Features + Abandoned Experiments

- **afm_scheduler.py** (see Dead Modules): Explicitly designed for "future" idle auto-compile (PR-12 plans) but zero consumers.
- **Stub backends** (see above): Explicit "future activation" per docstrings.
- **Deep research in ui-server.ts** (see above): Wired "just in case" external agent; zero core docs/MCP exposure.
- **openclaw-extension/** and scripts/: Abandoned post-deprecation (sovrd comments + README now emphasize main plugin + Unix sovrd).
- **Migrate script:** Explicitly "deprecated documentation" per plans.
- **PR plans without full corresponding surface:** Historical `docs/plans/execution/` (e.g. 16_PR15_... references quantized changes in faiss_index; some early phases have "PLANNED" subitems still echoed in contracts). `docs/plans/execution/RESUME.md` and `WORKTREE_STATE.md` show most landed, but docs lag. No major unimplemented core PRs (tests pr1-pr15 exist and pass per ledgers).
- **Other:** `engine/backends/multi.py` + some eval retrievers (vendor-memory baseline is stub that returns []); `ui-server.ts` "sovereign_candidates" + console-only paths not in MCP tool list (intentional per G15 comments in server.ts:502).

**Risk:** Accumulating "just in case" code increases surface (esp. exec in ui-server), slows onboarding, dilutes focus on "what makes this different from Just RAG" (per README thesis).

### What Looks Solid (Well-Scoped, Actively Used Core Surfaces)

- **Daemon entrypoint + RPC:** `engine/sovrd.py:2445` `_METHODS` dispatch + handlers (`_handle_search:946`, `_handle_learn:1445`, handoff/ack/await:504+, compile:2102, resolve_candidate, status etc.) — exhaustive, redaction, principal stamping (G11+), latency tracking. Directly referenced by README, CAPABILITIES, SKILL, tests.
- **MCP tool surface:** `plugins/sovereign-memory/src/server.ts:49-913` — registers 26+ `sovereign_*` tools (status, recall, prepare_*, learn, vault_write, audit_*, compile_vault, negotiate_handoff, ping_agent_*, team_*, route, drill, export_pack, ack/await/subscribe, resolve_candidate, learning_quality) with schemas, G11/G12/G13/G15 guards (no caller-controlled agentId/vaultPath/afm URLs). Cross-referenced in SKILL.md:73-88, README:229+, commands/*.md.
- **Retrieval + policy core:** `engine/retrieval.py` (hybrid, depth tiers, budget, redaction, envelopes), `engine/principal.py` (EffectivePrincipal, can_read_document G19/G20, vault binding G12), `engine/safety.py` (instruction_like). Heavily tested + cited in contracts (AGENT.md:123-130, POLICY.md).
- **Plugin hooks + CLI:** `plugins/sovereign-memory/src/hook.ts`, `codex-hook.ts`, `kilocode-hook.ts` (SessionStart/UserPromptSubmit etc. envelopes); `cli.ts` (local commands); `vault.ts`, `task.ts`, `team.ts`. All wired in package.json smokes, SKILL.md:12-18, tests.
- **Governance + tests:** Full `engine/test_pr*.py` suite (pr1-foundation through pr15-quant_semantic + G11/G12/G16 etc. tests); contracts (`docs/contracts/AGENT.md`, `VAULT.md`, `POLICY.md`, `THREAT_MODEL.md`); `SKILL.md` + 10 `commands/*.md` (audit, learn, prepare-*, recall, status, team-*). No permanent skips; high pass rates.
- **AFM compile passes:** All 5 in `engine/afm_passes/` (`session_distillation.py` etc.) + prompts + `afm_writer.py:271` (`endorse_draft`) + `afm_provider.py` — explicitly wired in `sovereign_memory.py:424` (pass_runners), `sovrd.py`, CLI `--pass`, tests pr12-15, README:295. (Scheduler separate issue.)
- **Vault + handoff docs:** `docs/contracts/VAULT.md`, `PAGE_TYPES.md`; `plugins/sovereign-memory/src/vault.ts`.

These surfaces have dense cross-refs (docs → code → tests → MCP/CLI), active callers, and align with the "local-first memory and governance layer" thesis.

## Recommendations (for RC cleanup, non-binding)

1. Delete or archive `openclaw-extension/`, `scripts/`, `engine/migrate_v3_to_v3_1.py`, `engine/afm_scheduler.py` (or mark explicitly "archival only" with removal date).
2. Excise deep-research code from `ui-server.ts` (or gate behind explicit env + document in README/console section).
3. Remove or fully implement stub backends (lance/qdrant) or move to optional extras only.
4. Update `docs/contracts/CAPABILITIES.md` + `AGENT.md` (remove [PLANNED] for landed features; pin current versions).
5. Audit for duplicate handoff/envelope logic; consider shared contract or thin adapters.
6. Add drift guard (like existing `api-mapping.test.mjs` for frontend-src) for contracts vs. `_METHODS` / server.ts registrations.
7. For future: enforce "no new module without documented public flow + test + import proof" in contrib guidelines.

**Output path:** `grok/audits/2026-05-19/findings/scope-creep.md` (absolute: `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19/grok/audits/2026-05-19/findings/scope-creep.md`).
**Flagged total:** 31+ (as tabled). All claims grounded in tool output above.

**End of Phase 1b scope-creep & dead-code report.**
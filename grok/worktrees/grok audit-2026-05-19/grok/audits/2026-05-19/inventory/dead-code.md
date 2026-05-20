# Inventory: Dead Code, Scope Creep & Unreferenced Surfaces (Sovereign Memory RC Audit 2026-05-19)

**Source:** Phase 1b scope-creep.md (75 tool calls, 31+ items with exhaustive import/call-graph proof, no speculation), Phase 2f code-quality.md (zero-coverage public surfaces), Phase 2e security.md (legacy surfaces as attack surface), ci-release-adversarial (supply-chain risk from abandoned shims).

## Highest-Risk Dead / Abandoned Surfaces (P1)

1. **openclaw-extension/ (entire directory + supporting sh)**
   - Files: plugin.json, openclaw.plugin.json, src/ (bridge.ts, bridge-process.ts, index.ts, sovereign-manager.ts, types.ts — 5 TS), sovrd.py (deprecated HTTP), migrate_phase2.py
   - Evidence of dead: only self-refs + deprecation notes in sovrd.py:36/2476; no docs, no MCP tools, no active callers outside its own tests
   - Risk: direct sqlite/agent_api bypass (architecture violations), supply-chain surface still shipping (ci-release-adversarial), attack surface (security.md)
   - **Recommended cut:** Delete entire tree + engine/openclaw-tool.sh in one PR; update any remaining plan/docs references.

2. **engine/afm_scheduler.py (entire thin module)**
   - Evidence: only referenced in test_pr12_afm_loop.py + old PR plans (00_MASTER_TRACKER etc.); no MCP/CLI/docs exposure; afm_passes/ already wired in sovrd + sovereign_memory CLI
   - **Recommended cut:** Delete; ensure all AFM scheduling routes through existing wired paths.

3. **plugins/sovereign-memory/src/ui-server.ts deep-research surface (257-613 + 625+ handlers)**
   - External exec on hardcoded non-repo path; 0 docs/MCP exposure; shallow error handling (no timeouts, exit checks, path redaction)
   - Flagged in both scope-creep and code-quality (P2)
   - **Recommended cut or harden:** Remove or fully gate + document + add timeouts/redaction.

## Stale Markers & Doc Drift (P2, high volume)

- 14+ outdated `[PLANNED: PR-N]` in docs/contracts/*.md for features that have shipped (handoff, compile, endorse, hygiene_report in CAPABILITIES.md:35-38; many in AGENT.md)
- Stale acceptance baselines repeated in README:453, SECURITY_PLAN:251, docs/goal-native-afm.md:61-62 ("333 passed", "121 passed", etc.)
- SKILL.md:73-89 lists ~15 tools while 26 are registered in server.ts:48-926
- **Action:** Global sweep to remove/update all stale PLANNED tags and baselines; align SKILL.md to actual 26-tool surface + G11+ notes.

## Duplicate Primitives (P2, 4 areas — proven via import/call-graph)

- Handoff packet builders (task.ts:820 buildHandoffPacket vs. sovrd.py:504 _handle_daemon_handoff + _compile_handoff_page)
- Envelope construction (agent_envelope.ts vs. daemon paths)
- Recall wrappers (multiple thin layers around retrieval)
- Backend selection / vector backends (stubs lance.py/qdrant.py + multi.py)

**Action:** Consolidate to single canonical implementation per area; delete or alias the duplicates with deprecation.

## Low-Consumer / Orphaned (P3)

- engine/backends/lance.py + qdrant.py (stub files, only self + plans)
- scripts/ (2 .mjs with narrow usage)
- migrate_v3_to_v3_1.py (one-off)
- ui-server deep-research (see above)
- Any afm_passes not exercised by current sovrd/CLI wiring (none found — all 5 are wired; scheduler was the outlier)

## Skipped / Xfailed / Flaky Tests

- None permanent in active suite (only justified conditionals for optional deps).
- 1 minor polling sleep(0.02) in deprecated OpenClaw test (test_socket_perms or related).
- Strong tmp_path usage and isolation in active tests (code-quality.md).

## "In Case" / Future Features with Zero Current Consumers

- Deep-research exec surface (ui-server)
- Deprecated OpenClaw HTTP bridge + direct shims (still shipping)
- afm_scheduler (unwired)
- Legacy "main" / broad "unknown" / "wiki:*" special cases in principal + retrieval (kept for backward compat but increase attack surface per security.md)

**See also:** scope-creep.md (full 31+ with methodology + git-age approximation + "What looks solid" core surfaces), code-quality.md (zero-coverage public functions table), proposals/separation-cuts.md (concrete deletion + consolidation plan), security.md (legacy surfaces as P0/P1 attack surface).
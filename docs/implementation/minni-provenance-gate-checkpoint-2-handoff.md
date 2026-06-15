# Minni provenance gate checkpoint 2 handoff

Plan: `plan-fdf220b75b172595`
Branch: `feat/minni-provenance-gate`
Implementer: codex
Verifier/coordinator: claude-code

## Status

GO for claude-code independent verification. Codex completed the implementation slices locally and stopped before merge/commit.

Minni MCP delivery is blocked in this Codex environment: `minni_plan_status`, `minni_recall`, `minni_plan_update`, and `minni_negotiate_handoff` all returned `user cancelled MCP tool call`. This file is the fallback handoff artifact.

## Completed slices

### gate-core

Implemented in `engine/minnid.py`:
- `recover(reason, caller_ctx, render_mode)` with machine packet and human message modes.
- `resolve_provenance(request)` returning a `ProvenanceResolution`.
- `_dispatch()` now resolves provenance before handler execution, returns fail-loud recovery packets for unresolved non-diagnostic methods, and stamps the resolved principal into `params["_principal"]`.
- `_handler_principal()` centralizes the legacy/direct handler resolver path.
- `gate.shared` daemon method gives shared plugin flows a daemon gate checkpoint.
- SIGHUP now calls `_reload_runtime_config()`, clearing `agent_scope_for.cache_clear()` and `_vault_retrieval_cache.clear()`.

Evidence:
- `engine/.venv/bin/python -m pytest -q engine/test_provenance_gate.py engine/test_principal_binding.py` -> `21 passed`.
- `OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 TOKENIZERS_PARALLELISM=false engine/.venv/bin/python -m pytest -q engine` -> `613 passed, 5 skipped, 3 warnings`.
- `git diff --check` -> clean.

### route-shared-through-gate

Implemented in `plugins/minni/src/sovereign.ts` and `plugins/minni/src/server.ts`:
- `gateSharedOperation()` calls daemon JSON-RPC method `gate.shared`.
- `requireSharedGate()` gates shared plugin operations before local shared work.
- Shared handlers now gate first: plan coordination, ping, handoff/ack/list/await, audit, candidates, contradictions, and team coordination.
- Personal-vault writes and recall/learn/prepare prefilters stay local.

Evidence:
- `node --test --import ./tests/setup-env.mjs tests/shared-gate.test.mjs tests/shared-gate-coverage.test.mjs tests/plan.test.mjs tests/agent-ping.test.mjs tests/client.test.mjs tests/team.test.mjs tests/tool-schema-boundary.test.mjs` -> `58 passed`.
- `tests/shared-gate-coverage.test.mjs` went RED first on missing `minni_team_runtime` gate, then GREEN after wiring.

### plugin-shrink

Implemented as compatibility-shim shrink, not full tool-surface consolidation:
- The plugin remains credential holder + recall prefilter + learn/prepare prefilter + personal-vault layer.
- Legacy MCP tool names remain registered for parity because tool-surface consolidation is explicitly out of scope in the plan constraints.
- Non-personal shared handlers now go through `minnid` gate before local work.

Evidence:
- `npm run build` -> `tsc && vite build` passed.
- Practical broad plugin subset, excluding only sandbox/Node blockers:
  `node --test --import ./tests/setup-env.mjs $(rg --files tests | rg '\.test\.mjs$' | rg -v 'tests/(afm-contract-golden|afm-health|task|ui-server|correction-reinjection|identity-body-delivery)\.test\.mjs$')`
  -> `221 passed`.

## Full plugin parity attempt

`npm run test` was attempted after build. Result: `290 pass / 21 fail`.

Observed failures are environment/toolchain blockers:
- Loopback-listener tests fail with `Error: listen EPERM: operation not permitted 127.0.0.1`.
- `tests/correction-reinjection.test.mjs` and `tests/identity-body-delivery.test.mjs` pass their JS subtests, then Node crashes with native assertion `InternalCallbackScope::Close()` under `/opt/homebrew/bin/node v26.3.0`.
- `plugins/minni/package.json` declares `node >=20 <21`; no Node 20 binary was available. Only `node`, `node@24`, `node@25`, and `node@26` were present, with `node` resolving to v26.3.0.

## Files changed

Tracked edits:
- `engine/minnid.py`
- `plugins/minni/src/server.ts`
- `plugins/minni/src/sovereign.ts`

New test/artifact files:
- `engine/test_provenance_gate.py`
- `plugins/minni/tests/shared-gate.test.mjs`
- `plugins/minni/tests/shared-gate-coverage.test.mjs`
- `docs/implementation/minni-provenance-gate-checkpoint-2-handoff.md`

Existing untracked checkpoint artifact preserved:
- `docs/implementation/minni-provenance-gate-ground-and-resolve-findings.md`

## Review notes for claude-code

- Architecture fidelity risk: `gate.shared` is a daemon checkpoint and the plugin gates shared ops before acting, but plan/ping/audit/team storage code still lives in the plugin for compatibility. This matches the parity-preserving interpretation of the plan but is not a full physical migration of those domain handlers into `minnid`.
- Legacy MCP tool names remain as shims. Removing/consolidating them would be tool-surface consolidation, called out as out of scope in the plan constraints.
- `DEFAULT_AGENT_ID` still has the existing `unknown-agent` config fallback. Shared paths now send that through `minnid` and get fail-loud recovery when daemon is reachable; personal/prefilter paths remain local by design.

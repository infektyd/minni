# Pass 3 External Review (Codex) — RC1 Phase 0/1/2

**Date:** 2026-05-20
**Branch reviewed:** rc1-phase-012
**Pass 2 implementation summary:** docs/implementation/rc1-phase-012-pass2-antigravity.md
**Pass 1 review (Gemini, for context):** docs/reviews/rc1-phase-012-gemini-review.md

## Verdict
NO-GO. Pass 2 clears the original full-suite pollution failure and the npm High/Moderate audit issues, and both broad test suites pass in my run. However, the RCM-008 rate limiter is applied to every plugin audit write and throws on normal same-agent operations within five seconds. That breaks the model-facing `sovereign_learn` vault-first path because `writeVaultPage` audits and then `vaultFirstLearn` audits again immediately. Existing vault tests mask this by globally enabling `SOVEREIGN_BYPASS_AUDIT_LIMIT`.

## Layer 1 — Pass 2 vs Gemini's Pass 1 findings
| Pass 1 finding | Pass 2 status | Evidence |
|---|---|---|
| Test pollution (module-level monkey-patch in 4 files) | RESOLVED | `docs/reviews/tool-output/pass3-codex/test-pollution.log` has zero `_permissive_resolve` / `G11 test relaxation` hits (`grep` exit 1). Full suite passes in `docs/reviews/tool-output/pass3-codex/pytest.log`. |
| Test pollution (function-scoped in test_pr10) | PARTIAL | The patch is scope-safe now, but several fixtures still synthesize a principal matching whatever `supplied_agent_id` was passed: `engine/test_pr10_handoff.py:35`, `engine/test_pr5_cache_layers.py:28`, `engine/test_pr6_contradictions.py:45`, `engine/test_pr9_feedback_trace.py:45`, `engine/test_pr11_observability.py:25`. This is better than module-level pollution, but it is still a permissive wrapper rather than a fixed principal-file fixture like `engine/test_principal_binding.py:86`. |
| Test integrity (full-suite pytest passes) | RESOLVED | `docs/reviews/tool-output/pass3-codex/pytest.log`: `334 passed, 3 skipped in 17.15s`; `test_principal_binding.py` and `test_vault_root_binding.py` pass in the full run. |
| npm audit High (fast-uri) | RESOLVED | `plugins/sovereign-memory/package-lock.json` upgrades `fast-uri` from `3.1.0` to `3.1.2`; `docs/reviews/tool-output/pass3-codex/npm-audit.log`: `found 0 vulnerabilities`. |
| npm audit Moderate (hono, ip-address) | RESOLVED | `plugins/sovereign-memory/package-lock.json` upgrades `hono` to `4.12.21` and `ip-address` to `10.2.0`; `docs/reviews/tool-output/pass3-codex/npm-audit.log`: `found 0 vulnerabilities`. |

## Layer 2 — Pass 2 vs Grok's partials
| Partial | Pass 2 status | Evidence |
|---|---|---|
| RCM-004 lease table | COMPLETED | Lease storage now uses `SOVEREIGN_HOME/pings/leases` (`plugins/sovereign-memory/src/agent_ping.ts:202`), request creation writes sender outbox + lease only (`plugins/sovereign-memory/src/agent_ping.ts:301`), and recipient materialization occurs during recipient poll/decide (`plugins/sovereign-memory/src/agent_ping.ts:327`, `plugins/sovereign-memory/src/agent_ping.ts:362`). Tests cover no pre-poll inbox file, poll materialization, decide materialization, TTL reap, and wrong-principal rejection (`plugins/sovereign-memory/tests/agent-ping.test.mjs:141`, `:155`, `:176`, `:203`, `:235`). |
| RCM-008 rotation+quota+rate-limit | PARTIAL | Rotation, prune, quota, and per-agent timestamp files exist (`plugins/sovereign-memory/src/vault.ts:498`). But rate limiting throws for all audit writers (`plugins/sovereign-memory/src/vault.ts:518`) and breaks `vaultFirstLearn` because it audits twice through `writeVaultPage` and `vaultFirstLearn` (`plugins/sovereign-memory/src/vault.ts:696`, `plugins/sovereign-memory/src/vault.ts:721`). Focused evidence: `docs/reviews/tool-output/pass3-codex/focused-vault-first-learn-rate-limit.log` returns `ERROR rate-limit: audit frequency exceeded`. Required concurrent-writer coverage is also absent: `docs/reviews/tool-output/pass3-codex/concurrent-audit-test-grep.log`. |

## Layer 3 — New findings
| Severity | RCM-NNN (if applicable) | File:line | Summary | Evidence (tool log or code reading) | Recommended fix |
|---|---|---|---|---|---|
| P1 | RCM-008 | `plugins/sovereign-memory/src/vault.ts:498`, `plugins/sovereign-memory/src/vault.ts:696`, `plugins/sovereign-memory/src/vault.ts:721`, `plugins/sovereign-memory/src/server.ts:517` | Audit rate-limiting breaks normal `sovereign_learn` / `vaultFirstLearn` calls. | `recordAudit` throws when the same inferred agent writes within 5s. `sovereign_learn` calls `vaultFirstLearn`, which calls `writeVaultPage` and then immediately records a second audit. `docs/reviews/tool-output/pass3-codex/focused-vault-first-learn-rate-limit.log` reproduces the failure with the bypass unset. | Scope rate limiting to hook/high-frequency paths, or make limit hits drop/defer only that audit entry without failing the user operation. Add a production-mode test for `sovereign_learn` / `vaultFirstLearn` with `SOVEREIGN_BYPASS_AUDIT_LIMIT` unset. |
| P2 | RCM-008 | `plugins/sovereign-memory/tests/vault.test.mjs:7`, `plugins/sovereign-memory/tests/team-repetition.test.mjs:7` | Existing plugin tests mask the new production behavior by globally bypassing the audit limiter. | `npm test` passes, including `vaultFirstLearn writes a note...`, because the vault test file sets `SOVEREIGN_BYPASS_AUDIT_LIMIT=true` before importing the module. | Keep bypass only in tests that are explicitly about unrelated audit volume, and add non-bypass tests for core exported APIs. |
| P2 | RCM-008 | `plugins/sovereign-memory/tests/audit-rcm008.test.mjs:9` | Required concurrent-writer no-drop test is missing. | The review prompt requires `test_audit_concurrent_writers_no_drop`; `docs/reviews/tool-output/pass3-codex/concurrent-audit-test-grep.log` has no hits. Current implementation also has no lock around rotation + append (`plugins/sovereign-memory/src/vault.ts:536`, `:572`, `:578`). | Add a concurrent `Promise.all(recordAudit(...))` regression that verifies every expected entry is present after simultaneous writes, including around rotation. Add a per-vault append/rotation mutex or equivalent atomic design if the test exposes drops/races. |
| P2 | Test integrity | `engine/test_pr5_cache_layers.py:28`, `engine/test_pr6_contradictions.py:45`, `engine/test_pr9_feedback_trace.py:45`, `engine/test_pr10_handoff.py:35`, `engine/test_pr11_observability.py:25` | Replacement principal fixtures remain permissive. | Each wrapper writes a principal file whose `agent_id` is derived from `supplied_agent_id`, then calls the real resolver. This is scope-safe but still mirrors the old "caller identity becomes truth" behavior for those suites. | Create fixed principal files per test scenario and route handlers to that principals directory, rather than synthesizing a matching principal inside the resolver wrapper. |

## Tool evidence diff
- bandit: The exact `cd engine && bandit -r .` pass entered vendored/venv scanning and was terminated after only warnings were logged in `docs/reviews/tool-output/pass3-codex/bandit.log`. A source-focused supplemental run (`bandit -r *.py afm_passes backends`) is saved at `docs/reviews/tool-output/pass3-codex/bandit-focused.log`; it reports 5 High SHA1 findings, matching pre-existing RCM-035 rather than Pass 2's modified files.
- semgrep: `docs/reviews/tool-output/pass3-codex/semgrep.log` reports 21 blocking findings, including pre-existing SHA1, urllib, and dynamic SQL patterns already in the RC register. I did not identify a new Pass 2-specific Semgrep High.
- ruff: `docs/reviews/tool-output/pass3-codex/ruff.log` reports 94 existing violations source-wide. Pass 2's modified-only ruff log is narrower; no new blocker from the Pass 2 delta.
- mypy: `python3 -m mypy .` failed because this Python lacks `mypy` (`docs/reviews/tool-output/pass3-codex/mypy.log`). The repo venv run (`docs/reviews/tool-output/pass3-codex/mypy-venv.log`) reports 93 errors in 23 files, consistent with the known Phase 3 type backlog.
- npm-audit: `docs/reviews/tool-output/pass3-codex/npm-audit.log`: `found 0 vulnerabilities`.
- pytest: `docs/reviews/tool-output/pass3-codex/pytest.log`: `334 passed, 3 skipped in 17.15s`.
- npm-test: `docs/reviews/tool-output/pass3-codex/npm-test.log`: `133 passed, 0 failed`.

## Coverage gaps
- RCM-008 still lacks the required concurrent-writer no-drop test.
- RCM-008 lacks a non-bypass production-path test for `vaultFirstLearn` / `sovereign_learn`.
- The principal-fixture replacement does not fully match the canonical fixed-principal pattern from `engine/test_principal_binding.py:86`; it dynamically writes whatever principal the caller supplied.

## Scope creep (if any)
No material scope creep found. Pass 2's changed files map to test-integrity repair, RCM-004, RCM-008, npm audit cleanup, and smoke-test repair. The small `team-repetition.test.mjs` change is an audit-rate-limit bypass for existing tests, but it is also part of the masking problem above.

## Hand-wave test assertions
- `plugins/sovereign-memory/tests/agent-ping.test.mjs:247` checks only that a wrong principal does not see the request in returned results; it does not assert the wrong-principal vault remains unmaterialized.
- `plugins/sovereign-memory/tests/agent-ping.test.mjs:249` accepts `/ENOENT|only pending requests|Only the recipient agent/`, which is too broad to prove the intended authorization path.
- `plugins/sovereign-memory/tests/audit-rcm008.test.mjs:47` and `:105` use `assert.ok(okPath)` for successful audit writes; the second test adds the mode assertion, but the first is largely redundant and does not assert partitioning or file identity.

## Recommendation
NO-GO. Have Antigravity or Claude do a small Pass 4 patch focused on RCM-008 only:
- Change audit rate limiting so it cannot fail normal operations like `sovereign_learn`.
- Add non-bypass regression coverage for `vaultFirstLearn` / `sovereign_learn`.
- Add the missing concurrent-writer no-drop test and fix any race it exposes.
- Optionally tighten the scoped principal fixtures so behavioral suites use fixed temp principal files rather than caller-matching resolver wrappers.

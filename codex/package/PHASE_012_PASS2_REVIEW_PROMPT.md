# Sovereign Memory RC1 — Pass 2 External Review (Codex)

Paste this into Codex desktop as the first message in a fresh session.

---

## What this is

You (Codex) are doing an external adversarial review of Antigravity's Pass 2 implementation of `docs/RC_PLAN.md` Phase 0/1/2 on branch `rc1-phase-012`. Two implementation passes have happened on this branch:

- **Pass 1 (Grok Build, `/implement effort=5`)** — produced ~80% of the work in 45 min. Snapshot at commit `7918b52`.
- **Pass 2 (Antigravity 2.0, Gemini)** — a full end-to-end re-pass on top of Pass 1, closing the test-integrity bugs, completing the partial RCMs, and clearing the npm audit High vuln.

Gemini already reviewed Pass 1 (the review file is on disk). Your role is **fourth-orchestrator perspective on Pass 2's delta**. You haven't been involved in this branch yet — that's the value you bring.

This is **read-only**. You modify no source code. You write only review artifacts.

---

## Required reading (in order)

Read all of these IN FULL before writing your review.

1. **`/Users/hansaxelsson/Projects/sovereignMemory/docs/RC_PLAN.md`** — the spec. Decision Log + Unified Findings Register + Phase 0/1/2 are your acceptance criteria.
2. **`/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/rc1-phase-012/docs/implementation/rc1-phase-012-grok.md`** — Pass 1 (Grok) implementation summary. What was originally delivered.
3. **`/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/rc1-phase-012/docs/reviews/rc1-phase-012-gemini-review.md`** — Pass 1 review (Gemini). What Pass 1 got wrong, scoped by severity.
4. **`/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/rc1-phase-012/docs/implementation/rc1-phase-012-pass2-antigravity.md`** — **Pass 2 (Antigravity) implementation summary.** What Pass 2 claims to have done. This is what you're reviewing.
5. **`/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/rc1-phase-012/docs/reviews/tool-output/pass2/*.log`** — Pass 2's own tool evidence. Treat as Pass 2's self-reported state; verify by running your own tools.

---

## The branch under review

Worktree path: `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/rc1-phase-012`
Branch: `rc1-phase-012`
Baseline (pre-Pass-1): `main`
Pass 1 snapshot: commit `7918b52`
Pass 2 HEAD: whatever the latest commit is at the time you run this

Run `git diff 7918b52..HEAD --stat` to see Pass 2's delta. Run `git diff main..HEAD --stat` to see the full Phase 0/1/2 scope from baseline.

---

## What to verify — three layers

### Layer 1 — Did Pass 2 actually fix what Pass 1's Gemini review flagged?

For each of Gemini's Pass 1 findings, check the current branch state and classify the status:

- **Test pollution gone?**
  - Run: `grep -l "_permissive_resolve\|G11 test relaxation" engine/test_*.py`
  - Expected: zero hits. Any hit = pollution still present.
  - Then run: `grep -n "monkeypatch\.setattr.*resolve_effective_principal" engine/test_*.py` to see how Pass 2 replaced the patches. Permissive impostors via `monkeypatch.setattr` are still wrong — there should be real principal files via `tmp_path` instead.
  - Verify by reading the canonical pattern: `engine/test_principal_binding.py:86-103, 203-231`. Pass 2's fixtures should mirror this shape.

- **Test integrity verified?**
  - Run: `cd engine && PYTHONPATH=. python3 -m pytest -q`
  - Expected: 0 failures. **Critically**, `test_principal_binding.py` and `test_vault_root_binding.py` must pass in the full-suite invocation, not just in isolation. If you see `RuntimeError: principal mismatch` from those files, the pollution is still active in some form.

- **npm audit High cleared?**
  - Run: `cd plugins/sovereign-memory && npm audit --omit=dev`
  - Expected: 0 High vulnerabilities. Document any remaining Moderate.

### Layer 2 — Did Pass 2 complete what Grok Pass 1 deferred?

Two specific RCMs Grok admitted as partial:

- **RCM-004 lease table (ping pre-consent)**
  - Look for lease directory logic: `grep -rn "pings/leases" plugins/sovereign-memory/src/`
  - Read `plugins/sovereign-memory/src/agent_ping.ts` — `syncContract` must NOT call `ensureVault` on the recipient or write to recipient inbox on `ping_request`. Materialization should happen only on explicit `listAgentPingInbox` or `decideAgentPingRequest` calls AND only when the recipient's principal matches.
  - Verify tests exist: `test_ping_request_does_not_create_recipient_inbox`, `test_ping_materializes_on_recipient_poll`, `test_ping_lease_expires_after_ttl`, `test_ping_materialization_rejects_wrong_principal`. Each must assert a concrete behavior, not hand-wave.

- **RCM-008 rotation + quota + rate-limit**
  - Look for rotation logic in `plugins/sovereign-memory/src/vault.ts`. `recordAudit` should check `log.md` size and trigger atomic rename via `fs.rename` at 5 MB threshold.
  - Look for daily-log prune logic (anything older than 30 days deleted).
  - Look for quota enforcement (50 MB total; oldest daily logs pruned first on overage).
  - Look for per-agent rate-limit timestamp files at `~/.sovereign-memory/.hook-audit-ts/<agent>.ts` with mode `0o600`. **Must be per-agent**, not a single shared file — flag if you see a shared file.
  - Look for status exposure: `engine/sovrd.py:_handle_status` should return an `audit_volume` field; `plugins/sovereign-memory/src/sovereign.ts:buildStatusReport` should expose it on the TS side with matching schema (bytes).
  - Verify tests: `test_audit_rotates_at_5mb_threshold`, `test_audit_daily_logs_pruned_after_30_days`, `test_audit_quota_prunes_oldest_first`, `test_audit_hook_rate_limit_per_agent_partitions`, `test_audit_concurrent_writers_no_drop`, `test_status_exposes_audit_volume`.

### Layer 3 — What did Pass 2 itself break or miss?

- Run your own tool pass — don't trust Pass 2's logs without verification:
  - `cd engine && bandit -r .` — compare to `docs/reviews/tool-output/pass2/bandit.log`. Any new High that Pass 2 didn't capture?
  - `cd engine && ruff check .` — compare to Pass 2's ruff log. New violations?
  - `cd engine && python3 -m mypy .` — compare to Pass 2's mypy log. Same diff or did Pass 2 introduce new type errors?
  - `cd plugins/sovereign-memory && npm audit --omit=dev` — same as Layer 1.

- Read the diff yourself: `git diff 7918b52..HEAD`. Look specifically for:
  - **Scope creep** — files touched that don't map to a Phase 0/1/2 RCM. If Pass 2 fixed something from Phase 3 (e.g., the dynamic SQL templates from RCM-012), flag it as scope creep — should be a separate PR.
  - **Coverage gaps** — RCMs in Phase 0/1/2 that the spec says should be done but the branch still doesn't address.
  - **Hand-wave test assertions** — tests that say `assert resp is not None` or `# doesn't block` instead of asserting concrete thresholds. Flag every one.
  - **Schema coherence** — does the Python `_handle_status` audit-volume telemetry agree with the TypeScript `buildStatusReport` on shape (bytes vs MB, field names)? Flag any mismatch.
  - **Symlink test coverage** — does the RCM-005 test suite actually create a symlink and assert rejection, or does it only test path-string parsing? `find engine plugins -name 'test_*' -exec grep -l 'symlink\|os\.symlink' {} +` then read those tests.

---

## Hard constraints

- **READ-ONLY.** Modify no source code. Write only to `docs/reviews/rc1-phase-012-codex-review.md` (under the `rc1-phase-012` worktree) and any tool-output logs under `docs/reviews/tool-output/pass3-codex/`.
- **No re-litigation.** Issues that Gemini's Pass 1 review already debated and that Pass 2's diff settled (with a resolution either in code or in the implementation summary's "Decisions" section) — do not re-open. Focus on NEW findings or NEW evidence that contradicts Pass 2's claims.
- **Cite file:line** for every finding. Every claim of "Pass 2 missed X" must reference a specific file path and line range.
- **Tool output as evidence.** Findings that come from your own tool runs get the raw log saved under `docs/reviews/tool-output/pass3-codex/<tool>.log` and referenced from the review doc.

---

## Output

Write your review to:

**`/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/rc1-phase-012/docs/reviews/rc1-phase-012-codex-review.md`**

With this structure:

```
# Pass 3 External Review (Codex) — RC1 Phase 0/1/2

**Date:** <today>
**Branch reviewed:** rc1-phase-012
**Pass 2 implementation summary:** docs/implementation/rc1-phase-012-pass2-antigravity.md
**Pass 1 review (Gemini, for context):** docs/reviews/rc1-phase-012-gemini-review.md

## Verdict
GO / GO-WITH-CONDITIONS / NO-GO (with one-paragraph reasoning)

## Layer 1 — Pass 2 vs Gemini's Pass 1 findings
| Pass 1 finding | Pass 2 status | Evidence |
|---|---|---|
| Test pollution (module-level monkey-patch in 4 files) | RESOLVED / PARTIAL / STILL-BROKEN | grep output, line range |
| Test pollution (function-scoped in test_pr10) | RESOLVED / PARTIAL / STILL-BROKEN | ... |
| Test integrity (full-suite pytest passes) | RESOLVED / PARTIAL / STILL-BROKEN | pytest output |
| npm audit High (fast-uri) | RESOLVED / PARTIAL / STILL-BROKEN | npm audit output |
| npm audit Moderate (hono, ip-address) | RESOLVED / PARTIAL / STILL-BROKEN | npm audit output |

## Layer 2 — Pass 2 vs Grok's partials
| Partial | Pass 2 status | Evidence |
|---|---|---|
| RCM-004 lease table | COMPLETED / PARTIAL / NOT-DONE | grep for pings/leases/, test names |
| RCM-008 rotation+quota+rate-limit | COMPLETED / PARTIAL / NOT-DONE | grep for fs.rename in vault.ts, test names, per-agent timestamp check |

## Layer 3 — New findings
| Severity | RCM-NNN (if applicable) | File:line | Summary | Evidence (tool log or code reading) | Recommended fix |
|---|---|---|---|---|---|

## Tool evidence diff
Compare your runs to Pass 2's `tool-output/pass2/*.log`:
- bandit: <summary; highlight any new findings>
- semgrep: ...
- ruff: ...
- mypy: ...
- npm-audit: ...
- pytest: ...
- npm-test: ...

## Coverage gaps
RCMs from RC_PLAN.md Phase 0/1/2 that this branch still doesn't address (per your own reading, not Pass 2's claims).

## Scope creep (if any)
Files modified that don't map to a Phase 0/1/2 RCM. Flag for split into separate PR.

## Hand-wave test assertions
Tests with non-concrete assertions. List by test name + file:line.

## Recommendation
What should happen next:
- If GO: open PR, merge to main.
- If GO-WITH-CONDITIONS: list the 1-2 conditions; suggest who patches them (Claude inline, or another orchestrator pass).
- If NO-GO: list the 3-5 must-fix items and recommend which orchestrator does Pass 4.
```

---

## Verdict criteria

- **GO** — All Phase 0/1/2 RCMs are correctly addressed. Test integrity restored. Tool runs clean (no new High vs Pass 1 baseline). No scope creep. Tests have concrete assertions.

- **GO-WITH-CONDITIONS** — Mostly correct, but ≥1 P2 finding or ≥1 small coverage gap (e.g., one test with hand-wave assertion, one stale doc reference). List conditions explicitly. The conditions should be tractable by a single small inline patch (~1 hour of work), not another full pass.

- **NO-GO** — ≥1 new P0/P1 finding from your own tool runs, OR ≥1 of Grok's partials still not actually completed, OR ≥1 Phase 0/1/2 RCM not correctly addressed (e.g., RCM-008 rotation present but uses a single shared rate-limit file instead of per-agent), OR test pollution still present in any form. List the must-fix items and recommend which orchestrator does Pass 4 (Antigravity again for implementation, Claude for small patches, Grok if it's a deep multi-critic refactor).

---

Begin.

# Sovereign Memory RC1 — Phase 0, 1, 2 External Review (Antigravity)

Run this in Antigravity 2.0 (Desktop Manager view preferred for tool-grounded validation; CLI works if Desktop unavailable). Use Gemini 3.5 Flash (High) for the orchestrator and Claude Sonnet 4.6 for the second-opinion pass where multi-model fan-out applies.

---

## Review prompt

You are performing an **external adversarial review** of a Grok-produced implementation. Grok `/implement effort=5` has already run its internal 6-reviewer critic loop to zero issues. Your job is to find what its critics missed.

### Context

- **Spec being implemented:** `/Users/hansaxelsson/Projects/sovereignMemory/docs/RC_PLAN.md`, Phase 0 + Phase 1 + Phase 2 (RCM-001 through RCM-011 in scope).
- **Implementation branch:** `rc1-phase-012` in `/Users/hansaxelsson/Projects/sovereignMemory/`.
- **Grok's implementation summary:** `/Users/hansaxelsson/Projects/sovereignMemory/docs/implementation/rc1-phase-012-grok.md` — read this to know what Grok claims to have done and which RCM-NNN map to which files.
- **Grok's internal review file:** path is documented in the summary above (look for `/tmp/grok-review-<IMPL_ID>.md`). Read this to know what Grok's internal critics already debated. Do NOT re-litigate settled issues there.

### What you're checking

Three layers, in order:

**Layer 1 — Did Grok do what the spec said?**

For each in-scope RCM-NNN in RC_PLAN.md Phase 0/1/2, verify the "Next action" column was genuinely addressed. Examples:

- RCM-002 — was `vaultPath` removed from ALL model-facing zod schemas in `server.ts`, `task.ts`, `vault.ts`? Or only some?
- RCM-009 — was the fix applied to BOTH `engine/sovrd.py` `_handle_status`/`_handle_trace` AND `plugins/sovereign-memory/src/server.ts` `sovereign_resolve_candidate`? If only one location, flag it.
- RCM-007 — does `grep -n "time.sleep" engine/sovrd.py` return zero hits in async-context paths now?
- RCM-008 — does the audit rotation actually rotate at 5MB? Run a test.

**Layer 2 — Did Grok's critics miss subtle issues?**

These are the high-blast-radius holes that single-orchestrator reviewers often miss:

- **RCM-004 opt-in heuristic.** Confirm the consent model is per-handoff lease, NOT persistent vault-presence-on-disk. If Grok used the simpler "vault exists = opted in" heuristic, that's a P0 — it defeats consent (initial setup becomes the only gate forever).
- **Hook rate-limit timestamp file permissions and partitioning.** Is the timestamp file world-readable? Single shared file or per-agent? Single shared = starvation surface.
- **Audit rotation race-safety.** Run a concurrent-write test: two appenders racing during rotation. Are events dropped? Is `fs.rename` atomic in this context?
- **Test assertion concreteness.** Are tests using concrete thresholds (`assert latency < 50ms`) or hand-wavy ("doesn't block")? Hand-wavy = flag.
- **Schema coherence.** Does the Python `_handle_status` audit-volume telemetry agree on shape with the TypeScript `buildStatusReport`? Or did one side use bytes and the other MB?
- **Symlink test coverage.** Does `RCM-005`'s test suite actually create a symlink and assert rejection, or does it only test path-string parsing?
- **Default principal mode coverage.** Does `RCM-003`'s test suite cover (a) strict + principal file, (b) no-principals + supplied "main", (c) no-principals + supplied "other" → reject? If only (a) and (b), flag (c) as missing.

**Layer 3 — Tool-grounded validation.**

Use Managed Agents to run real tools on the changed files:

- `bandit -r engine/` (Python security)
- `semgrep --config=auto plugins/sovereign-memory/src/ engine/` (cross-language SAST)
- `ruff check engine/` (Python lint)
- `mypy engine/` (type errors)
- `cd plugins/sovereign-memory && npm audit --omit=dev` (npm vulns)
- `cd plugins/sovereign-memory && tsc --noEmit` (TypeScript type check)
- If a Python or Node test exists, run it: `pytest -q` and `npm test`

Compare tool output to Grok's known fixes. Surface anything new the tools caught that Grok's diff didn't address.

### Diff scope

Get the diff: `git diff main...rc1-phase-012`. Confine your review to files this diff touches, plus any test files that should have been touched but weren't.

### Output

Write to `/Users/hansaxelsson/Projects/sovereignMemory/docs/reviews/rc1-phase-012-gemini-review.md` with the following structure:

```
# RC1 Phase 0/1/2 — External Review (Antigravity / Gemini 3.5 Flash)
**Date:** <today>
**Branch reviewed:** rc1-phase-012
**Grok implementation summary:** docs/implementation/rc1-phase-012-grok.md

## Verdict
GO / GO-WITH-CONDITIONS / NO-GO (with reasoning)

## New findings (issues Grok's internal critics missed)
| Severity | RCM-NNN | File:line | Summary | Evidence | Recommended fix |
|---|---|---|---|---|---|
...

## Confirmations (where I verified Grok's fix works)
| RCM-NNN | What I checked | How I verified | Result |
|---|---|---|---|
...

## Disagreements with Grok's wontfix decisions
| Item | Grok's reasoning | My counter | Recommendation |
|---|---|---|---|
...

## Tool-output evidence
- bandit.log: <summary + paste of any new high findings>
- semgrep.log: <summary>
- mypy.log: <summary>
- npm-audit.log: <summary>
- pytest.log: <pass/fail summary>
- npm test.log: <pass/fail summary>

## Coverage gaps in Grok's tests
<list test files that should exist but don't, or test cases that should exist but don't>

## Cross-model dissent (if Manager view multi-model fan-out is used)
<places where Gemini and Claude reviewers disagreed; document the disagreement and your synthesized resolution>
```

### Verdict criteria

- **GO** — All Phase 0/1/2 RCM-NNN are addressed correctly. Tool runs clean. Tests pass. No new findings of severity P0/P1.
- **GO-WITH-CONDITIONS** — Mostly correct, but ≥1 P2 finding or a coverage gap that should land before merge. List the conditions explicitly.
- **NO-GO** — ≥1 new P0/P1 finding, OR a Phase 0/1/2 RCM-NNN wasn't addressed correctly (e.g., RCM-009 fix only hit ui-server.ts but missed sovrd.py).

### Hard rules

- **Read-only.** Do NOT modify any source. Write only to `docs/reviews/rc1-phase-012-gemini-review.md` and any tool-output logs under `docs/reviews/tool-output/`.
- **No re-litigation.** If Grok's internal review file shows an issue was debated and resolved (`Status: fixed` or `Status: wontfix` with justification), don't re-open it unless you have NEW evidence the resolution is wrong.
- **Cite file:line.** Every new finding must cite a path + line range.
- **Tool output as evidence.** Bandit/semgrep findings get attached as raw log paths under `docs/reviews/tool-output/`.

Begin.

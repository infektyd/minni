# RESUME — Sovereign Memory v3.1 → v4 Dispatch State

> **Read this file first if you are picking up dispatch.** It is self-contained: you do not need to read the dispatch plan or any skill file to resume.

---

## Resume instructions for incoming agent

1. Read this entire file.
2. Read `00_MASTER_TRACKER.md` for wave-level status (`[ ]` pending, `[A]` in-flight, `[x]` merged).
3. For any PR marked `IN_FLIGHT` in the table below:
   - Read its worktree's `WORKTREE_STATE.md` (path listed below).
   - If the last commit looks sane and `pytest -q` passes in the worktree → re-dispatch the implementer with the "continuation prompt" template at the bottom of this file, supplying the last commit SHA and the un-done items from `WORKTREE_STATE.md`.
   - If the worktree is corrupt → `git worktree remove --force <path>`, recreate from the orchestration branch, restart that PR.
4. For queued PRs: follow the wave plan below. Use the "fresh dispatch prompt" template at the bottom.
5. After every commit/merge/tracker-flip, **commit this file** so the next resume sees the new state.

---

## Topology

- **Root repo:** `/Users/hansaxelsson/sovereignMemory` — branch `codex/reconcile-gemini-main`. **Do not touch.** Dirty working tree is intentional.
- **Orchestration worktree:** `/Users/hansaxelsson/sovereignMemory/.claude/worktrees/orchestration` — branch `orchestration/v3.1-to-v4`. This is where RESUME.md and the tracker are committed; per-PR branches are merged here.
- **Per-PR worktrees:** `/Users/hansaxelsson/sovereignMemory/.claude/worktrees/pr-N-shortname` — created on dispatch, merged + removed on completion.
- **Source-of-truth spec:** `docs/plans/SOVEREIGN-MEMORY-CORE-UPGRADES-SCALE-AGNOSTIC.md` (v1, the `-v2` is stale and excluded from this branch).
- **Final integration target:** `main`. The orchestration branch will be merged to `main` only when the user says so.

---

## Wave plan (aggressive, no reviewers)

| Wave | PRs | Mode |
|---|---|---|
| W1 | PR-1 | solo |
| W2 | PR-1b | solo |
| W3 | PR-2 | solo |
| W4 | PR-3 ∥ PR-4 ∥ PR-6 | parallel ×3 |
| W5 | PR-5 ∥ PR-9 | parallel ×2 |
| W6 | PR-7 ∥ PR-8 ∥ PR-15 | parallel ×3 |
| W7 | PR-10 ∥ PR-11 | parallel ×2 |
| W8 | PR-12 | solo |
| W9 | PR-13 | solo |
| W10 | PR-14 | solo |

**Skipped by user decision:** spec-compliance reviewer, code-quality reviewer. Implementer self-review only. Mechanical verification (pytest + npm test + JSON-RPC contract) still runs per PR.

---

## PR dispatch table

| PR | Wave | Status | Worktree | Branch | Last SHA | Notes |
|----|------|--------|----------|--------|----------|-------|
| PR-1 | W1 | MERGED | (removed) | `pr-01-foundation` | `7a09fc4` | Merged via `--no-ff` into orchestration. 13/13 pytest, 29/29 npm. |
| PR-1b | W2 | MERGED | (removed) | `pr-01b-contracts` | `cafb5bb` | 31/31 pytest, 29/29 npm. |
| PR-2 | W3 | MERGED | (removed) | `pr-02-faiss-envelope` | `be4492e` | 80 passed, 3 skipped (faiss-cpu missing); 29/29 npm; cold-start 15.7ms. |
| PR-3 | W4 | MERGED | `.claude/worktrees/pr-03-storage` | `pr-03-storage` | `2e01a2d` | Merged into orchestration. 84 passed, 3 skipped; vector CLI status passed. |
| PR-4 | W4 | MERGED | `.claude/worktrees/pr-04-eval-harness` | `pr-04-eval-harness` | `fc52e89` | Merged into orchestration. 121 passed, 3 skipped; mock harness baseline R@5=0.9608. |
| PR-6 | W4 | MERGED | `.claude/worktrees/pr-06-contradictions` | `pr-06-contradictions` | `38e0647` | Merged into orchestration. 114 passed, 3 skipped; focused contradictions suite 34 passed. |
| PR-5 | W5 | MERGED | `.claude/worktrees/pr-05-cache-layers` | `pr-05-cache-layers` | `3da5caf` | Merged into orchestration as `c5ba9e5`. Focused W5 integration tests 64 passed, 3 skipped. Post-merge DB init ordering fix included in orchestration. |
| PR-9 | W5 | MERGED | `.claude/worktrees/pr-09-feedback-trace` | `pr-09-feedback-trace` | `78987b1` | Merged into orchestration as `1dcb6d4`; additive conflicts resolved in `WORKTREE_STATE.md` and `retrieval.py`. Focused W5 integration tests 64 passed, 3 skipped. |
| PR-7 | W6 | MERGED | `.claude/worktrees/pr-07-query-expansion` | `pr-07-query-expansion` | `ccc60ae` | Merged into orchestration as `c6bc45e`. Worker concern: npm unavailable in isolated worktree; wave-level npm verification covers this. |
| PR-8 | W6 | MERGED | `.claude/worktrees/pr-08-hyde` | `pr-08-hyde` | `ad72008` | Merged into orchestration as `c2206ed`; additive conflicts resolved in `WORKTREE_STATE.md`, `config.py`, `eval/harness.py`, and `retrieval.py`. |
| PR-15 | W6 | MERGED | `.claude/worktrees/pr-15-quant-semantic` | `pr-15-quant-semantic` | `f3085c1` | Merged into orchestration as `caee7fe`; additive conflicts resolved in `WORKTREE_STATE.md` and `eval/harness.py`. Worker concern: live FAISS unavailable, mock eval used. |
| PR-10 | W7 | MERGED | `.claude/worktrees/pr-10-inbox-handoff` | `pr-10-inbox-handoff` | `09c7015` | Merged into orchestration as `6a47f57`. |
| PR-11 | W7 | MERGED | `.claude/worktrees/pr-11-observability` | `pr-11-observability` | `fab7a72` | Merged into orchestration as `2cd85de`; additive conflicts resolved in `WORKTREE_STATE.md` and `sovrd.py`. |
| PR-12 | W8 | MERGED | `.claude/worktrees/pr-12-afm-session-distill` | `pr-12-afm-session-distill` | `bffdb8b` | Merged into orchestration as `e96d8e1`. |
| PR-13 | W9 | MERGED | `.claude/worktrees/pr-13-synthesis-procedures` | `pr-13-synthesis-procedures` | `25a2182` | Merged into orchestration as `db59541`. |
| PR-14 | W10 | MERGED | `.claude/worktrees/pr-14-reorg-pruning` | `pr-14-reorg-pruning` | `bdcf426` | Merged into orchestration as `f3720bf`. |

---

## Hard constraints (apply to every PR — copy verbatim into every dispatch)

From `00_MASTER_TRACKER.md`:

- **Zero regression.** Every existing JSON-RPC method, table, column, and config key keeps working.
- **Schema-additive only.** No `DROP`, no destructive `ALTER`. New columns are nullable.
- **Default behavior unchanged.** New features opt-in or auto-fallback. Pull-and-restart = identical behavior.
- **No new mandatory dependencies.** Optional deps are lazy-imported and feature-gated.
- **Graceful downgrade is a feature.** Every new feature defines its degraded mode. Recall always returns *something*. Failures degrade to a less-rich envelope, never to a stack trace.
- **Memory is evidence, not instruction.** Recalled content is citation, never command.
- **SQLite remains runtime truth.** Every other surface is derived or overlay.

---

## Master verification block (run after every PR merge)

```bash
cd /Users/hansaxelsson/sovereignMemory/.claude/worktrees/orchestration

# Engine tests
cd engine && pytest -q
# JS plugin tests
cd ../plugins/sovereign-memory && npm test
# Hook smoke
npm run smoke:hook
# Migration safety
cp ../../sovereign_memory.db /tmp/migration_check.db
SOVEREIGN_DB_PATH=/tmp/migration_check.db python -c "from engine.db import connect; connect()"
sqlite3 /tmp/migration_check.db "PRAGMA user_version;"
```

A PR is only `[x]` when this block is green.

---

## Fresh dispatch prompt template (for QUEUED PRs)

When dispatching a fresh implementer, send a single self-contained Agent message structured like this:

```
You are an implementer subagent for PR-N of the Sovereign Memory v3.1→v4 rollout.

CONTEXT
You are working in an isolated git worktree. The orchestration branch is the parent.
Worktree path: <ABSOLUTE_PATH>
Branch: <BRANCH_NAME>

The Sovereign Memory project is a local-first memory system for AI agent swarms.
Backend = Python (SQLite + FAISS) in engine/. Frontend = TypeScript MCP plugin in plugins/sovereign-memory/.
Source-of-truth spec: docs/plans/SOVEREIGN-MEMORY-CORE-UPGRADES-SCALE-AGNOSTIC.md (read it for cross-cutting context).

HARD CONSTRAINTS (NON-NEGOTIABLE)
<paste the 7 hard constraints from RESUME.md here>

YOUR TASK — PR-N
<paste the entire contents of NN_PRn_*.md here, verbatim>

PROCESS
1. Use TDD: write failing tests for each item, then implement.
2. After EVERY commit, append a one-line entry to docs/plans/execution/WORKTREE_STATE.md
   in this worktree with: ISO timestamp, commit SHA, files changed, test status,
   one-sentence "next step". This is the resume marker — do not skip it.
3. Run the PR's verification block. If anything fails, fix; do not commit broken state.
4. When all PR-N completion-checklist items are ticked AND verification is green, commit
   a final "PR-N complete" commit and report status DONE with a summary of files touched
   and final commit SHA.

FAILURE MODES
- DONE: all items checked, verification green, ready for merge.
- DONE_WITH_CONCERNS: complete but flag specific concerns.
- NEEDS_CONTEXT: missing info I (orchestrator) need to provide.
- BLOCKED: cannot proceed; explain blocker and suggest fix.

DO NOT: touch files outside the PR-N spec; touch other agents' worktrees; modify the
orchestration branch directly; modify root.
```

---

## Continuation prompt template (for IN_FLIGHT PRs after rate-limit handoff)

```
You are continuing PR-N. The previous implementer was interrupted.

WORKTREE: <ABSOLUTE_PATH>
BRANCH: <BRANCH_NAME>
LAST GOOD COMMIT: <SHA>

Read docs/plans/execution/WORKTREE_STATE.md in this worktree first — it lists every commit
made so far and the "next step" the previous agent intended.

Then verify the worktree state:
  git status   # should be clean
  pytest -q    # should be green at <SHA>

If those pass, resume from the "next step" line in WORKTREE_STATE.md.
If they don't, report status BLOCKED with details — do NOT try to fix the previous
implementer's work without clear instruction.

Original task spec follows for full context:
<paste the entire contents of NN_PRn_*.md here, verbatim>

Hard constraints follow:
<paste the 7 hard constraints>

Process and failure modes are identical to the fresh dispatch.
```

---

## Per-wave action log (orchestrator appends here after each event)

| Timestamp (UTC) | Event |
|---|---|
| 2026-04-26T19:25Z | Orchestration branch created from `codex/reconcile-gemini-main` HEAD `6a5bbe0`. |
| 2026-04-26T19:25Z | Baseline snapshot commit on `orchestration/v3.1-to-v4`: 16 PR docs + master tracker + spec. |
| 2026-04-26T19:30Z | RESUME.md written. PR-1 marked IN_FLIGHT, worktree created, implementer dispatched. |
| 2026-04-26T19:50Z | PR-1 implementer reported DONE: 7a09fc4 (foundation) + 2b9c5f4 (state). 13/13 pytest, 29/29 npm. |
| 2026-04-26T19:51Z | PR-1 merged into orchestration via --no-ff. pytest re-verified green. |
| 2026-04-26T19:52Z | PR-1 worktree removed. PR-1b worktree created off orchestration HEAD. PR-1b dispatched. |
| 2026-04-26T20:05Z | PR-1b implementer reported DONE: cafb5bb + e9988d0. 31/31 pytest, 29/29 npm. |
| 2026-04-26T20:06Z | PR-1b merged into orchestration via --no-ff. PR-2 worktree created. PR-2 dispatched. |
| 2026-04-26T20:25Z | PR-2 implementer reported DONE: be4492e + 65519e1. 80 passed, 3 skipped, 29/29 npm. PR-2 merged. |
| 2026-04-26T20:30Z | INFRA FIX: patched engine/migrations.py to track applied migrations by name (schema_migrations table). Old runner gated on user_version, which would silently skip migration 004 (PR-5) after 005 (PR-6) lands in W4. Back-fills automatically against existing user_version. 80 prior tests still green. |
| 2026-04-26T20:32Z | W4 dispatch: PR-3, PR-4, PR-6 worktrees created off orchestration HEAD. All three implementers dispatched in parallel. |
| 2026-04-26T18:00Z | W4 recovered after interruption: PR-3, PR-4, and PR-6 partial worktrees were finished, tested, committed, and merged into orchestration. Merge conflicts were additive only (`WORKTREE_STATE.md`, `config.py`, `test_pr2_envelope.py`). Tracker updated to `[x]`. |
| 2026-04-26T18:03Z | W4 verification passed on orchestration: `pytest -q` 159 passed / 3 skipped; plugin `npm test` 29/29 passed after local `npm install`; `npm run smoke:hook` returned a valid envelope; migration safety on `/tmp/migration_check.db` reported `PRAGMA user_version = 5`. |
| 2026-04-26T18:20Z | W5 dispatch checkpoint: PR-5 and PR-9 worktrees created off orchestration HEAD `0a8d6a5`; tracker marked both `[A]`; implementers queued for parallel work. |
| 2026-04-26T18:31Z | W5 merge: PR-5 merged cleanly as `c5ba9e5`; PR-9 merged as `1dcb6d4` after additive `retrieval.py`/`WORKTREE_STATE.md` conflict resolution. Focused tests `pytest -q engine/test_pr5_cache_layers.py engine/test_pr9_feedback_trace.py engine/test_pr2_envelope.py` passed: 64 passed, 3 skipped. |
| 2026-04-26T18:38Z | W5 verification passed after orchestration fix: `cd engine && pytest -q` 174 passed / 3 skipped; `cd plugins/sovereign-memory && npm test` 29/29 passed; `npm run smoke:hook` returned valid envelope; migration safety on `/tmp/migration_check_w5.db` applied migrations 001-006, preserved 292 documents and 711 chunks, `PRAGMA user_version = 6`. |
| 2026-04-26T18:44Z | W6 dispatch checkpoint: PR-7, PR-8, and PR-15 worktrees created off W5-complete orchestration HEAD `6c39436`; tracker marked all three `[A]`; implementers queued for parallel work. |
| 2026-04-26T18:57Z | W6 merge: PR-7 merged cleanly as `c6bc45e`; PR-8 merged as `c2206ed` after additive conflicts; PR-15 merged as `caee7fe` after additive conflicts. Focused W6 tests `pytest -q engine/test_pr7_query_expansion.py engine/test_pr8_hyde.py engine/test_pr15_quant_semantic.py engine/test_pr4_eval_harness.py` passed: 57 passed. |
| 2026-04-26T19:03Z | W6 verification passed on orchestration: `cd engine && pytest -q` 190 passed / 3 skipped; `cd plugins/sovereign-memory && npm test` 29/29 passed; `npm run smoke:hook` returned valid envelope; migration safety on `/tmp/migration_check_w6.db` preserved 292 documents and 711 chunks, `PRAGMA user_version = 6`. |
| 2026-04-26T19:08Z | W7 dispatch checkpoint: PR-10 and PR-11 worktrees created off W6-verified orchestration HEAD `262cccd`; tracker marked both `[A]`; implementers queued for parallel work. |
| 2026-04-26T19:21Z | W7 merge: PR-10 merged cleanly as `6a47f57`; PR-11 merged as `2cd85de` after additive `sovrd.py`/`WORKTREE_STATE.md` conflict resolution. Focused tests `pytest -q engine/test_pr10_handoff.py engine/test_pr11_observability.py engine/test_pr9_feedback_trace.py` passed: 16 passed. Plugin `npm test` passed: 31/31. |
| 2026-04-26T19:27Z | W7 verification passed on orchestration: `cd engine && pytest -q` 200 passed / 3 skipped; `cd plugins/sovereign-memory && npm test` 31/31 passed; `npm run smoke:hook` returned valid envelope with handoff context surface; migration safety on `/tmp/migration_check_w7.db` preserved 292 documents and 711 chunks, `PRAGMA user_version = 6`. |
| 2026-04-26T19:31Z | W8 dispatch checkpoint: PR-12 worktree created off W7-verified orchestration HEAD `57defa5`; tracker marked `[A]`; implementer queued. |
| 2026-04-26T19:38Z | W8 merge: PR-12 merged cleanly as `e96d8e1`; final worktree bookkeeping committed as `bffdb8b`. |
| 2026-04-26T19:46Z | W8 verification passed on orchestration: `cd engine && pytest -q` 206 passed / 3 skipped; `cd plugins/sovereign-memory && npm test` 32/32 passed; `npm run smoke:hook` returned valid envelope; AFM compile dry-run with `SOVEREIGN_AFM_LOOP=off` returned clean `afm_unavailable`; migration safety on `/tmp/migration_check_w8.db` preserved 292 documents and 711 chunks, `PRAGMA user_version = 6`. |
| 2026-04-26T19:50Z | W9 dispatch checkpoint: PR-13 worktree created off W8-verified orchestration HEAD `3ccddae`; tracker marked `[A]`; implementer queued. |
| 2026-04-26T20:07Z | W9 merge: PR-13 merged cleanly as `db59541`; worker verification before merge reported `pytest -q engine` 209 passed / 3 skipped and plugin `npm test` 32/32 passed. |
| 2026-04-26T20:13Z | W9 verification passed on orchestration: `cd engine && pytest -q` 209 passed / 3 skipped; `cd plugins/sovereign-memory && npm test` 32/32 passed; `npm run smoke:hook` returned valid envelope; migration safety on `/tmp/migration_check_w9.db` preserved 292 documents and 711 chunks, `PRAGMA user_version = 6`. |
| 2026-04-26T20:17Z | W10 dispatch checkpoint: PR-14 worktree created off W9-verified orchestration HEAD `ee5f22d`; tracker marked `[A]`; implementer queued. |
| 2026-04-26T20:34Z | W10 merge: PR-14 merged cleanly as `f3720bf`; worker verification before merge reported `pytest -q engine` 213 passed / 3 skipped and plugin `npm test` 32/32 passed. |
| 2026-04-26T20:41Z | FINAL acceptance passed on orchestration: `cd engine && pytest -q` 213 passed / 3 skipped; `cd plugins/sovereign-memory && npm test` 32/32 passed; `npm run smoke:hook` returned valid envelope; migration safety on `/tmp/migration_check_final.db` preserved 292 documents and 711 chunks, `PRAGMA user_version = 6`; `SOVEREIGN_AFM_LOOP=off python3 -m sovereign_memory compile --pass reorganization --dry-run` and `--pass pruning --dry-run` both returned clean `afm_unavailable` without writes. |
| 2026-04-26T19:27Z | DEEP v4 pre-integration validation passed after fixes: direct in-process JSON-RPC smoke covered `ping`, `status`, `health_report`, `hygiene_report`, `feedback`, `trace`, redacted `daemon.handoff`, and all five `daemon.compile` passes with AFM loop off; AFM loop on dry-run compile produced retrievable traces for all five passes; CLI `hygiene` and `compile` passed with temp state; live daemon + plugin helpers passed `statusAndAudit`, `recallMemory`, `compileVault(synthesis)`, and `handoffMemory` against a temp Unix socket and stopped cleanly on SIGTERM. Fixes included v4 JSON-RPC-first plugin socket helpers with 30s timeout, all compile pass names exposed in the MCP schema, isolated latency histogram test state, and explicit daemon server shutdown. Final verification after fixes: `cd engine && pytest -q` 213 passed / 3 skipped; `cd plugins/sovereign-memory && npm test` 32/32 passed; `npm run smoke:hook` returned valid envelope; migration safety via SQLite backup preserved 292 documents and 711 chunks, `PRAGMA user_version = 6`. |

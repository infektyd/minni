# Local backlog board — 2026-10-23 (not pushed)

Residual work from the recall-to-use Phase 1 fix slices
(`docs/research/2026-10-23-recall-to-use-rev5.md`). **Local commits only.**
Every review named here is a same-lab review (Claude reviewing Claude).

## Landed on local main

| Item | Commits | Evidence |
|------|---------|----------|
| UPS spent-budget work outliving the handler: ENOTEMPTY teardown flake in `hook-behavior.test.mjs` | 532927af, db8f8e14 | Full gate 0 failures on da339447. Flaky test 10/10 under CPU load; an fs tracer shows no post-return write. |
| `make setup` admitted Python 3.15; exact `.python-version` gate | 37cb7246 | Behavioural RED via a fake 3.15 interpreter and a stub uv |
| `diagnose_legacy_aliases.py` opened its DB without `uri=True` | 37cb7246 | The engine suite has 0 failures on uv-managed CPython 3.14 |
| Row `a`: Pin `degraded.daemon_recall === "timed_out"` in UPS spent-budget test | feat/wave2-backlog | Pinned in `ups-spent-budget-no-lingering-lock.test.mjs` (`163`). |
| Row `b`: Apply spent-budget skip and bounded lock wait to `SessionStart` plan read | feat/wave2-backlog | `hook-handlers.ts:1713-1728`; mid-budget and spent-budget held-lock SessionStart tests RED→GREEN in `ups-spent-budget-no-lingering-lock.test.mjs`. |
| Row `c`: Remove `ensureVault` write seeding from read-only `searchVaultNotes` callers | feat/wave2-backlog | `vault.ts:1543-1551` skips `ensureVault` on `"scan"` and whenever 4th-arg `agentId` is omitted (`UPS`, `SubagentStart`, `prepareTask`, `buildHandoffPacket`, `minni_recall` prescan); `vault.test.mjs:947` stays green. |
| Row `d`: Bound `thread-lock.ts` lock wait when a hook holding a plan thread lock is killed | feat/wave2-backlog | `thread-lock.ts:356-380` checks `ownerLooksLive` immediately whenever `observedOwner !== undefined` (matching `withExclusiveReplanReservation`), verified RED→GREEN in `thread-lock.test.mjs`. |
| Row `i`: Refuse non-regular files (FIFOs) with non-blocking opens in `vault.ts` and `hook-handlers.ts` | feat/wave2-backlog | `O_NONBLOCK | O_NOFOLLOW` + `fstat().isFile()` across `vault.ts` (`readRegularTextFile`, `seedOrPromoteVaultFile`, `appendFileWithFsync`, `recordAudit`, `appendIndex`, `auditTail`, `sessionReceipt`, `listSessions`) and `stopCoreOutput` in `hook-handlers.ts`; verified RED→GREEN in `vault-fifo-log.test.mjs`. |
| Row `m`: Switch one-shot CLI `prepare-task` to the `"scan"` candidate path | feat/wave2-backlog | `cli.ts:168` and `task.ts:1070` pass `{ candidates: "scan" }`; pinned RED→GREEN in `vault-search-process-path.test.mjs`. |
| Row `o`: Fix `_`-joined snake_case compound tail candidate selection (`x_foo_bar`, `_x86_64`) | feat/wave2-backlog | `indexNoteTokensIntoMap` and `haystackHasIndexKey` in `vault.ts:1165-1240` index/match `_`-delimited suffix sub-compounds; extended `vault-search-candidates-parity.test.mjs`. |

## Still open (real)

| # | Item | Disposition | Notes |
|---|------|-------------|-------|
| a | Spent-budget test does not pin `degraded.daemon_recall === "timed_out"` | **done (feat/wave2-backlog)** | Pinned in `ups-spent-budget-no-lingering-lock.test.mjs`. |
| b | SessionStart keeps the old pattern: its plan read has no spent-budget skip and no bounded lock wait | **done (feat/wave2-backlog)** | Applied `planBudgetMs <= 0` skip and `lockWaitMs: Math.max(0, planBudgetMs - PLAN_LOCK_SLACK_MS)` in `handleSessionStart` (`hook-handlers.ts`). |
| c | `searchVaultNotes`, a read path, seeds vault files through `ensureVault` | **done (feat/wave2-backlog)** | `searchVaultNotes` no longer seeds on `"scan"` or when `agentId` is omitted. |
| d | A hook killed while holding a plan thread lock blocks writers until `DEFAULT_STALE_MS` (120 s) | **done (feat/wave2-backlog)** | `withThreadLock` checks `ownerLooksLive` immediately whenever `observedOwner !== undefined`, recovering dead-PID locks on the next poll (`stale_dead`). |
| e | Shared test fixture `withRawSessionFixture` (`hook-behavior.test.mjs`) removes its temp root without `maxRetries` | **needs Hans: edits an existing test** | Defence in depth only. The product fix removed the race; the new test file already uses `maxRetries: 5`. |
| f | `tests/test_wire_verify.py` is load-sensitive | note only, no change | 0.3 s handshake timeouts. It passes 10/10 run alone (22 tests each), and failed 2 tests in 1 of 4 full `make test` runs while four suites ran at once. Timeouts unchanged. Run gates one at a time. |
| g | UPS bare `await ensureVault` surfaces a broken vault root as a degraded hook instead of a silent no-op on the empty-prompt and write-intent paths | accepted | Matches SessionStart. No test covers UPS on an escaping vault root either way. |
| h | Idle-machine latency re-measure of S1a-2 (p99), when Hans is home; Phase B lanes kept load at about 3 | when Hans is home | The interleaved A/B (spawned hooks, fake daemon, 100 runs plus warm-up; base, head with the ledger off, head with it on) was measured at load ~3, so its p95/p99 are noisy. Re-run the same three columns on an idle machine. |
| i | A FIFO in place of the vault's `log.md` blocks Stop in `ensureVault` and again in the audit append | **done (feat/wave2-backlog)** | Non-blocking `O_NONBLOCK | O_NOFOLLOW` + `fstat().isFile()` guards in `vault.ts` and `hook-handlers.ts`, verified by `vault-fifo-log.test.mjs`. |
| j | `<prefix>_session_start` audit rows are subject to the 5 s per-event audit throttle, so two boots within 5 s keep one row | **needs Hans: edits two existing tests** | Exempting the event would change `audit-rcm008.test.mjs` (its duplicate example is `hook_session_start`, and its permissions test stats that event's rate-limit file). Until then it is a rev 5 §8.2 caveat: a dropped row loses one sample of the ledger-off stretches. |
| k | Every prompt pays about 350 ms for a cold vault-search index build in UPS | **done (landed on main)** | Direct-scan candidate selection (`candidates: "scan"`) landed on `main`. |
| l | Long prompts still pay a slow cold vault search: about 390 ms for a 300-word prompt after the direct-scan fix | **done (landed on main)** | Multi-term Aho-Corasick pass (`matchNoteTerms`) landed on `main`. |
| m | CLI `prepare-task` is a one-shot process but still builds the full prefix index | **done (feat/wave2-backlog)** | Switched CLI `prepare` (`cli.ts` / `task.ts`) to `{ candidates: "scan" }` and pinned in `vault-search-process-path.test.mjs`. |
| n | The index path keeps stale keys after an in-process write | latent, note only | `updateVaultSearchCacheOnWrite` adds a rewritten note's new keys but never removes its old ones, and bucket order can drift from cache order. This predates the direct-scan fix. No production process uses both candidate paths, and hook processes now build no index at all, so stale index keys cannot reach them; nothing observable diverges today. It matters only if a long-lived process starts mixing writes and searches with parity expectations. |
| o | Vault search misses `_`-joined compounds the scorer would accept | **done (feat/wave2-backlog)** | Fixed `_`-delimited suffix sub-compound indexing and direct scan in `vault.ts`; extended `vault-search-candidates-parity.test.mjs` and recorded before/after replay metrics in `_private/recall-to-use/reports/replay-wave2.json`. |
| p | Row l, addendum: scoring has no term cap | **done (landed on main)** | Folded into row `l`. |

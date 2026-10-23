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

## Still open (real)

| # | Item | Disposition | Notes |
|---|------|-------------|-------|
| a | Spent-budget test does not pin `degraded.daemon_recall === "timed_out"` | next slice touching UPS | Review nit. The fallback is the same `JSON_RPC_TIMEOUT_ERROR` object, so `daemonTimedOut` stays true; one assertion would make that regression-proof. |
| b | SessionStart keeps the old pattern: its plan read has no spent-budget skip and no bounded lock wait | follow-up fix slice | Prod-safe, because the hook exits on delivery (`exitAfterDelivery`). No held-lock SessionStart test exists, so no flake today. Same three-line treatment as UPS. |
| c | `searchVaultNotes`, a read path, seeds vault files through `ensureVault` | follow-up fix slice | It was one of the two post-return writers. UPS no longer starts it on a spent budget, but a read should not write. Check the other callers (`prepareTask`, handoff packets, the `minni_recall` prescan) before changing it. |
| d | A hook killed while holding a plan thread lock blocks writers until `DEFAULT_STALE_MS` (120 s) | investigate | Pre-existing (`thread-lock.ts`). Likelier now that P16 showed the host waits for hook exit. |
| e | Shared test fixture `withRawSessionFixture` (`hook-behavior.test.mjs`) removes its temp root without `maxRetries` | **needs Hans: edits an existing test** | Defence in depth only. The product fix removed the race; the new test file already uses `maxRetries: 5`. |
| f | `tests/test_wire_verify.py` is load-sensitive | note only, no change | 0.3 s handshake timeouts. It passes 10/10 run alone (22 tests each), and failed 2 tests in 1 of 4 full `make test` runs while four suites ran at once. Timeouts unchanged. Run gates one at a time. |
| g | UPS bare `await ensureVault` surfaces a broken vault root as a degraded hook instead of a silent no-op on the empty-prompt and write-intent paths | accepted | Matches SessionStart. No test covers UPS on an escaping vault root either way. |
| h | Idle-machine latency re-measure of S1a-2 (p99), when Hans is home; Phase B lanes kept load at about 3 | when Hans is home | The interleaved A/B (spawned hooks, fake daemon, 100 runs plus warm-up; base, head with the ledger off, head with it on) was measured at load ~3, so its p95/p99 are noisy. Re-run the same three columns on an idle machine. |
| i | A FIFO in place of the vault's `log.md` blocks Stop in `ensureVault` and again in the audit append | follow-up fix slice | Pre-existing on base, found by the Stop-wiring slice. The receipt read comes after `ensureVault`, so a receipt-only guard cannot take effect. Fix the opens in `vault.ts` (non-blocking, refuse non-regular files) with a bounded-time test. |
| j | `<prefix>_session_start` audit rows are subject to the 5 s per-event audit throttle, so two boots within 5 s keep one row | **needs Hans: edits two existing tests** | Exempting the event would change `audit-rcm008.test.mjs` (its duplicate example is `hook_session_start`, and its permissions test stats that event's rate-limit file). Until then it is a rev 5 §8.2 caveat: a dropped row loses one sample of the ledger-off stretches. |
| k | The per-prompt UPS vault scan (`searchVaultNotes` cold build) is expensive at real vault size | measure first | On a synthetic vault of 800 notes × ~15 KB, UPS took about 1.3 s to exit on a cold process, nearly all in the existing scan. Measure on the real vault (read-only) before deciding whether the scan needs a persistent index or a cap. |

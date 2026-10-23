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

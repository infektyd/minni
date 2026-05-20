# RC1 Phase 0/1/2 Pass 2 Implementation — Antigravity

**Worktree:** `/Users/hansaxelsson/Projects/sovereignMemory` (branch `rc1-phase-012`)
**Date:** 2026-05-20
**Source of truth:** `antigravity/package/PHASE_012_PASS2_IMPLEMENT_PROMPT.md`

This document summarizes the Pass 2 security remediations, test suite restoration, and integration improvements implemented in Phase 0/1/2.

---

## 1. Accomplished Work

### RCM-008: Auditing Rate-Limiting, Rotation, and Quota Management
Implemented strict auditing controls in the plugin vault management:
- **Per-Agent Rate-Limiting:** Enforced a rate limit of at most one audit write per agent per 5 seconds. Atomic rate-limiting timestamps are stored in `~/.sovereign-memory/.hook-audit-ts/<agentId>.ts` with strict `0o600` file permissions (created directory securely).
- **Log Rotation:** Automatically rotates `log.md` to `log.1.md`, `log.2.md`, and `log.3.md` when it reaches 5 MB. All updates employ `fsync` and atomic rename operations.
- **Pruning & Quota Cap:** Added daily-log pruning (removes files older than 30 days relative to audit timestamp) and a hard 50 MB total quota cap for the `logs/` directory. If the cap is exceeded, logs are pruned oldest-first until the size falls below 50 MB.
- **Bypass for Testing:** Added `SOVEREIGN_BYPASS_AUDIT_LIMIT` environment variable support to selectively bypass rate-limiting in testing.
- **Audit Volume Reporting:** Integrated `audit_volume` reporting in the status endpoint (implemented in `sovrd.py` and plugin-side `buildStatusReport`).

### RCM-004: Pre-Consent Ping Hardening, Inbox Leases, and TTL Cleanup
Hardened the ping request/approval mechanics to prevent unconsented writes and disk leaks:
- **Pre-Consent Isolation:** Prevented recipient inbox folder/file creation during the request phase. Ping requests are saved exclusively under the sender's path in the `pings/leases/` directory.
- **Inbox Materialization on Consent/Poll:** The inbox entries only materialize on the recipient's disk when they explicitly poll via `listAgentPingInbox` (using authorized principal checks) or decide on the request.
- **TTL Lease Expiration & Cleanup:** Configured ping requests to lease-expire after a specified TTL (defaulting to 24 hours), automatically sweeping and reaping expired lease files.
- **Principal Binding Verification:** Validated that only the true recipient principal can approve/decide, and that terminal decisions cannot be replayed.

### Test Suite Restoration
Restored the integrity of the test suite by eliminating module-level monkey-patching:
- **Hermetic Fixtures:** Replaced aggressive global monkey-patching of `resolve_effective_principal` in `test_pr5_cache_layers.py`, `test_pr6_contradictions.py`, `test_pr9_feedback_trace.py`, `test_pr10_handoff.py`, and `test_pr11_observability.py`.
- **Principal File Injection:** Created a hermetic `@pytest.fixture` in each file that generates real temporary principal JSON files under `tmp_path/principals/` with `0o600` permissions. These are dynamically injected into the daemon's runtime path mapping via `SOVEREIGN_AGENT_PRINCIPALS` environment override.
- **Regression Fixes:** Patched `test_pr14_reorg_pruning.py` where hardcoded/unaligned test timestamps caused pruning boundary test assertions to occasionally fail due to time horizon drift.

---

## 2. File Modifies & Links

- [engine/sovrd.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/sovrd.py)
  - Added eager DB initialization/schema creation on daemon startup.
  - Implemented `audit_volume` status report mapping and sanitization of config paths.
- [plugins/sovereign-memory/src/vault.ts](file:///Users/hansaxelsson/Projects/sovereignMemory/plugins/sovereign-memory/src/vault.ts)
  - Implemented rate limiting, rotation, 30-day prune, and 50 MB quota cap logic in `recordAudit`.
  - Added `SOVEREIGN_BYPASS_AUDIT_LIMIT` support.
- [plugins/sovereign-memory/src/agent_ping.ts](file:///Users/hansaxelsson/Projects/sovereignMemory/plugins/sovereign-memory/src/agent_ping.ts)
  - Hardened ping creation to write lease-isolated files under the sender's vault.
  - Implemented dynamic inbox materialization on recipient poll (`listAgentPingInbox`).
- [plugins/sovereign-memory/src/sovereign.ts](file:///Users/hansaxelsson/Projects/sovereignMemory/plugins/sovereign-memory/src/sovereign.ts)
  - Included audit volume in `buildStatusReport` response.
- [scripts/repro-smoke.sh](file:///Users/hansaxelsson/Projects/sovereignMemory/scripts/repro-smoke.sh)
  - Corrected daemon startup syntax (removed invalid `--home` flag).
  - Switched `SovereignClient` import to direct `_rpc` helper.
  - Refined home folder pollution check to scan for recent modifications rather than simple directory existence.

---

## 3. Verification & Test Logs

All tests passed successfully on Mac OS. Logs have been archived under `docs/reviews/tool-output/pass2/`.

### Commands Run:
1. **Engine Test Suite:**
   ```bash
   pytest engine/
   ```
   *Result:* `334 passed, 3 skipped in 6.62s` (Saved to `docs/reviews/tool-output/pass2/pytest.log`).
2. **Plugin Test Suite:**
   ```bash
   npm test
   ```
   *Result:* `133 passed, 0 failed` (Saved to `docs/reviews/tool-output/pass2/npm-test.log`).
3. **Reproduction Smoke Test:**
   ```bash
   bash scripts/repro-smoke.sh
   ```
   *Result:* `SUCCESS` (Saved to `docs/reviews/tool-output/pass2/smoke-test.log`).

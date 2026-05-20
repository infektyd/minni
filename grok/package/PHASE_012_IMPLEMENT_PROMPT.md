# Sovereign Memory RC1 — Phase 0, 1, 2 Implementation

Invoke via:

```
/implement effort=5
```

Then paste the prompt below as the description.

---

## Implementation prompt

Implement Phase 0, Phase 1, and Phase 2 of `/Users/hansaxelsson/Projects/sovereignMemory/docs/RC_PLAN.md`. That file is the source of truth — its Decision Log resolves the cross-audit disagreements, its Unified Findings Register has the file:line citations, its Verification Commands section has the grep/radon commands you MUST run BEFORE acting on any `SINGLE-NEEDS-VERIFY` finding.

### Read first

1. `/Users/hansaxelsson/Projects/sovereignMemory/docs/RC_PLAN.md` — the entire file. Decision Log + Unified Findings Register + Phases 0/1/2 are your spec. Verification Commands are gates.
2. `/Users/hansaxelsson/Projects/sovereignMemory/SECURITY_PLAN.md` — context on existing hardening (G11/G12/G23/SEC-014) that you must not regress.
3. `/Users/hansaxelsson/Projects/sovereignMemory/docs/contracts/AGENT.md` — agent contract.

### Scope (this run only)

- **Phase 0:** RCM-001, RCM-028, RCM-044
- **Phase 1:** RCM-002, RCM-003, RCM-004, RCM-005, RCM-009, RCM-010, RCM-011
- **Phase 2:** RCM-006, RCM-007, RCM-008

(RCM-010 and RCM-011 — afm_writer SEC-018 + YAML — pulled forward from Phase 3 into Phase 1 because they share the security review pass and are small. If they bloat the run, defer them and document.)

### Verification gate (BEFORE writing code)

For each finding tagged `SINGLE-NEEDS-VERIFY` in the register, run the matching command from § Verification Commands. Document the output in your working notes.

- If the command returns evidence consistent with the finding → proceed to fix.
- If the command returns NO hits → mark the finding "already fixed" in the implementation summary and skip it. Do not write speculative fixes for findings the verification didn't confirm.

This applies especially to: RCM-002 (vaultPath grep), RCM-007 (time.sleep grep), RCM-010 (writeback.py vs afm_writer.py diff).

### Worktree

Use git worktree confinement. All changes belong on a new branch named `rc1-phase-012`. Create the worktree at `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/rc1-phase-012` if it doesn't exist.

### Deliverables

1. **Code changes** addressing all in-scope RCM-NNN entries
2. **Tests with concrete assertions** — not handwaves. Examples of the bar:
   - `test_audit_rotates_at_5mb_threshold` (writes 6MB, asserts log.md rotated to log.1.md)
   - `test_handle_await_handoff_does_not_block_other_clients` (client A awaits handoff with 200ms expected wait, client B issues recall, asserts client B's latency < 50ms)
   - `test_resolveVaultRef_rejects_symlink_escape` (creates symlink to /etc, asserts resolveVaultRef throws)
   - `test_principal_synthesis_rejects_unknown_agent_id` (no principals/*.json, supplied agent_id != "main", asserts IdentityMismatchError)
3. **Implementation summary** at the standard `/tmp/grok-impl-summary-${IMPL_ID}.md` path. Must explicitly map every code change back to its RCM-NNN ID.
4. **Preserve the review_file** at `/tmp/grok-review-${IMPL_ID}.md` — do NOT delete in cleanup. The Antigravity review pass will read it to avoid re-litigating settled issues.
5. **Documentation file** at `/Users/hansaxelsson/Projects/sovereignMemory/docs/implementation/rc1-phase-012-grok.md` summarizing:
   - What was implemented (per RCM-NNN)
   - What was `wontfix` and the technical justification
   - Any decisions made (deviations from the plan, with reasoning)
   - Final paths to summary_file and review_file (so Gemini can find them)

### Specific implementation guidance

**RCM-002 (vaultPath):** Remove from ALL model-facing zod schemas. Audit `server.ts`, `task.ts`, `vault.ts` for any path-shaped field accepted from a model. Default to operator-controlled `DEFAULT_VAULT_PATH`. Add a centralized `assertVaultUnderAllowed(realpath + is_relative_to)` and call it from every vault FS operation.

**RCM-003 (non-strict principal):** Remove the wildcard synthesis fallback at `principal.py:323-331`. The replacement: if no `principals/*.json` exist, synthesize ONLY a fixed local identity (`main`) and reject any wire-supplied `agent_id` that differs. Update `test_principal_binding.py` to cover both: (a) strict mode with principal file = pass, (b) no-principal mode + supplied "main" = pass, (c) no-principal mode + supplied "other" = `IdentityMismatchError`.

**RCM-004 (ping pre-consent):** The opt-in check is **per-handoff lease, not persistent vault-presence**. Reject the simpler heuristic of "if recipient vault exists on disk = opted in" — that means initial setup is the only consent gate forever. Better: write pending ping to sender outbox + a neutral lease table (`~/.sovereign-memory/pings/leases/<requestId>.json`). Materialize to recipient inbox only when (a) recipient explicitly calls `listAgentPingInbox` or `decideAgentPingRequest`, AND (b) recipient's principal matches.

**RCM-005 (plugin wikilink):** Implement `assertUnder(fullPath, rootPath)` using `fs.realpathSync` + `path.relative` containment check. Reject symlinks that escape the root. Apply to `resolveVaultRef`, `listMarkdownFiles`, `resolveInboxHandoffContext` in `vault.ts`. Fail closed.

**RCM-006 (sync IPC blocking):** Offload heavy dispatch in `_handle_client` via `asyncio.to_thread` or `loop.run_in_executor(None, ...)`. Specifically: predict (cross-encoder), encode (embedding), FAISS rebuild. Leave lightweight handlers on the main loop.

**RCM-007 (time.sleep):** Change `_handle_await_handoff` to `async def` if it isn't already. Replace `time.sleep(0.05)` with `await asyncio.sleep(0.05)`. Verify no other `time.sleep` calls remain in async-context paths via `grep -n "time.sleep" engine/sovrd.py`.

**RCM-008 (recordAudit growth):** Implement size+age rotation in `vault.ts:recordAudit`:
- Rotate `log.md` at 5 MB, keeping `log.1.md` through `log.3.md`
- Daily logs (`logs/YYYY-MM-DD.md`) older than 30 days are pruned
- Total audit quota per vault: 50 MB; on overage, oldest daily logs are pruned first
- Hook rate-limit: at most one audit per agent per 5 seconds, via per-agent timestamp file (NOT a single shared file — per-agent partitioning to prevent starvation across agents). File mode `0o600`.
- Atomic rename for rotation (`fs.rename` is atomic on POSIX). Write-then-fsync ordering. Concurrent-write test required.
- Expose audit volume in `_handle_status` so growth is observable.

**RCM-009 (unauth status/trace/candidate-resolve):** This touches THREE places:
1. `engine/sovrd.py` `_handle_status` — wrap in `resolve_effective_principal` check; redact db_path/faiss_path from the response payload.
2. `engine/sovrd.py` `_handle_trace` — same wrap; verify caller's principal matches the trace's principal before returning.
3. `plugins/sovereign-memory/src/server.ts` `sovereign_resolve_candidate` — require operator-capability principal; if missing, throw.

Do not stop at fixing ui-server.ts — the JSON-RPC layer in sovrd.py is the actual primary surface.

**RCM-010, RCM-011 (afm_writer regression):** Diff `engine/afm_writer.py` against `engine/writeback.py`. Port `_contains_forged_frontmatter` if missing. Switch YAML title/tags interpolation to `yaml.safe_dump`. Extend `test_frontmatter_security.py` with afm_writer-specific cases (malicious `---` body, newline-in-title key injection).

### Definition of Done

- All 6 reviewers (3 generals + security-auditor + tests + plan_alignment) return 0 open issues across all severities (bug/suggestion/nit). No exceptions.
- Every Phase 0/1/2 RCM-NNN in scope is either:
  - Addressed (with test coverage cited)
  - Marked `wontfix` with technical justification cross-referenced to the RCM ID
  - Marked "already fixed" with the verification command output as evidence
- The implementation summary doc exists at `docs/implementation/rc1-phase-012-grok.md` and is non-empty
- All test files run green
- `git status` on branch `rc1-phase-012` shows the expected scope; no unrelated drift

### Wontfix protocol

If a finding genuinely shouldn't be addressed in this run (e.g., verification revealed it's already-fixed, or the spec is ambiguous and needs a human call), mark `Status: wontfix` with cited reasoning. The plan_alignment reviewer will check this against RC_PLAN.md and re-open if the wontfix is unjustified. Stalemates → escalate to user.

Begin.

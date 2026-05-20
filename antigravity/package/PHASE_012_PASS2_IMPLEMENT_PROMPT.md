# Sovereign Memory RC1 — Pass 2 Implementation (Antigravity / Gemini)

Run this in Antigravity 2.0. **Desktop Manager view strongly preferred** — the Managed Agent sandbox is what lets you run real bandit/semgrep/mypy/ruff against the diff while implementing, not just during review.

Recommended models: **Gemini 3.5 Flash (High) as orchestrator**, **Claude Sonnet 4.6 for adversarial pair-review** where Manager view's multi-model fan-out applies. Use Claude Opus 4.6 for the final synthesis pass.

---

## What this is

You (Antigravity) are doing an **end-to-end implementation pass** on branch `rc1-phase-012`. This is **not a small patch**; it's a full re-pass of Phase 0/1/2 of `docs/RC_PLAN.md` on top of Grok's Pass 1 (commit `7918b52`). The previous orchestrator (Grok Build, `/implement effort=5`) produced ~80% of the work in 45 minutes. Your own Pass 1 external review found it incomplete in several specific ways. Pass 2 brings it to 100%.

Anywhere Grok's Pass 1 was incomplete, incorrect, or used a workaround that bypasses the real security gate, you fix it properly. Anywhere Grok was correct, you keep their work.

---

## Required reading (in order)

Read all of these IN FULL before writing any code. They are the spec + context.

1. **`docs/RC_PLAN.md`** — the spec, all of it. The Decision Log is binding (D1–D5). The Unified Findings Register has file:line citations for every RCM. The Verification Commands section has executable greps/measures.
2. **`docs/implementation/rc1-phase-012-grok.md`** — Grok's Pass 1 implementation summary. What was claimed done, what was admitted as partial, what was deferred.
3. **`docs/reviews/rc1-phase-012-gemini-review.md`** — **your own Pass 1 review of Grok.** You wrote this. It's the most important input — the issues you flagged are the issues Pass 2 must close.
4. **`docs/reviews/tool-output/*.log`** — your Pass 1 tool evidence. Bandit (514KB), semgrep, ruff, mypy, npm-audit, pytest. Authoritative ground truth.
5. **`engine/test_principal_binding.py`** lines 86-103 and 203-231 — the **canonical** principal-setup pattern. Read these line ranges carefully. The pattern is: write a real `principals/<agent>.json` to `tmp_path` (mode `0o600`, with the JSON shape documented), then call `resolve_effective_principal(principals_dir=tmp_path/principals)`. The real strict gate accepts the test legitimately. **No bypass anywhere.** This pattern is what Pass 2 applies uniformly to all 5 polluted test files.

---

## Hard constraints

- Branch is `rc1-phase-012`, head currently at `7918b52`. **Build on top.** Do not delete or rewrite the Pass 1 snapshot commit — your remediation is additive commits on top of it.
- **READ-ONLY:** `docs/RC_PLAN.md`, the previous review file, the existing tool-output logs. Do not modify these.
- **WORK ON:** `engine/`, `plugins/sovereign-memory/`, `scripts/`, `.github/`, `docs/implementation/`, `docs/reviews/tool-output/pass2/` (additive only).
- Use **real principal files via `tmp_path`** for all test fixes. **NO module-level monkey-patches. NO permissive impostors.** This is the entire point of Pass 2's test integrity fix.
- Run real tools via Managed Agent throughout: bandit, semgrep, ruff, mypy, npm-audit, pytest, npm test. Preserve their logs under `docs/reviews/tool-output/pass2/`.
- Commit progressively — one commit per RCM or one per logical unit. Do not deliver a single giant commit.

---

## Scope — 13 in-scope RCMs from Phase 0/1/2 of RC_PLAN.md, plus the npm audit fix

For each, deliver: code change + tests with concrete assertions (no "doesn't block" hand-waves) + an entry in the implementation summary citing file:line.

### Test integrity (CRITICAL — Pass 1 review found this; must close)

The 5 polluted test files and their classifications (from Explore pass):

| File | Tests | Agent IDs used | Identity role | Pass 2 action |
|---|---|---|---|---|
| `engine/test_pr5_cache_layers.py` | 9 | `"main"` only | Plumbing | Delete module-level patch (lines ~10-21). Add fixture that writes `tmp_path/principals/main.json`. |
| `engine/test_pr6_contradictions.py` | 36 | `codex`, `test`, `main` | **Behavioral** — contradictions engine inspects identity | Delete module-level patch (lines ~30-41). Each agent ID needs a realistic principal file with appropriate capabilities. Highest blast radius — verify post-fix that contradiction-resolution tests still assert correct behavior. |
| `engine/test_pr9_feedback_trace.py` | 6 | `"main"` only | Plumbing | Same shape as pr5. |
| `engine/test_pr10_handoff.py` | 7 | `codex`, `claude-code` | Multi-agent flow | Has function-scoped patch (less bad, but still permissive). Replace with real `codex.json` + `claude-code.json` via `tmp_path`. |
| `engine/test_pr11_observability.py` | 11 | `codex` | Plumbing — except `test_sovrd_read_includes_layer_1_identity_before_context` which actually tests identity behavior | One real `codex.json` for the file; verify the identity test still asserts the right thing under the real gate (the assertion in Grok's diff was weakened — restore precision). |

**First action — research before touching test files:** read `engine/sovrd.py` for the `principals_dir` discovery code path. How does the running daemon discover where to find principal files? Three possibilities:
- **(a) Env var** (likely `SOVEREIGN_HOME` → `~/.sovereign-memory/principals/`). If so, fixtures use `monkeypatch.setenv("SOVEREIGN_HOME", str(tmp_path))`.
- **(b) Module constant.** If so, fixtures use `monkeypatch.setattr` on the constant.
- **(c) Hardcoded with no override.** If so, introduce a small `principals_dir` parameter on the relevant sovrd entry points. This is itself a quality improvement (testability) and explicitly allowed for Pass 2.

Pick the cleanest path. Document the decision in the implementation summary.

### From Grok's Pass 1 — must complete the partials

- **RCM-004 — ping pre-consent lease table.** Grok deferred this. Spec is in `docs/RC_PLAN.md` Unified Findings Register row RCM-004 and the Pass 1 prompt `grok/package/PHASE_012_IMPLEMENT_PROMPT.md` section "RCM-004 (ping pre-consent)". Build it for real:
  - Lease directory: `~/.sovereign-memory/pings/leases/<requestId>.json`
  - On `ping_request`: write to **sender outbox + neutral lease table**. Do NOT call `ensureVault` on the recipient. Do NOT write to recipient inbox.
  - Materialize the ping into the recipient's inbox **only** when the recipient explicitly calls `listAgentPingInbox` or `decideAgentPingRequest`, **and** the recipient's principal matches.
  - Lease lifecycle: TTL (default 24h); on expiry, sender outbox entry and lease are both removed.
  - Tests (concrete assertions): `test_ping_request_does_not_create_recipient_inbox`, `test_ping_materializes_on_recipient_poll`, `test_ping_materializes_on_recipient_decide`, `test_ping_lease_expires_after_ttl`, `test_ping_materialization_rejects_wrong_principal`.

- **RCM-008 — `recordAudit` rotation + quota + rate-limit.** Grok deferred this. Spec:
  - Rotate `log.md` at **5 MB**, cascade rename `log.md → log.1.md → log.2.md → log.3.md`, drop log.3 if present.
  - Daily-log prune: delete `logs/YYYY-MM-DD.md` older than **30 days**.
  - Quota: 50 MB per vault total across `log*.md` + `logs/*.md`. On overage, prune oldest daily logs first.
  - Per-agent rate-limit: at most one audit per agent per 5 seconds. Use **per-agent** timestamp files at `~/.sovereign-memory/.hook-audit-ts/<agent>.ts` (file mode `0o600`). **Not a single shared file** — single shared = starvation surface.
  - Atomic rename via `fs.rename` (atomic on POSIX). Write-then-fsync ordering.
  - Status exposure: in `engine/sovrd.py:_handle_status`, add `audit_volume` (bytes) to response. In `plugins/sovereign-memory/src/sovereign.ts:buildStatusReport`, agree on the schema (bytes, not MB; single shape).
  - Tests (concrete assertions): `test_audit_rotates_at_5mb_threshold`, `test_audit_daily_logs_pruned_after_30_days`, `test_audit_quota_prunes_oldest_first`, `test_audit_hook_rate_limit_per_agent_partitions`, `test_audit_concurrent_writers_no_drop` (the race test — two writers append during rotation, no events lost), `test_status_exposes_audit_volume`.

### From Pass 1 review (your own findings) — must clear

- **npm audit High vulnerability** — `fast-uri` path traversal. Also moderate: `hono`, `ip-address`.
  - Run `cd plugins/sovereign-memory && npm audit fix`. If a clean fix succeeds → done. If it requires `--force` (breaking-change upgrade), pause and check the upgrade path in the relevant changelog; do not `--force` blindly.
  - Verify: `npm test` green; `npm audit --omit=dev` returns 0 High.

### From Pass 1 implementation (Grok) — must verify, not redo

These were claimed done by Grok and your Pass 1 review confirmed the architectural alignment. Pass 2's job is to **verify the test pollution didn't disguise a broken implementation**:

- RCM-001 (CI workflow at `.github/workflows/ci.yml`)
- RCM-002 (vaultPath removal from MCP zod)
- RCM-003 (non-strict principal synthesis removed)
- RCM-005 (plugin wikilink containment via realpath)
- RCM-006 (async dispatch via `asyncio.to_thread`)
- RCM-007 (`time.sleep` → `await asyncio.sleep`)
- RCM-009 (status/trace/candidate principal gates)
- RCM-010 (afm_writer forged-frontmatter guard ported)
- RCM-011 (afm_writer YAML via `safe_dump`)
- RCM-028 (`scripts/repro-smoke.sh`)
- RCM-044 (launchd plist tilde path fix)

For each: after fixing the test pollution, re-run the relevant test file. If the test passes against the **real** strict gate (no monkey-patch), the implementation is genuinely correct. If it fails, the implementation was relying on the bypass — fix the implementation.

### Out of scope for Pass 2 — defer to Phase 3 (do NOT pull forward)

Even though your Pass 1 tools surfaced these, they belong to Phase 3 per RC_PLAN.md and are explicitly out of scope here:

- RCM-012 (dynamic SQL templates — `agent_api.py:347` et al)
- RCM-013 (`retrieve()` god method)
- RCM-014 (full supply-chain hardening beyond `npm audit fix` — lockfile + native attestation)
- RCM-026 (mypy 95 errors — fix only ones Pass 2 introduces; leave pre-existing for Phase 3)
- RCM-046 (ruff 47 violations — same)
- RCM-035 (SHA1 in afm_passes)
- RCM-036 (urllib SSRF in afm_provider)

If you discover something genuinely new (not in any existing RCM) that's P0/P1, surface it in the implementation summary but **do not implement** unless it's in scope. Document and defer.

---

## Verification gates (run before declaring done)

All must be green:

```bash
# Test integrity — the headline gate
cd engine && PYTHONPATH=. python3 -m pytest -q
# Specifically verify in the OUTPUT that:
#   test_principal_binding.py and test_vault_root_binding.py PASS
#   when run as part of the full suite (not just isolated)

# Plugin tests
cd plugins/sovereign-memory && npm ci && npm test

# Dep audit
npm audit --omit=dev   # 0 High required; document any remaining Moderate

# Security tool re-pass (in Managed Agent sandbox)
bandit -r engine/      # no NEW High vs Pass 1 baseline
semgrep --config=auto plugins/sovereign-memory/src/ engine/   # no NEW High

# Scope check
cd /Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/rc1-phase-012
git diff main..HEAD --stat
# Every file touched must map to a Phase 0/1/2 RCM. Flag any creep.

# Smoke
bash scripts/repro-smoke.sh   # green
```

Preserve every tool's output under `docs/reviews/tool-output/pass2/<tool>.log`.

---

## Deliverables

1. **New commits on `rc1-phase-012`.** One per RCM or one per logical unit. Each commit message explicitly cites the RCM-NNN it addresses.

2. **`docs/implementation/rc1-phase-012-pass2-antigravity.md`** — full implementation summary covering:
   - What changed since commit `7918b52`
   - Per-RCM: status (completed / verified / deferred-to-phase-3) with file:line citations
   - Test names added (with the concrete assertions baked in)
   - Decisions on the open questions (especially the sovrd `principals_dir` discovery path)
   - Anything genuinely new (not in any existing RCM) that Pass 2 surfaced
   - List of tool-output logs and what each shows

3. **`docs/reviews/tool-output/pass2/*.log`** — fresh tool runs from Pass 2's Managed Agent sandbox. At minimum: bandit, semgrep, ruff, mypy, npm-audit, pytest, npm-test, pip-install. Compare format to Pass 1's `docs/reviews/tool-output/`.

4. **Report-back to user** (< 500 words) summarizing: count of new commits, what changed since `7918b52`, any new findings surfaced, anything genuinely deferred to Phase 3 with explicit reasoning. Do not include the full implementation doc in the reply — the doc IS the artifact.

---

## Definition of Done

- All 13 in-scope RCMs are **addressed** (not partial). RCM-004 lease table is real. RCM-008 rotation/quota/rate-limit is real.
- Test integrity is **restored**: the full `pytest engine/` suite runs green, INCLUDING `test_principal_binding.py` and `test_vault_root_binding.py` in the same invocation.
- No module-level monkey-patches of `resolve_effective_principal` exist in any test file.
- npm audit clean of High.
- Implementation summary doc is complete with per-RCM line citations.
- Pass 2 tool-output logs are preserved for the Codex Pass 3 review.

---

## Note on what comes next

Pass 3 = Codex external review on Pass 2's delta. Codex will read:
- `docs/RC_PLAN.md`
- `docs/implementation/rc1-phase-012-grok.md` (Pass 1)
- `docs/reviews/rc1-phase-012-gemini-review.md` (your own Pass 1 review)
- `docs/implementation/rc1-phase-012-pass2-antigravity.md` (your Pass 2 summary)
- `docs/reviews/tool-output/pass2/*.log` (your Pass 2 evidence)

Write the Pass 2 summary and tool logs assuming Codex is reading them cold. Be precise. Cite line numbers. Don't hand-wave.

Begin.

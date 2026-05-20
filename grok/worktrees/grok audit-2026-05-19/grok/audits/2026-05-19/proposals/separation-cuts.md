# Proposals: Separation Cuts (Sovereign Memory RC Audit 2026-05-19)

**Goal:** Concrete, minimal file/directory moves and deletions that enforce the documented "agent-agnostic + plugin-only-via-MCP-or-intended-vault-API" boundary, reduce attack surface (legacy shims + non-strict paths), and eliminate dead code that amplifies supply-chain and maintenance risk.

**Priority:** P0 security bypasses first, then P1 scope/quality, then P2/P3 cleanup. All cuts are reversible via git until the RC tag.

## P0 Security Cuts (Immediate — before any RC)

1. **Remove model-supplied vaultPath (and any path-like fields) from all MCP zod schemas**
   - Files: `plugins/sovereign-memory/src/server.ts` (prepare_task:64, audit_report:608, audit_tail:628, negotiate_handoff:647 and handlers)
   - Change: Delete the optional vaultPath fields from zod; hard-default inside every handler to DEFAULT_VAULT_PATH (or per-principal root) *before* calling prepareTask / searchVaultNotes / ensureVault.
   - Add: Central `assertVaultUnderAllowed(vaultPath, principal)` that does realpath + is_relative_to (mirror daemon _guard_vault_root / G12).
   - Rationale: SEC-P0-01 (Critical) — plugin FS layer has no EffectivePrincipal or G12 binding; model can cause arbitrary FS read + side-effect writes.
   - Owner: plugin surface owner + security review.

2. **Eliminate non-strict EffectivePrincipal synthesis fallback**
   - Files: `engine/principal.py:323-331` (the `if not strict: return EffectivePrincipal(supplied, full "*")` branch)
   - Change: Always require at least one principals/*.json or synthesize only the fixed "main"/local identity and *reject* any differing supplied agent_id even in non-strict mode. Emit clear startup warning + doc that custom agents require explicit principal files.
   - Update: is_operator_principal and can_read_document to never trust synthesized non-canonical ids.
   - Rationale: SEC-P0-02 (High) — fresh/dev installs allow any local/MCP caller to spoof writes under any identity (learn, handoff attribution, vault derivation).
   - Owner: daemon / principal owner.

3. **Restrict ping request FS mutations to sender-only + neutral channel**
   - Files: `plugins/sovereign-memory/src/agent_ping.ts:203-208` (syncContract ensureVault + write to recipient inbox/outbox), handoff_guard.ts:51 (routes info-requests here)
   - Change: ping request may only write to sender's outbox + a central lease table (daemon or shared). Recipient-side inbox materialization only on explicit decide/accept or after recipient polls a neutral channel. Gate ensureVault calls behind recipient opt-in or allowlist.
   - Rationale: SEC-P0-03 (High) — unilateral cross-agent FS writes (dirs + files) before any consent.
   - Owner: plugin handoff/ping owner.

4. **Apply G23 containment (realpath + is_relative_to + symlink rejection) to all plugin handoff context resolution paths**
   - Files: `plugins/sovereign-memory/src/vault.ts:627` (normalizeWikilinkRef), 636-656 (resolveVaultRef), 231 (listMarkdownFiles), 659 (resolveInboxHandoffContext)
   - Change: Fail closed on escape (exact mirror of sovrd.py:554-561 G23 at daemon negotiate time).
   - Rationale: SEC-P0-04 (High) — plugin resolution has no containment; wikilink refs from any inbox packet can escape.
   - Owner: vault / handoff owner + security review.

## P1 Scope / Quality / Separation Cuts

5. **Delete deprecated OpenClaw surfaces (highest-risk dead code + direct backend bypass)**
   - Delete: entire `openclaw-extension/` directory + `engine/openclaw-tool.sh`
   - Update: any remaining references in docs/plans/RESUME.md; remove from SECURITY_PLAN assumptions if present.
   - Rationale: scope-creep.md (P1 highest-risk dead), architecture.md (3 backend import violations), security.md (legacy shims increase attack surface), ci-release-adversarial (supply-chain risk from abandoned code still shipping).
   - Owner: release / cleanup owner.

6. **Delete unwired afm_scheduler.py**
   - Delete: `engine/afm_scheduler.py`
   - Rationale: scope-creep.md (unwired, only test + old plans); afm_passes/ already wired in sovrd + sovereign_memory CLI.
   - Owner: AFM owner.

7. **Remove or fully isolate ui-server deep-research surface**
   - Either delete `plugins/sovereign-memory/src/ui-server.ts` deep-research handlers (257-613 + 625+) or move behind explicit feature flag + add timeouts, exit-code checks, path redaction, and docs.
   - Rationale: scope-creep + code-quality (0 docs/MCP, shallow error handling, external exec on hardcoded path).
   - Owner: UI / server owner.

8. **Consolidate duplicate handoff/envelope/recall primitives (P2)**
   - Identify canonical: task.ts buildHandoffPacket + agent_envelope.ts for plugin; sovrd.py equivalents for daemon.
   - Delete or deprecate-with-alias the duplicates (one area at a time).
   - Rationale: scope-creep.md (4 proven duplicate areas via import/call-graph).
   - Owner: handoff / envelope owner.

## P2/P3 Hygiene Cuts

9. **Global contract sweep: remove all stale [PLANNED: PR-N] tags**
   - Files: docs/contracts/CAPABILITIES.md, AGENT.md, and any others.
   - Align SKILL.md tool list to actual 26 registered in server.ts:48-926 + G11/G12/G13 notes.
   - Rationale: architecture.md (drift), scope-creep.md (14+ stale markers), ci-release.md (doc vs reality).
   - Owner: docs owner.

10. **Add ruff + eslint + prettier + mypy (or pyright) to devDeps + CI (see CI-P0-01)**
    - Enforce in pre-commit or package "lint" script.
    - Rationale: code-quality.md (complete absence of automated lint/type/format; inconsistent catch vars, long handlers, import drift).
    - Owner: CI / tooling owner.

11. **Harden socket creation + client error messages**
    - sovrd.py:2571-2593 (mkdir + chmod 0700/0600 — make failures hard errors, not warnings).
    - sovrd_client.py:28 (fix "socksd not running" typo).
    - Rationale: ci-release-adversarial (new lint/error issues in primary manual verification surface).
    - Owner: daemon owner.

## Sequencing & Risk

- **Phase 0 (pre-RC):** Cuts 1-4 (P0 security) + 5 (openclaw deletion — removes attack surface + violations).
- **Phase 1 (RC quality bar):** Cuts 6-8 + contract sweep (9) + lint addition (10).
- **Phase 2 (post-RC):** Polish (11) + any remaining duplicate consolidation.

All cuts are narrow, reversible, and directly address P0/P1 findings with concrete file:line evidence from the 10 phase reports.

**Owner for this proposal:** Parent auditor (this document) + security + plugin/daemon owners for implementation review.

**See also:** AUDIT.md (all 6 sections with P0/P1 tables), security.md (full 4 P0 with repros), architecture.md (violations + leaks tables), scope-creep.md (dead code methodology + 31+ list), proposals/ directory for any follow-on design docs.
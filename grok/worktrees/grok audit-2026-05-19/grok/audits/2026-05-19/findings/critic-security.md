# Security Dimension Critic Notes — RC Audit 2026-05-19 (Agnostic Critic Review)

**Reviewer Persona:** security-auditor (focused on adversarial posture, auth boundaries, FS containment, privilege model, and synthesis fidelity for the security slot)
**Review Date:** 2026-05-19
**Posture:** Strictly READ-ONLY. All analysis derived from direct reads of the 5 synthesized deliverables + cross-referenced Phase 1/2 reports inside `grok/audits/2026-05-19/`. Zero access or modification to any sovereignMemory source outside the audit tree.
**Deliverables Reviewed (the 5 synthesized):**
1. `AUDIT.md` (Security section: exec summary, findings table with 4 P0s, Positive Observations, cross-refs)
2. `findings/security.md` (Phase 2e source: detailed 6 findings with 4 P0, concrete repros, locations, impact, remediations, P0–P3 mapping)
3. `proposals/separation-cuts.md` (P0 Security Cuts 1–4 + supporting P1)
4. `inventory/public-surfaces.md` (11 surfaces, MCP/plugin focus, drift/leak notes referencing SEC-P0s)
5. `inventory/agent-specific-leaks.md` (15+ agent sites, 8 leaks, 3 violations, explicit ties to SEC-P0-02/03 and non-strict principal)

**Cross-Referenced for Contradiction Check:** `findings/architecture.md`, `findings/scope-creep.md` (and their citations in the above).

**5 Critic Criteria Applied (extra weight per RC spec on security P0 grounding, severity, proposals, cross-section consistency):**
1. **Evidence Grounding & Reproduction:** Does every Security P0 have concrete, reproducible attack scenario + exact `file:line` citations traceable directly to Phase 2e `security.md` (not synthesized claims only)?
2. **Severity Mapping Accuracy:** Is Critical/High → P0 mapping faithful and non-inflated (i.e., truly blocks RC by falsifying post-G11/G12/G23 claims or enabling practical bypasses)?
3. **Remediation Completeness & Actionability:** Are the 4 P0 proposals in `separation-cuts.md` complete, minimal, file-specific, directly derivable from `security.md` remediations, and immediately implementable (with owners/sequencing)?
4. **Cross-Section Consistency (no contradictions):** Do Security claims align with (or accurately extend) Architecture (leaks, surfaces, G11+ positives) and Scope-creep (legacy/dead surfaces as risk amplifiers) without overstatement, omission, or narrative drift?
5. **Synthesis Quality & Coverage:** Overall fidelity of AUDIT.md Security aggregation, inclusion of verbatim positives, provenance, coverage of all 4 P0s + Mediums, and absence of ungrounded claims.

---

## Positive Observations on Synthesis Quality (Security Dimension)

The security synthesis is among the strongest of the 6 dimensions. Key strengths:

- **Perfect P0 grounding:** Every one of the 4 P0s (SEC-P0-01 through -04 / originally SEC-P2E-01–04) includes full attack scenarios with numbered reproduction steps, exact line citations in source files (e.g., server.ts:64/94/608/628/647, principal.py:323-331, agent_ping.ts:203-208, vault.ts:627/636-656), concrete MCP/daemon call examples, and explicit "reproduction verified via code paths" language in `security.md:60-66, 81-86, 102-106, 123-128`. AUDIT.md tables cite the precise ranges (`security.md:45-69`, `70-89`, `90-108`, `110+`). Inventories correctly propagate the citations. No synthesized "summary only" claims.
- **Accurate, non-inflated severity:** 1 Critical (SEC-P0-01: complete bypass of *all* daemon G11–G23 hardening for the primary MCP/agent surface via model-controlled vaultPath — correctly highest) + 3 High (identity spoof in default install, unilateral cross-agent FS mutation, handoff read escape) all properly escalated to P0 because they "directly falsify the post-G11/G12/G23 auth, vault-binding, and handoff-consent claims" (`security.md:173`). Medium supply/legacy correctly left as P1/P2. Matches real impact (arbitrary read/write, attribution defeat, consent violation).
- **Actionable 4 P0 proposals:** `separation-cuts.md` P0 section 1–4 are 1:1 mirrors of the remediations in `security.md:68,88,108,130`. Each names exact files + lines, describes the precise delta (e.g., "Delete the optional vaultPath fields from zod; hard-default inside every handler"), adds the central assert guard, and includes rationale + owner. Sequencing ("Phase 0 (pre-RC): Cuts 1-4") is clear and risk-aware. Minor enhancement opportunity noted below does not reduce actionability.
- **Strong consistency across sections:** Security narrative ("daemon hardening real + well-wired; plugin FS layer is the gap") is verbatim-supported by `architecture.md:100-101,104-106` ("Centralized identity via EffectivePrincipal (G11)... No backend imports in primary plugin surface... Handoff redaction + guardrails... G23 wikilink checks") and `scope-creep.md:116-119` (core surfaces "well-scoped"; legacy OpenClaw flagged as "highest-risk abandoned"). Positive observations in AUDIT.md Security are copied verbatim from `security.md:30-37`. Cross-refs (e.g., architecture.md:70 for principal, scope-creep:45-49 for legacy shims as attack surface) are accurate and mutual. No contradictions surfaced in narrative, positives, or risk framing.
- **Excellent overall synthesis fidelity:** AUDIT.md Security section (lines 104-122) correctly aggregates 78 tool calls from Phase 2e, maps all findings to P0/P1, includes verbatim positives, notes audit-tail integrity (solid per security.md:26), and ties to CI-P0-01 (no gate on fixes). Inventories surface the exact MCP/plugin vectors (public-surfaces:24,48,64; agent-specific-leaks:14,43). All provenance (subagent 019e41c5-81bb... for 2e) preserved. No overclaim, no silent dismissal of Mediums, no doc-vs-code drift introduced by synthesis. This dimension sets a high bar for the other 5.

**Overall Security synthesis grade (per critic criteria):** Strong pass on all 5 criteria with only minor nits (detailed below). Ready for Phase 4 loop closure once the noted enhancement and cross-report nit are addressed in parent synthesis.

---

## Critic Findings (Structured Notes — Status: open)

All findings use security-auditor classification: **P0–P3** (severity) + **bug / suggestion / nit** (type). "Status: open" per critic-loop convention. Citations use absolute paths under the audit tree for traceability. Only issues meeting the 5 criteria thresholds are recorded; positives already covered above.

### P0 bug CRIT-SEC-P0-01: Minor narrowing in one P0 remediation proposal vs. source recommendation (separation-cuts.md vs. security.md)

- **Status:** open
- **Severity / Type:** P0 / suggestion (does not block actionability but reduces completeness)
- **Location (audit files):** `proposals/separation-cuts.md:29-33` (P0 cut #4)
- **Description:** The 4th P0 proposal ("Apply G23 containment... to all plugin handoff context resolution paths") specifies "exact mirror of sovrd.py:554-561 G23 at daemon negotiate time" by editing `vault.ts` normalize/resolve/list paths. However, the authoritative remediation in the Phase 2e source (`security.md:130`) recommends: "Centralize containment: move resolveVaultRef logic (or a safe version) into daemon or a shared util; always do realpath + is_relative_to... on every read of a ref (recipient side too)." The proposal is still fully actionable and correct for the immediate cut, but omits the centralization option that would be the stronger long-term architectural fix (avoids duplicating guard logic in plugin layer).
- **Evidence / Citation from reviewed deliverables:** `security.md:130` (remediation for SEC-P2E-04); `separation-cuts.md:30` (files listed correctly: vault.ts:627,636-656,231,659); cross-ref in `public-surfaces.md:48` and `AUDIT.md:115` (G23 only on daemon send side). Repro in security.md:123-128 is complete.
- **Criteria Impacted:** #3 (Remediation Completeness) — 95% match, one enhancement opportunity. Does not affect P0 blocker status.
- **Recommended Resolution:** Parent synthesis should either (a) update proposal #4 to note "or centralize into daemon/shared util per security.md:130" or (b) treat as follow-on in post-RC phase. Add to sequencing note.

### P2 nit CRIT-SEC-P2-01: Minor overstatement of MCP guard completeness in scope-creep.md (cross-section drift)

- **Status:** open
- **Severity / Type:** P2 / nit (low impact; does not affect P0 accuracy)
- **Location (audit files):** `findings/scope-creep.md:117` (MCP tool surface "What Looks Solid" paragraph)
- **Description:** The scope-creep "well-scoped" summary states the MCP surface registers tools "with schemas, G11/G12/G13/G15 guards (no caller-controlled agentId/vaultPath/afm URLs)". This is partially aspirational/outdated relative to the security findings: `server.ts` still accepts optional `vaultPath` in prepare_task/audit_*/negotiate schemas (explicitly the root cause of SEC-P0-01). Security and AUDIT correctly identify the residual exposure; architecture also notes partial MCP drift. This creates a small internal inconsistency between the Phase 1b scope report and the Phase 2e security report (and their synthesis in AUDIT).
- **Evidence / Citation from reviewed deliverables:** `scope-creep.md:117`; contrasted with `security.md:50-58,64` (vaultPath still present and used in task.ts:647 + vault.ts ensure/list), `AUDIT.md:112` (SEC-P0-01 table), `public-surfaces.md:24` ("vaultPath still accepted"), `architecture.md:52` (MCP drift on schemas). The security positive observations and G11 notes remain accurate for agentId stamping.
- **Criteria Impacted:** #4 (Cross-Section Consistency) — minor narrative friction only; synthesis (AUDIT Security) does not propagate the overstatement.
- **Recommended Resolution:** In final AUDIT.md or a parent note, add a one-line reconciliation: "Note: scope-creep.md:117 guard description for vaultPath is aspirational; residual exposure remains per SEC-P0-01 (addressed in pre-RC cuts)." Or update scope-creep "what looks solid" to qualify "intended G12/G13... with one residual vaultPath vector (see security.md)".

### P3 nit CRIT-SEC-P3-01: Internal finding ID mismatch between source and synthesis (cosmetic)

- **Status:** open
- **Severity / Type:** P3 / nit (purely notational)
- **Location (audit files):** `findings/security.md:45` (SEC-P2E-01 etc.) vs. `AUDIT.md:112` (SEC-P0-01), `separation-cuts.md:13` (SEC-P0-01), inventories
- **Description:** Phase 2e source uses SEC-P2E-01/02/03/04 for the detailed findings; synthesis and proposals uniformly use SEC-P0-01/02/03/04 for the P0-mapped versions. Mapping is always explicit and citations are correct, so no functional issue. However, it creates a tiny traceability tax for future readers comparing raw Phase 2e output to AUDIT tables.
- **Evidence / Citation from reviewed deliverables:** `security.md:45,70,90,110` (SEC-P2E-* headings) + `173` (P0 mapping); `AUDIT.md:112-115` (SEC-P0-*); `separation-cuts.md:13,20,26,32` (SEC-P0-* rationales).
- **Criteria Impacted:** #5 (Synthesis Quality) — cosmetic only; does not impair any other criterion.
- **Recommended Resolution:** Optional: In a future audit run, normalize Phase 2 adversarial reports to use final P0/P1 IDs from the start, or add a small "ID Mapping" table in AUDIT.md appendices.

### P1 suggestion CRIT-SEC-P1-01: Supply-chain / legacy surface (SEC-P2E-05) correctly classified P1 but could be elevated in visibility in proposals

- **Status:** open
- **Severity / Type:** P1 / suggestion
- **Location (audit files):** `findings/security.md:132-151` (SEC-P2E-05 Medium/P1); `proposals/separation-cuts.md:37-41` (P1 cut #5 OpenClaw deletion); `AUDIT.md:116`
- **Description:** Security correctly treats supply-chain (unpinned requirements, repo-shipped native binary, openclaw direct-bypass shims) as P1 (violates SECURITY_PLAN Assumption #8, enables full bypass). Proposals correctly prioritize the 4 P0 security cuts first, then include OpenClaw deletion as P1 cut #5 with strong cross-refs to security + scope-creep + ci-release-adversarial. However, the proposal does not explicitly call out the native_afm_helper attestation gap or requirements hash enforcement as a tracked P1 item alongside the deletion. This is minor because legacy shims (the direct bypass) are covered, but full supply-chain hygiene (per ci-release-adversarial) could be more prominent.
- **Evidence / Citation from reviewed deliverables:** `security.md:144-151` (repro for native + legacy + pip); `separation-cuts.md:40` (rationale cites security.md:24); `AUDIT.md:116` (SEC-P1-01); `scope-creep.md:45-49` (openclaw highest-risk dead).
- **Criteria Impacted:** #3 (minor completeness on non-P0 security items); does not affect the required 4 P0 proposals.
- **Recommended Resolution:** Add a short "P1 Supply-Chain Hygiene" subsection or bullet in proposals referencing the existing cut #5 + CI requirement for `pip-tools` lock + attestation. (Low priority vs. pre-RC P0s.)

No additional P0/P1 bugs or contradictions were identified. All core 4 P0s, severity judgments, and cross-section narratives hold under the 5 critic criteria.

---

**Critic Loop Disposition:** All 4 Security P0s have concrete reproduction/citation from Phase 2e `security.md`; severity mapping is accurate and non-inflated; the 4 remediation proposals in `separation-cuts.md` are complete and actionable (one minor enhancement suggested); no material contradictions exist between Security and Architecture/Scope-creep sections (only the one low-severity nit on guard wording). Synthesis quality for security is high (verbatims, provenance, coverage all strong).

**Next for Parent:** Incorporate the 2 nits + 2 suggestions (or explicitly rebut) before declaring Phase 4 complete. Re-run critic on updated AUDIT.md if material changes. Security dimension does not block overall RC closure once the 4 P0 cuts + CI-P0-01 are executed.

**Output path (this file):** `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19/grok/audits/2026-05-19/findings/critic-security.md`

*Generated exclusively from tool-grounded reads of the audit deliverables in the dedicated worktree. All citations reference the synthesized reports (no source-tree access).*

---

**Appendix: Quick Trace Matrix (for parent verification)**

| P0 ID (AUDIT) | Source in security.md | Repro lines | Proposal in separation-cuts.md | Cross-ref in inventories/arch/scope | Grounded? |
|---------------|-----------------------|-------------|--------------------------------|-------------------------------------|-----------|
| SEC-P0-01 (Critical, vaultPath) | 45-69 | 60-66 | Cut 1 (9-15) | public-surfaces:24,64; arch:52; scope:117 (noted) | Yes |
| SEC-P0-02 (High, non-strict) | 70-89 | 81-86 | Cut 2 (16-22) | agent-specific-leaks:14,43; arch:70 | Yes |
| SEC-P0-03 (High, ping inbox) | 90-108 | 102-106 | Cut 3 (23-28) | agent-specific-leaks:11; scope:117 | Yes |
| SEC-P0-04 (High, wikilink) | 110-130 | 123-128 | Cut 4 (29-33) | public-surfaces:48; arch:104 | Yes |

All 4 rows pass the 4 weighted critic checks. End of security critic notes.
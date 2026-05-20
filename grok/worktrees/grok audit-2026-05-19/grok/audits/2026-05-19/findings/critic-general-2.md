# Sovereign Memory RC Audit — Phase 4 Critic (General Slot 2, Agnostic Reviewer Persona)

**Reviewer:** Grok Build subagent (general/agnostic critic, RC audit Phase 4)
**Date:** 2026-05-19
**Posture:** Strictly READ-ONLY on all source + the 5 synthesized deliverables only. All output confined to `grok/audits/2026-05-19/findings/critic-general-2.md`. No modifications anywhere else.
**Deliverables reviewed (the exact 5):**
- `AUDIT.md` (full synthesis, 167 lines)
- `inventory/public-surfaces.md` (67 lines)
- `inventory/agent-specific-leaks.md` (43 lines)
- `inventory/dead-code.md` (59 lines)
- `proposals/separation-cuts.md` (88 lines)

**Raw findings cross-referenced for evidence verification (read-only):** `findings/{architecture.md, scope-creep.md, performance.md, ci-release.md, security.md, code-quality.md, performance-adversarial.md, ci-release-adversarial.md}` (targeted reads + grep on P0/P1 claims, line citations, "What looks good", and repro sections).

**Exact 5 critic criteria applied (per RC audit spec Phase 4):**
1. Scope discipline (stayed within the 11 enumerated public surfaces + no unenumerated expansion?)
2. Contradictions between sections/files (AUDIT vs. any inventory/proposal; inventories vs. proposals; exec summary vs. tables; Phase1 vs. Phase2 claims)
3. Missing evidence for any P0/P1 (every severity claim has direct repro/citation in a phase-1/2 `findings/*.md`?)
4. Severity inflation (P0 reserved strictly for blocks-RC security/contract/corruption per spec; DoS/UX/perf/availability only P0 if they falsify core claims)
5. Actionability (every recommendation states a concrete next action with file:line + owner or sequencing where relevant?)

**Methodology:** Full read of all 5; targeted re-reads of long sections (e.g., AUDIT tables, proposals P0 cuts); grep for P0/P1/SEC-/CI-/PERF-/CQ-/ARC-/SCP- IDs + "Recommended next action" + "What looks good" + "evidence" + "repro" across the 5 + key raw reports; verification that line citations resolve to real content in raw findings; list_dir on audit tree to confirm structure and absence of prior `critic-general-2.md`.

---

## Positive Observations on Synthesis Quality (What Looks Good)

The 5 deliverables demonstrate high-quality synthesis overall:

- **Fidelity and provenance:** AUDIT.md tables (all 6 sections) accurately aggregate the 10 phase reports with precise `findings/*.md:line-range` citations and "Evidence link" columns. No new ungrounded claims. Appendix (AUDIT.md:157-166) correctly lists the 4 inventory/proposal files and asserts "All P0/P1 map directly to evidence in the phase-1/2 deliverables" — this holds on verification.
- **Structural consistency:** All 5 use compatible formats (tables with ID/Severity/Location/Summary/Evidence/Recommended next action; "What looks good" sections; "Status: open"; "See also" cross-refs). Inventories feed AUDIT and proposals without duplication or drift.
- **Strong actionability in proposals:** `proposals/separation-cuts.md` is exemplary — 11 numbered cuts with exact `file:line` (e.g., `server.ts:64,94,...`, `principal.py:323-331`, `vault.ts:627/636-656`), "Change:" deltas, "Rationale:" tying to specific P0 IDs (SEC-P0-01 etc.), "Owner:", and clear pre-RC vs. post-RC sequencing (lines 78-82). Directly actionable for implementers.
- **Scope discipline in inventories:** `public-surfaces.md` enumerates exactly the 11 surfaces from Phase 1a architecture.md (with per-surface "Leaks", "Agent-agnostic?", "Documented?" columns and citations). `agent-specific-leaks.md` (15+ table + 8 leaks + 3 violations) and `dead-code.md` (31+ with methodology + import/call-graph proof from scope-creep) stay narrowly within the RC public-surfaces + dead/unreferenced charter. No expansion into un-audited areas (e.g., no new eval internals or native Swift details beyond supply notes).
- **Cross-ref hygiene:** Every inventory ends with "See also: proposals/separation-cuts.md + AUDIT.md + raw findings". AUDIT appendix (lines 159-164) explicitly points back to the 4. Proposals cite the 10 phase reports. Bidirectional and complete.
- **Positive observations preserved:** AUDIT.md includes "What looks good" per dimension (e.g., daemon G11/G12/G23 wiring, zero primary-plugin engine imports, G03 test, SEC-014 audit-tail integrity, tmp_path test hygiene). These are supported by raw reports (security.md:30-37 positives verbatim in AUDIT; scope-creep.md:114-124 "What Looks Solid"; architecture.md:98+).
- **No silent dismissals:** The Phase 4 placeholder section (AUDIT.md:147-151) explicitly calls for critics to surface scope/contradiction/evidence/sev/actionability issues "with evidence or explicitly rebutted with citations."

**Overall synthesis grade (across the 5 files):** Strong. The parent synthesis (Phase 3) is tight, evidence-grounded, and well-organized. Minor issues below are low-severity and do not undermine the core P0 security + CI findings.

---

## Structured Critic Notes (by Criterion)

Notes use format: `[type] Title (file:line or AUDIT Section) — Status: open`

### 1. Scope Discipline

- **[suggestion] AUDIT.md:19 (Architecture & Separations scope) + inventory/public-surfaces.md:5 (Canonical List) — Status: open**
  Scope statement says "All 11 public surfaces" and inventories deliver exactly that enumeration (daemon, Python Agent/CLI, MCP 26-tool, 5 plugin contracts, vault/handoff/identity schemas, OpenClaw wrapper, console). Excellent discipline. Minor: the "Local Console / UI surfaces (secondary)" (public-surfaces.md:59-60) includes ui-server deep-research (flagged dead in dead-code.md:17 and SCP-P2-01); this is correctly scoped as secondary but could be called out more explicitly as "high-risk secondary surface under review for deletion."

- **[nit] proposals/separation-cuts.md:35-52 (P1 Scope/Quality cuts) — Status: open**
  Cuts 5-8 stay within dead-code inventory scope (openclaw, afm_scheduler, ui-server deep-research, duplicates). Good. The duplicate consolidation (cut 8) references "4 proven duplicate areas via import/call-graph" from scope-creep.md — correctly narrow. No over-scope.

### 2. Contradictions Between Sections/Files

- **[bug] scope-creep.md:117 ("What Looks Solid" for MCP) vs. AUDIT.md:112 (SEC-P0-01 table) + security.md:50-58 + proposals/separation-cuts.md:9-15 — Status: open**
  scope-creep.md:117 claims: "MCP tool surface: ... with schemas, G11/G12/G13/G15 guards (**no caller-controlled ...vaultPath...**)" (bold emphasis in original context).
  This is directly falsified by the later adversarial pass: SEC-P0-01 (Critical) still has optional `vaultPath` in `server.ts:64,94,608,628,647` (prepare_task/audit_*/negotiate) and `task.ts:647`/`vault.ts` handlers perform ensure + list on caller-controlled path with no EffectivePrincipal/G12.
  AUDIT.md correctly surfaces the P0 in Security section and proposals cut #1 addresses it. However, the Phase 1b "What Looks Solid" claim was not reconciled/qualified in the Phase 3 synthesis (AUDIT.md does not call out this Phase1-vs-Phase2 tension in its scope-creep "What looks good" at lines 58-60 or cross-refs). This is a material contradiction between deliverables. (Security-critic persona also surfaced a closely related nit; general review confirms it under contradictions criterion.)

- **[suggestion] AUDIT.md:10 (Executive Risk Summary) vs. tables at AUDIT.md:72,92,112-115 + security.md:178 — Status: open**
  Exec summary: "**P0 blockers (RC):** 4+ from Security (vaultPath..., non-strict..., cross-agent..., plugin wikilink...) . 1 from CI/Release ... Multiple high from Code Quality**".
  Tables enumerate: 4× SEC-P0 (security.md:178 confirms "1 Critical + 3 High = 4 (all map to P0)"), + CI-P0-01, + PERF-P0-01 (AUDIT.md:72). The "4+" phrasing is loose/ambiguous (the "+" does not clearly account for PERF-P0-01 or CI-P0-01, which are listed separately). Minor wording inconsistency between exec narrative and the actual P0 count across tables. Not a blocker but reduces precision.

- **[nit] inventory/dead-code.md:19 (31+ count) vs. scope-creep.md:19 (methodology) — Status: open**
  Both correctly explain the 31+ as "counting distinct files/dirs/areas; duplicates counted separately". Transparent, but the exact arithmetic (8 core +1 dir +14+ markers +4 dup areas +6 orphaned) is only in scope-creep.md. A one-sentence reconciliation in dead-code.md or AUDIT would eliminate any reader double-counting doubt. Low impact.

### 3. Missing Evidence for Any P0/P1

- **[suggestion] PERF-P0-01 evidence chain (AUDIT.md:72) — Status: open**
  Claim: "Universal audit append ... creates unbounded disk growth + inode exhaustion vector (DoS)". Evidence link: "performance.md: hot path table; performance-adversarial.md (DoS surfaces)".
  performance.md:19/112 does document "2 fs.appendFile per recordAudit ... 47+ call sites ... linear growth with no rotation" and labels the rec for caps/rotation as **P1 (high impact)** in its own recommendations (performance.md:94). The elevation to P0 + explicit "DoS" + "inode exhaustion" language originates in the adversarial translation + synthesis. The raw performance.md provides the volume data but not the P0 severity label itself. Acceptable (adversarial pass is allowed to escalate), but the "evidence link" citation is slightly indirect for the P0 classification. All other P0s (4 SEC + 1 CI) have stronger direct "blocks RC" or "falsifies post-Gxx claims" grounding in their source reports.

- **[nit] CQ-P1-01 zero-coverage claim (AUDIT.md:132) — Status: open**
  "Entire public Python Agent API + CLI entrypoints + GraphExporter have **zero direct test coverage**." Evidence link: "code-quality.md:7 (coverage mapping + P1-first table)".
  code-quality.md:70-96 provides the "exhaustive mapping (daemon RPC via _dispatch + _METHODS, MCP via register...)" methodology + specific grep/cross-ref process + table. The claim is well-supported, but the synthesis does not reproduce the "zero direct calls/exercises" definition or list the exact test files that were grepped (only high-level). Minor: for a P1 quality item, a one-line pointer to the methodology paragraph would strengthen it further. No actual missing evidence — just citation brevity.

- All other P0/P1 (SEC 4, CI-P0-01, ARC-P1-01/02, SCP-P1-01/02, etc.) have direct repros or exhaustive import/call-graph citations in their linked phase reports. No other gaps found after grep + targeted reads.

### 4. Severity Inflation (P0 only for blocks-RC security/contract/corruption)

- **[bug] PERF-P0-01 classification (AUDIT.md:72 + performance-adversarial.md) vs. spec definition + security.md:171-173 P0 mapping — Status: open**
  PERF-P0-01 is labeled P0 in the synthesis table for "unbounded disk growth + inode exhaustion vector (DoS)".
  Per critic criterion #4 and AUDIT.md:173 (quoting security.md): "**P0 (blocks RC — security flaw / broken contract / data corruption)**" and the 4 SEC items are the only ones mapped because "These directly falsify the post-G11/G12/G23 auth, vault-binding, and handoff-consent claims."
  CI-P0-01 qualifies (no regression gate on the security fixes themselves).
  Unbounded audit growth is a real availability/resource-exhaustion risk (correctly flagged in performance-adversarial as P0 for DoS under high hook volume), but it does not break the *auth/contract* model or cause *corruption* (SEC-014 escape/redaction integrity remains solid per security.md:26 and AUDIT.md:118). It is more P1 quality/UX/stability (consistent with performance.md's own P1 label for the cap rec). This is a borderline severity inflation under the strict "P0 only for blocks-RC security/contract/corruption" rule. Recommend: either reclassify PERF-P0-01 → P1 (with explicit DoS note) or add a footnote in AUDIT.md justifying why resource exhaustion on the primary audit path falsifies a core RC claim.

- **[nit] AUDIT.md:10 executive summary groups "Multiple high from Code Quality" correctly as non-P0** — Status: open (positive observation, minor wording only)
  The CQ items (zero coverage on SovereignAgent/CLI/GraphExporter = CQ-P1-01; bare excepts = CQ-P1-02) are correctly kept at P1 in the detailed table (AUDIT.md:130-136). Exec summary uses "high" descriptively, not as P0. Good discipline here (contrast with the PERF-P0 case above).

### 5. Actionability (concrete next action for every rec)

- **[suggestion] AUDIT.md tables (all sections) — Status: open**
  Every row has a "Recommended next action" column with concrete steps + file targets (e.g., ARC-P1-01: "Remove non-canonical aliases from core daemon; make seed_identity.py accept arbitrary agents..."; CI-P0-01: "Add minimal GHA (ubuntu + macos matrix..."; SEC-P0-01: "Remove vaultPath ... Add central assertVaultUnderAllowed..."). Strong. The only minor gap is that not every P1/P2 action is expanded into a numbered cut in proposals/separation-cuts.md (which rightly prioritizes the 4 P0 security + highest-risk dead). For example, ARC-P1-01/02 and CQ-P1-01/02 actions are table-only. This is acceptable scoping for the proposals doc, but a short "Non-cut P1 actions remain in AUDIT tables" note in proposals/separation-cuts.md:78 would improve traceability.

- **[positive] proposals/separation-cuts.md:7-82 (all 11 cuts) — Status: open (strong pass)**
  Every cut has: exact files+lines, precise "Change:" description, "Rationale:" linking to specific finding IDs, "Owner:", and sequencing. Cut 1-4 (P0) are 1:1 mirrors of the security.md remediations (68,88,108,130). Exemplary actionability.

- **[nit] inventory/public-surfaces.md:62-65 (Cross-Cutting Observations) + dead-code.md:54-58 ("In Case" features) — Status: open**
  Observations are diagnostic and correctly point at root causes (plugin FS layer lacks EffectivePrincipal/G12). Recommendations are implicit ("See also: proposals..."). Adding one explicit "Next action" sentence per observation (even if "Address via separation-cuts.md Cut X") would make the inventory files themselves fully actionable per criterion 5. Low severity.

---

## Summary Matrix (P0 Coverage Across the 5 Deliverables)

| P0 ID (AUDIT) | Present in inventories? | Present in proposals? | Evidence citation in raw findings | Actionable next step? | Notes from this critic |
|---------------|-------------------------|-----------------------|-----------------------------------|-----------------------|------------------------|
| SEC-P0-01 (vaultPath) | Yes (public-surfaces:24,64; agent-specific: n/a) | Yes (Cut 1:9-15) | security.md:45-69 (full repro) | Yes (concrete) | Grounded + actionable |
| SEC-P0-02 (non-strict) | Yes (agent-specific-leaks:14) | Yes (Cut 2:16-21) | security.md:70-89 | Yes | Grounded + actionable |
| SEC-P0-03 (ping inbox) | Yes (agent-specific-leaks:11,14) | Yes (Cut 3:22-27) | security.md:90-108 | Yes | Grounded + actionable |
| SEC-P0-04 (plugin G23 gap) | Yes (public-surfaces:48) | Yes (Cut 4:28-33) | security.md:110+ | Yes | Grounded + actionable |
| CI-P0-01 (no CI) | Indirect (via legacy risk) | Indirect (Cut 5 bundles openclaw) | ci-release.md + ci-release-adversarial | Yes (AUDIT table + proposals Cut 5) | Good |
| PERF-P0-01 (audit DoS) | No (not in dead-code or surfaces) | No (proposals focuses sec + dead) | performance.md + performance-adversarial (see missing-evidence note) | Yes (AUDIT table) | **Severity inflation candidate** (see #4) |

All 4 core security P0s are fully covered with evidence + concrete actions. CI-P0-01 well-supported. PERF-P0-01 is the only classification outlier.

---

## Outcome & Recommendations for Parent

- **Overall on the 5 deliverables:** Pass on 4/5 criteria with high marks. Excellent scope discipline, strong actionability (especially proposals), good evidence grounding for the critical security + CI items, and high synthesis fidelity.
- **Issues requiring parent attention (before 0 unresolved critical):**
  1. Reconcile or footnote the Phase 1b vs. Phase 2e contradiction on MCP guard completeness (scope-creep.md:117) — either qualify the "What Looks Solid" claim or add an explicit cross-ref in AUDIT.md scope-creep "What looks good".
  2. Decide on PERF-P0-01 severity (reclassify to P1 or add explicit justification vs. the "security/contract/corruption" definition). Update AUDIT.md:10 exec summary phrasing for exact P0 count and PERF-P0-01 visibility.
  3. Minor: tighten evidence links for PERF-P0-01 and CQ-P1-01; add one-line traceability note in proposals for non-P0 actions.
- No findings rise to new P0/P1 on the *synthesis itself*. The underlying 4 security P0s + CI-P0-01 remain the RC blockers.
- The created `critic-general-2.md` (this file) is the sole artifact. Parent should incorporate (or rebut with citations) into the Phase 4 critic loop status section of AUDIT.md.

**Files touched (only inside audit dir):**
`/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19/grok/audits/2026-05-19/findings/critic-general-2.md` (new;  ~280 lines; read-verified post-write).

All task requirements completed directly. Read-only posture maintained 100%. Ready for parent synthesis.
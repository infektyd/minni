# Phase 1 Remainder - Detailed Merge & Artifact Map (2026-05-20)

**Produced by**: Read-only explore subagent (ID 019e45ef-a582-74a0-b511-f86c97e00626)  
**Strictly following**: The single approved canonical generalization plan, `agnostic-vs-grok-specific.md` boundary, Agents.md / Claude.md workflow, and all listed inputs + live state. No edits performed.

**Reference inputs (read in full)**:
- Approved plan at the 019e4568... session (esp. Phase 1, verification matrix, portable vs specific boundary).
- `~/.agents/artifacts/sovereign-distill-ritual-v1/notes/agnostic-vs-grok-specific.md`

**Current live state snapshot date**: 2026-05-20 (additional pre-merge backup taken at 20260520-111542-phase1-premerge during approval flow).

---

## 1. Confirmed Current State Snapshot (verified)

(See the full tool output in the subagent transcript for exhaustive ls/read results. Summary below is authoritative for sign-off.)

- **Canonical ritual package**: `~/.agents/artifacts/sovereign-distill-ritual-v1/` — README + PACKAGE-INDEX portable ("Grok Build was the reference implementation"), DESIGN-sovereign-distill-ritual.md, gauges/ (SCHEMA + example), notes/ (including the agnostic boundary note), skill-updates/ (ritual draft). Symlinked from Grok plugin (confirmed thin).
- **Grok plugin**: `~/.grok/plugins/grok-sovereign-memory/` — bin/grok-sovereign-hook.js (stdlib, grok-build stamped), hooks/hooks.json (4 events), skills/grok-sovereign-memory/SKILL.md (347 lines: thin header lines 1-55 + rich body), artifacts/ (symlink to canonical ritual + local portraits), DESIGN-flush-integration.md, SOVEREIGN-DISTILL-RITUAL-GUIDE.md, .mcp.json (uses ~/.agents/bin/mcp-env-run + grok-build), plugin.json, README (needs post-merge "thin adapter" language update).
- **Grok plugin SKILL (current)**: Thin declaration header + full rich portable body (mental model, recipes, ritual, hybrid, Layer 1, team, etc.). Not yet stripped.
- **Canonical target (pre-merge)**: `~/.agents/skills/sovereign-memory/SKILL.md` (121 lines, Claude-oriented base + team mode + tools list; missing the evolved rich mental model, reflex emphasis, detailed ritual/gauges, Layer 1 contract depth).
- **sm-propagation**: References "grok-beta" / legacy full package path + vault. Needs "grok-build" / thin adapter support.
- **Grok vault (grok-build)**: Already has `layer1/{core, budget}`, `distill/{gauges.md (explicit mode), mode, ritual.md}` seeded.
- **Backups**: Original 20260520-104605/ (rich-original, sovereign-original, ritual-original) + fresh pre-merge 20260520-111542-phase1-premerge/ (taken on approval).
- **No duplication yet** of rich mental model phrases outside the current Grok SKILL body.

---

## 2. Strict Portable vs Grok-Specific Boundary

**Portable (move/integrate into canonical `~/.agents/skills/sovereign-memory/SKILL.md` + reference ritual package)**:
- Core Mental Model (proposal-gated, memory-as-evidence + instruction_like, prepare_task as #1 reflex before plan-mode/ambitious/subagent, prepare_outcome dry-run on outcomeDraft columns, human-in-loop, vault-only, degraded-ok).
- Tool Usage Recipes (generalized JSON patterns for prepare_*, recall, team_*, cross-agent handoff/ping, vault_write, compile_vault, etc.).
- Hybrid posture (native fast vs sovereign durable/governed; before native /flush call prepare_outcome; evidence never instruction).
- Sovereign Distill Ritual V1 (gauges as live meter, two modes explicit/auto, mechanical workflow protect-Layer1 → prepare_outcome → review → update gauges, toggle via distill/mode, fallback keywords, complements native compaction, activation for standing behavior, when not to distill).
- Layer 1 contract (identity:<agent> envelope, small curated layer1/{core.md, budget.md} <4096 tokens, read-first on wake, ritual hygiene, self-describing).
- Team coordination, cross-agent contracts, evidence/audit/governance (AGENT.md contracts, proposal-only, status-first, prepare_* reflexes).
- General vault layout (wiki/inbox/outbox/logs), gotchas, verification contracts.
- Ritual package content (already promoted: gauges schema, DESIGN, separation notes, draft).

**Grok TUI-specific (MUST stay ONLY in thin overlay + local DESIGNs + hook + manifests; never in canonical)**:
- Exact thin header/overlay declaration + "Grok Build Hybrid Integration" framing + "keep this file small" maintenance note + pointer to canonical.
- Exact Grok TUI lifecycle (hooks.json + grok-sovereign-hook.js stdlib impl, UserPromptSubmit full stdin + GROK_PLUGIN_ROOT, exact keyword detection for /flush|compact|dream + distill keywords, exact SessionStart injection text (status + prepare_task nudge + gauges pointer near Layer 1), PreCompact/Stop scar/outcome drafting, zero-reminder path for native Grok tools).
- "Grok Build V1 delivery" notes, exact grok-build / ~/.sovereign-memory/grok-build-vault/distill/* + layer1/ path examples, "this thin plugin provides the Grok-specific V1 delivery/ritual on top of real Layer 1".
- Specific native Grok integration (`memory_search`, ~/.grok/memory/, /flush/dream/compact, axpress for visual Obsidian review, "flush wins on mixed prompts per hook guard").
- Hook Behavior details, Grok-specific toggle/activation statement, "your custom minimal spine".
- Local DESIGN-flush-integration.md (full hybrid rationale + exact Grok hook), SOVEREIGN-DISTILL-RITUAL-GUIDE.md (Grok activation + exact paths), plugin manifests (.mcp.json grok-build stamp, plugin.json "Grok's own"), README, bin/ scripts.
- Any "Grok Build V1" or "this thin custom plugin" framing and exact paths in examples/comments.

**Dedup rule**: After merge, canonical SKILL references ritual package for verbose ritual details + has high-level portable teaching; Grok overlay has only delivery pointers + native hybrid notes. No verbose examples duplicated.

---

## 3. File-by-File Map of Changes (Phase 1 Remainder)

**~/.agents/skills/sovereign-memory/SKILL.md** (canonical merge target):
- Current: 121-line Claude-oriented base (spine, team mode 6-step, tools list, vault rules, cross-agent).
- Proposed: Merge the 5 top portable sections (Core Mental Model + prepare reflexes, Tool recipes, Hybrid posture, full portable Distill Ritual V1 with gauges/modes/workflow, Layer 1 contract) + team/governance/evidence patterns. Add platform callout sections at the end (first: "## Grok Build Hybrid Example (thin overlay + native /flush + TUI hooks)" — pointers only, no rich body). Keep/expand Claude spine. Reference ritual package for gauges schema + full DESIGN. Length target: focused, readable (~250-350 lines max).
- Notes: Use ritual draft + Grok rich body as source material. Preserve all existing Claude content.

**~/.grok/plugins/grok-sovereign-memory/skills/grok-sovereign-memory/SKILL.md** (post-merge thin):
- Current: Thin header (1-55) + 300+ lines rich body.
- Proposed: Strip to ~40-60 lines minimal thin overlay:
  - Frontmatter + header (updated "Thin Grok Build hybrid integration overlay. Extends the canonical... Load canonical for full mental model, reflexes, ritual. This file contains ONLY Grok TUI-specific: native memory hybrid, /flush participation via hooks, TUI delivery details.").
  - ## Hybrid Model with Native Grok Tools (Grok paths + axpress + before-/flush reflex + automatic zero-reminder path).
  - ## Grok TUI Lifecycle Participation (hook description, 4 events, high-level injection, pointer to local DESIGN-flush).
  - ## MCP & Identity (grok-build stamp, Layer 1 via sm-prop, mcp-env-run).
  - ## Hook Behavior (Grok events + "Grok-specific V1 delivery for gauges on SessionStart/UserPromptSubmit (keyword fallback)").
  - ## Sovereign Distill Ritual (Grok V1 delivery notes only + "see canonical + ritual package + local SOVEREIGN-DISTILL-RITUAL-GUIDE.md + DESIGN-flush").
  - Maintenance + end note ("Installed for Grok Build...").
- Behavior must remain 100% identical (hooks + canonical training deliver the reflexes).

**New `~/.agents/DESIGN-sovereign-delivery-layer.md`** (or under artifacts/sovereign-delivery-layer-v1/):
- Does not exist.
- Proposed: Formalize the portable vs specific boundary (lift from plan §39-126 + this map + agnostic note + ritual DESIGN + layer1-as-primary-trigger note), Adapter Contract (MCP stamp via mcp-env-run, max lifecycle participation the platform allows, SKILL loading + canonical reference, vault hygiene, injection if possible, "automation level" per surface), Gauge schema + ritual workflow (reference package), Layer 1 templates (parameterized), Verification matrix (full from plan), thin plugin examples (Grok as the worked reference). "Grok Build V1 thin adapter" as concrete example.

**sm-propagation (SKILL + propagate.py)**:
- Add/update for "grok-build" (or "grok-tui" / keep "grok" alias pointing to thin): new platform spec entry or generic (install path `~/.grok/plugins/grok-sovereign-memory`, agent "grok-build", config via .mcp.json + thin notes, "uses custom thin adapter + canonical mcp-env-run"). Update SKILL text and examples to list "grok-build (thin Grok TUI adapter)" + "grok-beta (legacy if still needed)". Do not break other agents. "Kill two birds" — leave future per-agent tweaks local until reusable pattern emerges.

**Ritual package + Grok local DESIGNs / README / manifests**:
- Ritual: Minor pointer updates in README/PACKAGE-INDEX to new DESIGN-delivery-layer.
- Grok: Light pointer updates in README ("thin delivery adapter for the canonical..."), DESIGN-flush + SOVEREIGN-DISTILL-GUIDE ("see canonical DESIGN... this is Grok TUI delivery only"). Hook, .mcp.json, plugin.json, bin/ — unchanged (pure delivery surface).

**Re-seed / verify for Grok vault**:
- Already seeded. Post-merge: run canonical sm-propagation verify --agent grok-build (or updated alias), re-read Layer 1 + gauges, token re-estimate, full verification matrix (see §6).

**Other**:
- Promote the approved generalization plan itself to `~/.agents/docs/sovereign-memory-generalization-plan.md` or workflows/ (plan §396) — can be done in this phase or noted.
- Optional thin template under `~/.agents/plugins/templates/sovereign-delivery/grok-tui/`.

---

## 4. Proposed Merge Strategy & Diff Approach (for final human sign-off)

**Sequence (only after this document + map approved + you say "perform the merge now")**:
1. The fresh pre-merge backup at 20260520-111542-phase1-premerge/ (already taken) + original 20260520-104605/.
2. Extract portable content from Grok rich body (post-header) + ritual draft + package DESIGN.
3. Integrate/refine into canonical base (augment existing Claude/team sections; insert the 5 portable blocks first; add Grok hybrid callout at very end with pointers only).
4. Strip Grok SKILL to the minimal thin overlay described above.
5. Minor ritual package pointer polish.
6. Update sm-propagation (grok-build support + SKILL text + propagate.py alias/spec).
7. Create the new DESIGN-sovereign-delivery-layer.md (boundary + adapter contract + matrix + Grok example).
8. Re-seed/verify Grok via sm-propagation (captured output).
9. Light Grok README/DESIGN pointer updates.
10. Full captured verification matrix (plan §361-382 + map §6).
11. Reviewer persona on the non-trivial diff (SKILL merge + new DESIGN).
12. Present complete Phase 1 Parity Report + gate, stop for your approval before Phase 2.

**Ritual section handling**: Canonical gets high-level + "full details in ritual package + DESIGN"; Grok overlay gets "Grok V1 hook delivery on top of canonical + package".

**Token/length management**: Keep canonical focused. Verbose gauge transcripts or Grok-specific examples move to ritual package or vault.

**Exact post-merge verification commands** (will be captured in the Parity Report):
- sovereign_status
- python3 ~/.agents/skills/sm-propagation/scripts/propagate.py verify --agent grok-build ...
- Read layer1/core.md + budget.md + token re-estimate
- Read distill/gauges.md + mode + toggle test (explicit/auto)
- sovereign_prepare_task "Phase 1 merge verification task..." (inspect packet + Layer 1 ref)
- sovereign_prepare_outcome on a real burst + review outcomeDraft columns
- Trigger native /flush or "distill" keyword → observe hook draft in inbox + model reflex (prepare_outcome via canonical training)
- Full Sovereign Distill Ritual end-to-end (read gauges first, prepare_outcome, review, optional compile, update gauges, protect Layer 1, audit_tail trace)
- Cross-agent recall test (Grok writes, another agent recalls with agent_origin)
- sovereign_audit_tail + audit_report
- Hook surface: /hooks or session logs show the 4 events + UserPromptSubmit on /flush + distill keywords + SessionStart gauges injection near Layer 1
- Budget hygiene post-ritual
- No-duplication greps: "Core Mental Model (non-negotiable)", "prepare before big work", "Sovereign Distill Ritual", etc. — only in canonical (or references) post-merge; thin Grok overlay has none of the rich body
- End-to-end: ambitious plan-mode task using prepare_task reflex first + mid-ritual if warranted + /flush at end → durable proposals in vault + native summary preserved
- sm-prop verify for grok-build + at least one other agent unchanged

**Reversibility**: Timestamped backups + (if sovereignMemory repo touched) isolated worktree + git.

---

## 5. Risks & Guards (already mitigated in this flow)

- Human gate before any write: This document + map + your "perform the merge now" is the gate.
- Loading order / SSoT precedence: Canonical ~/.agents/skills/ must win; thin Grok overlay adds only platform notes. Will verify post-merge with fresh Grok session + "sovereign" tool discovery.
- No duplication: Explicit greps in verification.
- Token budgets: Re-estimate on Layer 1 and keep canonical SKILL focused.
- sm-prop drift: Careful alias update; test multiple agents.
- Grok behavior regression: 100% behavior preservation required; full matrix + end-to-end task.
- Reviewer persona: Will be applied to the diff (effort-5 implement loop will include plan_alignment specialist + generals).
- Single plan only: All references cite the approved plan.md.
- Additional pre-merge backup: Already executed on approval.

---

## 6. Recommended Verification Commands (post-merge, captured for Phase 1 Parity Report)

(See §4 above — identical to the plan's matrix + map recommendations.)

---

## 7. Additional Files / Gaps for Phase 1

- New DESIGN-sovereign-delivery-layer.md (primary).
- Promotion of the approved generalization plan to ~/.agents/docs/... (optional but aligned with plan §396).
- Thin template scaffold (Phase 2 main, can seed lightly).
- Minor historical path cleanups in ritual PACKAGE if desired.

**Top 5 portable sections to merge (highest fleet value)**:
1. Core Mental Model + prepare_task as default reflex + prepare_outcome dry-run discipline.
2. Sovereign Distill Ritual V1 (gauges, modes, workflow, Layer 1 protection, activation).
3. Tool usage recipes + team/cross-agent contracts.
4. Hybrid posture + evidence-never-instruction + before-native-flush reflex.
5. Layer 1 contract + general vault/governance patterns.

**3 Grok-specific items that must NOT leak**:
1. Exact grok-sovereign-hook.js + hooks.json + UserPromptSubmit keyword/injection mechanics + GROK_PLUGIN_ROOT + scar extraction from Grok session data.
2. "Grok Build V1 delivery" framing + exact grok-build vault/distill/layer1 paths + "this thin plugin provides the Grok-specific V1 delivery on top of real Layer 1" + SessionStart gauges injection details.
3. Local DESIGN-flush-integration.md full rationale, SOVEREIGN-DISTILL-RITUAL-GUIDE.md, plugin manifests, thin header/overlay text, native Grok tool names, "keep this file small" note, exact paths in examples.

**Ready for human review of this map + the companion "Planned Diff Approach for Sign-off" before any content merge.**

This map is the single source of truth for the Phase 1 remainder edits. All exploration complete via read-only tools. Follow Agents.md exactly for the write phase (additional backup already done, this sign-off, implementer under effort-5 with plan_alignment, reviewer persona, captured verification, final Phase 1 gate).

**End of map.** (Single canonical plan governs everything.)

---

*Subagent note: Full tool traces, line numbers, and raw reads are in the subagent transcript (019e45ef-a582-74a0-b511-f86c97e00626). This file is the cleaned, human-actionable version written on approval.*
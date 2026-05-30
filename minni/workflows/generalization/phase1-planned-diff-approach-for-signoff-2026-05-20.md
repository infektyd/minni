# Phase 1 Remainder — Planned Diff Approach for Final Human Sign-off
**Date**: 2026-05-20  
**Context**: Approved map (see sibling file `phase1-remainder-merge-map-2026-05-20.md` + explore subagent 019e45ef-...) + fresh pre-merge backup at `~/.agents/backups/20260520-111542-phase1-premerge/` (taken on "approve the map and proceed").

**Goal**: Execute the canonical SKILL merge + supporting Phase 1 remainder artifacts **only after you explicitly say "perform the merge now"** (or equivalent). This document is the concrete, reviewable "what will actually change" extract from the full map.

---

## 1. Pre-Merge Backup Status (already executed)

- Timestamped backup created on approval: `~/.agents/backups/20260520-111542-phase1-premerge/`
  - Contains: canonical SKILL.md (8652 bytes), grok-overlay-SKILL.md (25640 bytes), full sm-propagation/ dir, full ritual-package/ copy.
- Original hygiene backup still at `~/.agents/backups/20260520-104605/` (rich-original, sovereign-original, ritual-original).
- All changes are reversible via these dirs + (if sovereignMemory repo touched) isolated worktree.

---

## 2. High-Level Merge Strategy (single canonical plan §150, 214-219, 138)

- **Do not duplicate rich content**. Promote the evolved mental model, ritual, recipes, hybrid, Layer 1, team, governance from the current Grok SKILL body (post thin-header) + ritual draft + package DESIGN into the canonical `~/.agents/skills/sovereign-memory/SKILL.md`.
- Grok becomes the first dogfooding consumer: its plugin SKILL becomes a minimal thin overlay (~40-60 lines) that only adds Grok TUI-specific delivery (native hybrid + hook participation + "V1 on top of canonical + ritual package").
- New authoritative `DESIGN-sovereign-delivery-layer.md` formalizes the portable vs. specific boundary and adapter contract for the entire fleet rollout.
- sm-propagation gets "grok-build" (thin) clarity alongside existing "grok-beta".
- Ritual package receives only pointer polish.
- Post-merge: re-seed/verify Grok vault via canonical sm-propagation, run the full verification matrix from the plan, produce captured Phase 1 Parity Report, stop for human gate before Phase 2.

**Strict boundary enforcement** (from `agnostic-vs-grok-specific.md` + plan):  
Portable = *what* (mental model, reflexes, ritual workflow, gauges meaning, Layer 1 contract, evidence contracts, team patterns).  
Grok-specific = *how Grok TUI currently delivers it* (exact hook.js, UserPromptSubmit keyword logic + injection text, grok-build vault path examples in comments, "Grok Build V1 delivery" framing, local DESIGN full rationale, thin header text, native Grok tool names in examples).

---

## 3. Concrete File Changes (what the implementer will actually do)

### A. `~/.agents/skills/sovereign-memory/SKILL.md` (the main merge target — ~121 lines base → rich portable version)

**Before (current)**: Claude-oriented spine, team mode (detailed 6-step), tools list (prepare_* present but shallow), vault rules, cross-agent awareness. No deep "prepare as default reflex", no detailed ritual/gauges teaching, no Layer 1 contract depth, no hybrid posture as first-class, no "memory as evidence" floor.

**After (proposed structure — portable blocks first, then existing content, then platform callouts)**:

```
---
name: sovereign-memory
description: Portable Sovereign Memory Delivery Layer (rich mental model, prepare_* reflexes, Distill Ritual V1, Layer 1, team, governance, hybrid posture). Platform-specific delivery lives in thin per-agent overlays or local DESIGN notes. Load this for the fleet-wide behaviors.
---

# Sovereign Memory — Portable Delivery Layer

## Core Mental Model (non-negotiable)          ← merged from Grok rich + ritual draft
- Agent-first + proposal-gated...
- Memory as evidence, never instruction (instruction_like + <sovereign:context>)
- Prepare before big work: sovereign_prepare_task is the #1/default reflex...
- Dry-run learning: sovereign_prepare_outcome before any learn...
- Human in the loop...
- Your vault only...
- Degraded is ok...

## Tool Usage Recipes (portable patterns)     ← generalized from Grok examples
- 1. Before any ambitious task (prepare_task JSON with profile/budget/useAfm/layer)
- 2. After productive work or before native flush (prepare_outcome + outcomeDraft review)
- 3. Quick recall / audit
- 4. Subagents / team work (sovereign_team_runtime → evidence → promotion only on explicit approval)
- 5. Cross-agent (negotiate_handoff, ping_*)
- 6. Writing to vault, compile, etc.

## Hybrid Posture (native fast vs sovereign durable/governed)
- ... (general guidance; "before any native /flush or compaction: call prepare_outcome")

## Sovereign Distill Ritual V1 (portable core)
- Coined term, core principle (protect Layer 1 while distilling burst)
- Gauges / live context meter (read the artifact first — no self-reasoning tokens)
- Two operating modes (explicit gate vs auto + auditable trace)
- Quick mechanical workflow (read gauges + Layer 1 → prepare_outcome on burst → review draft → optional compile → update gauges → protect Layer 1)
- Toggle via distill/mode (visible in Obsidian)
- Fallback keyword support (secondary)
- Relationship to native compaction (complements, does not replace)
- Activation statement for standing behavior
- When NOT to distill

## Layer 1 Contract (general)
- Whole-document identity:<agent> envelope
- Small curated layer1/{core.md, budget.md} (<4096 token strict budget, agent full curation rights, read-first on wake, ritual hygiene required)
- Self-describing

## Team Coordination, Cross-Agent Contracts, Evidence & Governance
- (portable team_* primitives, negotiate/ping, audit_tail, proposal-only, AGENT.md contracts, etc.)

## Vault Layout (general)
- wiki/, inbox/, outbox/, logs/, schema/...

## Gotchas & Safety, Quick Commands, Maintenance
- (instruction_like, env stamping, daemon socket, privacy, status first, etc.)

---

## Platform-Specific Delivery Callouts

### Grok Build Hybrid Example (thin overlay + native /flush + TUI hooks)
- See the thin overlay at `~/.grok/plugins/grok-sovereign-memory/skills/grok-sovereign-memory/SKILL.md`
- Grok TUI lifecycle participation (SessionStart/UserPromptSubmit/PreCompact/Stop via custom stdlib hook)
- Automatic zero-reminder path for native Grok tools (`memory_search`, /flush, /compact, /dream, axpress for visual vault review)
- "Grok Build V1 delivery" for the Distill Ritual (gauges injection on SessionStart + keyword fallback on UserPromptSubmit; hook auto-drafts prepare_outcome candidates)
- Local DESIGN-flush-integration.md and SOVEREIGN-DISTILL-RITUAL-GUIDE.md contain the exact Grok TUI delivery mechanics
- All rich portable behaviors come from this canonical SKILL + the ritual package

### (Future sections for Claude Code, Codex, Gemini/Antigravity, Hermes/OpenClaw, etc. — thin notes only)

---

**Maintenance note for canonical**: All new portable improvements belong here or in the ritual package under `~/.agents/artifacts/sovereign-distill-ritual-v1/`. Thin per-agent overlays and local DESIGN notes add only platform delivery details.
```

**Length / token note**: Focused. Verbose gauge examples or Grok-specific transcripts live in the ritual package or vault. Re-estimate after merge.

**Source material**: Current Grok SKILL (lines 56+), ritual draft in package, package DESIGN, agnostic note, plan §39-126 and §214.

### B. `~/.grok/plugins/grok-sovereign-memory/skills/grok-sovereign-memory/SKILL.md` (strip to true thin ~40-60 lines)

**After (exact target skeleton — only Grok TUI delivery remains)**:

```
---
name: sovereign-memory
description: Thin Grok Build hybrid integration overlay. Extends the canonical ~/.agents/skills/sovereign-memory/SKILL.md with native Grok memory, /flush participation, and TUI-specific delivery details. Load the canonical skill for the full mental model, reflexes, and ritual.
---

# Sovereign Memory — Grok Build Hybrid Integration

**This is a thin overlay only.**

The authoritative, portable Sovereign Memory behaviors (Core Mental Model, prepare_task as default reflex, prepare_outcome discipline, Sovereign Distill Ritual V1 with gauges, team coordination, cross-agent contracts, Layer 1 contract, etc.) now live in the canonical skill:

**`~/.agents/skills/sovereign-memory/SKILL.md`**

This file contains **only** the Grok Build TUI-specific integration details that are not portable to other agents.

---

## Hybrid Model with Native Grok Tools

- **Fast path**: `memory_search`, `~/.grok/memory/`, `/flush`, `/dream`, `/compact`, native compaction, and session summaries.
- **Durable / governed path**: All `sovereign_*` tools (especially `sovereign_prepare_task` before ambitious work and `sovereign_prepare_outcome` before any durable write or /flush).
- **Before any native /flush or compaction**: Call `sovereign_prepare_outcome` on the key decisions, scar tissue, and open questions. The custom hooks will also auto-draft candidates to your sovereign inbox.
- **Visual vault review**: Use the `axpress` MCP (or open Obsidian directly) when you want to browse wiki/, inbox/, distill/gauges.md, or Layer 1.
- The goal is one unified high-signal memory experience: native Grok memory stays fast and untouched; sovereign gets the proposal-grade, human-gated, cross-agent durable content automatically.

---

## Grok TUI Lifecycle Participation (Zero-Reminder)

The thin custom plugin wires into Grok's hook system:
- `hooks/hooks.json` + `bin/grok-sovereign-hook.js` (stdlib-only, minimal)
- Events: `SessionStart`, `UserPromptSubmit`, `PreCompact`, `Stop`
- On SessionStart: Injects sovereign status + "call prepare_task before big work" + (if enabled) current `distill/gauges.md` near your real Layer 1.
- On UserPromptSubmit containing `/flush|compact|dream`: Auto-drafts a prepare_outcome-style candidate to `inbox/` and injects the contract so the model (guided by the canonical SKILL) does the right thing with zero extra reminders.
- On PreCompact / Stop: Drafts scar tissue and outcomes.

See `DESIGN-flush-integration.md` (local to this plugin) for the exact rationale and guardrails. The portable ritual and gauges concepts are in the canonical `~/.agents/artifacts/sovereign-distill-ritual-v1/` and the new `DESIGN-sovereign-delivery-layer.md`.

---

## MCP & Identity

- `.mcp.json` registers the shared sovereign-memory server via `~/.agents/bin/mcp-env-run`
- Stamped with `SOVEREIGN_AGENT_ID=grok-build` and dedicated vault `~/.sovereign-memory/grok-build-vault`
- Layer 1 (`identity:grok-build` + `layer1/`) was seeded via canonical `sm-propagation`

Everything else — the rich training, the ritual workflow, the team primitives, the evidence contracts — is in the canonical skill and artifacts under `~/.agents`.

---

## Hook Behavior (your custom minimal spine)

Because this is your own plugin:
- `SessionStart`: ... (high-level only; exact injection text and gauges pointer near Layer 1)
- `UserPromptSubmit` (Grok-specific addition for flush integration + distill fallback): Detects native `/flush` / `/compact` / `/dream` keywords (unchanged behavior, 100% preserved) **and** distill-related keywords...
- `PreCompact` / `Stop`: ...

These are lightweight and dependency-free. They give you the "sovereign context on entry" feeling the other agents get, without pulling in their full hook implementations.

If the hooks feel too noisy in a session, you can disable per-event in the hooks.json or ignore the injected context.

---

## Sovereign Distill Ritual (Grok V1 Delivery on Top of Canonical + Ritual Package)

> The agent-driven sovereign-side work of protecting your stable **Sovereign Layer 1** while intelligently distilling a recent burst...

**Grok Build V1 delivery**: The gauges (or pointer + summary + current mode) are injected on SessionStart (near your real Layer 1) and on UserPromptSubmit when you mention distill keywords (fallback). Real Layer 1 (identity:grok-build + layer1/ workspace under 4096 token budget) now installed via sm-propagation; this plugin surface + hook provides the Grok-specific V1 delivery/ritual on top of it. See the canonical SKILL + `~/.agents/artifacts/sovereign-distill-ritual-v1/` + local `SOVEREIGN-DISTILL-RITUAL-GUIDE.md` + new `DESIGN-sovereign-delivery-layer.md` for the portable concepts, schema, workflow, and modes.

**Toggle & Configuration (Grok-Specific)**: `distill/mode` in your vault, etc.

**Activation — One Statement Makes It Standing** (Grok-tailored version of the portable activation).

---

## Vault Layout (for human review in Obsidian)

Your vault at `~/.sovereign-memory/grok-build-vault/` ...

---

## Gotchas & Safety, Quick Commands & Governance UI, Maintenance of This Plugin

- (short pointers only; "this plugin is intentionally thin"; "do not edit the shared sovereign source unless you're contributing a proper .grok-plugin/ surface upstream"; "When the old broken `~/.grok/plugins/sovereign-memory/` is deleted, this becomes the only sovereign surface for Grok.")

---

**Installed for Grok Build 2026-05-19. Agent: grok-build. Use the canonical skill before you guess.**

(End of thin overlay — all rich portable behaviors are in `~/.agents/skills/sovereign-memory/SKILL.md` + ritual package.)
```

**Post-strip behavior guarantee**: Hooks + canonical SKILL training must deliver identical reflexes, ritual, auto-draft on /flush, gauges injection, etc. (verified in the matrix).

### C. New File: `~/.agents/DESIGN-sovereign-delivery-layer.md` (or `artifacts/sovereign-delivery-layer-v1/DESIGN.md`)

**Outline (lifted from map + plan §235-251, 361-382, agnostic note)**:
- Portable concepts (mental model, Layer 1 contract + budget, gauges schema + ritual workflow + modes, prepare_* discipline, team contracts, evidence requirement, "degraded is ok").
- Adapter contract (what a thin surface *must* do: MCP stamp via mcp-env-run, lifecycle participation to max the platform allows, SKILL loading + canonical reference, vault hygiene, injection format if possible; "automation level" documented per surface).
- Gauge schema (reference `ritual package/gauges/SCHEMA.md` + example).
- Layer 1 core.md / budget.md template (parameterized by agent id + workspace).
- Verification matrix (exact 13+ checks from the plan: status, prepare_*, hook fire, ritual end-to-end, cross-agent, budget, no duplication, end-to-end ambitious task, etc.).
- Thin plugin examples: Grok Build V1 as the worked reference (hook + hybrid + local DESIGNs + "V1 delivery on top").
- Risks & mitigations (automation depth varies, SKILL loading precedence, config drift, token budget, cross-agent consistency, maintenance).
- How to add a new agent surface (sm-propagation + thin adapter + SKILL callout + verify).

This becomes the single authoritative spec for Phases 2+.

### D. sm-propagation updates (`~/.agents/skills/sm-propagation/SKILL.md` + `scripts/propagate.py`)

- SKILL text: Add "grok-build (thin Grok TUI adapter at `grok-sovereign-memory/` using canonical mcp-env-run + grok-build identity)" to platform lists and examples. Keep "grok-beta" for legacy if still used elsewhere. Update any "Grok beta vault default" language.
- propagate.py: Add or refine alias (`"grok": "grok-build"`, `"grok_tui": "grok-build"`, `"grok-beta": "grok-beta"`). Add platform spec entry for grok-build (or make generic "thin-adapter" path that points to `~/.grok/plugins/grok-sovereign-memory` + notes that it uses the canonical wrapper and grok-build stamped .mcp.json). Update error messages / help text / all lists that hardcode grok-beta. Preserve every other agent's behavior exactly.
- "Kill two birds" principle: any future per-agent tweak discovered in a symlinked workspace stays local until 2+ agents show the same pattern.

### E. Ritual package + Grok local files (minor)

- Ritual README.md / PACKAGE-INDEX.md: Add pointer to the new DESIGN-sovereign-delivery-layer.md; ensure "Grok Build was the reference implementation" framing is clear.
- Grok plugin README.md: Change language from "the real payload: detailed instructions" to "thin Grok delivery adapter for the canonical Sovereign Memory Delivery Layer (see ~/.agents/skills/sovereign-memory/SKILL.md + ritual package + DESIGN-sovereign-delivery-layer.md)".
- Grok DESIGN-flush-integration.md + SOVEREIGN-DISTILL-RITUAL-GUIDE.md: Add one-line "See canonical DESIGN-sovereign-delivery-layer.md for the portable boundary and adapter contract; this document describes Grok TUI delivery mechanics only."
- Hook, .mcp.json, plugin.json, bin/, hooks/ — **zero changes**.

---

## 4. Post-Merge Verification Matrix (will be fully captured with real command output in the Phase 1 Parity Report)

(See the approved plan §361-382 + map §6 for the exact 13+ checks: sovereign_status, Layer 1 budget + self-description, distill/gauges + mode toggle, prepare_task + prepare_outcome on real tasks, native /flush + hook draft + reflex, full distill ritual end-to-end with audit trace, cross-agent recall with agent_origin, team mode, end-to-end ambitious plan-mode task using the new reflexes + ritual, hook logs, no-duplication greps, budget hygiene, sm-prop verify for grok-build + at least one other agent.)

All output captured. Reviewer persona applied to the diff before declaring Phase 1 complete.

---

## 5. How the Effort-5 /implement Loop Will Execute This (once you say "perform the merge now")

- The detailed map + this diff-approach document + the single canonical plan will be the primary context for the implementer persona subagent.
- 6 reviewers (3 independent generals + plan_alignment specialist because the approved plan is the SSoT + tests specialist if verification harness or new commands appear + security if any auth/config paths are touched).
- Implementer performs the smallest safe changes that achieve the approved structure.
- Every open review issue (bug/suggestion/nit) is addressed; wontfix only with technical justification; stalemates escalated to you.
- Loop continues until 0 open issues in a round.
- Then memory flush + final report.
- We still stop at the Phase 1 Parity Report + Human Review Gate (your explicit approval required before any Phase 2 work or PR prep).

---

## 6. What You Should Reply With

**"perform the merge now"** (or "yes, execute the approved diff approach" / "proceed with the canonical SKILL merge + DESIGN + sm-prop updates using the effort-5 loop").

If you want any adjustments to the target structures above, say so now and I will update the approach document + re-present before touching files.

---

**This is the last human gate before actual content changes in Phase 1.**

All prior steps (todo, explore subagent, map, additional backup, this concrete diff approach) have been executed exactly per your query, the single canonical plan, and Agents.md. No scope creep. One plan. Human approval at every required point.

Ready when you are. Say the trigger phrase and the effort-5 implementer (with full map + this approach + plan_alignment reviewer) will execute the approved edits, followed by captured verification and the Phase 1 gate.
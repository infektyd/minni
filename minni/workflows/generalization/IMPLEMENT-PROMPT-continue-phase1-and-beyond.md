You are continuing the multi-phase Sovereign Memory Delivery Layer Generalization work from the approved canonical plan.

**Approved Plan Location (read this first in detail)**:
`~/.grok/sessions/%2FUsers%2Fhansaxelsson/019e4568-e93c-7ae1-8a9e-ef0fd1b9a08b/plan.md`

**Current State Snapshot (as of 2026-05-20)**:
- Phase 0 (general ~/.agents SSoT hygiene + propagate.py diff review) is COMPLETE. The two propagate.py copies were identical; Gemini's changes are unified. System hygiene rule applied fleet-wide.
- Phase 1 (Canonicalize Grok reference / dogfooding) is PARTIALLY COMPLETE:
  - Full timestamped backups exist under `~/.agents/backups/20260520-104605/`
  - The portable `sovereign-distill-ritual-v1/` package has been promoted to `~/.agents/artifacts/sovereign-distill-ritual-v1/` (new canonical SSoT).
  - The Grok plugin now has a symlink: `~/.grok/plugins/grok-sovereign-memory/artifacts/sovereign-distill-ritual-v1` → canonical location (thin confirmed).
  - Canonical README.md and PACKAGE-INDEX.md inside the ritual package have been updated to portable language ("Grok Build was the reference implementation").
  - The rich Grok SKILL has been replaced in the plugin with a **thin neutral overlay** (explicitly not labeled as a full "grok" sovereign skill — it is a short hybrid integration note that points at the canonical skill + contains only Grok TUI native memory + /flush + hook surface details).
  - An explore subagent produced a complete file-by-file map for the rest of Phase 1 (SKILL merge, new DESIGN doc, sm-propagation tweaks, re-seed/verify, risks, etc.).
- The single canonical generalization plan (the one above) governs everything. There are no separate per-agent plans — only thin adaptations of this one plan.
- All work must follow the user's Agents.md contract exactly: explore subagent first for complex areas → detailed thinking/plan artifacts → human approval at gates → (preferred) isolated worktrees for edits → reviewer persona on non-trivial diffs → captured verification output → human merge approval.

**Your Mission (execute the approved plan iteratively and safely)**:

Continue from the current state and finish **all phases** of the single approved plan:

1. Complete the remainder of **Phase 1** (Grok canonicalization):
   - Perform the rich mental model merge from the old Grok SKILL into the canonical `~/.agents/skills/sovereign-memory/SKILL.md` (use the explore map + the `agnostic-vs-grok-specific.md` note as the strict boundary).
   - Create the new `DESIGN-sovereign-delivery-layer.md` (or equivalent under artifacts/).
   - Make any necessary small updates to sm-propagation (SKILL + propagate.py) for "grok-build" clarity.
   - Re-seed/verify Grok's own Layer 1 + distill/ artifacts using the new canonical package.
   - Produce a full captured "Phase 1 Parity Report" using the verification matrix defined in the plan.
   - At the end of Phase 1, present a clear "Ready for human review" gate: show todo status, key diffs, verification output, risks addressed, and wait for explicit approval before moving on.

2. Execute **Phase 2** (Portable Spec + sm-propagation enhancements) using the same discipline.

3. Execute **Phase 3** (Gemini/Antigravity to full parity) — this was explicitly pinned by the user.

4. Execute later phases for Claude Code, Codex, Hermes/OpenClaw, etc., one at a time or in parallel only where truly independent (using separate subagents or worktrees).

**Strict Operating Rules (non-negotiable)**:

- This is **one single canonical plan**. Never create separate plans for individual agents.
- Use `todo_write` at the start of every major phase and keep it updated.
- For any non-trivial file area or new agent surface, first spawn an `explore` subagent (or `general-purpose` with read-only mode) to map affected files before writing code or large merges.
- Before any large/risky edit (especially the canonical SKILL merge), create a backup, show the planned diff approach, and get human confirmation.
- After every phase (and after each major agent), produce a concise "Parity / Verification Report" with the exact checks listed in the plan (sovereign_status, prepare_task/outcome, hook firing, distill ritual, Layer 1 budget, cross-agent recall, no duplication, etc.). Capture real command output.
- Use isolated git worktrees when making edits that touch the sovereignMemory repo or shared plugin code.
- Before any commit or merge of changes that affect other agents' experience, spawn a reviewer persona (or use the skill-reviewer subagent) and present its findings.
- Human approval is required at these gates (stop and present):
  - End of Phase 1 (before Phase 2)
  - End of Phase 3 (Gemini completion)
  - Before starting work on any new agent after Gemini
  - Before any PR preparation to the sovereignMemory repo
- When preparing repo contributions: portable improvements → `main`; Grok-specific thin delivery (hook, hybrid notes, etc.) → a `grok/` feature branch.
- At natural stopping points (end of a phase or before a big merge), output a ready-to-paste status + "what I plan to do next" and wait for the user to say "continue", "approve", or give new direction.
- If the user is away, you may do read-only exploration, todo updates, small safe doc edits, and verification runs, but **pause before any content merge or new agent surface work** and leave a clear handoff message.

**How to work iteratively while the user is away**:
- Work one logical sub-step at a time.
- After each sub-step that produces visible artifacts or verification, write a short progress note in the chat and update the todos.
- When you reach a human gate, stop cleanly, present the review package, and do not continue until the user responds.
- If a step would take a very long time, break it into smaller reviewable chunks.

**Reference Materials** (read as needed):
- The approved plan (primary source of truth)
- The explore subagent file map for Phase 1 (you can resume the subagent with its ID if the transcript is still available, or re-run a fresh explore using the plan description)
- Current backups under `~/.agents/backups/20260520-104605/`
- Canonical ritual package + the thin Grok overlay SKILL that already exists
- `~/.agents/skills/sm-propagation/SKILL.md` and `scripts/propagate.py`

Start by:
1. Re-reading the approved plan (especially Phases 1–3 and the verification matrix).
2. Updating the todo list with the current snapshot.
3. Confirming the current state of the files (symlinks, backups, thin overlay SKILL).
4. Producing a short "Current State + Next Action" summary for the user.
5. Then, if the user has given standing approval or you are in a long autonomous session, proceed to the next safe step in Phase 1 (the canonical SKILL merge), but **always stop at the human gate** at the end of the phase.

Execute carefully, verify everything with real output, and keep the user able to walk away and come back to clear handoff points.

When you reach the end of a phase or a gate, end your response with a clear "Human Review Gate" block containing:
- Phase status
- Key artifacts changed / created
- Full verification report (captured)
- Todo list snapshot
- Proposed next steps
- "Waiting for explicit approval to continue to <next phase or sub-step>"

Begin.
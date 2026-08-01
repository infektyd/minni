# Minni Distill Ritual — {{agent}} (V1)

**Status**: Seeded by `minni-install`. Living skeleton — the agent appends
traces here during each distill; the human edits notes here freely.

**Canonical instructions**: `plugins/minni/skills/minni/SKILL.md`, sections
"Minni Distill Ritual V1 (portable core)" and "Gauges / Live Context Meter".
That SKILL section is the source of truth; this file is the per-vault surface
and log. Do not fork the workflow here — fix the SKILL instead.

## Purpose
Mid-session, agent-driven ritual that protects Minni **Layer 1** (stable
identity, never chunked) while distilling the recent work burst out of the
temporary active context balloon. Ballooning during a focused sprint is
allowed; the ritual reduces it at the right moment using concrete gauges
instead of self-modelled token estimates.

Complements — does not replace — native compaction / flush.

## Toggle
Controlled by `distill/mode` in this vault:
- `explicit` (default): agent reads gauges, surfaces a short yes/no gate.
- `auto`: agent decides from gauges, acts, writes a human-auditable trace.
- `disabled`: ritual inactive (native compaction / flush only).

## Quick Workflow (follow verbatim once the decision is made)
1. Read `distill/gauges.md`; confirm the Layer 1 reference is healthy
   (`layer1/core.md`, `layer1/budget.md`, under the 4096-token budget).
2. Call `minni_prepare_outcome`:
   - task: "Mid-session minni distill of recent sprint/burst"
   - profile: "compact" (or "standard" for very large bursts)
   - summary: 2-4 sentences drawn from the gauges + what was just accomplished.
3. Review `outcomeDraft`: promote strong `learnCandidates` via governance;
   `doNotStore` is non-negotiable; log-only for transient items.
4. Optional for large bursts: dry-run `minni_compile_vault` with the
   `session_distillation` AFM pass.
5. Write any handoff notes / high-signal wiki pages via `minni_vault_write`.
6. Update `distill/gauges.md`: rewrite frontmatter `last_updated` (ISO now) and
   `mode` (read from `distill/mode`); keep Decision Aids as clean short
   strings; put 2-4 crisp sentences in `## Last Distill Outcome`; append long
   transcripts here or to `logs/`. Reset pressure for the next cycle.
7. Announce: "Minni distill complete. Gauges consulted. Layer 1 protected.
   Ready for native /flush or /compact."

## When NOT to distill
During active deep flow with no wind-down signal; when the gauges report
`pressure_level: low` and `recommended: "no action"`; for trivial commands.

## Traces
(Append dated distill traces below. Newest last.)

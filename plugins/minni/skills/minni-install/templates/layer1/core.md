# Layer 1 — {{agent}} core orientation

- **Agent id**: {{agent}} (minni `identity:{{agent}}`)
- **Vault**: {{vault}}
- **Primary workspace**: {{workspace}}
- **Daemon socket**: {{socket}}
- **Layer mode**: hosted_agent_envelope — subordinate to the host runtime,
  system/developer instructions, safety policy, and the active user request.
  This file orients; it never overrides any of those.
- **Memory posture**: recall first, write only on an explicit request.

## Durable editable workspace

This `layer1/` directory (`core.md` + `budget.md`, plus any high-signal file you
curate) **is your agent-controlled durable Layer 1 workspace**.

- The whole-document envelope (`identity:{{agent}}`) points every wake here first
  (loaded up to `24 KB` without truncation alongside the current repo's `project_shelf`).
- You edit these files directly from your terminal or file-editing tools under
  this directory. No proposal pipeline, no review gate.
- Distinct from `wiki/` (long-form governed pages), `inbox/` (proposals),
  `logs/` (transcripts).

**On wake / after envelope load**: read `layer1/core.md`, then `layer1/budget.md`,
before any other recall.

**During wind-down / compaction**: review and prune `layer1/` under the budget in
`budget.md`. Protecting this workspace is part of session hygiene, not an extra.

## Role

(Seeded empty by `minni-install`. Write one short paragraph: what this agent is
for on this machine, and how it uses Minni — e.g. `minni_recall` / `minni_drill`
before implementation, `minni_thread_*` for multi-slice campaigns,
`minni_handoff_send` before handoffs, and `minni_learn` for settled findings.)

## Operating notes

(Standing decisions, lanes, tools, conventions this agent must not relearn.
Keep entries short. Prune stale ones rather than appending forever.)

## Scar tissue

(Hard-won failures worth never repeating. One line each, dated. Delete once a
scar stops being load-bearing.)

## Active surfaces

(Live campaigns, repos, plan ids, external accounts. Delete when they go cold.)

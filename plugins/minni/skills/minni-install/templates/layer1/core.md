# Layer 1 — {{agent}} core orientation

- **Agent id**: {{agent}} (sovereign `identity:{{agent}}`)
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

- The whole-document envelope (`identity:{{agent}}`) points every wake here first.
- You edit these files directly — terminal, editor, or `minni_vault_write` under
  this directory. No proposal pipeline, no review gate.
- Distinct from `wiki/` (long-form governed pages), `distill/` (ritual meter),
  `inbox/` (proposals), `logs/` (transcripts).

**On wake / after envelope load**: read `layer1/core.md`, then `layer1/budget.md`,
before any other recall.

**During the distill ritual**: review and prune `layer1/` under the budget in
`budget.md`. Protecting this workspace is part of the ritual, not an extra.

## Role

(Seeded empty by `minni-install`. Write one short paragraph: what this agent is
for on this machine, and how it uses Minni — e.g. `prepare_task` before
delegation, plans for campaigns, `prepare_outcome` before wind-down, learns
operator-gated.)

## Operating notes

(Standing decisions, lanes, tools, conventions this agent must not relearn.
Keep entries short. Prune stale ones rather than appending forever.)

## Scar tissue

(Hard-won failures worth never repeating. One line each, dated. Delete once a
scar stops being load-bearing.)

## Active surfaces

(Live campaigns, repos, plan ids, external accounts. Delete when they go cold.)

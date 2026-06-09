# Minni `/dream` — agent-driven vault consolidation (design note)

> Idea (Hans, 2026-06-03): a `/dream` for Minni where the USER asks an agent (e.g. Claude) to
> take over the AFM's consolidation job and "dream" in that vault — specific to the user + platform.
> This note records the concept; it's a design sketch, not built.

## The key distinction Hans drew: two tiers of dreaming

Minni already has ONE dreamer (the on-device AFM loop). The `/dream` idea adds a SECOND, and they're
complementary — not a replacement:

| Tier | Dreamer | Trigger | Strength | Cost |
|------|---------|---------|----------|------|
| **1 — AFM loop** (exists) | small on-device model | continuous, idle-gated background | cheap, deterministic dedup/promote/prune | ~free, always-on |
| **2 — `/dream`** (proposed) | a frontier agent the user invokes (Claude/Gemini/etc.) | on-demand (`/dream`) | deep reflection, synthesis, contradiction *resolution*, reorg — things the small model can't do well | a real inference run, so on-demand only |

So: **AFM = the always-on janitor; `/dream` = the user calling in a capable agent for a deep clean.**
The agent dreams *in that user's own vault* — user- and platform-specific, because the vault is.

## What "dreaming" means (researched 2026-06-03)
Three senses across platforms; only the first two apply to a memory vault:
1. **Consolidation / sleep-time compute** (Claude Code AutoDream, OpenClaw auto-dream, Letta sleep-time
   compute — ~5x test-time cost cut): idle dedup, merge, prune, refresh, precompute.
2. **Reflection / abstraction** (Generative Agents): synthesize clusters of low-level memories into
   higher-order insights.
3. **Imagination / world-model rollouts** (Dreamer): NOT applicable — no world model to roll out; the
   "environment" here is the memory graph, not a controllable world.

## Non-negotiable principle
For an LLM, dreaming must NOT be free generation into memory (that's confabulation → pollution).
**Dreams PROPOSE; waking ENDORSES.** Every dream output is a candidate on the existing proposal→endorse
gate. Minni already enforces this (learn is proposal-first), so it's the right substrate.

## The `/dream` cycle (orchestrate EXISTING organs)
Minni already has the passes in `engine/afm_passes/` — `/dream` chains them with frontier-agent reasoning:
1. **Consolidate** — dedup/merge (AFM already does this; agent verifies).
2. **Reflect** (`synthesis`) — cluster recent learnings → meta-lessons (e.g. "verify on disk, never
   trust agent self-report" recurs across sessions → one durable synthesis doc, not 6 scattered notes).
3. **Resolve contradictions** (`contradiction_events`/`contradiction_log` exist) — find + RESOLVE
   conflicts. Live example: `"Minni rename: identity-only"` vs `"deep rename supersedes identity-only"`
   both sit in the vault and conflict. The small model flags; the `/dream` agent can actually resolve.
4. **Prune/forget** (`pruning`) — expire ephemeral/`temporary` (ties to the durability work).
5. **Reorganize** (`reorganization`) — fix structure: empty `projects/` layer, under-promoted decisions
   (2 decision docs vs 43 session notes).
6. **Generate gaps** — surface open questions for the next waking session.

## Trigger
Steal AutoDream's dual gate for any *auto* variant (≥24h elapsed AND ≥N sessions). But the headline
`/dream` is **user-invoked**: the user calls it when they want the deep clean, and picks which agent dreams.

## Why it matters
A dream loop is exactly the self-maintenance that would have prevented every hygiene issue found
2026-06-03 (565-file inbox litter, ephemeral-promoted-as-durable, the unresolved rename contradiction,
empty projects layer). Minni is ~80% there — the passes exist; what's missing is the orchestrator +
the agent-as-dreamer entry point. See `MINNI_HYGIENE_BACKLOG.md` for the concrete fixes those passes need.

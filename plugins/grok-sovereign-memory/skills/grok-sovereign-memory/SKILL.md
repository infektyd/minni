---
name: sovereign-memory
description: Grok Build session hook integration and TUI-specific delivery for Minni. Extends the canonical ~/.agents/skills/sovereign-memory/SKILL.md with native Grok memory, /flush participation, and lifecycle hook delivery. Load the canonical skill for the full mental model, reflexes, and ritual.
---

# Minni — Grok Build Hybrid Integration

**This file contains only the Grok Build TUI-specific integration details.**

The authoritative, portable Minni behaviors (Core Mental Model, prepare_task as default reflex, prepare_outcome discipline, Sovereign Distill Ritual V1 with gauges, team coordination, cross-agent contracts, Layer 1 contract, etc.) now live in the canonical skill:

**`~/.agents/skills/sovereign-memory/SKILL.md`**

This file contains **only** the Grok Build TUI-specific integration details that are not portable to other agents.

---

## Minni Project-Specific Rule (V1)

When we're working inside `~/Projects/minni` (project name: **Minni**; internal architecture: `sovereign-memory`), the following is active (full version lives in `~/Projects/minni/AGENTS.md`):

Minni is a big, sprawling, multi-surface system — not just whatever folder has "sovereign-memory" in the name. `plugins/sovereign-memory/` is one piece of the architecture, not the whole thing.

If the user starts talking about "live sovereign", "global sovereign", "the system", "downstream", or "what actually needs updating" after merges on main, treat it as a request to look at the gap between current `main` in the repo and *all* the installed/running/propagated pieces on the machine (daemon + engine behavior, the various skills, the big plugin, thin overlays like this one, hooks, etc.).

Hold the full map. Don't let lexical anchoring on the substring "sovereign-memory" shrink the scope. Re-read the root `AGENTS.md` if you feel the picture getting narrow. This project is too big and personal for us to keep doing that to ourselves.

---

## Hybrid Model with Native Grok Tools

- **Fast path** (the stuff that feels instant): `memory_search`, `~/.grok/memory/`, `/flush`, `/dream`, `/compact`, native compaction, session summaries — use these like you normally would, no extra ceremony.
- **Durable / governed path** (the stuff that actually survives crashes and matters later): All the `sovereign_*` tools. Especially `sovereign_prepare_task` before any real work and `sovereign_prepare_outcome` before you let anything get written or /flushed.
- Before you hit native /flush or compact, run `sovereign_prepare_outcome` on the decisions, scar tissue, and open questions. The hooks will quietly draft the good stuff to your sovereign inbox so it doesn't get lost in the noise.
- Want to actually see the vault? Use the `axpress` MCP or just open Obsidian and browse wiki/, inbox/, the gauges, Layer 1, whatever. It's all there.
- The whole point: native Grok memory stays snappy and local. Minni is the long-term, high-signal, human-gated, cross-agent stuff that actually sticks around when the TUI dies on you (again).

---

## Grok TUI Lifecycle Participation (Zero-Reminder, Because the TUI Keeps Dying on Us)

The session hooks in this integration participate in the Grok Build lifecycle so you don't have to remember the ritual every single time the TUI restarts or crashes:

- `hooks/hooks.json` + the tiny `bin/grok-sovereign-hook.js` (pure stdlib, no drama)
- Events we actually care about: `SessionStart`, `UserPromptSubmit`, `PreCompact`, `Stop`

What it actually does:
- On SessionStart (new tab, new launch after a crash, whatever): It quietly injects the current sovereign status, reminds you to `sovereign_prepare_task` before you go off the rails, and (when enabled) drops the latest `distill/gauges.md` right next to your real Layer 1 so you don't start blind.
- When you type `/flush`, `/compact`, or `/dream`: It auto-drafts a proper `prepare_outcome` candidate into your sovereign inbox and wires up the contract so the model does the right thing without you having to babysit it.
- On PreCompact or Stop: It grabs the scar tissue and outcomes before everything evaporates.

The goal is that even when the TUI locks up and you have to murder the tab, the important shit still made it into the durable layer.

Full rationale and guardrails live in the local `DESIGN-flush-integration.md`. The real portable ritual lives in the canonical skill under `~/.agents`. This file is just the Grok-specific glue that keeps us from losing the plot every time the pager has a bad day.

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

- (short pointers only; this is the Grok-specific hook integration surface; do not edit the shared sovereign source unless you're contributing to the canonical plugin; the old full-plugin attempt under `~/.grok/plugins/sovereign-memory/` has been superseded by this hooks-based delivery.)

---

**Installed for Grok Build 2026-05-19. Agent: grok-build. Use the canonical skill before you guess.**

(End of Grok Build hook integration surface — all rich portable behaviors are in `~/.agents/skills/sovereign-memory/SKILL.md` + ritual package.)
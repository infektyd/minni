# Minni rename — identity-only

> Status: approved 2026-05-26. Scope: identity surface only. Runtime contracts unchanged.

## Why

Agents repeatedly treat "Minni" and `~/Projects/sovereignMemory` as two different projects, over-engineering integration bridges between what is actually one repo. The user has to keep typing "sovereign memory" everywhere to be understood. This is friction in chat, not a code problem.

The brand voice doc (`.claude/brand-voice-guidelines.md`) framed the rebrand as "presentation-only — no code changes." This spec interprets that as: **no functional change**. Identity surfaces (directory name, repo name, top-level docs, plugin display names, agent-facing skill descriptions) are in scope. Runtime identifiers (slash-command prefix, MCP namespace, vault directory path) stay on the legacy `sovereign-memory` string for now and will be migrated in a separate pass.

## What changes

| # | Surface | Change |
|---|---|---|
| 1 | Local working copy | `~/Projects/sovereignMemory/` → `~/Projects/minni/` — **pending; manual step after daemon restart** |
| 2 | GitHub repo | `infektyd/sovereign-memory` → `infektyd/minni` (auto-redirect) |
| 3 | `README.md` | Title + body rebrand to "Minni" — single name, no dual-name disclaimer |
| 4 | `AGENTS.md` | Header + anti-narrowing rule reworded to use "Minni" as the product noun |
| 5 | `DESIGN.md` | Title + body "Sovereign Memory Console" → "Minni Console" |
| 6 | `SECURITY_PLAN.md` | "Sovereign Memory" product mentions → "Minni" |
| 7 | ~~Hardcoded project paths~~ | **Deferred to a follow-up PR after the local `mv`** — runtime configs (e.g. Grok `SOVEREIGN_WORKSPACE_ID`) and helper-script defaults (`propagate.py --repo`) must change together with the actual filesystem move to avoid breaking workspace scoping. This PR keeps every `~/Projects/sovereignMemory` path intact. |
| 8 | Plugin manifests (display name, author, description, homepage) | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `openclaw-extension/plugin.json` + `openclaw.plugin.json` + `package.json` + `README.md`. Plugin `name` field stays `sovereign-memory` because it's the slash-command prefix (eventual rename). |
| 9 | Agent-facing skill descriptions | `sm-propagation` SKILL.md and `grok-sovereign-memory` SKILL.md frontmatter `description` + bodies |
| 10 | Synonyms / NL aliases | `engine/data/synonyms.yml` adds `minni daemon` alias |
| 11 | Reference plates README | "Sovereign Memory Reference Plates" → "Minni Reference Plates" |
| 12 | Project memory | One sovereign-memory `learn` recording the equivalence; update global `~/CLAUDE.md` project table |

## What stays (explicit) — runtime contracts, eventual rename

These are the legacy `sovereign-memory` strings still in use. Migration to `minni` is a separate, coordinated pass (it forces MCP re-registration across every agent host).

- Slash-command prefix: `/sovereign-memory:recall`, `/sovereign-memory:learn`, …
- MCP tool namespace: `mcp__sovereign-memory__*`
- Vault directory: `~/.sovereign-memory/`
- Plugin directories: `plugins/sovereign-memory/`, `plugins/grok-sovereign-memory/`, top-level `sovereign-memory/`
- Plugin manifest `name`/`id` fields (drive the slash prefix above)
- Python/TS module names, daemon protocol, DB schema, `agent_origin` tags
- Skill IDs (`sovereign-memory:recall`, etc.)
- Deep historical / analytical docs (`docs/RC_PLAN.md`, `docs/OBSERVED-USAGE.md`, `docs/ENGINEERING-REVIEW.md`, `docs/plans/`, `docs/research/`, `docs/reviews/`, `sovereign-memory/workflows/generalization/`) keep their original "Sovereign Memory" product-noun usage as historical record.

## Order of operations

1. Branch from clean `main`
2. Docs + hardcoded-path rebrand → commit
3. `gh repo rename minni`
4. Local `mv ~/Projects/sovereignMemory ~/Projects/minni` + `git remote set-url`
5. Push from new path, open PR
6. Global `~/CLAUDE.md` project-table update
7. Sovereign-memory `learn` recording the equivalence

## Out of scope

- Renaming any runtime identifier (MCP namespace, vault dir, plugin IDs)
- Visual identity work (`brand-rebrand/minni-design-system/` scaffolds are empty; separate pass)
- Cross-host agent configs (Codex/Gemini/Hermes/OpenClaw) that hardcode the path — flagged for follow-up, not done here

## Risks

- Other terminal sessions or IDE workspaces pinned to the old path will need re-opening
- README badges or external links referencing the old repo URL will redirect but may want a manual update later
- Memory entries in the sovereign-memory daemon that store the old path stay readable (the spine doesn't care) but will look stale; addressed by the `learn` in step 7

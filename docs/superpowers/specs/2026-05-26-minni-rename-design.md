# Minni rename — identity-only

> Status: approved 2026-05-26. Scope: identity surface only. Runtime contracts unchanged.

## Why

Agents repeatedly treat "Minni" and `~/Projects/sovereignMemory` as two different projects, over-engineering integration bridges between what is actually one repo. This is friction in chat, not a code problem. The fix is to make the equivalence loud and unmistakable where any agent looks first.

The brand voice doc (`.claude/brand-voice-guidelines.md`) frames the rebrand as "presentation-only — no code changes." This spec interprets that as: **no functional change, no runtime-contract rename**. Identity surfaces (directory name, repo name, top-level docs) are in scope.

## What changes

| # | Surface | Change |
|---|---|---|
| 1 | Local working copy | `~/Projects/sovereignMemory/` → `~/Projects/minni/` |
| 2 | GitHub repo | `infektyd/sovereign-memory` → `infektyd/minni` (auto-redirect) |
| 3 | `README.md` | Hero + first section rebrand to "Minni"; add one-line disambiguator |
| 4 | `AGENTS.md` | New top banner: "This project is **Minni**. Substring `sovereign-memory` is internal architecture, not a separate project." Existing anti-narrowing rule stays. |
| 5 | `DESIGN.md`, `SECURITY_PLAN.md` | Title + one-line note. Body unchanged. |
| 6 | Hardcoded paths | 5 files reference `/Users/hansaxelsson/Projects/sovereignMemory/...` — replace with `.../minni/...` |
| 7 | Project memory | Record one sovereign-memory `learn`: "Minni == infektyd/minni == ~/Projects/minni == architecture sovereign-memory"; update global `~/CLAUDE.md` project table |

## What stays (explicit)

- `plugins/sovereign-memory/`, `plugins/grok-sovereign-memory/`, top-level `sovereign-memory/`
- `mcp__sovereign-memory__*` MCP tool namespace
- `~/.sovereign-memory/` vault directory
- All Python/TS module names, daemon protocol, schema, `agent_origin` tags
- Skill IDs (`sovereign-memory:recall`, etc.)
- Brand voice doc content (already correct)

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

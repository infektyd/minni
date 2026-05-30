# Minni Rename — P4 (Per-Platform Manifests) Plan

> Daemon stays DOWN. Repo/config only. Clean direct rename, no aliases.

**Goal:** Every platform's config + per-platform skill/command docs resolve to the minni anchors (§3b): `MINNI_*` env, `~/.minni/<a>-vault`, `~/Projects/minni`, `mcp__minni__minni_*` verbs. Fix grok's stale `~/Projects/sovereignMemory` and rename the grok plugin dir.

**Spec:** `docs/superpowers/specs/2026-05-29-minni-deep-rename-design.md`

## Task 1: Active env blocks (config the runtime reads)
- `plugins/minni/.gemini-plugin/gemini-extension.json`: `SOVEREIGN_AGENT_ID`→`MINNI_AGENT_ID`, `SOVEREIGN_VAULT_PATH`→`MINNI_VAULT_PATH` (`~/.sovereign-memory/gemini-vault`→`~/.minni/gemini-vault`), `SOVEREIGN_SOCKET_PATH`→`MINNI_SOCKET_PATH` (`~/.sovereign-memory/run/sovrd.sock`→`~/.minni/run/sovrd.sock`).
- `plugins/grok-sovereign-memory/.mcp.json`: all `SOVEREIGN_*`→`MINNI_*`; `SOVEREIGN_WORKSPACE_ID=~/Projects/sovereignMemory`→`MINNI_WORKSPACE_ID=~/Projects/minni`; vault/socket paths `.sovereign-memory`→`.minni`; the runner arg `mcp-env-run sovereign-memory`→`mcp-env-run minni`.
- `plugins/grok-sovereign-memory/bin/grok-sovereign-hook.js` line 19: `process.env.SOVEREIGN_VAULT_PATH`→`MINNI_VAULT_PATH`, default `.sovereign-memory/grok-build-vault`→`.minni/grok-build-vault`.
- Check `.codex-plugin/.mcp.json` and `.kilocode-plugin/.mcp.json` for any `SOVEREIGN_*`/`.sovereign-memory` env and apply the same.

## Task 2: Per-platform skill + command docs (the P2 sweep missed these dirs)
- `plugins/minni/.kilocode-plugin/skills/**`, `plugins/minni/.kilocode-plugin/commands/**`, `plugins/minni/.codex-plugin/**`, `plugins/grok-sovereign-memory/skills/**` and `*.md`:
  - tool verbs `sovereign_<x>`→`minni_<x>` (e.g. `sovereign_audit_tail`→`minni_audit_tail`), `mcp__sovereign-memory__`→`mcp__minni__`, skill-id `sovereign-memory:`→`minni:`.
  - env var refs in prose/tables `SOVEREIGN_<X>`→`MINNI_<X>`.
  - paths `~/.sovereign-memory`→`~/.minni`, `~/Projects/sovereignMemory`→`~/Projects/minni`.
  - brand phrase "Sovereign Memory"→"Minni" (prose).
  - Targeted edits, NEVER touch `docs/`.

## Task 3: Rename the grok plugin dir
- `git mv plugins/grok-sovereign-memory plugins/grok-minni`. Update any in-repo references to that path (marketplace/manifests). Leave home-dir install-target paths alone.
- Rename the inner skill dir `skills/grok-sovereign-memory`→`skills/grok-minni` if it mirrors the plugin name.

## Task 4: Verify
- `rg -n "SOVEREIGN_|\.sovereign-memory|Projects/sovereignMemory|sovereign_[a-z]|mcp__sovereign-memory__" plugins/minni/.gemini-plugin plugins/minni/.codex-plugin plugins/minni/.kilocode-plugin plugins/grok-minni -g '!*.md'` → EMPTY for config; the `*.md` should also be minni except legitimate historical mentions.
- TS build exit 0; `npm test` (expect 136/137, the date test).
- Only pre-existing untracked files + committed P4; no WIP bundled (WIP is stashed).

## NOT in P4
- Identity-envelope prose/branding in `~/.minni/identities` (vault content; structural paths already fixed in P3) — optional light pass.
- P5 skills keep/merge/retire audit.
- Bring-up.

# Minni Rename — Agent Platform Playbook

**What this is:** the "what to update on your side" reference for every agent platform during the `sovereign-memory` → `minni` deep rename. One section per platform: where its config lives, its current (verified) state, the required changes, and how to verify.

**Spec:** `docs/superpowers/specs/2026-05-29-minni-deep-rename-design.md`
**Status as of 2026-05-29:** repo-side manifests inspected; **live installed configs not yet verified** (marked ⚠️ where so).

---

## The two canonical anchors (everything resolves to one of these)

1. **Root project dir** — `~/Projects/minni` (git repo; source of truth).
2. **Installed plugin** — `~/.agents/plugins/sovereign-memory@minni` (runtime the agents load).

The per-agent **vault** (`~/.minni/<agent>-vault`) is **derived from the daemon**, not a third anchor — see "Vault path simplification" below. No config may reference `~/Projects/sovereignMemory`, bare `sovereign-memory@sovereign-memory`, or any other path.

## Canonical target config (applies to every platform)

| Field | Current | Target |
|---|---|---|
| Plugin `name` | `sovereign-memory` | `minni` |
| `mcpServers` key | `sovereign-memory` | `minni` (→ namespace `mcp__minni__`) |
| Tool verbs | `sovereign_recall`, … | `minni_recall`, … |
| Env: agent id | `SOVEREIGN_AGENT_ID` | `MINNI_AGENT_ID` |
| Env: vault | `SOVEREIGN_VAULT_PATH=~/.sovereign-memory/<a>-vault` | `MINNI_VAULT_PATH=~/.minni/<a>-vault` |
| Env: socket | `SOVEREIGN_SOCKET_PATH=~/.sovereign-memory/run/sovrd.sock` | `MINNI_SOCKET_PATH=~/.minni/run/minnid.sock` |
| Env: workspace | `SOVEREIGN_WORKSPACE_ID` (grok: stale path) | `MINNI_WORKSPACE_ID=~/Projects/minni` (anchor 1) |
| Author field | operator's real name | `Infektyd` (guard blocks the real name) |
| Brand strings / URLs | "Sovereign Memory", `…/sovereign-memory` | "Minni", `…/minni` |

During transition the daemon accepts BOTH `MINNI_*` and `SOVEREIGN_*` (P2 alias layer), so a platform keeps working until it's individually cut over.

## Vault path simplification (the link-sprawl fix)

Today each platform's manifest **re-types** its vault path. After migration, derive it from a single rule — `<vault_root>/<agent>-vault` where `vault_root = ~/.minni/` is owned by the daemon — so no manifest hardcodes a divergent path. Minimum bar: all five resolve under `~/.minni/`, none under `~/.sovereign-memory/` (which becomes a back-compat symlink) and none under `~/Projects/sovereignMemory`.

---

## Order of operations

Cut over **one platform at a time**, aliases stay live as the net:
1. **Gemini/Antigravity** — good-but-not-gold; verify against the canonical config first, fix any drift, use the *verified* result as a cross-check.
2. **Claude Code**
3. **Codex**
4. **Kilocode**
5. **Grok-build** — last and most work (stale-path repair).

---

## Per-platform

### 1. Gemini / Antigravity  — confidence: HIGHEST (still verify)
- **Repo config:** `plugins/minni/.gemini-plugin/gemini-extension.json`
- **Current (verified):** `name: sovereign-memory`; `mcpServers.sovereign-memory`; env `SOVEREIGN_AGENT_ID=gemini`, `SOVEREIGN_VAULT_PATH=~/.sovereign-memory/gemini-vault`, `SOVEREIGN_SOCKET_PATH=~/.sovereign-memory/run/sovrd.sock`. No stale workspace pointer. Vault `~/.sovereign-memory/gemini-vault`.
- **Change:** apply canonical config table. No workspace-pointer repair needed (clean).
- **⚠️ Live verify:** confirm where Antigravity actually loads the extension from (the `~/.agents` anchor) and that it picks up the renamed `minni` server.
- **Verify:** recall + learn round-trip through `mcp__minni__*` returns from `~/.minni/gemini-vault`.

### 2. Claude Code — confidence: SUSPECT (audit)
- **Repo config:** `plugins/minni/.claude-plugin/plugin.json` + root `.claude-plugin/`. **Installed at:** `~/.agents/plugins/sovereign-memory@sovereign-memory` → becomes `minni@minni`.
- **Current (verified, repo):** `name: sovereign-memory`; `mcpServers.sovereign-memory` → `${CLAUDE_PLUGIN_ROOT}/dist/server.js`. Vault `~/.sovereign-memory/claudecode-vault`.
- **Change:** canonical config; reinstall plugin so the marketplace/source path points at `plugins/minni` and the install dir becomes `minni@minni`.
- **⚠️ Live verify:** the install is what this very session loads; cutover requires a reinstall + new session to pick up `mcp__minni__*`.
- **Verify:** new CC session shows `minni:recall` skill/command and `mcp__minni__*` tools.

### 3. Codex — confidence: SUSPECT (audit)
- **Repo config:** `plugins/minni/.codex-plugin/plugin.json` (+ `./.mcp.json`, `./hooks/hooks-codex.json`).
- **Current (verified):** `name: sovereign-memory`; `interface.displayName "Sovereign Memory"`; `defaultPrompt` strings say "Sovereign Memory"; author = operator's real name. Vault `~/.sovereign-memory/codex-vault`.
- **Change:** canonical config + rebrand the `interface` block (displayName, longDescription, defaultPrompt) to Minni + scrub author to Infektyd.
- **⚠️ Live verify:** how Codex registers the MCP server on its side (`.mcp.json` referenced by the manifest).
- **Verify:** Codex recall/learn round-trip on `~/.minni/codex-vault`.

### 4. Kilocode — confidence: SUSPECT (audit)
- **Repo config:** `plugins/minni/.kilocode-plugin/plugin.json` (+ `.mcp.json`, `hooks/hooks.json`, `commands/`).
- **Current (verified):** `name: sovereign-memory`; `mcpServers.sovereign-memory` → `${KILO_PLUGIN_ROOT}/../dist/server.js`. Vault `~/.sovereign-memory/kilocode-vault`.
- **Change:** canonical config; verify `${KILO_PLUGIN_ROOT}` still resolves after the dir rename.
- **⚠️ Live verify:** Kilocode's plugin install location + that it reads the renamed server.
- **Verify:** Kilocode recall/learn round-trip on `~/.minni/kilocode-vault`.

### 5. Grok-build — confidence: SUSPECT / WORST (audit + stale-path repair)
- **Repo config:** `plugins/grok-sovereign-memory/.mcp.json` → becomes `plugins/grok-minni/` (separate plugin from the main one).
- **Current (verified) — the disease:**
  - `.mcp.json` sets `SOVEREIGN_WORKSPACE_ID=~/Projects/sovereignMemory` (STALE), `SOVEREIGN_VAULT_PATH=~/.sovereign-memory/grok-build-vault`, `SOVEREIGN_SOCKET_PATH=~/.sovereign-memory/run/sovrd.sock`, runs via `~/.agents/bin/mcp-env-run sovereign-memory`.
  - `~/.sovereign-memory/identities/grok-build/GROK-BUILD_HOSTED_AGENT_ENVELOPE.md` also hardcodes `workspace: /Users/hansaxelsson/Projects/sovereignMemory`.
- **Change:** rewrite BOTH the `.mcp.json` workspace pointer AND the identity envelope `workspace:` to the anchor `~/Projects/minni`; canonical env rename; point `mcp-env-run` at `minni`.
- **⚠️ Live verify:** what "symlinked into claude code DB" actually is — confirmed so far as shared daemon socket + stale workspace pointers, **no real symlink found**. Re-confirm at cutover.
- **Verify:** grok-build recall/learn round-trip on `~/.minni/grok-build-vault`; `grep -r sovereignMemory` over grok config returns nothing.

---

## Cross-cutting cleanup (do once, not per platform)

- **Author-field scrub:** every manifest (`.claude-plugin`, `.codex-plugin`, `.kilocode-plugin`, and any author block) hardcodes the operator's real name in `author.name` → change to `Infektyd`. The git-guard blocks commits otherwise.
- **Brand strings + URLs:** "Sovereign Memory" → "Minni"; `github.com/infektyd/sovereign-memory` → `…/minni` (repo already redirects).
- **Keep `~/.sovereign-memory` working:** it becomes a symlink → `~/.minni` until P6 deprecation, so any missed reference still resolves.

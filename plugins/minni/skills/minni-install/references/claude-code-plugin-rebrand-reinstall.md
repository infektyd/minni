# Claude Code Plugin Rebrand / Reinstall Runbook

Concrete, verified procedure for moving an installed Claude Code plugin from one
marketplace identity to another — e.g. the `sovereign-memory@sovereign-memory` →
`minni@minni` rebrand. Use this when the plugin's `marketplace.json`/`plugin.json`
`name` has changed in the repo and the old install now fails to load.

> Use the native `claude plugin` CLI, **not** hand-edits to
> `~/.claude/plugins/installed_plugins.json` or `known_marketplaces.json`.
> The registry is fragile and the CLI keeps the two files consistent.

## Symptom that triggers this

`claude plugin list` shows the old plugin as:

```
sovereign-memory@sovereign-memory
  Status: ✘ failed to load
  Error: Plugin sovereign-memory not found in marketplace sovereign-memory
```

This happens because the repo's `.claude-plugin/marketplace.json` was renamed
(its `name` and the entry under `plugins[]` are now `minni`), so the old install
points at a plugin name that no longer exists in the (same) source directory.

## Why a swap, not an in-place rename

A marketplace name and a plugin name are part of the install identity
(`<plugin>@<marketplace>`). There is no "rename" verb — you uninstall the old
identity, repoint the marketplace, and install the new identity. The on-disk
cache dir (`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`) is keyed
by those names, so the new install lands in a fresh path automatically.

## Preconditions

1. **Repo configs already renamed** to the new identity. Verify all three agree
   on the new `name`:
   - `<repo>/.claude-plugin/marketplace.json` → top-level `name` + `plugins[].name` + `plugins[].source`
   - `<repo>/.claude-plugin/plugin.json` → `name`
   - `<repo>/plugins/<plugin>/.claude-plugin/plugin.json` → `name` + `mcpServers`
2. **Plugin is built.** The `mcpServers` entry runs
   `node ${CLAUDE_PLUGIN_ROOT}/dist/server.js`, so `dist/` must exist in the
   plugin source:
   ```bash
   ls <repo>/plugins/<plugin>/dist/server.js <repo>/plugins/<plugin>/dist/cli.js
   ```
   If missing, build first (`npm run build` in the plugin dir).
3. **Canonical repo path.** Old marketplaces may point at a stale path
   (e.g. `~/Projects/sovereignMemory`, now a symlink → `Minni`). Add the
   marketplace from the real canonical path to avoid symlink drift.

## Procedure (verified 2026-05-30)

All commands are non-interactive-safe. `claude` resolves to `~/.local/bin/claude`.

```bash
# 0. Inspect current state
claude plugin list
claude plugin marketplace list

# 1. Uninstall the old plugin identity.
#    --keep-data preserves ~/.claude/plugins/data/<id>/ in case anything lived there.
#    (Minni's real data is in ~/.minni, so this is just belt-and-suspenders.)
#    -y is required when stdin/stdout is not a TTY.
claude plugin uninstall sovereign-memory@sovereign-memory -y --keep-data

# 2. Remove the stale marketplace (also drops its known_marketplaces.json entry).
claude plugin marketplace remove sovereign-memory

# 3. Add the new marketplace from the canonical repo path.
#    Reads <repo>/.claude-plugin/marketplace.json → name "minni".
claude plugin marketplace add ~/Projects/Minni

# 4. Install the new identity. Form is <plugin>@<marketplace>.
claude plugin install minni@minni
```

## Verification

```bash
# Plugin enabled?
claude plugin list          # expect: minni@minni  Status: ✔ enabled

# Cache install is complete (dist + slash commands present)?
CACHE=$(ls -d ~/.claude/plugins/cache/minni/minni/*/ | head -1)
ls "$CACHE/dist/server.js" "$CACHE/dist/cli.js"
ls "$CACHE/commands/"       # recall.md, learn.md, status.md, audit.md, ...
cat "$CACHE/.claude-plugin/plugin.json"   # mcpServers → node dist/server.js

# Daemon live and the NEW install talks to it (full path through renamed plugin)?
launchctl list | grep com.minni.minnid
MINNI_SOCKET_PATH=~/.minni/run/minnid.sock \
MINNI_DB_PATH=~/.minni/minni.db \
  node "$CACHE/dist/cli.js" status          # expect socket.ok: true, vault resolves
```

A healthy baseline shows the daemon serving the real vault (hundreds of docs,
thousands of learnings) and `socket.ok: true`.

## The mandatory fresh-session step

The MCP tools and slash commands are loaded **at session start**. The session
that performs the swap keeps the *old* `mcp__sovereign-memory__*` deferred tools
for its lifetime. You cannot self-verify the new tools in the same session.

Tell the user to open a **fresh Claude Code session** and confirm:

- `mcp__minni__minni_recall` (and the other `mcp__minni__*` tools) appear
- `/minni:recall`, `/minni:learn`, `/minni:status` slash commands appear
- A quick `recall` → `learn` round-trip succeeds (proves daemon → DB → FAISS
  through the new MCP server)

## Leftover cruft

After the swap, the old cache dir is orphaned (no registry entry references it):

```
~/.claude/plugins/cache/<old-marketplace>/<old-plugin>/<version>/
```

Safe to delete once the fresh session confirms the new install works. Leave it
until then so you have a rollback copy.

## Canonical names (post-rebrand, observed 2026-05-30)

| Thing | Value |
| --- | --- |
| Daemon launchd label | `com.minni.minnid` |
| LaunchAgent plist | `~/Library/LaunchAgents/com.minni.minnid.plist` |
| Daemon process | `python -m minni.minnid` |
| Socket | `~/.minni/run/minnid.sock` |
| DB | `~/.minni/minni.db` |
| Vault root | `~/.minni/codex-vault` (per-agent: `~/.minni/<agent>-vault`) |
| Env vars | `MINNI_SOCKET_PATH`, `MINNI_DB_PATH` |
| Marketplace / plugin | `minni` / `minni@minni` |

> Legacy names still referenced elsewhere in this skill and in older installs:
> `sovrd.sock`, `SOVEREIGN_SOCKET_PATH`, `SOVEREIGN_DB_PATH`,
> `sovereign_memory.db`, `com.*.sovrd`. Prefer the `MINNI_*` / `minnid` forms
> above; fall back to the legacy forms only when an unmigrated surface needs them.

## Common mistakes

- Hand-editing `installed_plugins.json` / `known_marketplaces.json` instead of
  using the CLI → the two drift out of sync and the plugin silently fails to load.
- Forgetting `-y` on `uninstall` in a non-TTY context → it blocks on the prune
  prompt.
- Adding the marketplace from a symlinked path (`~/Projects/sovereignMemory`)
  instead of the canonical target (`~/Projects/Minni`).
- Installing before `dist/` is built → plugin enables but the MCP server can't
  spawn `dist/server.js`.
- Claiming success from the same session that did the swap → it still has the old
  tool namespace. Always require a fresh session for final verification.

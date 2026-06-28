# Minni install directive — Antigravity (CLI `agy` + IDE) · gemini-cli

Goal: Minni at Claude-Code parity across Antigravity surfaces (MCP `minni@minni` 26 tools · Layer-1 via GEMINI.md · memory-first · per-agent `gemini` vault). Audited 2026-05-30. All three surfaces are installed and share the `~/.gemini/` tree (there is no `~/.antigravity`).

## Current state (broken)
All MCP config views point at the **legacy `sovereign-memory`** server → dead `~/.sovereign-memory/run/sovrd.sock`; the IDE view has an outright broken `cwd: ~/sovereignMemory/...` (nonexistent). Only ~7 old `sovereign_*` tools are auto-granted, not the 26 `minni_*`. The repo `minni` plugin is NOT installed on any Gemini surface (no `~/.gemini/extensions/minni/`). `GEMINI.md` (`~/.agents/GEMINI.md`) is old-brand. Vault `~/.minni/gemini-vault` exists; envelope stale (`sovrd.sock`). Daemon up on `~/.minni/run/minnid.sock`.

## Mechanism (official)
- MCP (all 3 Antigravity surfaces): single shared `~/.gemini/config/mcp_config.json` (symlinked to `~/.agents/mcp-servers/views/.gemini__config__mcp_config.json.json`); `antigravity-cli/mcp/` + `antigravity-ide/mcp/` are *generated* from it. Stdio entry = `command`+`args`+`env`; IDE entries carry `"$typeName":"exa.cascade_plugins_pb.CascadePluginCommandTemplate"` (preserve on hand-edit). Remote key is `serverUrl` (n/a for Minni). gemini-cli (standalone) uses a DIFFERENT file: `~/.gemini/settings.json` → `mcpServers` (with `"trust": true`).
- Context/memory: `GEMINI.md` (hierarchical) is the Layer-1 substitute — **static text, no per-prompt hook** equivalent to Claude Code on these surfaces.
- Memory-first: GEMINI.md directive + auto-grant tools (`~/.gemini/config/config.json globalPermissionGrants.allow` and `~/.gemini/antigravity-cli/settings.json permissions.allow`).
- Auth: OAuth is per-surface (IDE and `agy` auth separately; token at `~/.gemini/antigravity-cli/credentials.enc`). Minni itself needs no OAuth (local stdio).

## AUTO (scriptable)
1. **Add an `antigravity` target to propagate.py** (the named gap): writer injects `mcpServers.minni` (`command:node`, `args:[<install>/dist/server.js]`, `env:{MINNI_AGENT_ID:gemini, MINNI_VAULT_PATH:~/.minni/gemini-vault, MINNI_SOCKET_PATH:~/.minni/run/minnid.sock}`, `disabled:false`) into `~/.gemini/config/mcp_config.json` (resolve the symlink/view), preserving the IDE `$typeName` wrapper; and removes the legacy `sovereign-memory` entry from every view (incl. the broken IDE `cwd`).
2. Install plugin: copy `plugins/minni/.gemini-plugin/` + built `dist/` → `~/.gemini/extensions/minni/`.
3. Auto-grant **read-only** tools explicitly (no `mcp(minni/minni_*)` wildcard). Example allowlist for `~/.gemini/config/config.json globalPermissionGrants.allow` and `~/.gemini/antigravity-cli/settings.json permissions.allow`:
   - `mcp(minni/minni_status)`
   - `mcp(minni/minni_recall)`
   - `mcp(minni/minni_drill)`
   - `mcp(minni/minni_route)`
   - `mcp(minni/minni_prepare_task)`
   - `mcp(minni/minni_plan_status)`
   - `mcp(minni/minni_plan_history)`
   - `mcp(minni/minni_ping_agent_inbox)`
   - `mcp(minni/minni_ping_agent_status)`
   Writes (`minni_learn`, `minni_vault_write`, `minni_resolve_candidate`, `minni_plan_update`, handoff/ping decide, etc.) must **not** appear in auto-grant — they require per-session prompt approval.
   Drop the old `sovereign-memory/sovereign_*` grants.
4. (gemini-cli, optional) add the `minni` block to `~/.gemini/settings.json mcpServers` with `"trust": true`.
5. Rebrand `~/.agents/GEMINI.md` → Minni, vault `~/.minni/gemini-vault`, server `minni`, tools `minni_*`; add "consult `minni_status` + `minni_recall` first" directive.
6. Refresh envelope `~/.minni/identities/gemini/GEMINI_HOSTED_AGENT_ENVELOPE.md` → socket `minnid.sock`, Minni brand (propagate.py `render_hosted_envelope`/`seed-hosted` already generates the correct one).

## MANUAL
- OAuth per surface: run `agy` (CLI browser consent); sign in to the IDE separately.
- Antigravity IDE: Settings → Customizations → Open MCP Config → confirm `minni`, reload MCP servers.
- Restart CLI + IDE so `antigravity-cli/mcp/minni` and `antigravity-ide/mcp/minni` regenerate.

## Verify
`ls ~/.gemini/antigravity-{cli,ide}/mcp/minni` (generated dirs appear); fresh `agy` session + IDE agent panel show `minni` connected with 26 `minni_*` tools; `minni_status` reports the daemon on `minnid.sock`.

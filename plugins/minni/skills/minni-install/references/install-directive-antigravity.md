# Minni install directive — Antigravity (CLI `agy` + IDE) · gemini-cli

Goal: Minni at Claude-Code parity across Antigravity surfaces (MCP `minni`
tools · Layer-1 via GEMINI.md / hooks · memory-first · per-agent `gemini`
vault at `~/.minni/gemini-vault`). All Antigravity surfaces share the
`~/.gemini/` tree (there is no `~/.antigravity`).

## Current state (post-wire / post-`minni sync`)

**Day-to-day path is Antigravity**, not pure `gemini`. Prefer:

```bash
minni wire antigravity
# dogfood checkout:
.venv/bin/minni wire antigravity --from-repo .
# or after wire adoption (D7 fleet):
# propagate.py update-plugin --platform antigravity
# (also covered by minni sync / make sync-root)
```

On a healthy dogfood machine you should see:

| Surface | Expected |
|---------|----------|
| MCP | `mcpServers.minni` in `~/.gemini/config/mcp_config.json` → stdio to `…/dist/server.js` (often via a small env-run wrapper), `MINNI_SOCKET_PATH=~/.minni/run/minnid.sock`, vault `~/.minni/gemini-vault` |
| Legacy | **No** default `sovereign-memory` / `sovrd.sock` server (propagate removes it when wiring antigravity) |
| agy hooks | `~/.gemini/config/plugins/minni/` (`hooks.json` + `plugin.json`); entry `dist/gemini-hook.js` / installed plugin under `~/.agents/plugins/minni@minni` |
| Daemon | `minnid` on `~/.minni/run/minnid.sock` |
| Pure gemini | **Provisional** — `minni wire gemini` is skipped (`gemini-provisional`); see [docs/runtimes/gemini.md](../../../../../docs/runtimes/gemini.md) |

If MCP still points at `sovereign-memory` or `sovrd.sock`, treat that as **drift**
and re-run antigravity wire/propagate — not as the default install story.

**Historical (2026-05-30 audit):** early surfaces still had legacy
`sovereign-memory` stdio + broken IDE `cwd` and only a handful of
`sovereign_*` grants. That snapshot is **not** present-tense operator truth
after wire-era propagate + `minni sync`.

## Mechanism (official)

- **MCP (Antigravity surfaces):** shared `~/.gemini/config/mcp_config.json`
  (often symlinked into the agents view tree); `antigravity-cli/mcp/` +
  `antigravity-ide/mcp/` are *generated* from it. Stdio entry =
  `command`+`args`+`env`; IDE entries may carry
  `"$typeName":"exa.cascade_plugins_pb.CascadePluginCommandTemplate"`
  (preserve on hand-edit). Standalone gemini-cli uses a different file:
  `~/.gemini/settings.json` → `mcpServers` (often with `"trust": true`).
- **Context/memory:** `GEMINI.md` is the Layer-1 substitute on pure
  gemini-cli. **agy** loads Minni hooks from
  `~/.gemini/config/plugins/minni/` (`propagate.py update-plugin --platform
  antigravity`). Current agy (**1.1.7+**) dispatches SessionStart /
  PreInvocation (UPS analogue) / PreToolUse / Stop; PreToolUse is
  deny-capable via `gemini-adapter.ts` (`allow`/`deny`/…, not `approve`).
  Historical agy **1.0.15** only dispatched PreToolUse/PostToolUse/Stop —
  do not treat that as current. See `docs/runtimes/gemini.md` and
  `docs/contracts/hook-platforms.md`.
- **Memory-first:** GEMINI.md directive + wire’s **read-only** auto-grants
  (`MINNI_READONLY_TOOLS` in `propagate.py` → `globalPermissionGrants.allow` /
  CLI `permissions.allow`). Wire adds only those RO tools and strips the
  `mcp(minni/*)` wildcard + legacy `sovereign_*` grants; it does **not** purge
  hand-added write grants operators may have left in config.
- **Auth:** OAuth is per-surface (IDE and `agy` separately). Minni itself is
  local stdio — no OAuth.

## AUTO (scriptable)

1. **Wire / propagate antigravity** (preferred):
   `minni wire antigravity` or
   `propagate.py update-plugin --platform antigravity`.
   Injects `mcpServers.minni`, env
   (`MINNI_AGENT_ID=gemini`, vault, `MINNI_SOCKET_PATH`), drops legacy
   `sovereign-memory`, refreshes grants and agy hooks.
2. **Fleet refresh:** `minni sync` (or `make sync-root` on editable dogfood)
   redeploys plugin payload and re-runs D7 propagate for antigravity + cursor.
3. **Plugin tree:** wire/propagate installs built `dist/` into the active
   install root (often `~/.agents/plugins/minni@minni` and/or extension paths).
4. **Read-only auto-grants** — wire defaults are exactly
   `MINNI_READONLY_TOOLS` in `propagate.py`:
   `minni_recall`, `minni_drill`, `minni_status`, `minni_audit_tail`,
   `minni_audit_report`, `minni_route`, `minni_list_pending_handoffs`,
   `minni_ping_agent_inbox`, `minni_ping_agent_status`.
   Do **not** auto-grant write/learn/thread-update tools in the wire path.
   Drop any remaining `sovereign_*` grants.
5. **(Optional pure gemini-cli)** add `minni` to `~/.gemini/settings.json`
   `mcpServers` with `"trust": true` — provisional; prefer antigravity for
   day-to-day.
6. **Layer-1 / envelope:** rebrand `~/.agents/GEMINI.md` and hosted envelope
   to Minni + `minnid.sock` if still on old brand (propagate seed paths
   generate the correct envelope).

## MANUAL

- OAuth per surface: `agy` browser consent; IDE sign-in separately.
- Antigravity IDE: Settings → Customizations → Open MCP Config → confirm
  `minni`, reload MCP servers.
- Restart CLI + IDE so generated `antigravity-*/mcp/minni` views refresh.

## Verify

```bash
minni doctor                          # socket + daemon
# MCP points at minni + minnid.sock (not sovrd):
rg -n 'minni|sovereign|sovrd' ~/.gemini/config/mcp_config.json
ls ~/.gemini/config/plugins/minni/    # hooks.json + plugin.json
# Fresh agy / IDE session: minni tools connected; minni_status → minnid.sock
```

See also: [docs/runtimes/gemini.md](../../../../../docs/runtimes/gemini.md),
fleet: `minni sync` / [docs/install.md](../../../../../docs/install.md).

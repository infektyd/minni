# Cursor

Cursor is a **fleet-known, propagate-managed** platform: `cursor` is in wire's
`VALID_PLATFORMS`, but `minni wire cursor` and `minni wire all` expand to a
skip (`ALL_SKIPS["cursor"]`) rather than installing under `~/.minni/plugin/`.
Install and refresh go through the `minni-install` skill's `propagate.py`,
which owns the Cursor-specific hook path. `make sync-root` (see
[deploy/README.md](../../deploy/README.md)) also runs
`propagate update-plugin --platform cursor` as part of the D7 fleet partition.

```bash
git clone https://github.com/infektyd/minni.git && cd minni
make setup          # venv + deps + plugin build
.venv/bin/python plugins/minni/skills/minni-install/scripts/propagate.py \
  update-plugin --platform cursor
# or, for the full live fleet (wire-primary hosts + antigravity/cursor):
# make sync-root
```

This builds/copies the plugin to `~/.cursor/plugins/local/minni`, registers
the MCP server via `.cursor-plugin/plugin.json` (`~/.cursor/plugins/local/minni/.mcp.json`),
and — the part that matters — deploys a **User hook wrapper**
(`~/.cursor/hooks/minni-cursor.sh`) that is merged into `~/.cursor/hooks.json`.

## Why a wrapper instead of the plugin manifest

Cursor's plugin-manifest hooks (`.cursor-plugin/plugin.json` → `hooks`) are
**intentionally not registered**. Live testing found User hooks are the sole
reliable fire path for Minni on Cursor, so `propagate.py` writes
`~/.cursor/hooks/minni-cursor.sh` — a small script that stamps
`MINNI_CURSOR_AGENT_ID`, `MINNI_CURSOR_VAULT_PATH`, and
`MINNI_CURSOR_WORKSPACE_ID` and execs
`~/.cursor/plugins/local/minni/dist/cursor-hook.js`. `~/.cursor/hooks.json` and
`<project>/.cursor/hooks.json` are shared by **both** the Cursor editor and the
`cursor-agent` CLI — one install wires both surfaces. Every run redeploys the
wrapper and strips any prior Minni User-hook entries (legacy absolute paths
included) before writing fresh ones, so re-running the installer is safe.

## Hook coverage

Per [docs/contracts/hook-platforms.md](../contracts/hook-platforms.md), Cursor
wires five events (`plugins/minni/hooks/hooks-cursor.json`):
`sessionStart`, `beforeSubmitPrompt`, `preCompact`, `stop`, and `preToolUse`
(matcher `Read|Grep|Glob|Shell`). Coverage is broad, but **context injection is
the weak point on this platform**:

- `sessionStart`'s `additional_context` is a **confirmed vendor bug** — Cursor
  staff acknowledged it as a timing issue between hook execution and
  composer-handle creation, open as of CLI 3.8.23 with no workaround. Memory
  reaches the model on Cursor via **MCP tool calls, not hooks**.
- `beforeSubmitPrompt` cannot inject at all (documented as `continue` +
  `user_message` only).
- `stop` accepts exactly one field, `followup_message`, and it is an
  **auto-follow-up action** (bounded by `loop_limit`, default 5), not a
  human-facing note channel — using it to announce anything is self-feeding
  and was observed live driving 6 Stop hooks in 90 seconds from one prompt.
  Cursor therefore has no Stop-side note channel.
- `preCompact` can only return `user_message`.
- `preToolUse` **does** support deny (`allow`/`deny`/`ask`, though `ask` is
  unenforced there), so the recall guard is genuinely wireable on Cursor —
  see [docs/contracts/AGENT.md §8](../contracts/AGENT.md).

Practical upshot: on Cursor, plan on the agent reaching memory through MCP
tool calls (`minni_recall`, `minni_status`, `minni_list_candidates`,
`minni_resolve_candidate`, …) rather than boot-time hook injection. `minni_list_candidates` pages with a single `LIMIT n+1` read: when `has_more` is true, `total` is the lower bound (`len(page)+1`), not an exact count. Cursor's
principal template does not include the cross-principal `resolve_candidate`
grant; it can list and reject/redact its own proposed rows, but accepting
into durable memory still needs operator/govern.

## Vault path

`~/.minni/cursor-vault` (`wiki/inbox/outbox/logs`), principal id `cursor` —
`~/.minni/principals/cursor.json`. One `cursor` identity spans both the
Cursor.app editor and the `cursor-agent` CLI, since they share
`~/.cursor/hooks.json`.

## Verify

From a Cursor session (editor or CLI), call `minni_status` (or ask the agent
to recall something) and check `socket.ok` and the vault path. Since boot
injection is unreliable here, the practical test is that the MCP tools work
and Cursor can retrieve recalled evidence when it explicitly calls
`minni_recall`.

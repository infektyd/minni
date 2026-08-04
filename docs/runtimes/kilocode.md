# Kilo Code

**Prefer `minni wire kilocode`.** Kilo is a wire-primary platform
(`ALL_EXPANSION_V03` — included in `minni wire all` and in `make sync-root`'s
wire pass).

```bash
.venv/bin/minni up   # if the daemon isn't already running
minni wire kilocode                                   # v0.3+ wheel (bundled payload)
.venv/bin/minni wire kilocode --from-repo .           # source checkout
```

`propagate.py update-plugin --platform kilocode` is a **pre-wire recovery**
path only. After wire adoption, re-running propagate for kilocode rewrites MCP
onto legacy trees and can undo wire-managed roots. Propagate's
`--platform all` does **not** include kilocode (only antigravity + cursor).
Refresh with `minni wire kilocode --from-repo .` or `make sync-root` — see
[deploy/README.md](../../deploy/README.md).

This registers the MCP server (`plugins/minni/.kilocode-plugin/`,
`~/.config/kilo/kilo.json`), pins the agent identity
(`MINNI_KILOCODE_AGENT_ID=kilocode`), the per-agent vault
(`~/.minni/kilocode-vault`), and installs the Kilo hook entrypoint
(`dist/kilocode-hook.js`) from the wire-managed plugin tree.

## In-process bridge, not a manifest

Kilo Code has **no hook manifest** — hooks are an in-process JS plugin. Minni
ships `plugins/minni/kilo/minni-plugin.js`, which Kilo auto-scans from
`~/.config/kilo/plugin/*.js`. The plugin spawns the compiled
`dist/kilocode-hook.js` (Node) as a child process per event — Kilo runs
plugins under Bun, so the hook itself is a separate Node entry point, not code
running in-process. It bridges four native Kilo events, in
`plugins/minni/kilo/minni-plugin.js`:

| Kilo event | Minni intent |
|---|---|
| `chat.message` | prompt submit |
| `tool.execute.before` | pre-tool (deny-capable — throws to block) |
| `event` → `session.idle` | turn end (Stop) |
| `experimental.session.compacting` | pre-compact |

The MCP env key for the server entry is `environment`, **not** `env` — using
`env` bricks the whole CLI.

## Hook coverage

Per [docs/contracts/hook-platforms.md](../contracts/hook-platforms.md), Kilo
Code is the **only platform that can inject context at pre-compact**
(`experimental.session.compacting` → the bridge's `system.transform`).
Session start, prompt submit, and pre-tool all inject via the same bridge
mechanism. Turn end (`session.idle`) has no injection channel — it carries no
message text at all, so the plugin stashes the last prompt seen at
`chat.message` and threads it through to the Stop-equivalent handler for that
session (`lastPrompt`, keyed and bounded per session so two interleaved
sessions cannot cross-contaminate each other's candidate text).

Pre-tool deny works by throwing from `tool.execute.before`, or returning
`permission.ask` → `ask|deny|allow` — already wired through the bridge, so
the recall guard is available here (see
[docs/contracts/AGENT.md §8](../contracts/AGENT.md)).

Kilo has **no hook timeout at all** — a hang hangs the CLI — so the bridge's
child-process spawn is expected to return promptly; there is no platform-side
backstop.

The `experimental.*` injection hooks are documented by Kilo as subject to
change, and Kilo has already shipped one breaking config-path change
(`.opencode` → `.kilo`). Re-verify after any Kilo upgrade.

## Compaction-summary harvest

Kilo Code is one of the two delivery paths for the compaction-summary harvest
(see [docs/concepts.md — Compaction-summary harvest](../concepts.md#compaction-summary-harvest)): it
reads the summary back via the SDK at `session.compacted` rather than tailing
a transcript file, then hands it to the same shared harvest trunk Claude Code
uses.

## Vault path

`~/.minni/kilocode-vault` (`wiki/inbox/outbox/logs`), principal id `kilocode`
— `~/.minni/principals/kilocode.json`.

## Verify

From a Kilo Code session, call `minni_status` and check `socket.ok` and the
vault path. Re-run `minni wire kilocode` for MCP handshake, hook dry-run, and
config readback without a live Kilo session. `minni doctor` only checks the
local daemon subset (interpreter, socket, status, recall, models) — it does
not run wire verify probes.

# Grok

**Prefer `minni wire grok`.** Grok is a wire-primary platform (`ALL_EXPANSION_V03`
— included in `minni wire all` and in `make sync-root`'s wire pass).

```bash
.venv/bin/minni up   # if the daemon isn't already running
minni wire grok                                   # v0.3+ wheel (bundled payload)
.venv/bin/minni wire grok --from-repo .           # v0.2 wheel / source checkout
```

`propagate.py update-plugin --platform grok` is a **pre-wire recovery** path
only. After wire adoption, re-running propagate for grok rewrites MCP onto
legacy `~/.agents/plugins/…` trees and can undo wire-managed roots.
Propagate's `--platform all` does **not** include grok (only antigravity +
cursor). `make sync-root` wires grok via `minni wire` and refreshes
hooks/rules against the active wire install root — see
[deploy/README.md](../../deploy/README.md).

Grok is registered via `~/.grok/config.toml` against the wire-managed plugin
tree under `~/.minni/plugin/<version>/`, with a Grok-specific hook entrypoint
(`plugins/minni/src/grok-hook.ts` / `plugins/minni/hooks/hooks-grok.json`).

Like every wired runtime, Grok shares the daemon's memory pool under its own
agent identity — recall is shared (scope-governed), durable writes go through
the propose→approve gate, and cross-agent work moves via handoffs.

## PreToolUse product (s6 cold-tool guard)

Customer problem: docs said Grok could deny cold-file tools, but the hook used
bare Claude-shaped I/O. Grok speaks camelCase (`toolName` / `toolInput`), native
tool names (`read_file`, `list_dir`, `grep`, `run_terminal_command`), and
`{decision, reason?}` stdout — so the shared guard never saw tools in scope.

**Product surface** (same class as `minni sync` for fleet freshness):

| Piece | Role |
|-------|------|
| `grok-adapter.ts` | Map Grok envelope ↔ shared handlers |
| `grok-hook.ts` | Compose adapters around `createHookHandlers` (not bare `runHookMain`) |
| Contract matrix | [platform-hook-contracts.md](../ops/platform-hook-contracts.md) + [hook-platforms.md](../contracts/hook-platforms.md) |

After package/main moves, redeploy so Grok reloads the adapter:

```bash
minni sync              # or minni sync --full on an editable dogfood checkout
# then restart Grok Build so hooks reload dist/grok-hook.js
```

Verify: from a Grok session, call `minni_status` and check `socket.ok` and the
vault path. For PreToolUse, a pending strong-recall state must deny a native
`read_file` (see `plugins/minni/tests/grok-hook.test.mjs`).

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

Verify: from a Grok session, call `minni_status` and check `socket.ok` and the
vault path.

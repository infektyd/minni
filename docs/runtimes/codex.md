# Codex

**Prefer `minni wire codex`.** Codex is a wire-primary platform (`ALL_EXPANSION_V03`
— included in `minni wire all` and in `make sync-root`'s wire pass).

```bash
.venv/bin/minni up   # if the daemon isn't already running
minni wire codex                                   # v0.3+ wheel (bundled payload)
.venv/bin/minni wire codex --from-repo .           # v0.2 wheel / source checkout
```

`propagate.py update-plugin --platform codex` is a **pre-wire recovery** path
only. After wire adoption, re-running propagate for codex rewrites MCP onto
legacy `~/.codex/plugins/cache/…` trees and can undo wire-managed roots.
Propagate's `--platform all` does **not** include codex (only antigravity +
cursor). To refresh a live checkout fleet, use `make sync-root` or
`minni wire codex --from-repo .` — see [deploy/README.md](../../deploy/README.md).

This installs the Codex adapter (`plugins/minni/.codex-plugin/` — plugin
manifest, hooks, and MCP config) with agent identity `codex` and vault
`~/.minni/codex-vault`. The plugin's MCP server is a Node process
(`dist/server.js`) that talks to the daemon over the Unix socket; under wire
the install root is versioned under `~/.minni/plugin/<version>/`.

Codex shares the memory pool with every other wired runtime: its notes are
tagged with its `agent_origin`, and cross-agent work moves through explicit
handoffs rather than shared scratch state.

Reference: `plugins/minni/skills/minni-install/references/install-directive-codex.md`.

Verify: from a Codex session, call `minni_status` and check `socket.ok`, the
`codex-vault` path, and the audit tail.

## Workspace labels across projects

New Codex wiring leaves the workspace environment overrides unset. Each fresh
MCP process derives its default label from the enclosing Git repository of its
startup working directory. A launch from a cache, home directory, or another
non-repository directory reports `workspace-unknown`. A running MCP process
does not automatically follow later project switches; reconnect it when its
startup context changes.

Ordinary rewiring preserves an existing workspace pin. To remove a pin left
by an earlier installation and return to startup-directory discovery, run:

```bash
minni wire codex --dynamic-workspace
```

This removes `MINNI_WORKSPACE_ID` and `MINNI_CODEX_WORKSPACE_ID` from Codex's
wired environment while retaining its agent, vault, and socket bindings.
The option applies to `wire codex` only. For a deliberately fixed label, use
`minni wire codex --workspace /path/to/project` instead; the two options are
mutually exclusive. Reconnect existing Codex MCP sessions after changing
their launch environment.

Workspace labels describe context; they are not project authorization
boundaries. Daemon recall continues to use the registered principal's read
policy and can return authorized context across projects. A per-tool workspace
argument does not add a daemon search filter. Codex hooks obtain project context
from their supported event payload; without a usable payload or explicit
Codex workspace override, their label remains unknown rather than inheriting
the MCP process's directory.

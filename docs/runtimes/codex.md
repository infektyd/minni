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

This copies the Codex adapter payload (including its plugin manifest and hooks)
and configures the MCP server with agent identity `codex` and vault
`~/.minni/codex-vault`. The plugin's MCP server is a Node process
(`dist/server.js`) that talks to the daemon over the Unix socket; under wire
the install root is versioned under `~/.minni/plugin/<version>/`.

Codex shares the memory pool with every other wired runtime: its notes are
tagged with its `agent_origin`, and cross-agent work moves through explicit
handoffs rather than shared scratch state.

Reference: `plugins/minni/skills/minni-install/references/install-directive-codex.md`.

Verify: from a Codex session, call `minni_status` and check `socket.ok`, the
`codex-vault` path, and the audit tail.

## Automatic hooks and running sessions

`minni wire codex` configures MCP. It does not install or enable the payload as
an active Codex plugin, approve hook definitions, or restart existing host
sessions. Its `verify.hook_dry_run` checks the packaged hook entrypoint only;
it does not show that Codex has loaded or fired a hook. Successful wire output
therefore reports `lifecycle.automatic_hooks: "not_verified"` and
`lifecycle.existing_host_sessions: "not_verified"`. Existing independently
installed hooks may still be active; this result does not declare them absent.

Codex supports hooks beside active config layers (`hooks.json` or inline
`[hooks]`), and through enabled plugins. Minni's packaged manifest points to
`hooks/hooks-codex.json`; Codex supplies `PLUGIN_ROOT` for its commands.
Non-managed hooks must also be reviewed and trusted in Codex before execution.
Check the host's hook inventory and trust state (CLI `/hooks`) to establish
activation. See the [official Codex hook documentation](https://learn.chatgpt.com/docs/hooks).

After updating the payload, reconnect the relevant MCP session through its
host when convenient. Existing processes can keep the old code loaded even
when the configured path points to the new version. A current manifest or a
successful fresh probe does not establish the version of every running session.

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

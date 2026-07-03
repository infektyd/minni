# Codex

Wire Codex to a running Minni daemon from your checkout:

```bash
.venv/bin/minni up   # if the daemon isn't already running
.venv/bin/python plugins/minni/skills/minni-install/scripts/propagate.py update-plugin --platform codex
```

This installs the Codex adapter (`plugins/minni/.codex-plugin/` — plugin
manifest, hooks, and MCP config) with agent identity `codex` and vault
`~/.minni/codex-vault`. The plugin's MCP server is a Node process
(`dist/server.js`) that talks to the daemon over the Unix socket; the plugin
cache location Codex uses is `~/.codex/plugins/cache/minni/…`.

Codex shares the memory pool with every other wired runtime: its notes are
tagged with its `agent_origin`, and cross-agent work moves through explicit
handoffs rather than shared scratch state.

Reference: `plugins/minni/skills/minni-install/references/install-directive-codex.md`.

Verify: from a Codex session, call `minni_status` and check `socket.ok`, the
`codex-vault` path, and the audit tail.

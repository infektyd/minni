# Claude Code

Wire Claude Code to a running Minni daemon from your checkout:

```bash
.venv/bin/minni up   # if the daemon isn't already running
.venv/bin/python plugins/minni/skills/minni-install/scripts/propagate.py update-plugin --platform claude-code
```

This registers the MCP server (`plugins/minni/.claude-plugin/`), pins the
agent identity (`MINNI_AGENT_ID=claude-code`), the per-agent vault
(`~/.minni/claudecode-vault`), and the socket path, and installs the Claude
Code hook entrypoints. The agent-driven `minni-install` skill handles
first-time identity and vault seeding.

Claude Code is the most deeply integrated runtime:

- Session hooks inject the `<minni:context>` envelope with identity, active
  plan state, correction re-assertions, and the lifecycle spine
  (`prepare_task → prepare_outcome → plan → learn`).
- A deny-capable `PreToolUse` **recall guard** nudges recall before tool use —
  Claude Code is currently the only host that exposes a pre-tool hook that can
  deny. Knobs: `MINNI_RECALL_GUARD_MODE` (`off`/`soft`/`strict`),
  `MINNI_LIFECYCLE_NUDGE_MODE` (`off` disables). The guard fails open.

Verify: in a Claude Code session, call `minni_status` (or `/minni:status`) and
check `socket.ok` and the vault path.

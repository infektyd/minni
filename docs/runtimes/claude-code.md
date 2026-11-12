# Claude Code

Wire Claude Code to a running Minni daemon from your checkout:

```bash
.venv/bin/minni up   # if the daemon isn't already running
minni wire claude-code                                   # v0.3+ wheel (bundled payload)
.venv/bin/minni wire claude-code --from-repo .           # v0.2 wheel / source checkout
```

On a machine that still has the retired marketplace/cache install, run the
one-time cutover once after wiring — dry-run first, it writes nothing without
`--apply`:

```bash
minni wire-adopt claude-code            # shows what it would change
minni wire-adopt claude-code --apply
```

(`propagate.py update-plugin --platform claude-code` is gone: it now exits with
a pointer to `minni wire`.)

Hooks, skills and commands are served from the wire-managed tree
(`~/.minni/plugin/<version>`), which wire records in Claude Code's plugin
registry at `~/.claude/plugins/installed_plugins.json`. See
`docs/design/DESIGN-wire-claude-plugin-adoption.md`.

This registers the MCP server (`plugins/minni/.claude-plugin/`), pins the
agent identity (`MINNI_AGENT_ID=claude-code`), the per-agent vault
(`~/.minni/claudecode-vault`), and the socket path, and installs the Claude
Code hook entrypoints. The agent-driven `minni-install` skill handles
first-time identity and vault seeding.

Claude Code is the most deeply integrated runtime:

- `SessionStart` and `SubagentStart` hooks inject the `<minni:context>` envelope
  with the **Two-Part Permanent Boot Shelf** — `layer1/core.md` (up to `24 KB`
  un-truncated) plus `project_shelf` (`<= 2,500` chars of Living Repo Map
  subsystem cards, git-diff staleness notices, top scars, and active plan
  summary) — alongside correction re-assertions, `PostCompact` one-shot shelf
  recovery, and the v1 lifecycle spine (`recall → drill → thread → learn`).
- **Strictly non-blocking hooks (`PreToolUse`, `UserPromptSubmit`, `PostToolUse`)**:
  `PreToolUse` never emits `permissionDecision: "deny"` or blocks `Read`, `Grep`,
  `Glob`, `Bash`, or edit calls. Because Claude Code supports
  `preToolUseContext: true`, unconsulted strong recall hits and matched failure
  lessons are surfaced once per arming via `hookSpecificOutput.additionalContext`
  (`continue: true`). `UserPromptSubmit` skips `<task-notification>` / background
  wakeup payloads, and `PostToolUse` scopes failure-lesson checks strictly to
  file edits and failed commands. Full per-host matrix:
  [docs/contracts/hook-platforms.md](../contracts/hook-platforms.md). Knobs:
  `MINNI_RECALL_GUARD_MODE` (`off`/`soft`/`strict`),
  `MINNI_LIFECYCLE_NUDGE_MODE` (`off` disables). The guard fails open.

Verify: in a Claude Code session, call `minni_status` (or `/minni:status`) and
check `socket.ok` and the vault path.

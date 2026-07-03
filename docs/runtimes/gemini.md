# Gemini / Antigravity

Wire Gemini to a running Minni daemon from your checkout:

```bash
.venv/bin/minni up   # if the daemon isn't already running
.venv/bin/python plugins/minni/skills/minni-install/scripts/propagate.py update-plugin --platform gemini
```

Antigravity rides the same `~/.gemini` surface (shared agent identity
`gemini`, vault `~/.minni/gemini-vault`) but is wired **individually**:

```bash
.venv/bin/python plugins/minni/skills/minni-install/scripts/propagate.py update-plugin --platform antigravity
```

Note: `--platform all` covers codex, claude-code, kilocode, gemini, and grok —
**it does not include antigravity**; run the antigravity target explicitly.

The adapter (`plugins/minni/.gemini-plugin/gemini-extension.json`) launches
the MCP server via the extension path; Antigravity surfaces get their MCP
configs under `~/.gemini/antigravity*/`. These surfaces receive a read-only
tool allowlist by default (`minni_recall`, `minni_drill`, `minni_status`,
audit tools, …) — write and export tools are deliberately excluded there.

## Hooks (agy CLI)

Both platform targets above also register a hook plugin with the **agy**
(Antigravity CLI) plugin system when the `agy` binary is on PATH (skipped
with a reason otherwise — re-run after installing agy). The entrypoint is
`dist/gemini-hook.js`, driven by `hooks-gemini.json` and the agy payload
adapter (`src/gemini-adapter.ts`).

What actually fires on agy 1.0.15 (verified live):

- **`Stop`** — drafts candidate learnings into `~/.minni/gemini-vault/inbox/`
  (the same governed propose→approve loop as every other platform). The agy
  payload carries no task text, so the hook reads the last explicit user
  message out of agy's own transcript (`transcript_full.jsonl`) so drafts are
  about the actual task; when the transcript is unreadable it degrades to the
  shared fallback chain.
- **`PreToolUse`** — carries the s6 recall guard through agy's deny-capable
  decision protocol. On this surface the guard defaults to **strict** mode
  (override with `MINNI_RECALL_GUARD_MODE`): agy funnels every shell/search
  through `run_command`, which maps to Bash — and the shared default "soft"
  mode deliberately ignores Bash, so it would guard nothing here. Inert for
  now regardless: agy has no `UserPromptSubmit` event, so no recall-state
  exists for the guard to act on. Every invocation answers with an explicit
  `{"decision": "approve"}` — agy 1.0.15's permission manager errors on empty
  decisions.
- `SessionStart` / `UserPromptSubmit` / `PreCompact` are **pre-declared** in
  the manifest but agy 1.0.15 does not dispatch them — no boot injection and
  no per-prompt recall pointer on this surface yet. They activate without a
  reinstall once agy adds the events.

The installed plugin lives at `~/.gemini/config/plugins/minni/` (real files:
`plugin.json` + a hooks.json stamped with absolute paths — agy does not expand
`${CLAUDE_PLUGIN_ROOT}`). Registration goes through `agy plugin install` from
a staging directory; never hand-drop files there (unregistered hook manifests
wedge agy at startup behind an invisible consent prompt), and never run
`agy plugin install` pointing at the destination directory (agy copies the
tree onto itself and truncates every file to zero bytes). Disable with
`agy plugin disable minni` or `MINNI_GEMINI_HOOKS=off`.

Reference: `plugins/minni/skills/minni-install/references/install-directive-antigravity.md`.

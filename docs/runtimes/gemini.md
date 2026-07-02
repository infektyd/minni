# Gemini / Antigravity

Wire Gemini to a running Minni daemon from your checkout:

```bash
engine/.venv/bin/minni up   # if the daemon isn't already running
engine/.venv/bin/python plugins/minni/skills/minni-install/scripts/propagate.py update-plugin --platform gemini
```

Antigravity rides the same `~/.gemini` surface (shared agent identity
`gemini`, vault `~/.minni/gemini-vault`) but is wired **individually**:

```bash
engine/.venv/bin/python plugins/minni/skills/minni-install/scripts/propagate.py update-plugin --platform antigravity
```

Note: `--platform all` covers codex, claude-code, kilocode, gemini, and grok —
**it does not include antigravity**; run the antigravity target explicitly.

The adapter (`plugins/minni/.gemini-plugin/gemini-extension.json`) launches
the MCP server via the extension path; Antigravity surfaces get their MCP
configs under `~/.gemini/antigravity*/`. These surfaces receive a read-only
tool allowlist by default (`minni_recall`, `minni_drill`, `minni_status`,
audit tools, …) — write and export tools are deliberately excluded there.

Reference: `plugins/minni/skills/minni-install/references/install-directive-antigravity.md`.

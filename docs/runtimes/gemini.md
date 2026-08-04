# Gemini / Antigravity

**Day-to-day Gemini family path is Antigravity**, not pure `gemini`. Prefer:

```bash
minni wire antigravity                                 # v0.3+ wheel
.venv/bin/minni wire antigravity --from-repo .         # v0.2 wheel / checkout
# or from a dogfood checkout after wire adoption:
# propagate.py update-plugin --platform antigravity
# (also covered by `make sync-root`)
```

Antigravity rides `~/.gemini` (shared agent identity `gemini`, vault
`~/.minni/gemini-vault`) and is the fleet path that also writes surface MCP
views + agy hook registration. Propagate’s D7 skip table marks pure `gemini`
as covered by antigravity.

Pure `gemini` is **provisional** in `minni wire` (`${extensionPath}` still
under verification — issue #142 open question 8): `minni wire gemini` reports
`skipped`, and `minni wire all` names it as a skip with a warning. Checkout
recovery only:

```bash
.venv/bin/minni up   # if the daemon isn't already running
.venv/bin/python plugins/minni/skills/minni-install/scripts/propagate.py update-plugin --platform gemini
```

That pure-gemini path writes `gemini-extension.json` and still registers **agy
hooks** when `agy` is on PATH (same `update_agy_plugin_hooks` path as
antigravity). It does **not** write Antigravity surface MCP views /
permission grants (`update_antigravity_config`). Prefer antigravity as the
supported day-to-day surface + hooks path.

Note: `make sync-root` uses the D7 fleet partition — `minni wire all
--from-repo` for codex/claude-code/kilocode/grok, then
`propagate.py update-plugin --platform antigravity` and `--platform cursor`
only (plus a grok hooks/rules refresh). `propagate --platform all` expands
only to antigravity+cursor (codex/kilocode/grok are named wire-managed skips).
After wire adoption, do **not** re-run explicit
`update-plugin --platform codex|kilocode|grok` — those still rewrite MCP onto
legacy cache trees. Prefer:

```
propagate.py update-plugin --platform antigravity
propagate.py update-plugin --platform cursor
```

`minni wire all` expands to codex, claude-code, kilocode, grok and names
antigravity as an explicit skip so bulk wire does not fight over the shared
~/.gemini tree; run `minni wire antigravity` or the antigravity propagate
target explicitly.

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

Verified against **agy 1.1.7** (and `docs/contracts/hook-platforms.md`). Older
“1.0.15 inert PreToolUse / no UPS” notes were stale:

- **`SessionStart`** — fires; context via agy `injectSteps` when the hook has
  something to inject (same fail-open rules as other surfaces).
- **`PreInvocation`** — agy’s prompt-submit analogue (there is **no** Claude
  `UserPromptSubmit` name on agy). The adapter maps it to the shared UPS path
  and mines the last user message from agy’s transcript when stdin has no
  task text, so file-backed recall-state can be written for the guard.
- **`PreToolUse`** — s6 recall guard via agy’s deny-capable protocol. Decision
  enum is **`allow` / `deny` / `ask` / `force_ask`** — Claude’s `approve` /
  `block` are **rejected** by agy. On this surface the guard defaults to
  **strict** mode (override with `MINNI_RECALL_GUARD_MODE`): agy funnels
  shell/search through `run_command` → Bash, and shared default “soft” would
  ignore Bash. Empty decisions error; the adapter always emits an explicit
  `allow` or `deny`.
- **`Stop`** — drafts candidate learnings into `~/.minni/gemini-vault/inbox/`
  (same governed propose→approve loop). Payload often lacks task text; the
  hook reads the last user message from `transcript_full.jsonl` when needed.

The installed plugin lives at `~/.gemini/config/plugins/minni/` (real files:
`plugin.json` + a hooks.json stamped with absolute paths — agy does not expand
`${CLAUDE_PLUGIN_ROOT}`). Registration goes through `agy plugin install` from
a staging directory; never hand-drop files there (unregistered hook manifests
wedge agy at startup behind an invisible consent prompt), and never run
`agy plugin install` pointing at the destination directory (agy copies the
tree onto itself and truncates every file to zero bytes). Disable with
`agy plugin disable minni` or `MINNI_GEMINI_HOOKS=off`.

Reference: `plugins/minni/skills/minni-install/references/install-directive-antigravity.md`.

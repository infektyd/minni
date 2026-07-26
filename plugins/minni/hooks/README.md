# Hook manifests

One file per platform, because **no two of these platforms share a hook
contract**. Before editing any of them, read `docs/contracts/hook-platforms.md`
— it records the verified event names, manifest shapes, injection points and
deny enums, with vendor sources.

| File | Platform | Shape |
|---|---|---|
| `hooks.json` | Claude Code | `{"hooks": {Event: [{matcher?, hooks:[]}]}}` |
| `hooks-codex.json` | Codex | same nesting as Claude; `${PLUGIN_ROOT}` |
| `hooks-grok.json` | Grok Build (xAI) | same nesting; `${GROK_PLUGIN_ROOT}` |
| `hooks-cursor.json` | Cursor | `{"version":1,"hooks":{event:[handler]}}` — **flat** |
| `hooks-gemini.json` | agy / Antigravity | `{"<hook-name>": {Event: […]}}` — **key is a NAME** |

Kilocode has no manifest at all; it loads `kilo/minni-plugin.js` in-process.

## hooks-gemini.json has two traps

**1. No `_comment` key, and no other non-hook key.** agy reads every top-level
key as a hook *name*, and rejects the ENTIRE file if any entry fails to
validate. That is not hypothetical — it is the bug this file used to have:

```
Failed to parse hooks file ~/.gemini/config/plugins/minni/hooks.json:
  invalid hook "hooks": command hook must specify 'command'
```

A literal `"hooks"` wrapper declared a hook *named* "hooks", the file was
discarded, and **zero Minni hooks fired on any Antigravity surface** — the CLI,
the IDE, and the 2.0 desktop app all share the loader. Keep documentation here,
not in the JSON.

**2. Grouped vs flat is per-event.** `PreToolUse`/`PostToolUse` take
`{matcher, hooks:[…]}`; every other event takes a flat handler array. Using the
grouped form on `Stop` is what produced the error above.

## Editing a Codex hook re-arms its trust gate

Codex records trust against a **hash** of each hook definition. Any edit to
`hooks-codex.json` — or to the script it points at — marks the hook "needs
review" and **silently skips it** until the user re-approves via `/hooks`.
There is no installer-side pre-approval. Ship a release note saying so, or
Minni just goes quiet on Codex with no error.

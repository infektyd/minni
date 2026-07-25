# Hook platform contracts

Minni registers lifecycle hooks on six agent platforms. **Their contracts are not
the same**, and the historical failure mode has been emitting Claude Code's wire
shape everywhere and assuming it lands. It frequently does not — and every
platform here fails *silently* rather than loudly when it doesn't.

This document is the per-platform source of truth. Verified 2026-07-25 against
the versions in the table below, using each vendor's published docs plus the
locally installed CLI. **Re-verify on upgrade: no vendor publishes a hook
deprecation policy or changelog.**

## Rule

> Core handlers express *intent*. Per-platform adapters translate intent to the
> native wire shape, or explicitly degrade. Never emit one platform's shape to
> another and hope.

If a platform cannot express an intent, the adapter must degrade **loudly** (log
it), not return a shape the platform will discard in silence.

## Versions verified

| Platform | Version | Contract source |
|---|---|---|
| Claude Code | 2.1.220 | [docs](https://code.claude.com/docs/en/hooks) + installed bundle |
| Codex | 0.145.0 | [docs](https://learn.chatgpt.com/docs/hooks) + [generated JSON Schemas](https://github.com/openai/codex/tree/main/codex-rs/hooks/schema/generated) |
| Cursor | 2026.07.23-e383d2b | [docs](https://cursor.com/docs/hooks) + installed bundle |
| agy (Antigravity CLI) | 1.1.7 | [docs](https://antigravity.google/docs/hooks) + binary-embedded spec |
| Grok Build (xAI) | 0.2.112-alpha | [docs](https://docs.x.ai/build/features/hooks) + [xai-org/grok-build](https://github.com/xai-org/grok-build) |
| Kilocode | 7.1.0 | [kilo.ai](https://kilo.ai/docs/automate/extending/plugins) + [opencode](https://opencode.ai/docs/plugins) |

## Manifest shape — all six differ

| Platform | Top-level | Event value | Env expansion |
|---|---|---|---|
| Claude Code | `{"hooks": {Event: [...]}}` | grouped `{matcher?, hooks:[]}` | `${CLAUDE_PLUGIN_ROOT}` |
| Codex | `{"hooks": {Event: [...]}}` | grouped | `${PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}` |
| Grok Build | `{"hooks": {Event: [...]}}` | grouped | `${GROK_PLUGIN_ROOT}`, `${CLAUDE_PLUGIN_ROOT}` — **not bare `PLUGIN_ROOT`** |
| Cursor | `{"version":1,"hooks":{event:[...]}}` | **flat** handler objects | `${CURSOR_PLUGIN_ROOT}` — **undocumented**, plugin-scoped only |
| agy | `{"<hook-name>": {Event: [...]}}` — **key is a NAME** | PreToolUse/PostToolUse grouped; **all others flat** | none — stamp absolute paths |
| Kilocode | no manifest — **in-process JS plugin**; Minni bridges via `kilo/minni-plugin.js` | n/a | n/a (`directory`/`worktree` on plugin input) |

agy's shape is the trap: a literal `"hooks"` top-level key declares a hook
*named* "hooks", and one invalid entry rejects **the entire file**.

## Event names

| Intent | Claude Code | Codex | Grok Build | Cursor | agy | Kilocode |
|---|---|---|---|---|---|---|
| session start | `SessionStart` | `SessionStart` | `SessionStart` | `sessionStart` | `SessionStart` ⚠️undocumented | — |
| prompt submit | `UserPromptSubmit` | `UserPromptSubmit` | `UserPromptSubmit` | `beforeSubmitPrompt` | `PreInvocation` | `chat.message` |
| pre-tool | `PreToolUse` | `PreToolUse` ⚠️Bash only | `PreToolUse` | `preToolUse` | `PreToolUse` | `tool.execute.before` |
| turn end | `Stop` | `Stop` | `Stop` | `stop` | `Stop` | `event`→`session.idle` |
| pre-compact | `PreCompact` | `PreCompact` | `PreCompact` | `preCompact` | — | `experimental.session.compacting` |

`UserPromptSubmit` and `PreCompact` **do not exist on agy** — declaring them is
dead config, not forward-compatibility.

## Context injection — where memory can actually reach the model

This is the table that matters most, and the one the codebase got wrong.

| Platform | Session start | Prompt submit | Turn end | Pre-compact |
|---|---|---|---|---|
| Claude Code | ✅ `additionalContext` | ✅ | ✅ | ❌ **not in the union** |
| Codex | ✅ `additionalContext` | ✅ | ❌ **block only** | ❌ cannot inject *or* block |
| Grok Build | ❌ **stdout ignored** | ❌ **stdout ignored** | ✅ `additionalContext` | ❌ **stdout ignored** |
| Cursor | ✅ `additional_context` ⚠️ | ❌ **not supported** | ❌ `followup_message` only | ❌ `user_message` only |
| agy | ⚠️ via `injectSteps` | ✅ `injectSteps` (`PreInvocation`) | — | — |
| Kilocode | `experimental.chat.system.transform` | same | — | `experimental.session.compacting` |

Load-bearing details:

- **Grok Build ignores hook stdout on all passive events.** Officially: *"For
  passive events, stdout is ignored; exit 0 on success."* Only `PreToolUse` and
  `Stop`/`SubagentStop` parse output. Session-start hydration on Grok must go
  through skills/instructions/MCP — **hooks cannot do it**.
- **`PreCompact` can inject on no platform except Kilocode**, whose bridge feeds
  opencode's `experimental.session.compacting`. Everywhere else its side effects
  (inbox handoff, stale-belief stash) are the only reason it exists.
- **Codex cannot inject at `Stop`**; Claude Code can. A shared Stop path must branch.
- **Cursor `beforeSubmitPrompt` cannot inject** — documented output is `continue`
  + `user_message`. The bundle validates `additional_context`, but it is
  undocumented and there are open vendor feature requests to add it. Do not rely
  on it. Cursor `sessionStart` injection also has an open bug report claiming it
  is accepted but never applied — verify empirically per version before trusting.

## Pre-tool deny — enums differ, and coverage differs more

| Platform | Decision values | Coverage |
|---|---|---|
| Claude Code | `allow` `deny` `ask` `defer` | all tools |
| Codex | `allow` `deny` `ask` (**no `defer`**) | ⚠️ **Bash only** |
| Grok Build | `allow` `deny` | broad (aliases `Read`→`read_file` etc.) |
| Cursor | `allow` `deny` `ask` (`ask` unenforced on `preToolUse`) | broad |
| agy | `allow` `deny` `ask` `force_ask` — **rejects `approve`/`block`** | broad |
| Kilocode | throw from `tool.execute.before`, or `permission.ask` → `ask\|deny\|allow` | broad |

**Recall-guard consequence:** the guard gates `Read`/`Grep`/`Glob`. Codex's
`PreToolUse` intercepts Bash only, so the guard **cannot** work there regardless
of the deny capability. Grok Build, Cursor and agy can all support it.

## Stop payload — the field that exists nowhere

`last_user_message` is read by the codebase and **exists on no platform**:

| Platform | Actual field |
|---|---|
| Claude Code | `last_assistant_message` |
| Codex | `last_assistant_message` (schema is `additionalProperties:false`) |
| Grok Build | `lastAssistantMessage` (camelCase envelope) |

Note it is the *assistant's* message on every platform, never the user's.

Grok Build also fires an extra observe-only `Stop` at session end; filter on
`reason == "end_turn"` or outcomes get double-counted.

## Timeouts

Seconds everywhere except Kilocode. Defaults differ sharply: Claude Code 600s
(but **30s** for `UserPromptSubmit`), Codex per-manifest, Grok Build 5s — **600s
for `Stop`/`SubagentStop`**, Cursor 60s, agy 30s and **blocking the agent loop**.
Kilocode has no hook timeout at all (a hang hangs the CLI); its MCP `timeout` is
milliseconds.

All platforms fail **open** on hook error/timeout — a slow daemon silently drops
memory rather than surfacing an error.

## Install paths

| Platform | Path | Notes |
|---|---|---|
| Claude Code | `hooks/hooks.json` in plugin root, or `plugin.json` `hooks` key | auto-discovered |
| Codex | same, via `.codex-plugin/plugin.json` | **SHA-256 trust-gated** — see below |
| Grok Build | `~/.grok/hooks/*.json`; plugin `hooks/hooks.json` | also reads Claude/Cursor configs |
| Cursor | `.cursor-plugin/plugin.json` `hooks` key, else `hooks/hooks.json` | `${CURSOR_PLUGIN_ROOT}` expands for **manifest-registered hooks only** |
| agy | docs say `~/.gemini/antigravity-cli/plugins/<name>/`; CLI empirically also reads `~/.gemini/config/plugins/<name>/` | ⚠️ documented root and working root disagree |
| Kilocode | config `plugin: []` key (blessed) or `~/.config/kilo/plugin/*.js` | MCP env key is `environment`, **not `env`** — `env` bricks the whole CLI |

### Codex hook trust (operational landmine)

Codex records trust against a **hash of each hook definition**. Editing a hook
manifest or script marks it "needs review" and **skips it silently** until the
user re-approves via `/hooks`.

There is **no documented way for an installer to pre-approve.** Managed hooks are
MDM/policy-only; `--dangerously-bypass-hook-trust` is per-invocation and must not
be baked into an installer. **Every Minni release that touches a Codex hook must
tell the user to run `/hooks` and re-trust** — otherwise Minni goes quiet on
Codex with no error.

## Stability

No vendor publishes a hook deprecation policy or changelog. Specific risks:

- **agy**: hooks documented only on the Antigravity **2.0 desktop** page;
  `/docs/cli/hooks` 404s while being linked from the CLI sidebar. `SessionStart`
  is undocumented-but-real.
- **Grok Build**: binary self-reports `[alpha]`, yet the docs carry **no**
  stability warning. Under-warned, not stable.
- **Kilocode**: the injection hooks are `experimental.*` and documented as
  subject to change; Kilo has already shipped one breaking config-path change
  (`.opencode` → `.kilo`).
- **Cursor**: `${CURSOR_PLUGIN_ROOT}` is absent from all official docs.
- **Claude Code**: at least one silent breaking change shipped
  (`${user_config.KEY}` substitution removed before v2.1.207).

## Do not confuse: Gemini CLI ≠ Antigravity

`google-gemini/gemini-cli` has its own, **incompatible** hook system
(`BeforeTool`, `BeforeAgent`, `BeforeModel`…, configured in `settings.json`).
It shares the `~/.gemini/` home directory with Antigravity and nothing else.

Likewise, **Grok Build** (xAI, `xai-org/grok-build`) is not the several
unaffiliated community "grok-cli" npm packages. Their docs are not authoritative.

## The three Antigravity surfaces

One `gemini` agent identity spans the **IDE**, the **2.0 desktop app**, and the
**`agy` CLI**. They share `~/.gemini/` but have separate app-data roots
(`antigravity-ide/`, `antigravity/`, `antigravity-cli/`). A manifest that fails
to parse fails on **all** of them — the loader is shared.

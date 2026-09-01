# Hook platform contracts

Minni registers lifecycle hooks on six agent platforms. **Their contracts are not
the same**, and the historical failure mode has been emitting Claude Code's wire
shape everywhere and assuming it lands. It frequently does not — and every
platform here fails *silently* rather than loudly when it doesn't.

This document is the per-platform source of truth. Verified 2026-07-25 against
the versions in the table below, using each vendor's published docs plus the
locally installed CLI. **Re-verify on upgrade: no vendor publishes a hook
deprecation policy or changelog.**

When the matrix says a host has a live s6 cold-tool guard, shipping without a
working adapter is a **defect** — fix code, do not cut the row. Operator
summary and fleet redeploy: [platform-hook-contracts.md](../ops/platform-hook-contracts.md).

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
| session start | `SessionStart` | `SessionStart` | `SessionStart` | `sessionStart` | `SessionStart` ⚠️undocumented but WORKS | — |
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
| Cursor | ⚠️ **doc-true, runtime-broken** (vendor bug) | ❌ **not supported** | ❌ **no channel** — `followup_message` loops the agent | ❌ `user_message` only |
| agy | ✅ `injectSteps` (verified live) | ✅ `injectSteps` (`PreInvocation`) | ❌ rejects `injectSteps` | — |
| Kilocode | ✅ via bridge → `system.transform` | ✅ same | — | ✅ **only platform that can** |

Load-bearing details:

- **Grok Build ignores hook stdout on all passive events.** Officially: *"For
  passive events, stdout is ignored; exit 0 on success."* Only `PreToolUse` and
  `Stop`/`SubagentStop` parse output. **Hooks cannot hydrate memory at boot on
  Grok** — see "Boot hydration on Grok Build" below for what we do instead.
- **`PreCompact` can inject on no platform except Kilocode**, whose bridge feeds
  opencode's `experimental.session.compacting`. Everywhere else its side effects
  (inbox handoff, stale-belief stash) are the only reason it exists.
- **Codex cannot inject at `Stop`**; Claude Code can. A shared Stop path must branch.
- **Cursor cannot inject at all, in practice.** `beforeSubmitPrompt` is
  documented as `continue` + `user_message` only. And `sessionStart`'s
  `additional_context` is a **staff-confirmed open bug** — *"This is a bug on our
  side"*, a timing issue against composer-handle creation, unfixed 3.1.15 →
  3.8.23, *"no workaround right now"*. Measured twice here: the agent went to MCP
  both times. `cursorWire` still declares SessionStart injectable because that is
  the documented contract; treat it as aspirational until the vendor ships a fix.

## Pre-tool deny — enums differ, and coverage differs more

| Platform | Decision values | Host coverage | Minni s6 cold-tool guard live? |
|---|---|---|---|
| Claude Code | `allow` `deny` `ask` `defer` | all tools | Yes — native Claude protocol + shared guard |
| Codex | `allow` `deny` `ask` (**no `defer`**) | ⚠️ **Bash only** | No for Grep/Read/Glob — registration ≠ cold-file deny |
| Grok Build | `allow` `deny` | broad (matcher aliases `Read`→`read_file` etc.) | **PARTIAL** — host deny-capable. `grok-adapter.ts` maps camelCase + natives and `{decision,reason}` out (capability, not liveness). Host deny ≠ Minni s6 liveness. UPS/SS stdout ignored (`GROK_INJECTABLE={Stop}`), so dropped UPS does not plant `consumed=false`; leftover false is cleared even on daemon timeout. PreToolUse allows immediately when UPS cannot inject, so a leftover file cannot deny. |
| Cursor | `allow` `deny` `ask` (`ask` unenforced on `preToolUse`) | broad | **PARTIAL** — host deny-capable (Cursor adapters). Host deny ≠ Minni s6 liveness. UPS inject dropped (`CURSOR_INJECTABLE={SessionStart}`; `beforeSubmitPrompt` has no inject channel); leftover false is cleared; PreToolUse allows immediately so a leftover file cannot deny. |
| agy | `allow` `deny` `ask` `force_ask` — **rejects `approve`/`block`** | broad | Yes — `gemini-adapter.ts` |
| Kilocode | throw from `tool.execute.before`, or `permission.ask` → `ask\|deny\|allow` | broad | Yes — bridge plugin |

**Recall-guard consequence:** the guard gates `Read`/`Grep`/`Glob` (and Grok
natives / shell under strict) only when UPS actually delivered the envelope.
Host **deny capability** is necessary but not sufficient: Codex's PreToolUse
intercepts Bash only, so the guard **cannot** gate cold-file tools there even
though deny exists. Grok Build and Cursor are the other side of the same split
— adapters map tools, but dropped UPS inject means leftover `consumed=false`
cannot deny (do not expand `GROK_INJECTABLE` to fake liveness). Live s6
cold-tool guard: Claude Code, agy, Kilo. Grok/Cursor remain PARTIAL until UPS
(or equivalent) delivers the envelope.

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
| Kilocode | `~/.config/kilo/plugin/*.js` auto-scan (verified live; no `plugin: []` key needed) | MCP env key is `environment`, **not `env`** — `env` bricks the whole CLI |

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

## Boot hydration on Grok Build

Grok is the one platform where hooks cannot deliver memory at session start, so
it gets a different mechanism. Everything else was ruled out against xAI's own
docs: **skills** activate on-demand only (*"Grok activates a skill only when it
applies to your current task"*), **plugins** expose no context component at all,
**MCP** has no eager-load equivalent of Claude Code's `alwaysLoad`, and no config
key contributes a preamble.

What does load unconditionally is `$GROK_HOME/rules/*.md` — documented as
*"Always scanned; applies to all projects"*, read into the system prompt at
session start, every `*.md` regardless of filename, and (unlike hooks and MCP)
**not gated on folder trust**. So the installer writes `~/.grok/rules/minni.md`
instructing the model to call `minni__minni_recall` on its first turn.

The file is static; the context it produces is **live**, because the recall runs
in-session against the daemon. That matters — no Grok mechanism delivers
dynamically generated content into the boot context, so a pre-written envelope
file could only ever be as fresh as the last write.

Honest limitations: it is model **compliance, not harness enforcement**; context
arrives one tool round-trip in rather than before the first token; and
`~/.grok/rules/` is global, so it costs a few hundred tokens in every session on
the machine including repos where Minni is irrelevant. Keep the file short —
long rules files are followed less reliably. `grok inspect` lists loaded
instruction files with token counts and is a free regression check.

## agy: two traps that cost real debugging time

**1. `injectSteps` is per-event, not universal.** It is honored on `SessionStart`
and `PreInvocation` (both verified live with a marker probe: the model quoted the
injected token back). `Stop` has a different output proto and rejects the field:

```
failed to unmarshal result from hook jsonhook__minni_Stop_0_0 via protojson:
  unknown field "injectSteps"
```

**2. A changed manifest is loaded LAZILY, at first-prompt time.** The log shows
`Loaded hooks.json ... 4 total handlers` arriving one second before
`SessionStart` fires — after that first invocation's context was already
assembled. So **the first agy session after any manifest change silently gets no
injection**; every session after it is fine. Do not diagnose off that first run.

Also note `ephemeralMessage` content IS persisted to `transcript_full.jsonl`, so
grepping the transcript is a valid check — but only for sessions that started
after the manifest was loaded.

## Every platform must own a wire

`wireFor()` falls back to the Claude Code shape for an unknown id. That is a
narrow safety net for a not-yet-profiled platform — it must never become
load-bearing for a shipped one.

It did, once: Cursor had no profile, so it resolved to `claudeCodeWire`, the
handlers emitted Claude envelopes, and `adaptCursorOutput` translated them after
the fact. Anything Cursor could not carry — the prompt-submit envelope — was
dropped by the adapter **with no record**, which is precisely the silence the
wire layer exists to eliminate. A test now pins `wireFor("cursor").id === "cursor"`.

If you add a platform: give it a profile, set `wire:` on its config, and let the
adapter handle only what genuinely bypasses the wire (the `PreToolUse` guard
output, which is not an `EnvelopeEvent`).

## Cursor: `followup_message` is an action, not a message

Cursor's `stop` accepts exactly one field, `followup_message`, and it is the
**auto-follow-up** mechanism — returning it drives another agent turn, bounded
by `loop_limit` (default 5). It is not a way to tell the user something.

Using it to announce a drafted learn candidate is self-feeding, and this was
observed live: **6 Stop hooks in 90 seconds from a single prompt**, each
drafting an inbox candidate built from the audit trail the previous one had just
written. The Cursor agent diagnosed it itself — *"the stop hook is feeding its
own audit trail back into the inbox each turn"* — and spent five turns rejecting
its own garbage.

Cursor therefore has **no human-facing note channel**. Notes are dropped and
recorded. Announcing a candidate is never worth spending a model turn.

The general rule this is an instance of: **before rendering an intent into a
platform field, check whether that field has side effects.** `systemMessage`
(Claude), `followup_message` (Cursor) and `injectSteps` (agy) look
interchangeable and are not — one is inert, one continues the agent, one is
rejected outright at `Stop`.

## Cursor: the editor and the CLI are ONE install, two capability sets

`cursor.com/docs/hooks` describes the **editor** (plus a cloud-agent subset). The
CLI is the *reduced* surface, and its hook behaviour is **not documented at all**
— the CLI reference pages mention `cli-config.json` and say nothing about hooks.

**They share config.** Both read `~/.cursor/hooks.json` (user) and
`<project>/.cursor/hooks.json` (project). So the installer's user-level write
covers Cursor.app as well as cursor-agent — one install, both surfaces. There is
no per-user app config for hooks; `/Library/Application Support/Cursor/hooks.json`
(system `/Library`, not `~/Library`) is the **enterprise MDM** path only.

Cursor watches these files and reloads automatically; restarting is the
documented remedy if it doesn't.

Note the CWD differs by scope — user hooks run from `~/.cursor/`, project hooks
from the project root. The installer stamps **absolute** command paths, which
sidesteps this entirely. Keep it that way.

### `sessionStart.additional_context` is a confirmed vendor bug — do not use it

Cursor staff acknowledged it: *"This is a bug on our side"* — a timing issue
between hook execution and composer-handle creation. Reported on 3.1.15 (Apr
2026) and still open on 3.8.23 (Jun 2026): *"No ETA on a fix yet… there's no
workaround right now."* The same `additional_context` plumbing gap is reported
for `postToolUse`.

So `cursorWire` declaring `SessionStart` injectable is **doc-correct and
runtime-broken**. Memory reaches the model on Cursor via MCP tool calls, not
hooks. Our own testing matches: the agent went to MCP both times.

What *is* confirmed working is `sessionStart`'s **`env`** return — session-scoped
vars passed to later hook executions. That is a hook-to-hook channel, not a
model-visible one, so it does not help hydration.

### CLI event parity is partial AND version-dependent

Staff confirmed the CLI fires only a subset, expanding over time: shell hooks
only (Jan 2026), plus `afterFileEdit`/`postToolUse`/`stop`/`sessionStart` (Apr
2026), with gaps acknowledged again in Jun 2026.

**But our live evidence supersedes that.** On CLI **2026.07.23**,
`beforeSubmitPrompt` fires — we have the audit entries. The June report said it
did not. Treat CLI parity as a moving target and re-probe per version rather
than trusting any single report, including this paragraph.

Editor-only by design: the Tab hooks (`beforeTabFileRead`, `afterTabFileEdit`).
`workspaceOpen` is the one event documented to run in **both** app and CLI, and
it omits `conversation_id`, `session_id` and `transcript_path`.

### `transcript_path` is not documented — do not mine it blind

Only the field's existence is documented: *"Path to the main conversation
transcript file (null if transcripts disabled)"*. **No schema, no record shape,
no location.** A CLI changelog (18 Feb 2026) implies JSONL, but gives no schema
and does not claim the editor uses the same format.

This is the obvious fix for Cursor's Stop candidates degrading to a bare session
id — its `stop` payload carries only `status` and `loop_count`, no message text.
But mining an undocumented format is how the agy assumptions rotted. If it gets
built: sniff the schema, verify on **each** surface separately, and fall back to
the current degrade. Also handle `transcript_path: null` — a documented state
whose triggering setting is itself undocumented.

## Desktop apps: one shares config, one does not, neither has hooks

| Surface | Hooks | Shares CLI config | Boot hydration |
|---|---|---|---|
| **Cursor.app** | ✅ — it is the PRIMARY hook surface | ✅ same `~/.cursor/hooks.json` | ❌ (the sessionStart bug) |
| **Codex / ChatGPT desktop** | documented for the Codex host; desktop scope **implied, not stated** | ✅ *"share MCP configuration for the same Codex host"* | via MCP `instructions` |
| **Claude Desktop** | ❌ none, by design | ❌ **fully disjoint tree** | ❌ tool-call on demand only |

Claude Desktop reads `~/Library/Application Support/Claude/claude_desktop_config.json`.
Writing `~/.claude/` reaches it not at all. Mind the near-miss:
`/Library/Application Support/ClaudeCode/` belongs to Claude **Code**.

Its identity is deliberately `claude-code` on the `claudecode-vault` — Desktop
and Code are the same person at the same machine, so they share one memory, the
way the three Antigravity surfaces share one `gemini` identity.

**Unverified, deliberately:** whether Claude Cowork reads `~/.claude`, and
whether `~/.codex/hooks.json` hooks fire in the Codex desktop app. Both are
NOT DOCUMENTED. Resolve by observation before either goes in a support matrix.

## The MCP `instructions` field is the hydration channel of last resort

Returned at initialize as server-wide guidance alongside the tools. It needs no
hooks and is silently dropped by hosts that ignore it.

**It is NOT a universal answer.** Measured, not assumed:

| Surface | `instructions` drives turn-1 hydration? |
|---|---|
| Claude Code | ✅ delivered and visible in-session |
| Codex | documented as read; **not re-measured** as of 2026-08-04 (local residual pass) |
| **Cowork** | ❌ **no** — asked permission instead |
| **Claude Desktop Chat** | ❌ **no** — asked permission instead |
| Grok Build / Cursor | **untested** since ship; **still not re-measured** as of 2026-08-04 |

Honesty: a local residual pass did **not** run live host probes for Codex/Grok/Cursor.
Do not treat the dated “not re-measured” cells as a new measurement — only as
an explicit refresh of the backlog timestamp.

Both Claude desktop surfaces were tested on Opus — the strongest model, chosen
so a null result could not be blamed on compliance — and both declined to act.
Chat could *name* the tools, so the server was connected and the field's
delivery is not the question; it simply does not drive behavior there. Cowork
says outright that MCP tools are **"not yet loaded"** on turn one, which would
make boot hydration structurally impossible regardless of what we ship.

So `instructions` is worth having — it costs 509 characters and helps where it
lands — but it does **not** replace a hook, and it did not rescue the
no-hook surfaces it was adopted for. On Claude Desktop and Cowork, memory
arrives only when the user asks for it.

Minni sets it in `server.ts` (`MINNI_INSTRUCTIONS`). Two constraints, both
pinned by tests: keep the **first 512 characters self-contained** (Codex
documents that budget), and keep the whole thing short — it is billed into
every MCP session on every host.

It also carries the evidence-not-instruction boundary, which matters more here
than anywhere else: this text reaches hosts we do not control, so the server
states plainly that recalled memory is data and has no authority over the agent.

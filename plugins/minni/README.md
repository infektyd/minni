# Minni Plugin (`minni-multi-plugin`)

Local-first multi-host MCP plugin and hook bridge for Minni. It ships Codex (`.codex-plugin/`), Claude Code (`.claude-plugin/`), Cursor (`.cursor-plugin/`), Gemini (`.gemini-plugin/`), and KiloCode (`.kilocode-plugin/`) surfaces from one TypeScript server and shared `dist/` build. Each agent has its own Obsidian-compatible Markdown vault while sharing one governed local daemon (`minnid`).

## v1.0 Surface Highlights

- **Exact 12-Tool MCP Surface:** 6 Core Memory tools (`minni_recall`, `minni_drill`, `minni_learn`, `minni_list_candidates`, `minni_resolve_candidate`, `minni_status`), 2 Handoff tools (`minni_handoff_send`, `minni_handoff_inbox`), and 4 compact `< 2 KB` Thread tools (`minni_thread_create`, `minni_thread_status`, `minni_thread_update`, `minni_thread_scar`).
- **Two-Part Permanent Boot Shelf (`SessionStart` / `SubagentStart`):** Loads the Agent Permanent Shelf (`layer1/core.md`, up to `24 KB` without truncation) plus the Project Permanent Shelf (`project_shelf` `<= 2,500` chars: Living Repo Map subsystem directory, git-diff staleness notices, top workspace scars, and active plan summary), with automatic one-shot `PostCompact` shelf recovery.
- **Strictly Non-Blocking Hooks Across All Hosts:** Hooks inject context and never block `Read`, `Bash`, or edit tool calls; `<task-notification>` background wakeups are skipped early; `PostToolUse` failure-lesson checks fire only on file edits and failed commands.
- **Plain-Markdown Human Visibility:** Operator status and daily activity live in `~/.minni/TODAY.md` and CLI commands (`minni status`, `minni activity`, `minni map`, `minni recall`, `minni learn`, `minni watch`, `minni doctor`) plus vault `log.md` — zero web server required.

## 12-Tool MCP Contract

| Category | Tool | Summary |
|---|---|---|
| **Core Memory** | `minni_recall` | Repo-First search across Living Repo Maps, docs, learnings, and scars (`<EVIDENCE>` envelopes) |
| | `minni_drill` | Expand `m:<8hex>` handles, `d:<src>:<id>` refs, or `wikilink` targets with typed graph links |
| | `minni_learn` | Vault-first quality/credential gate + candidate staging or durable workspace-scoped storage |
| | `minni_list_candidates` | List staged learning candidates (`status=proposed` default) scoped to caller principal |
| | `minni_resolve_candidate` | Owner/operator resolution (`accept`, `learn`, `reject`, `redact`, `do_not_store`, `log_only`, `merge`, `supersede`) |
| | `minni_status` | Daemon socket health, AFM status, fixed vault path, audit summary, and 7-day recall-to-use stats |
| **Handoff** | `minni_handoff_send` | 1-step work-transfer handoff packet (`to_agent` defaults to own principal for next-session handoff; `< 1 KB` receipt) |
| | `minni_handoff_inbox` | List pending handoffs, acknowledge a single `lease_id`, or pass `ack: true` to read and clear inline |
| **Thread** | `minni_thread_create` | Create a proposal-first multi-slice plan (`< 2 KB` receipt, per-session `activate` pointer) |
| | `minni_thread_status` | Read `< 2 KB` compact thread view (`<= 160` char scar previews) + surface-only shelf drift check |
| | `minni_thread_update` | Advance slice status safely under `withThreadPlanLock` (`< 2 KB` compact receipt, no lease tokens) |
| | `minni_thread_scar` | Record `failed_command`, `dead_end`, or `rejected_hypothesis` (`< 1 KB` receipt) |

Only canonical `minni_*` tool names are registered. Internal worker-claim and host-dispatch helpers (`plan.ts`, `thread-lock.ts`, `thread-worker.ts`, `thread-worker-packet.ts`, `thread-host-dispatch.ts`) remain internal library modules rather than model-facing MCP tools.

## macOS Thread Claim Storage

Thread lock and worker receipts on macOS use a bundled stdlib-only Python helper for descriptor-relative filesystem operations (`MINNI_CLAIM_PYTHON`, `PYTHON`, `PYTHON3`, or `python3` on `PATH`), isolated per awaited mutation with a 10-second deadline. Linux uses its native descriptor-path backend.

## Runtime Defaults & Environment

All paths and env var names are read in `src/config.ts` and `src/afm.ts`. `MINNI_HOME` overrides `~/.minni` for all defaults:

- Minni home: `MINNI_HOME` (default `~/.minni`; digest at `<minni_home>/TODAY.md`).
- Daemon socket: `MINNI_SOCKET_PATH` (default `~/.minni/run/minnid.sock`).
- MCP vault: `MINNI_VAULT_PATH` (host manifests stamp per-agent paths; platform hook entrypoints read `MINNI_CLAUDECODE_VAULT_PATH`, `MINNI_CODEX_VAULT_PATH`, `MINNI_GEMINI_VAULT_PATH`, `MINNI_KILOCODE_VAULT_PATH`, `MINNI_GROK_VAULT_PATH`, `MINNI_CURSOR_VAULT_PATH`).
- Local judge / AFM settings: `MINNI_AFM_PROVIDER_MODE` (`bridge`, `native`, `auto`, `off`), `MINNI_AFM_HEALTH_URL` (`http://127.0.0.1:11437/health`), `MINNI_AFM_NATIVE_HELPER`, and `~/.minni/providers.json` (`MINNI_PROVIDERS_CONFIG`). Non-loopback model hosts require explicit listing in `MINNI_AFM_ALLOWED_TARGETS` / `MINNI_MODEL_ALLOWED_TARGETS` plus HTTPS.

## Host Surfaces & Slash Commands

- **Claude Code (`.claude-plugin/`)** & **KiloCode (`.kilocode-plugin/`)**:
  - Hooks (`SessionStart`, `UserPromptSubmit`, `PreCompact` / `PostCompact`, `PostToolUse`, `Stop`) emit deterministic `<minni:context version="1" ...>` envelopes. Disable with `MINNI_CLAUDECODE_HOOKS=off` or `MINNI_KILOCODE_HOOKS=off`.
  - Slash commands (`/minni:*`): `recall`, `learn`, `status`, `threads`.
- **Staging & Non-Clobbering Bundling (`BUG-06`):**
  - `npm run build` emits modular TypeScript ESM into `dist/` (required by process-path unit tests).
  - `node scripts/bundle_server.mjs --outfile <staged>/dist/server.js` bundles `dist/server.js` directly into staged wheel or `--from-repo` directories without overwriting `plugins/minni/dist/server.js` in place.

## Development

```bash
npm ci                   # deterministic install from package-lock.json
npm run build            # writes modular dist/*.js via tsc
npm test                 # full pipeline: build + node --test suite
npm run typecheck        # tsc --noEmit
npm run lint             # eslint . — lints src/, tests/, and scripts/
```

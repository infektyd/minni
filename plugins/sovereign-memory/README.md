# Sovereign Memory Plugin

Local-first plugin for Sovereign Memory. Multi-target: ships a Codex plugin (`.codex-plugin/`), Claude Code plugin (`.claude-plugin/`), and Gemini extension (`.gemini-plugin/`) from the same source tree and shared `dist/` build. Each agent gets its own Obsidian vault; all agents share the same daemon. Exposes recall, AI-facing vault context packs, manual vault-first learning, learning quality checks, structured Obsidian note writes, compile dry-runs, handoffs, information request contracts, and audit tools through MCP. On Claude Code, hooks wire memory in as a session spine.

The optional frontend lives in `frontend/`. Run `npm run console` to start the local-only bridge at `http://127.0.0.1:8765/` and generate real `sovereign_prepare_task` and `sovereign_prepare_outcome` packets from the same backend functions used by MCP/CLI. Opening `frontend/index.html` directly still works as a static packet inspector fallback.

## Runtime Defaults

- Sovereign daemon socket: `SOVEREIGN_SOCKET_PATH` if set, otherwise `/tmp/sovereign.sock`; v4 JSON-RPC helpers also fall back to `/tmp/sovrd.sock`.
- AFM health URL: `http://127.0.0.1:11437/health`
- AFM provider mode: `bridge` by default; set `SOVEREIGN_AFM_PROVIDER_MODE=native`, `auto`, or `off` to change opt-in AFM calls.
- Codex vault: `~/.sovereign-memory/codex-vault`

Override with:

```bash
export SOVEREIGN_CODEX_VAULT_PATH=/path/to/codex-vault
export SOVEREIGN_SOCKET_PATH=/tmp/sovrd.sock
export SOVEREIGN_AFM_HEALTH_URL=http://127.0.0.1:11437/health
export SOVEREIGN_AFM_PREPARE_TASK_URL=http://127.0.0.1:11437/v1/chat/completions
export SOVEREIGN_AFM_PROVIDER_MODE=native
```

`sovereign_prepare_task` and `sovereign_prepare_outcome` also accept
`afmProviderMode` per call (`bridge`, `native`, `auto`, or `off`). `native`
checks the AFM health endpoint for an Apple Foundation Models backend before
distillation and records sanitized provider metadata (`backend`,
`availability`, `adapterConfigured`) in the packet. Adapter paths are not
returned or sent to the model prompt. `bridge` preserves the earlier
OpenAI-compatible localhost behavior.

Set `SOVEREIGN_AFM_NATIVE_HELPER` to the repo-local
`engine/native_afm_helper` wrapper to let native prepare-task/outcome
distillation call Foundation Models directly through the JSON helper contract.
Future adapter configuration may be indicated with `SOVEREIGN_AFM_ADAPTER_PATH`
or `SOVEREIGN_AFM_ADAPTER_ID`; status reports only `adapterConfigured`, never
the private path.

## Tools

- `sovereign_status`
- `sovereign_prepare_task`
- `sovereign_prepare_outcome`
- `sovereign_route`
- `sovereign_recall`
- `sovereign_learning_quality`
- `sovereign_learn`
- `sovereign_vault_write`
- `sovereign_audit_report`
- `sovereign_audit_tail`
- `sovereign_compile_vault` — dry-run AFM compile passes: `session_distillation`, `synthesis`, `procedure_extraction`, `reorganization`, `pruning`
- `sovereign_negotiate_handoff` — runtime-stamped agent-to-agent work-transfer envelope (top recalls, scar tissue, open questions, inbox pointer)
- `sovereign_ping_agent_request` — create a vault-backed information request contract for another agent
- `sovereign_ping_agent_inbox` — list this runtime agent's pending and decided request contracts
- `sovereign_ping_agent_decide` — approve or deny a request addressed to this runtime agent
- `sovereign_ping_agent_status` — let requester or recipient track the contract lifecycle

## Claude Code Spine

Install in Claude Code (local plugin dir, or via marketplace):

```bash
claude plugin install --plugin-dir /path/to/sovereign-memory/plugins/sovereign-memory
```

The Claude Code surface adds:

- **Vault**: `~/.sovereign-memory/claudecode-vault` (override: `SOVEREIGN_CLAUDECODE_VAULT_PATH`).
- **Agent identity**: `claude-code` (override: `SOVEREIGN_CLAUDECODE_AGENT_ID`).
- **Hooks** (`hooks/hooks.json`):
  - `SessionStart` — boots identity, audit tail, pending-inbox learnings.
  - `UserPromptSubmit` — auto-recalls before each turn, injects ranked vault + daemon results.
  - `PreCompact` — captures scar tissue (failed paths, dead ends) so post-compaction Claude doesn't repeat them.
  - `Stop` — drafts candidate learnings to vault inbox; never auto-writes.
- **Slash commands** (namespaced as `/sovereign-memory:*`): `recall`, `learn`, `status`, `audit`, `prepare-task`, `prepare-outcome`.
- **Agent-first envelope**: hook output is wrapped as `<sovereign:context version="1" event="..." agent="claude-code" tokens="...">` containing deterministic JSON for prompt-cache stability.

Disable hooks without uninstalling: `export SOVEREIGN_CLAUDECODE_HOOKS=off`.

The Codex plugin (`.codex-plugin/`), Gemini extension (`.gemini-plugin/`), and other integrations (Hermes, OpenClaw) are unaffected — they share the daemon, not the vault.

Automatic behavior should remain recall-only. `sovereign_route` can recommend recall/status/audit automatically, but learning and vault writes stay manual and vault-first. `sovereign_learn` returns a quality report and can block weak memories with `requireQuality`.

## Agent Information Requests

Direct cross-agent recall is intentionally not exposed. When one model needs
information from another agent, it must create a pseudo-contract with
`sovereign_ping_agent_request`. The plugin stamps the sender from the runtime
principal (`SOVEREIGN_CODEX_AGENT_ID` by default), writes a pending contract to
the sender outbox and recipient inbox, and records an audit entry. The request
contains only the question, purpose, TTL, allowed topics, and response cap.

`sovereign_negotiate_handoff` is kept as a direct work-transfer path: it lets the
runtime agent hand its own task packet to another agent. It may not impersonate a
different sender. If the requested handoff is really asking the target agent to
share its vault, recall, notes, prior handoff, or private context, the server
routes the call into `sovereign_ping_agent_request` instead of `daemon.handoff`.
This keeps the module boundary explicit: handoff moves caller-owned work context;
ping requests recipient-owned information and requires recipient approval.

The recipient sees requests with `sovereign_ping_agent_inbox` and decides with
`sovereign_ping_agent_decide`. Approval requires an explicit answer. Denial
requires no answer. Approved answers are capped and redacted for secret-shaped
values and machine-local paths before syncing back to the requester outbox.
`sovereign_ping_agent_status` shows the requester or recipient the current
lifecycle state (`pending`, `approved`, `denied`, or `expired`).

Agent vault roots are resolved from `SOVEREIGN_AGENT_VAULTS`, per-agent
`SOVEREIGN_<AGENT>_VAULT_PATH`, or the local `~/.sovereign-memory/<agent>-vault`
default. This keeps identity and storage routing in config/runtime ownership
rather than in model-provided paths.

## Local Console

```bash
npm run console
```

The console exposes only local HTTP endpoints:

- `GET /api/health`
- `GET /api/status`
- `GET /api/audit-tail?limit=20`
- `POST /api/prepare-task`
- `POST /api/prepare-outcome`

The server binds to `127.0.0.1`, refuses non-local bind hosts, rejects non-local host/origin/fetch-metadata requests, requires JSON POST bodies, caps JSON request bodies, redacts machine-local paths in browser-facing status/audit responses, and does not expose learn or vault-write endpoints. Browser requests cannot override the server-owned vault path or AFM target. `prepare-task` keeps its existing audit behavior; `prepare-outcome` remains dry-run only.

## Development

```bash
npm install
npm test
npm run console
npm run design:lint
npm run test:live:prepare
```

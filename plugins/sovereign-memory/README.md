# Sovereign Memory Plugin

Local-first plugin for Sovereign Memory. It ships Codex (`.codex-plugin/`),
Claude Code (`.claude-plugin/`), Gemini (`.gemini-plugin/`), and KiloCode
(`.kilocode-plugin/`) surfaces from the same TypeScript MCP server and shared
`dist/` build. Each agent gets its own Obsidian-compatible vault; all agents
share the same local daemon.

The plugin exposes recall, prepare-task packets, proposal-first learning,
learning quality checks, structured vault notes, compile dry-runs, handoffs,
information request contracts, candidate resolution, audit tools, and temporary
team runtime packets. On Claude Code and KiloCode, hooks wire memory in as a
session spine.

The frontend is a Vite + React + TypeScript app under [frontend-src/](frontend-src/), built into the served [frontend/](frontend/) directory. Run `npm run console` (which runs `tsc && vite build && node dist/ui-server.js`) to start the local-only bridge at `http://127.0.0.1:8765/`. Eight screens are wired through the Tweaks panel-controlled rail:

- **Recall** — POST `/api/prepare-task`. Type a query, get ranked vault sources with privacy / authority / AFM chips and an Inspector pane.
- **Prepare Packet** — reads the same `prepare-task` response and renders the `<sovereign:context>` envelope, token-budget meter, included-source list, and risk callouts.
- **Dry-run Review** — POST `/api/prepare-outcome`. Submit task + summary; the response's `outcomeDraft` partitions into LEARN CANDIDATES / LOG-ONLY / DO-NOT-STORE columns. Approve/Defer/Reject is a UI decision only — nothing is stored.
- **Audit Trail** — GET `/api/audit-tail?limit=N`. Parses the daemon's `## [iso-ts] tool | summary` markdown into a sortable table.
- **Settings** — GET `/api/status` + `/api/health`. Shows daemon socket, AFM adapter, vault path, and bridge tools.
- **Handoffs / Vaults / Policy & AFM** — read-only placeholders for surfaces
  that are still operated through MCP tools or daemon calls rather than console
  screens.

Two themes ship: **Paper** (default, warm bone + persimmon stamp + verdigris accents) and **Phosphor** (CRT operator board with telemetry rail and live activity stream). Toggle from the gear button bottom-right. Layout sizes and theme persist to `localStorage`.

## Runtime Defaults

- Sovereign daemon socket: `SOVEREIGN_SOCKET_PATH` if set, otherwise `/tmp/sovereign.sock`; v4 JSON-RPC helpers also fall back to `/tmp/sovrd.sock`.
- AFM health URL: `http://127.0.0.1:11437/health`
- AFM provider mode: `bridge` by default; set `SOVEREIGN_AFM_PROVIDER_MODE=native`, `auto`, or `off` to change opt-in AFM calls.
- Codex vault: `~/.sovereign-memory/codex-vault`
- KiloCode vault: `~/.sovereign-memory/kilocode-vault`

Override with:

```bash
export SOVEREIGN_CODEX_VAULT_PATH=/path/to/codex-vault
export SOVEREIGN_KILOCODE_VAULT_PATH=/path/to/kilocode-vault
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

Set `SOVEREIGN_AFM_NATIVE_HELPER` to an executable JSON helper to let native
prepare-task/outcome distillation call a local Foundation Models adapter.
Adapter configuration may be indicated with `SOVEREIGN_AFM_ADAPTER_PATH` or
`SOVEREIGN_AFM_ADAPTER_ID`; status reports only `adapterConfigured`, never the
private path.

## Tools

- `sovereign_status`
- `sovereign_prepare_task`
- `sovereign_prepare_outcome`
- `sovereign_route`
- `sovereign_recall`
- `sovereign_learning_quality`
- `sovereign_learn`
- `sovereign_resolve_candidate` — operator-gated resolution for staged learning
  candidates
- `sovereign_vault_write`
- `sovereign_audit_report`
- `sovereign_audit_tail`
- `sovereign_compile_vault` — dry-run AFM compile passes: `session_distillation`, `synthesis`, `procedure_extraction`, `reorganization`, `pruning`
- `sovereign_negotiate_handoff` — agent-to-agent handoff envelope (top recalls, scar tissue, open questions, inbox pointer)
- `sovereign_team_runtime` — temporary team packet with agent profiles, task ledger, hydration packets, gates, and non-goals
- `sovereign_team_evidence` — dry-run evidence report plus promotion candidates; never promotes or learns automatically
- `sovereign_team_promotion` — dry-run permanent-profile draft gated by explicit approval; never writes durable memory

## Sovereign Team Runtime

`sovereign_team_runtime` is a coordinator-side planning surface for short-lived helper agents. It creates:

- temporary profiles with role, focus, ownership, permissions, and recall-only memory policy
- a task ledger with evidence requirements and dependencies
- one hydration packet per temporary agent, built with `sovereign_prepare_task`
- gates and non-goals that keep promotion, learning, and vault writes explicit

`sovereign_team_evidence` is the matching close-out surface. It grades each temporary agent report as `missing`, `partial`, or `complete`, collects blockers, and marks promotion candidates for human review only.

`sovereign_team_promotion` turns a temporary profile plus evidence candidate into a permanent-profile draft only when `approved` is explicitly true. It still does not write durable memory; the returned profile is a review artifact that must be persisted through an intentional profile/write workflow.

The team runtime does not spawn agents, execute background work, write durable memory, or promote profiles automatically.

## Candidate Learning

Durable learning is proposal-first. Ordinary learn calls stage candidate packets
through the daemon instead of silently mutating long-term memory. Operators can
list and resolve candidates through the local console API (`/api/candidates`,
`/api/resolve-candidate`) or the explicit `sovereign_resolve_candidate` tool.

Candidate acceptance writes a durable learning. Rejection, redaction, log-only,
and sensitivity decisions remain auditable without promoting the content into
recall.

## Agent Information Requests

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
- **Team runtime commands**: `team-runtime`, `team-evidence`, and `team-promotion` help coordinate temporary helper agents without automatic learning or promotion.
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

## KiloCode Plugin

Install in KiloCode (local plugin dir):

```bash
kilo plugin install --plugin-dir /path/to/sovereign-memory/plugins/sovereign-memory/.kilocode-plugin
```

The KiloCode surface adds:

- **Vault**: `~/.sovereign-memory/kilocode-vault` (override: `SOVEREIGN_KILOCODE_VAULT_PATH`).
- **Agent identity**: `kilocode` (override: `SOVEREIGN_KILOCODE_AGENT_ID`).
- **Hooks** (`hooks/hooks.json`):
  - `SessionStart` — boots identity, audit tail, pending-inbox learnings.
  - `UserPromptSubmit` — auto-recalls before each turn, injects ranked vault + daemon results.
  - `PreCompact` — captures scar tissue (failed paths, dead ends) so post-compaction KiloCode doesn't repeat them.
  - `Stop` — drafts candidate learnings to vault inbox; never auto-writes.
- **Slash commands** (namespaced as `/sovereign-memory:*`): `recall`, `learn`, `status`, `audit`, `prepare-task`, `prepare-outcome`.
- **Agent-first envelope**: hook output is wrapped as `<sovereign:context version="1" event="..." agent="kilocode" tokens="...">` containing deterministic JSON for prompt-cache stability.

Disable hooks without uninstalling: `export SOVEREIGN_KILOCODE_HOOKS=off`.

The Codex plugin (`.codex-plugin/`), Claude Code plugin (`.claude-plugin/`), Gemini extension (`.gemini-plugin/`), and other integrations (Hermes, OpenClaw) are unaffected — they share the daemon, not the vault.

## Local Console

```bash
npm run console            # tsc + vite build + node dist/ui-server.js
npm run dev:frontend       # vite dev server with /api proxy to :8765 (HMR)
```

The console exposes only local HTTP endpoints:

- `GET /api/health`
- `GET /api/status`
- `GET /api/audit-tail?limit=20`
- `POST /api/prepare-task`
- `POST /api/prepare-outcome`
- `GET /api/candidates`
- `POST /api/resolve-candidate`

The server binds to `127.0.0.1`, refuses non-local bind hosts, rejects non-local host/origin/fetch-metadata requests, requires JSON POST bodies, caps JSON request bodies, redacts machine-local paths in browser-facing status/audit/candidate responses, and does not expose learn or vault-write endpoints. Browser requests cannot override the server-owned vault path or AFM target. `prepare-task` keeps its existing audit behavior; `prepare-outcome` remains dry-run only.

The bridge defaults to `~/.sovereign-memory/codex-vault`. Override with `SOVEREIGN_CODEX_VAULT_PATH=~/.sovereign-memory/claudecode-vault npm run console` to point Recall at a different vault.

## Development

```bash
npm install
npm test
npm run console
npm run design:lint
npm run test:live:prepare
```

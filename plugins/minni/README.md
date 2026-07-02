# Minni Plugin (`minni-multi-plugin`)

Local-first multi-host plugin for Minni. It ships Codex (`.codex-plugin/`),
Claude Code (`.claude-plugin/`), Gemini (`.gemini-plugin/`), and KiloCode
(`.kilocode-plugin/`) surfaces from the same TypeScript MCP server and shared
`dist/` build. Each agent gets its own Obsidian-compatible vault; all agents
share the same local daemon (`minnid`).

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

All paths and env var names below are read in `src/config.ts` (and `src/afm.ts`
for the AFM adapter vars). `MINNI_HOME` overrides the home directory for every
`~/.minni/...` default; all the per-agent vault defaults live under it.

- Minni home: `MINNI_HOME` if set, otherwise `~/.minni`.
- Daemon socket: `MINNI_SOCKET_PATH` if set, otherwise `~/.minni/run/minnid.sock`.
- AFM health URL: `MINNI_AFM_HEALTH_URL` if set, otherwise `http://127.0.0.1:11437/health`.
- AFM prepare-task URL: `MINNI_AFM_PREPARE_TASK_URL` if set, otherwise `http://127.0.0.1:11437/v1/chat/completions`.
- AFM prepare-task model: `MINNI_AFM_PREPARE_TASK_MODEL` if set, otherwise `apple-foundation-models`.
- AFM provider mode: `bridge` by default; set `MINNI_AFM_PROVIDER_MODE=native`, `auto`, or `off` to change opt-in AFM calls.
- Codex vault: `MINNI_VAULT_PATH` (or legacy fallback `MINNI_CODEX_VAULT_PATH`), otherwise `~/.minni/unknown-vault`. The Codex surface normally sets `MINNI_VAULT_PATH=~/.minni/codex-vault` in its manifest env.
- Claude Code vault: `MINNI_CLAUDECODE_VAULT_PATH`, otherwise `~/.minni/claudecode-vault`.
- KiloCode vault: `MINNI_KILOCODE_VAULT_PATH`, otherwise `~/.minni/kilocode-vault`.
- Grok Build vault: `MINNI_GROK_VAULT_PATH`, otherwise `~/.minni/grok-build-vault`.

Override with:

```bash
export MINNI_HOME=~/.minni
export MINNI_VAULT_PATH=/path/to/codex-vault
export MINNI_CLAUDECODE_VAULT_PATH=/path/to/claudecode-vault
export MINNI_KILOCODE_VAULT_PATH=/path/to/kilocode-vault
export MINNI_GROK_VAULT_PATH=/path/to/grok-build-vault
export MINNI_SOCKET_PATH=~/.minni/run/minnid.sock
export MINNI_AFM_HEALTH_URL=http://127.0.0.1:11437/health
export MINNI_AFM_PREPARE_TASK_URL=http://127.0.0.1:11437/v1/chat/completions
export MINNI_AFM_PROVIDER_MODE=native
```

`minni_prepare_task` and `minni_prepare_outcome` also accept
`afmProviderMode` per call (`bridge`, `native`, `auto`, or `off`). `bridge`
preserves the earlier OpenAI-compatible localhost behavior. `native` calls an
executable JSON helper, checks for an Apple Foundation Models backend, and
records sanitized provider metadata (`backend`, `availability`,
`adapterConfigured`) in the packet. `auto` prefers native when healthy and
falls back to bridge. Adapter paths are not returned or sent to the model
prompt.

Set `MINNI_AFM_NATIVE_HELPER` to an executable JSON helper to let native
prepare-task/outcome distillation call a local Foundation Models backend. The
repo ships a compile-safe helper at `engine/native_afm_helper`; callers can
point the plugin at it or at a platform-specific helper with the same JSON
contract. Adapter configuration is indicated with `MINNI_AFM_ADAPTER_PATH` or
`MINNI_AFM_ADAPTER_ID`; status reports only `adapterConfigured`, never the
private path.

### Non-loopback model targets

`MINNI_AFM_ALLOWED_TARGETS` and `MINNI_MODEL_ALLOWED_TARGETS` (provider-protocol
alias) define a comma-separated operator allowlist of non-loopback hosts that
AFM/model calls may target (e.g. `192.168.1.10,afm.internal`). Loopback
(`127.0.0.1`, `localhost`, `::1`) is always allowed; both env vars are honored
as a union. Non-loopback targets additionally require HTTPS. A non-local
target configured without being listed is denied with a structured error.

### Provider chain

`~/.minni/providers.json` (override with `MINNI_PROVIDERS_CONFIG`) configures
the provider chain and per-operation routing policy. `MINNI_AFM_*` env vars
keep precedence over file values. Secrets are never stored in `providers.json`:
cloud credentials come only from `apiKeyEnv` (env var name) or `apiKeyFile` (a
0600 file under `~/.minni/secrets/`). Inline `providers.cloud.apiKey` is
rejected outright and disables the cloud provider.

## Tools

- `minni_status`
- `minni_prepare_task`
- `minni_prepare_outcome`
- `minni_route`
- `minni_recall`
- `minni_drill`
- `minni_export_pack`
- `minni_learning_quality`
- `minni_learn`
- `minni_resolve_candidate` — owner-or-explicit-operator candidate resolution
  for staged learning candidates
- `minni_vault_write`
- `minni_audit_report`
- `minni_audit_tail`
- `minni_compile_vault` — dry-run AFM compile passes: `session_distillation`, `synthesis`, `procedure_extraction`, `reorganization`, `pruning`
- `minni_negotiate_handoff` — agent-to-agent handoff envelope (top recalls, scar tissue, open questions, inbox pointer)
- `minni_ack_handoff`
- `minni_list_pending_handoffs`
- `minni_await_handoff`
- `minni_plan_create` / `minni_plan_update` / `minni_plan_status` / `minni_plan_activate` / `minni_plan_deactivate` / `minni_plan_replan` / `minni_plan_history` / `minni_plan_diff` / `minni_plan_restore` / `minni_plan_scar`
- `minni_team_runtime` — temporary team packet with agent profiles, task ledger, hydration packets, gates, and non-goals
- `minni_team_evidence` — dry-run evidence report plus promotion candidates; never promotes or learns automatically
- `minni_team_promotion` — dry-run permanent-profile draft gated by explicit approval; never writes durable memory
- `minni_ping_agent_request` / `minni_ping_agent_inbox` / `minni_ping_agent_decide` / `minni_ping_agent_status`
- `minni_subscribe_contradictions`

Compatibility aliases for older `sovereign_*` workflows may still resolve to
these tools, but new integrations should use the `minni_*` names above.

## Minni Team Runtime

`minni_team_runtime` is a coordinator-side planning surface for short-lived helper agents. It creates:

- temporary profiles with role, focus, ownership, permissions, and recall-only memory policy
- a task ledger with evidence requirements and dependencies
- one hydration packet per temporary agent, built with `minni_prepare_task`
- gates and non-goals that keep promotion, learning, and vault writes explicit

`minni_team_evidence` is the matching close-out surface. It grades each temporary agent report as `missing`, `partial`, or `complete`, collects blockers, and marks promotion candidates for human review only.

`minni_team_promotion` turns a temporary profile plus evidence candidate into a permanent-profile draft only when `approved` is explicitly true. It still does not write durable memory; the returned profile is a review artifact that must be persisted through an intentional profile/write workflow.

The team runtime does not spawn agents, execute background work, write durable memory, or promote profiles automatically.

## Candidate Learning

Durable learning is proposal-first. Ordinary learn calls stage candidate packets
through the daemon instead of silently mutating long-term memory. Operators can
list and resolve candidates through the local console API (`/api/candidates`,
`/api/resolve-candidate`) or the explicit `minni_resolve_candidate` tool.

Candidate acceptance writes a durable learning. Rejection, redaction, log-only,
and sensitivity decisions remain auditable without promoting the content into
recall.

## Agent Information Requests

- `minni_negotiate_handoff` — runtime-stamped agent-to-agent work-transfer envelope (top recalls, scar tissue, open questions, inbox pointer)
- `minni_ping_agent_request` — create a vault-backed information request contract for another agent
- `minni_ping_agent_inbox` — list this runtime agent's pending and decided request contracts
- `minni_ping_agent_decide` — approve or deny a request addressed to this runtime agent
- `minni_ping_agent_status` — let requester or recipient track the contract lifecycle

## Claude Code Spine

Install in Claude Code (local plugin dir, or via marketplace):

```bash
claude plugin install --plugin-dir /path/to/minni/plugins/minni
```

The Claude Code surface adds:

- **Vault**: `~/.minni/claudecode-vault` (override: `MINNI_CLAUDECODE_VAULT_PATH`). The manifest pins `MINNI_AGENT_ID=claude-code`, `MINNI_VAULT_PATH=~/.minni/claudecode-vault`, and `MINNI_SOCKET_PATH=~/.minni/run/minnid.sock`.
- **Agent identity**: `claude-code` (override: `MINNI_CLAUDECODE_AGENT_ID`).
- **Hooks** (`hooks/hooks.json`):
  - `SessionStart` — boots identity, audit tail, pending-inbox learnings.
  - `UserPromptSubmit` — auto-recalls before each turn, injects ranked vault + daemon results.
  - `PreCompact` — captures scar tissue (failed paths, dead ends) so post-compaction Claude doesn't repeat them.
  - `Stop` — drafts candidate learnings to vault inbox; never auto-writes.
- **Slash commands** (namespaced as `/minni:*`): `recall`, `learn`, `status`, `audit`, `prepare-task`, `prepare-outcome`.
- **Team runtime commands**: `team-runtime`, `team-evidence`, and `team-promotion` help coordinate temporary helper agents without automatic learning or promotion.
- **Agent-first envelope**: hook output is wrapped as `<sovereign:context version="1" event="..." agent="claude-code" tokens="...">` containing deterministic JSON for prompt-cache stability.

Disable hooks without uninstalling: `export MINNI_CLAUDECODE_HOOKS=off`.

The Codex plugin (`.codex-plugin/`), Gemini extension (`.gemini-plugin/`), and other integrations (Hermes, OpenClaw, Grok Build) are unaffected — they share the daemon, not the vault.

Automatic behavior should remain recall-only. `minni_route` can recommend recall/status/audit automatically, but learning and vault writes stay manual and vault-first. `minni_learn` returns a quality report and blocks weak memories by default (`requireQuality` defaults to `true`; pass `requireQuality: false` to store a weak note deliberately).

## Agent Information Requests

Direct cross-agent recall is intentionally not exposed. When one model needs
information from another agent, it must create a pseudo-contract with
`minni_ping_agent_request`. The plugin stamps the sender from the runtime
principal (`MINNI_AGENT_ID`, with `MINNI_CODEX_AGENT_ID` as a Codex-scoped
fallback; default `unknown-agent`), writes a pending contract to the sender
outbox and recipient inbox, and records an audit entry. The request contains
only the question, purpose, TTL, allowed topics, and response cap.

`minni_negotiate_handoff` is kept as a direct work-transfer path: it lets the
runtime agent hand its own task packet to another agent. It may not impersonate a
different sender. If the requested handoff is really asking the target agent to
share its vault, recall, notes, prior handoff, or private context, the server
routes the call into `minni_ping_agent_request` instead of `daemon.handoff`.
This keeps the module boundary explicit: handoff moves caller-owned work context;
ping requests recipient-owned information and requires recipient approval.

The recipient sees requests with `minni_ping_agent_inbox` and decides with
`minni_ping_agent_decide`. Approval requires an explicit answer. Denial requires
no answer. Approved answers are capped and redacted for secret-shaped values and
machine-local paths before syncing back to the requester outbox.
`minni_ping_agent_status` shows the requester or recipient the current lifecycle
state (`pending`, `approved`, `denied`, or `expired`).

Agent vault roots are resolved from `MINNI_VAULT_PATH` (plus the per-agent
`MINNI_<AGENT>_VAULT_PATH` overrides documented above) or the local
`~/.minni/<agent>-vault` default. This keeps identity and storage routing in
config/runtime ownership rather than in model-provided paths.

## KiloCode Plugin

Install in KiloCode (local plugin dir):

```bash
kilo plugin install --plugin-dir /path/to/minni/plugins/minni/.kilocode-plugin
```

The KiloCode surface adds:

- **Vault**: `~/.minni/kilocode-vault` (override: `MINNI_KILOCODE_VAULT_PATH`). The manifest pins `MINNI_AGENT_ID=kilocode`, `MINNI_VAULT_PATH=~/.minni/kilocode-vault`, and `MINNI_SOCKET_PATH=~/.minni/run/minnid.sock`.
- **Agent identity**: `kilocode` (override: `MINNI_KILOCODE_AGENT_ID`).
- **Hooks** (`hooks/hooks.json`):
  - `SessionStart` — boots identity, audit tail, pending-inbox learnings.
  - `UserPromptSubmit` — auto-recalls before each turn, injects ranked vault + daemon results.
  - `PreCompact` — captures scar tissue (failed paths, dead ends) so post-compaction KiloCode doesn't repeat them.
  - `Stop` — drafts candidate learnings to vault inbox; never auto-writes.
- **Slash commands** (namespaced as `/minni:*`): `recall`, `learn`, `status`, `audit`, `prepare-task`, `prepare-outcome`.
- **Agent-first envelope**: hook output is wrapped as `<sovereign:context version="1" event="..." agent="kilocode" tokens="...">` containing deterministic JSON for prompt-cache stability.

Disable hooks without uninstalling: `export MINNI_KILOCODE_HOOKS=off`.

The Codex plugin (`.codex-plugin/`), Claude Code plugin (`.claude-plugin/`), Gemini extension (`.gemini-plugin/`), and other integrations (Hermes, OpenClaw, Grok Build) are unaffected — they share the daemon, not the vault.

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

The bridge defaults to the Codex vault resolved from `MINNI_VAULT_PATH` (or `MINNI_CODEX_VAULT_PATH`). Override with `MINNI_VAULT_PATH=~/.minni/claudecode-vault npm run console` to point Recall at a different vault.

## Development

```bash
npm ci                   # deterministic install from package-lock.json
npm run build            # writes dist/server.js for MCP/plugin manifests
npm test                 # full pipeline: build + node --test suite
npm run test:server      # server/hook tests only (build:server + node --test, no vite build)
npm run test:file tests/hook-behavior.test.mjs   # single test file
npm run typecheck        # tsc --noEmit
npm run lint             # eslint . — lints src/, tests/, and frontend-src/ (built frontend/ is ignored)
npm run coverage         # node --test coverage with line/branch/function floors
npm run console
npm run design:lint      # validate ../../DESIGN.md via the pinned @google/design.md dev dependency
npm run test:live:prepare
```

> `npm install` is only needed when adding/updating dependencies. Use `npm ci`
> for reproducible installs from the committed `package-lock.json`.

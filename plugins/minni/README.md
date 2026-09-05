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

The Vite/React console starts on **Memory Board**. From this directory run
`npm run console`, then open the token-bearing localhost URL printed at startup.
The bearer token protects HTTP access; daemon operations still use the console's
stamped principal. Use the navigation rail to open:

- **Memory Board** — live agent/catalogue state, owner-scoped pending suggestions,
  log-only/quarantine views, and recent recall. A limited page is a lower bound,
  not a fleet-wide total. Refresh and update-time labels expose freshness.
- **Recall / Prepare Packet** — `/api/prepare-task` results and the exact context
  packet. Token allowance is not a measured usage percentage.
- **Dry-run Review** — `/api/prepare-outcome` drafts candidate/log-only/do-not-store
  suggestions; preview decisions do not resolve staged candidates.
- **Handoffs, Vaults, Policy & AFM, Sessions** — live route-backed views with empty,
  unavailable, and error states; these are not placeholder screens. Host delivery
  and actual model availability still need their own verification.
- **Audit / Settings** — activity and runtime status. Observability differs from
  Board candidate resolution: **Approve/Reject performs a real governed decision**.
  Acceptance requires operator authority; owners can reject/redact their own
  candidates under daemon policy.

Two themes ship: **Paper** (default, warm bone + persimmon stamp + verdigris accents) and **Phosphor** (CRT operator board with telemetry rail and live activity stream). Toggle from the gear button bottom-right. Layout sizes and theme persist to `localStorage`.

## macOS Thread claim storage

Thread claims and worker receipts on macOS use a bundled, standard-library Python
helper for descriptor-relative filesystem operations. The helper inherits an
already-open vault descriptor; it does not follow `/dev/fd` child paths or fall
back to mutable logical paths. Python is required only when this storage backend
runs. Interpreter selection is `MINNI_CLAIM_PYTHON`, then `PYTHON`, then `PYTHON3`,
then `python3` on PATH. Set `MINNI_CLAIM_PYTHON` to an absolute interpreter path
when the plugin host has a minimal PATH. No extra Python packages are needed.

The helper runs in isolated Python mode, ships inside the compiled JavaScript
payload, and is reused only within one awaited Thread mutation before being
closed. Each location still closes its child descriptors; separate mutations
never share an idle helper. Missing or failed helpers
fail the claim operation explicitly; a stuck helper is killed after a 10-second
request deadline. Linux continues using its native descriptor-path backend.

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
repo ships a compile-safe helper at `src/minni/native_afm_helper`; callers can
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
- `minni_list_candidates` — list this runtime principal's staged candidates
  (own rows only; defaults to `status=proposed`; redacted/rejected content
  is not returned to the model)
- `minni_resolve_candidate` — owner-or-explicit-operator candidate resolution
  for staged learning candidates. Accept into durable memory still requires
  operator/govern; a platform host may reject/redact its own rows without that
  grant, and must not resolve another principal's rows unless explicitly allowed.
- `minni_vault_write`
- `minni_audit_report`
- `minni_audit_tail`
- `minni_compile_vault` — dry-run AFM compile passes: `session_distillation`, `synthesis`, `procedure_extraction`, `reorganization`, `pruning`
- `minni_negotiate_handoff` — agent-to-agent handoff envelope (top recalls, scar tissue, open questions, inbox pointer)
- `minni_ack_handoff`
- `minni_list_pending_handoffs`
- `minni_await_handoff`
- `minni_thread_create` / `minni_thread_update` / `minni_thread_status` / `minni_thread_activate` / `minni_thread_deactivate` / `minni_thread_replan` / `minni_thread_history` / `minni_thread_revision` / `minni_thread_diff` / `minni_thread_restore` / `minni_thread_scar` / `minni_thread_ready` / `minni_thread_assign` / `minni_thread_claim` / `minni_thread_worker_update` / `minni_thread_events` (16 tools; the pre-rename `minni_plan_*` aliases were removed in v0.5.0 — canonical names only)
- `minni_team_runtime` — project one vault Thread as a Team packet (`plan_id`, `rev`, `readySlices`); leftover `taskLedger` is a view of ready, not a second graph
- `minni_team_evidence` — dry-run evidence report plus promotion candidates; never promotes or learns automatically
- `minni_team_promotion` — dry-run permanent-profile draft gated by explicit approval; never writes durable memory
- `minni_ping_agent_request` / `minni_ping_agent_inbox` / `minni_ping_agent_decide` / `minni_ping_agent_status`
- `minni_subscribe_contradictions`

Compatibility aliases for older `sovereign_*` workflows may still resolve to
these tools, but new integrations should use the `minni_*` names above.

For a complete claim/start/complete sequence and queued-response handling, see
[Run a Thread](../../docs/thread-workflow.md). A lease token can authorize several
mutations until expiry/revocation; a queued completion is not yet applied.

## Minni Team Runtime

`minni_team_runtime` projects one vault Thread for short-lived helper agents. `plan_id` present reads that Thread and fails if it is missing. `plan_id` absent creates one Thread from the task. Ready is the expiry sweep plus `readySlices`. It also returns:

- temporary profiles with role, focus, ownership, permissions, and recall-only memory policy
- leftover `taskLedger`, a view of `ready` keyed by `PlanSlice.id` (not a second graph; `ledgerFor` is gone)
- one coordinator-side hydration packet per temporary agent, built with `prepare_task` (not the worker contract)
- gates and non-goals that keep promotion, learning, and vault writes explicit

The worker contract is library `buildWorkerPacketAfterClaim` after assign → claim, then honesty-only `dispatchWorkerPacket`. Neither is an MCP tool. grok worker-start is missing. Default agy cannot run `minni_thread_worker_update`. Codex dispatch is UNPROVEN and `spawned` is false. G3 daemon relay, automatic spawning, and immediate wake are not implemented.

`minni_team_evidence` is the matching close-out surface. It grades each temporary agent report as `missing`, `partial`, or `complete`, collects blockers, and marks promotion candidates for human review only.

`minni_team_promotion` turns a temporary profile plus evidence candidate into a permanent-profile draft only when `approved` is explicitly true. It still does not write durable memory; the returned profile is a review artifact that must be persisted through an intentional profile/write workflow.

The team runtime does not spawn agents, execute background work, write durable memory, or promote profiles automatically.

## Candidate Learning

Durable learning is proposal-first. Ordinary learn calls stage candidate packets
through the daemon instead of silently mutating long-term memory. Hosts can
list their own staged rows with `minni_list_candidates` and resolve them with
`minni_resolve_candidate`. Operators can also list and resolve through the
local console API (`/api/candidates`, `/api/resolve-candidate`).

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

Authorized shared recall is exposed through `minni_recall` scope and
`cross_agent` options. Shared document eligibility and explicit cross-agent
learning recall have separate gates; this does not grant access to private peer
sessions. For an explicit addressed question, use `minni_ping_agent_request`. The plugin stamps the sender from the runtime
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
npm run test:server      # builds server, board test bundle, frontend, then Node tests
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

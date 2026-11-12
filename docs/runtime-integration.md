# Runtime Integration

Minni can run in three layers:

1. **Python engine**: the code under `src/minni/`, responsible for SQLite, FAISS,
   Repo-First retrieval, Living Repo Maps (`minni map`), typed memory graph &
   supersession, learnings, episodic events, `~/.minni/TODAY.md`, the `minni` CLI,
   and daemon JSON-RPC.
2. **Multi-host plugin**: the TypeScript bridge under `plugins/minni/`,
   connecting Claude Code, Codex, Gemini/Antigravity, Kilo Code, Grok Build, and
   Cursor to Minni through the 12-tool MCP surface and non-blocking host hooks.
3. **Local model services**: optional adjacent services used by an operator,
   including the native Apple Foundation Models JSON helper and the
   OpenAI-compatible localhost bridge. Machine-specific model artifacts,
   adapter bundles, logs, and private datasets stay outside the public
   repository.

## Public Repository Boundary

The repository should contain source code, tests, templates, and integration
instructions. It should not contain:

- SQLite databases or FAISS indexes
- generated TypeScript output or `node_modules`
- Python virtual environments or model caches
- adapter packages such as `.fmadapter`
- launchd plists with local machine paths
- logs, conversation exports, or user-derived training data

## Plugin Bridge

The plugin bridge uses environment-driven defaults:

- `MINNI_SOCKET_PATH` for the Unix socket
- `MINNI_DB_PATH` for the SQLite database
- `MINNI_VAULT_PATH` for markdown/vault reads
- `MINNI_AGENT_ID` for requests without an agent ID
- `MINNI_HOME` for the local runtime root

This keeps the public integration portable while allowing local installs to
preserve compatibility with existing runtime layouts.

## Observability (`minni watch`)

`minni watch` is the engine/CLI live tail: it follows each agent vault's
`log.md` audit trail and the daemon's episodic events (including durable
`recall` traces from raw JSON-RPC `search` calls via the read-only
`list_events` RPC). Use it to verify memory is actually being consulted.

- `MINNI_RECALL_TRACE` — default on; set to `off`/`0`/`false`/`no` to disable
  durable recall-trace inserts for perf-sensitive setups.
- `list_events` is capability-gated under `read` and performs a single
  parameterized SELECT. Cross-agent listing follows the same gate as
  cross-agent recall.

## Status & markdown observability (web console retired)

The localhost React web console (`ui-server` on `127.0.0.1:8765`, formerly started via `npm run console`) was retired in favor of the stdlib-only CLI (`minni status`, `minni activity`, `minni map`, `minni watch`, `minni doctor`), the auto-maintained plain-Markdown `~/.minni/TODAY.md` (`0600`) operator digest, and per-vault Markdown surfaces (`log.md`, `index.md`).

Three signals answer "what are the agents doing and did they actually use Minni?":

- **Daily operator digest (`~/.minni/TODAY.md` & `minni activity`)** — summarizes daemon health, genuine unresolved rule conflicts (`Needs Your Call`), live/recent agent sessions, active Threads and pending handoffs, 24-hour project learnings and scars, and Living Repo Map freshness.
- **Session receipts (`log.md`)** — one boot/stop window per session in each agent's `log.md`, with the receipt tally the Stop hook emits (recalls strong/weak, recall-guard observations, learns, candidates staged) plus 7-day recall-to-use ratios in `minni_status`. An all-zero receipt is itself the answer: the session ran without touching memory.
- **Live activity (`minni watch`)** — tails two lanes in real time: the vault lane (hook and MCP tool entries from `log.md` without materializing vault skeletons) and the daemon lane (episodic events, including `recall` traces from raw JSON-RPC `search` calls, fetched through the read-only `list_events` RPC). Together the lanes cover both hook-wired runtimes and agents speaking JSON-RPC directly.

Cross-agent `list_events` listing follows the same gate as cross-agent recall: a plain principal is scoped to its own stamped agent id, so in a strict-identity home (any `principals/*.json` present) fleet-wide daemon event tailing requires an operator grant (`principals/main.json` with `"read"` and `"govern"`). Candidate review is a separate **governed mutation** via `minni_list_candidates` + `minni_resolve_candidate` (or `minni learn --accept` for operators): accepting into shared durable memory requires operator authority (`resolve_candidate` / `govern`) or per-principal `auto_accept_own`, while an owner may reject or redact its own staged candidate.

## Recall Guard (non-blocking verifiable memory use)

All wired platforms run **strictly non-blocking** `PreToolUse` hooks: Minni never emits `permissionDecision: "deny"` or blocks `Read`, `Grep`, `Glob`, `Bash`, or edit calls on any runtime. When strong recall for the current turn is unconsulted, the file-backed guard (`<vault>/.runtime/recall-state.d/<sessionKey>.json`) claims the arming once and:

- On hosts declaring `preToolUseContext: true` (**Claude Code**), attaches up to 3 top recall hits via `hookSpecificOutput.additionalContext` (`continue: true`) so the tool call proceeds uninterrupted while keeping relevant memories in view.
- On hosts without `preToolUseContext` (**Codex, Gemini/Antigravity, Kilo Code, Grok Build, Cursor**), records non-blocking telemetry in `log.md` and returns `{continue: true}`.

`UserPromptSubmit` skips `<task-notification>`, `<SYSTEM_MESSAGE>`, `[Message] timestamp=`, and `<background-task>` wakeups, and `PostToolUse` restricts failure-lesson checks strictly to file edits and failed commands. Host matcher aliases and injection capabilities are documented in [hook-platforms.md](contracts/hook-platforms.md). Tune with `MINNI_RECALL_GUARD_MODE` (`off` / `soft` / `strict`) and `MINNI_RECALL_POINTER_THRESHOLD` (default `0.55`).

## Local AFM and Extraction

The engine includes local provider helpers for Apple Foundation Models and OpenAI-compatible localhost bridges (`http://127.0.0.1:11437/v1/chat/completions` in bridge mode). Operators can configure provider and background loop behavior with:

- `MINNI_AFM_PROVIDER_MODE` (`off`, `bridge`, `native`, `auto`)
- `MINNI_AFM_MODE`
- `MINNI_AFM_LOOP` (`on` / `off` — background consolidation & distillation loop)
- `MINNI_AFM_INPUT_BUDGET_TOKENS` (proactive chunking budget, default `3200`)
- `MINNI_AFM_ALLOWED_TARGETS`

The daemon entrypoint is:

```bash
minni up   # or: .venv/bin/python -m minni.minnid --socket ~/.minni/run/minnid.sock
```

This keeps extraction and graph-classification code usable (with chunked inputs and deterministic fallbacks when AFM is off or unavailable) while leaving model binaries, adapters, training data, and launchd configuration outside the repository.

Runtime AFM calls use an explicit provider boundary (`off`, `bridge`, `native`, `auto`). The native path talks JSON over stdin/stdout to `src/minni/native_afm_helper`, providing `@Generable` Apple Foundation Models execution where the framework is available. Normalized native operations include:

- `query_expansion` -> `{ "queries": string[] }`
- `neighborhood_summary` -> `{ "summary": string }`
- `hyde_generation` -> `{ "answer": string }`
- `triage` / `judge` / `contradiction` -> structured candidate quality & conflict verdicts
- `entity_extract` / `session_distill` -> structured entities & compaction distillations
- `edge_inference` / `edge_inference_compact` -> typed `memory_links` (`updates`, `contradicts`, `extends`, `relates`)
- `compile_pass_proposals` -> `{ "drafts": [...] }`

Status reports expose provider mode, backend, availability, and boolean configuration flags without emitting private adapter paths. FoundationModels transcripts or asset metadata prove only the local native runtime path that produced them; they do not prove Private Cloud Compute/offload behavior unless Apple documentation or an explicit runtime telemetry signal is cited.

## Multi-Agent Coordination & Repo Orientation

Across Claude Code, Codex, Gemini/Antigravity, Kilo Code, Grok Build, and Cursor, multi-agent work is coordinated through three shipped v1 surfaces:

- **Living Repo Maps & Two-Part Boot Shelf:** `minni map [<repo>]` builds file-and-SHA-pinned Subsystem Map Cards (`~/.minni/vault/maps/<workspace>/` + `repo_map_cards`), delivered automatically in `project_shelf` (`<= 2,500` chars) alongside `layer1/core.md` (up to `24 KB`) on `SessionStart` and `SubagentStart` (with `PostCompact` shelf recovery).
- **Compact Shared Threads (4 MCP tools):** `minni_thread_create`, `minni_thread_status`, `minni_thread_update`, and `minni_thread_scar` maintain shared multi-slice plans (`< 2 KB` compact responses, file-locked slice transitions, per-session active-thread pointers, and workspace scar promotion) without spawning host processes or requiring worker lease tokens.
- **1-Step Cross-Agent Handoffs (2 MCP tools):** `minni_handoff_send` and `minni_handoff_inbox` package bounded task state, cited recalls, and scars across sessions or peer principals under capability-gated leases (`handoff` capability default-deny until granted).

# Runtime Integration

Minni can run in three layers:

1. **Python engine**: the code under `engine/`, responsible for SQLite, FAISS,
   retrieval, learnings, episodic events, daemon JSON-RPC, and graph export.
2. **Multi-host plugin**: the TypeScript bridge under `plugins/minni/`,
   responsible for connecting Codex, Claude, Gemini, Kilo, Grok, and related
   agent surfaces to Minni through a local Unix-socket daemon.
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

## Local AFM and Extraction

The engine includes local provider helpers for OpenAI-compatible model bridges.
By default, bridge mode targets a local Apple Foundation Models style bridge at
`http://127.0.0.1:11437/v1/chat/completions`. Operators can override the
provider behavior with:

- `MINNI_AFM_PROVIDER_MODE`
- `MINNI_AFM_MODE`
- `MINNI_AFM_HEALTH_URL`
- `MINNI_AFM_PREPARE_TASK_URL`

The daemon entrypoint is:

```bash
engine/.venv/bin/python engine/minnid.py --socket ~/.minni/run/minnid.sock
```

This keeps extraction code usable while leaving model binaries, adapters,
training data, and launchd configuration outside the repository.

Runtime AFM calls now use an explicit provider boundary rather than assuming
only the localhost bridge. Supported modes are `off`, `bridge`, `native`, and
`auto`. The native path talks JSON over stdin/stdout to an executable helper,
with `engine/native_afm_helper` providing a compile-safe Apple Foundation Models
implementation where the framework is available. The bridge path remains for
compatibility.

The normalized native operation contracts are:

- `query_expansion` -> `{ "queries": string[] }`
- `neighborhood_summary` -> `{ "summary": string }`
- `hyde_generation` -> `{ "answer": string }`
- `prepare_task` -> `{ "brief": string, "recommendedNextActions": string[], "risks": string[] }`
- `prepare_outcome` -> `{ "outcomeDraft": { ... } }`
- `compile_pass_proposals` -> `{ "drafts": [...] }`

Status reports expose provider mode, backend, availability, and a boolean
adapter-configured flag. They do not emit private adapter paths.

FoundationModels transcripts or asset metadata prove only the local native
runtime path that produced them. They do not prove Private Cloud Compute/offload
behavior. Treat PCC/offload claims as unverified unless Apple documentation or
an explicit runtime telemetry/API signal is cited in the same finding.

## Team Runtime

The Codex/Claude/Gemini/Kilo plugin exposes a coordinator-side Minni Team Runtime
for temporary helper agents:

- `sovereign_team_runtime` builds temporary profiles, a task ledger, hydration
  packets, gates, and non-goals.
- `sovereign_team_evidence` summarizes helper-agent reports and identifies
  promotion candidates for human review.

This layer is deliberately non-executing. It does not spawn agents, write
durable learnings, promote profiles, or bypass cross-agent vault boundaries.
Each hydration packet is derived from `sovereign_prepare_task`, so the same
recall-only default and public repository boundary apply.

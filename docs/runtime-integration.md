# Runtime Integration

Sovereign Memory can run in three layers:

1. **Python package core**: the installable `sovereign_memory` package under
   `src/`, responsible for SQLite, FAISS, retrieval, learnings, episodic
   events, and graph export.
2. **OpenClaw extension**: the optional TypeScript bridge under
   `integrations/openclaw-extension/`, responsible for connecting OpenClaw
   agents to Sovereign Memory through a local Unix-socket daemon.
3. **Local model services**: optional adjacent services used by an operator,
   such as an Apple Foundation Models bridge for structured extraction. These
   services should be configured by environment variables and kept outside the
   public repository when they contain machine paths, model artifacts, adapter
   bundles, logs, or private datasets.

## Public Repository Boundary

The repository should contain source code, tests, templates, and integration
instructions. It should not contain:

- SQLite databases or FAISS indexes
- generated TypeScript output or `node_modules`
- Python virtual environments or model caches
- adapter packages such as `.fmadapter`
- launchd plists with local machine paths
- logs, conversation exports, or user-derived training data

## OpenClaw Bridge

The OpenClaw extension uses environment-driven defaults:

- `SOVEREIGN_SOCKET_PATH` for the Unix socket
- `SOVEREIGN_DB_PATH` for the SQLite database
- `SOVEREIGN_VAULT_PATH` for markdown/vault reads
- `SOVEREIGN_DEFAULT_AGENT_ID` for requests without an agent ID
- `SOVEREIGN_PYTHON` for the process supervisor
- `OPENCLAW_HOME` for optional flat-file mirroring

This keeps the public integration portable while allowing local installs to
preserve compatibility with existing runtime layouts.

## Local Extraction

The package includes `sovereign_memory.extraction`, a small stdlib-only client
for local OpenAI-compatible model bridges. By default it targets a local Apple
Foundation Models style bridge at `http://127.0.0.1:11437/v1/chat/completions`.
Operators can override the endpoint and model with:

- `SOVEREIGN_EXTRACTOR_URL`
- `SOVEREIGN_EXTRACTOR_MODEL`
- `SOVEREIGN_EXTRACTOR_TIMEOUT`

The CLI entrypoint is:

```bash
sovereign-memory extract ./session.md
sovereign-memory extract ./session.md --learn-agent hermes --durable-only
```

This keeps extraction code usable while leaving model binaries, adapters,
training data, and launchd configuration outside the repository.

## Team Runtime

The Codex/Claude/Kilo plugin exposes a coordinator-side Sovereign Team Runtime
for temporary helper agents:

- `sovereign_team_runtime` builds temporary profiles, a task ledger, hydration
  packets, gates, and non-goals.
- `sovereign_team_evidence` summarizes helper-agent reports and identifies
  promotion candidates for human review.

This layer is deliberately non-executing. It does not spawn agents, write
durable learnings, promote profiles, or bypass cross-agent vault boundaries.
Each hydration packet is derived from `sovereign_prepare_task`, so the same
recall-only default and public repository boundary apply.

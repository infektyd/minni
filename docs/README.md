# ᛗ Minni documentation

Minni is a local-first, multi-agent memory daemon: one governed `minnid` process,
per-agent Markdown vaults, and an MCP surface shared across agent runtimes.
Start with the [README](../README.md) for the pitch and Quickstart; this tree is
the reference.

| Page | What it covers |
|---|---|
| [Concepts](concepts.md) | The four verbs (recall → learn → approve → handoff), two-tier storage, the governance gate, evidence enveloping, the AFM pass pipeline |
| [Install & troubleshooting](install.md) | Supported install paths, `minni doctor`, the Docker eval image, daemon lifecycle, `make sync-root`, common failures |
| [Runtimes](runtimes/) | Per-runtime wiring: [Claude Code](runtimes/claude-code.md), [Codex](runtimes/codex.md), [Gemini / Antigravity](runtimes/gemini.md), [Grok](runtimes/grok.md), [Kilo Code](runtimes/kilocode.md), [Cursor](runtimes/cursor.md) |
| [Run a Thread](thread-workflow.md) | MCP walkthrough, lease credentials, queued versus applied completion, and recovery |
| [Architecture](architecture.md) | Request flow, components, data model, core invariants, the literal MCP tool list |
| [Security model](security.md) | Local-first boundaries and how the threat model is enforced |
| [Contracts](contracts/) | The agent, capability, vault, workflow, and threat-model contracts |
| [Deploy / root sync](../deploy/README.md) | `make sync-root`, D7 fleet partition (wire vs propagate), `deploy.stale` / `plugin_dist` |
| [Ops notes](ops/) | Operator-facing notes (e.g. Grok local-path retirement, reviewer app) |

Operational references that predate this tree and remain canonical:
[TROUBLESHOOTING.md](TROUBLESHOOTING.md), [VAULT_INGEST.md](VAULT_INGEST.md),
[runtime-integration.md](runtime-integration.md),
[native-afm-implementation-note.md](native-afm-implementation-note.md).

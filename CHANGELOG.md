# Changelog

All notable changes to Minni are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/). Minni is
pre-1.0: minor versions may contain breaking changes until v1.0.0.

## [Unreleased]

## [0.1.0] - 2026-07-02

First tagged release. Minni has been developed in the open since April 2026;
this entry summarizes the system as it stands rather than replaying every
commit.

### Added

- **minnid daemon** (`engine/minnid.py`): asyncio JSON-RPC 2.0 over a Unix
  domain socket at `~/.minni/run/minnid.sock` (0600 socket in a 0700 run dir),
  with SQLite (FTS5, WAL) + FAISS storage, schema migrations, and an
  observability surface (`status`, `health_report`, `hygiene_report`, `ping`).
- **Two-tier memory**: per-agent Markdown vaults (`<agent>-vault/wiki/**`)
  with a personal index (`.index/vault.db` + FAISS) plus a shared
  `~/.minni/minni.db` for durable learnings and pooled documents; recall
  merges both legs with provenance markers.
- **Retrieval**: lexical (BM25/FTS5) + vector search with cross-encoder
  reranking, optional NLI claim-attribution scoring, and evidence enveloping —
  recalled memory is cited as evidence, never injected as instruction.
- **Governed learning lifecycle**: proposal-first candidates with
  accept / reject / redact / merge / supersede resolution and an on-disk audit
  trail; identity-and-capability gating (EffectivePrincipal) on durable writes
  and cross-agent operations.
- **Cross-agent handoffs** with leases, and durable, evidence-gated plans that
  survive sessions and compaction.
- **MCP plugin** (`plugins/minni`, TypeScript): one server surface with
  per-runtime adapters for Claude Code, Codex, Gemini/Antigravity, and
  KiloCode, plus lifecycle hooks and skills; OpenClaw bridge under
  `openclaw-extension/`.
- **Security hardening** per `SECURITY_PLAN.md` (SEC-001…SEC-022), including
  socket permissions, path safety, injection detection/perturbation for
  instruction-like content, and redacted health reporting.
- **membench** (`bench/`): deterministic, offline benchmark harness with a
  byte-reproducible Layer-1 scorecard (fixture corpus only; no headline
  benchmark numbers are published).
- **CI**: hermetic clean-runner smoke (`scripts/repro-smoke.sh`) proving
  daemon start, migrations, status and recall round-trips, and home-directory
  isolation on every push.
- **Packaging & docs** (this release effort): `minni` CLI
  (`up`/`down`/`status`/`doctor`), `engine/pyproject.toml`, a uv-compiled
  lockfile, uv-managed interpreter provisioning in `make setup`, first-run
  model-download notices, contributor/security hygiene files, and a rewritten
  README with a `docs/` tree.

[Unreleased]: https://github.com/infektyd/minni/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/infektyd/minni/releases/tag/v0.1.0

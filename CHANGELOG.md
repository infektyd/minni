# Changelog

All notable changes to Minni are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/). Minni is
pre-1.0: minor versions may contain breaking changes until v1.0.0.

## [Unreleased]

## [0.3.0] - 2026-07-04

### Added

- **`minni wire <platform>`** ([#142](https://github.com/infektyd/minni/issues/142),
  [#144](https://github.com/infektyd/minni/pull/144)): agents wire themselves
  from the wheel-shipped plugin payload — no repo checkout, no Node at
  wheel-build time. Versioned installs under `~/.minni/plugin/<version>/`
  with locked atomic install, post-wire verification probes (MCP handshake,
  hook dry-run, config readback), `wired.json` wire records, reference-aware
  PEP 440 garbage collection, `--from-repo` dev builds
  (`<version>+git.<sha>[.dirty]`), `--use-version` rollback, and a JSON
  stdout / exit-code contract. `all` expands to codex, claude-code, kilocode,
  grok; gemini wiring stays provisional. The payload ships inside wheels
  from this release (`make stage-payload` + `make release-wheel`, wired into
  the release workflow).
- `make check-versions` CI lint: pyproject, plugin package.json, and the four
  platform manifests must agree; version-pinned path literals in propagate.py
  fail the build.

### Fixed

- **Learn quality gate flags credential material, not vocabulary**
  ([#138](https://github.com/infektyd/minni/issues/138),
  [#146](https://github.com/infektyd/minni/pull/146)): notes about `id-token`
  permissions, tokenizers, or key-hygiene procedures are learnable again,
  while well-known secret prefixes, keyword-assigned literals (tiered
  high-risk/lower-risk rules), and high-entropy pastes hard-block. Hardened
  through six automated review rounds; the one regex-unreachable case
  (unquoted multi-word passphrases) is tracked in
  [#147](https://github.com/infektyd/minni/issues/147).
- propagate.py stale `0.1.0` path/version literals now resolve dynamically
  (the `current` symlink is authoritative over the installed package version).
- TOML config writers escape control characters, preventing corruption of
  `~/.codex/config.toml` / `~/.grok/config.toml` from hostile or unusual
  workspace/socket values.

## [0.2.0] - 2026-07-03

### Changed

- **Packaging restructure**: the flat `engine/` tree became the `src/minni/`
  package ([#135](https://github.com/infektyd/minni/pull/135)) — Minni is now
  a real wheel, installable with `pipx install minni` (daemon + CLI, no
  checkout), publishing to [PyPI](https://pypi.org/project/minni/) via OIDC
  trusted publishing from tagged builds.
- Docs sweep for the pipx era; Docker eval-image CI fix.

### Added

- Gemini / Antigravity `agy` CLI hook support
  ([#133](https://github.com/infektyd/minni/issues/133)).

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

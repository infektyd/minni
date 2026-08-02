# Changelog

All notable changes to Minni are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/). Minni is
pre-1.0: minor versions may contain breaking changes until v1.0.0.

## [Unreleased]

### Fixed

- **Learn-gate semantic tier observability (#237 / SEC-G6):**
  `LearningQualityReport` now carries `semanticTier: "ran" | "unavailable" |
  "skipped"` so audit records can distinguish "AFM examined and cleared" from
  "AFM never ran / off / failed" and from "tier not invoked". Fail-open
  behavior is unchanged; only observability is added.
- **POLICY.md §2 redaction contract (#237 / SEC-G7):** docs no longer claim a
  MUST-redact guarantee for JSON-quoted secrets or all absolute paths. §2.1/§2.2/§2.3
  are marked PARTIAL and aligned with `redaction.py` plus
  `THREAT_MODEL.md` residual language; bare plist/socket names are not claimed
  as rewritten.
- **Learn-gate unknown AFM verdict fail-open:** non-enumerated classifier
  returns no longer hard-block with `semanticTier: "ran"`; they fail-open as
  `unavailable` (credential / prose / unavailable remain explicit).

## [0.4.1] - 2026-08-01

### Added

- **`readme-audit` skill** (`plugins/minni/skills/readme-audit/`): a repo skill
  any agent can run to check the README against the actual code and land the
  fixes as a PR. Verifies every factual claim and every Mermaid node/edge with
  `file:line` evidence (verdicts CORRECT / STALE / UNVERIFIABLE), sweeps
  CHANGELOG, merged PRs, `docs/`, and the skill/tool registries for capabilities
  that should be represented but are not, and applies a three-part worthiness bar
  so the README does not accrete. Enforces a per-section line/word/row budget
  with a total cap below the sum of the sections, plus a retirement rule
  (one-in-one-out, prioritised destinations, no silent deletion, protected floor
  for the honest caveats) — measured mechanically by
  `scripts/readme_budget.py`, which also counts diagram nodes and edges against
  their own ceiling. Layout redesign proposals are produced every run and never
  auto-applied. `references/budget-rationale.md` documents the numbers and
  `references/example-audit-2026-08-01.md` is a real run against this repo's
  README (67 items checked; 3 stale, 4 unverifiable).

### Changed

- **`minni:plan` is now `minni:threads`**: the 11 plan tools are renamed
  `minni_plan_*` → `minni_thread_*` (create, update, scar, status, replan,
  history, revision, diff, restore, activate, deactivate), and the slash
  command `/minni:plan` becomes `/minni:threads`. The envelope keys injected
  into agent context are renamed outright — `active_plan` → `active_thread`
  and `active_plan_ref` → `active_thread_ref` — with no dual key: nothing
  parses them (they are read by the model as prose) and duplicating the
  pointer would spend up to ~12% of the small-context envelope budget for no
  consumer benefit. The key regenerates from on-disk state on the next hook
  fire, so the migration is self-healing.

  **Deprecation window:** all 11 old `minni_plan_*` names stay registered as
  aliases bound to the canonical handler, each with a description leading
  `DEPRECATED — renamed to <new>`. They will be removed in the release after
  next; a server registers 48 tools while the aliases are live (37 canonical
  + 11 aliases), returning to 37 afterwards.

  **Frozen on purpose — the rename is tool/command-layer only.** The `plan-`
  artifact id prefix, the `plan_id` parameter name, the `plan.*` shared-gate
  operation strings, the `minni_plan: true` / `plan_*` frontmatter keys, the
  `_active_plan.json` pointer filename and the `hook_active_plan_error` audit
  tool name are all unchanged. Those strings are baked into existing vault
  filenames, `[[plan-<hex>]]` wikilinks, journals and audit history; renaming
  them would split the vault's id space and orphan every inbound wikilink.
  A regression test (`freeze guard` in `tests/plan.test.mjs`) fails loudly if
  someone later "finishes" the rename.

### Fixed

- **Inert inbox files now self-archive** — the AFM consolidation tick sweeps
  stop-candidate files whose every candidate ingest rejects (audit echo /
  `log_only` / `do_not_store` / blank) into `<inbox>/.archive/`
  (`archive_inert_files` in `afm_passes/inbox_archive.py`; rename only, never
  an unlink). Such files could never earn a `candidate_packets` row, so the
  archive-on-resolution lifecycle could never reclaim them: 107 echo-only
  files written by pre-#201 hook builds accumulated across claudecode/grok-build
  vaults, re-surfacing in every SessionStart pending-inbox count as apparent
  "triage" work that was Minni's own telemetry all along. No TTL: the verdict
  is a pure function of the write-once file content. Kill switch:
  `afm_loop_schedule.passes.consolidation.archive_inert_inbox` (default on);
  observable via the `inbox_inert_archived_total` counter and an INFO log line
  per sweep. `_agent_mismatch` files still drain through `inbox_quarantine`,
  non-stop kinds and unparseable files are untouched.
- **Layer 1 identity workspace is now seeded**: `bootstrap-vault` creates
  `<vault>/layer1/` with `core.md` and `budget.md` from in-repo templates
  parameterized by agent id, vault path, workspace, and socket path. The Layer 1
  contract assumes every vault carries an agent-curated durable workspace — read
  first on wake, kept under a strict 4096-token budget — but nothing ever created
  it, so only the two hand-seeded vaults had one and the doctrine pointed at
  files that did not exist. Seeding is idempotent: these files are agent-owned
  living state the agent rewrites during every distill, so existing files are
  never overwritten. Layer 1 is seeded before `distill/`, so a fresh vault's
  gauges report the identity workspace as present. Re-run `bootstrap-vault
  --agent <id>` to backfill an older vault; `--workspace` records the agent's
  primary workspace.
- **Distill ritual artifacts are now seeded**: `bootstrap-vault` (and therefore
  `update-plugin`) creates `<vault>/distill/` with `mode` (default `explicit`),
  `gauges.md`, and `ritual.md` from in-repo templates parameterized by agent id.
  The Minni Distill Ritual V1 tells the agent to read `distill/gauges.md` first
  at any wind-down signal, but nothing ever created those files, so every vault
  ran the ritual blind against a missing meter. Seeding is idempotent: `mode`
  and `gauges.md` are operator-owned living state and `ritual.md` accumulates
  traces, so existing files are never overwritten. Re-run `bootstrap-vault
  --agent <id>` to backfill an older vault. Wheel `package-data` and
  `stage_payload` globs now include the distill templates so pip installs ship
  them.

## [0.4.0] - 2026-07-30

### Added

- **Compaction-summary harvest**
  ([#194](https://github.com/infektyd/minni/pull/194),
  [#196](https://github.com/infektyd/minni/pull/196)): platform compaction
  summaries are harvested raw at the hook (Claude Code: PostCompact primary
  delivery with a transcript-tail backstop on SessionStart resume/compact;
  KiloCode: `session.compacted` SDK read-back), stored to the agent's vault
  inbox as `kind: compact_summary` with content-sha1 dedup, and distilled on
  the daemon's AFM consolidation timer (`compact_distillation` pass, flag
  `distill_compact_summaries`). Audience routing keeps session-specific
  context out of the shared pool: shared-knowledge sections (Key Technical
  Concepts, Errors and fixes, Problem Solving, Learnings, Decisions) become
  governance candidates stamped `audience: shared`; everything else is
  written only to a personal vault session note (`audience: personal`,
  never a shared learning). AFM distillation is spent on shared sections
  only.
- **Memory Board console**: the frontend is now an infinite-canvas Memory
  Board with live daemon data, auto/custom free-layout zones, a live Staged
  learnings zone, real traffic pulses, console auth flow, and candidate
  status expansion (`log_only`, `do_not_store`)
  ([#150](https://github.com/infektyd/minni/pull/150),
  [#151](https://github.com/infektyd/minni/pull/151),
  [#152](https://github.com/infektyd/minni/pull/152)).
- **Console observability**: `/api/events` with fleet audit-tail plus
  Sessions and live Audit screens
  ([#181](https://github.com/infektyd/minni/pull/181)); session receipts
  and `/api/sessions` ([#179](https://github.com/infektyd/minni/pull/179));
  engine watch/list_events/recall_trace slice
  ([#177](https://github.com/infektyd/minni/pull/177)).
- **PreToolUse on all platforms**: Codex, Grok, and KiloCode hook manifests
  now wire PreToolUse ([#178](https://github.com/infektyd/minni/pull/178)).
- **Deployment vintage**: deployed plugin builds declare their source
  commit, and `scripts/check_deployments.py` reports drift loudly instead
  of silently serving stale code.
- **Learn gate AFM enhancement for unquoted multi-word passphrases**
  ([#147](https://github.com/infektyd/minni/issues/147)): when the regex
  material detector is inconclusive on a high-risk keyword assigned an
  unquoted multi-word value (`password: correct horse battery staple` vs
  `password: use a manager`), `minni_learn` / `minni_learning_quality` /
  `cli quality` run a local AFM semantic classification. A `credential`
  verdict hard-blocks; `prose` and AFM-unavailable/off fail open (regex
  remains the fast path — the gap remains when AFM cannot classify).
  Passphrase text is never echoed into quality warnings.
  ([#147](https://github.com/infektyd/minni/issues/147),
  [#182](https://github.com/infektyd/minni/pull/182))

### Fixed

- **P0 recall blackout** ([#168](https://github.com/infektyd/minni/pull/168)):
  recall could return nothing on a populated DB — fixed scope-out, dead
  encoder detection, strict-AND FTS fallback, and vault_write identity.
  Follow-on hardening series: process-wide `vault_fts` schema gate ending
  the vtable DDL race, honest daemon health (`status` pid/started_at,
  dynamic VERSION, counter deltas + health_flags), quarantine drain with a
  one-time migration for unresolvable inbox residue, plugin AFM probe
  matching the daemon's budget (ending the false-negative `afm.ok`
  contradiction), and temp-agent daemon recall delegated to the
  coordinator's principal.
- **minnid fd exhaustion under multi-agent load**
  ([#190](https://github.com/infektyd/minni/pull/190)): shared database
  connections and RPC thread hygiene stop the daemon exhausting file
  descriptors; the launchd template raises the fd floor to 8192.
- **Workspace identity**: workspace id derives from the git repo root
  instead of a static default
  ([#184](https://github.com/infektyd/minni/pull/184)); stop-time telemetry
  self-feeding severed and workspace identity hardened
  ([#174](https://github.com/infektyd/minni/pull/174)); hooks apply per
  PLATFORM contract, not per agent
  ([#175](https://github.com/infektyd/minni/pull/175)); Cursor hooks
  sole-fire via a User wrapper
  ([#183](https://github.com/infektyd/minni/pull/183)).
- **Security slice**: session pages are agent-scoped with fail-closed
  platform vault roots, AFM passes bind to the caller principal and fail
  closed for non-owners, writers stamp explicit privacy (with NULL-backlog
  unparking), the Codex hook gets its own native identity (`CODEX_*`
  env), and workflow tokens run least-privilege
  ([#167](https://github.com/infektyd/minni/pull/167) follow-ups).
- **Daemon robustness**: concurrent daemon initialization race fixed;
  schema race on first boot fixed
  ([#158](https://github.com/infektyd/minni/pull/158)); vault indexes stay
  current from inside minnid; the cursor vault slug maps correctly with the
  two slug maps locked together.

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
  [#147](https://github.com/infektyd/minni/issues/147) (AFM enhancement
  tier landed in Unreleased — fail-open when AFM cannot classify).
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
- **Security hardening** per `docs/archive/SECURITY_PLAN.md` (SEC-001…SEC-022), including
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

[Unreleased]: https://github.com/infektyd/minni/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/infektyd/minni/releases/tag/v0.4.1
[0.1.0]: https://github.com/infektyd/minni/releases/tag/v0.1.0

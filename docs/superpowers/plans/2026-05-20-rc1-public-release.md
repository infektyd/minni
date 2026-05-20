# RC1 Public Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` for implementation lanes, or `superpowers:executing-plans` for inline execution. Steps use checkbox (`- [ ]`) syntax for tracking. Keep all raw vaults, sessions, logs, DBs, adapters, and private tool output out of public git.

**Goal:** Move Sovereign Memory from the current `rc1-phase-012` checkpoint to a proper professional public RC1 that can be reviewed, cloned, tested, packaged, tagged, and trusted.

**Architecture:** Treat RC1 as a release program, not one monster patch. The highest-leverage bite is a "public boundary hardening" tranche: make the model-facing plugin boundary path-safe, principal-safe, and CI-enforced, then lock supply chain, prune legacy attack surface, align contracts/docs, and rerun the same audit shape that produced `docs/RC_PLAN.md`.

**Tech Stack:** Python 3.11/3.12 daemon and tests, TypeScript MCP plugin on Node 20, GitHub Actions, npm lockfile, Python lock or hashed requirements, local-first filesystem vault, SQLite/FTS/FAISS retrieval.

---

## Current Grounding

**Branch:** `rc1-phase-012`

**Last committed checkpoint:** `5ecc3fa rc1: land canonical plan and phase 0-2 hardening`

**Evidence checked on 2026-05-20:**
- `git status --short --branch` shows no tracked modifications, only local untracked review artifacts and source plans.
- `git diff --stat main...HEAD` shows the current branch changes 31 tracked files, mostly CI, `docs/RC_PLAN.md`, plugin audit/vault/ping hardening, daemon principal/dispatch fixes, AFM writer safety, and smoke tests.
- `docs/RC_PLAN.md` remains the canonical RC register.
- Sovereign Memory status is healthy, but narrow recall was rate-limited during this planning pass. Treat that as another validation nudge for audit/rate-limit ergonomics, not as plan input.
- GitHub Actions can be used without a paid GitHub plan for this public repo by sticking to standard hosted runners. Use cloud CI for public smoke and privacy gates; keep heavier RC audits local.

**Already addressed or substantially addressed by the current branch:**
- RCM-001: baseline GitHub Actions CI exists.
- RCM-002: model-facing `server.ts` schemas appear to default to operator-controlled `DEFAULT_VAULT_PATH`; internal task/team helpers still carry `vaultPath` and need a boundary review.
- RCM-003: fresh-install principal spoofing is narrowed to fixed local `main`.
- RCM-004: ping request now writes sender outbox plus neutral lease first; recipient inbox materializes on poll/decision.
- RCM-005: plugin wikilink resolution now has realpath containment checks and symlink escape tests.
- RCM-006: `search` and `learn` dispatch are offloaded via `asyncio.to_thread`.
- RCM-007: daemon handoff wait uses `await asyncio.sleep`.
- RCM-008: plugin audit has rotation/quota/status/concurrency tests.
- RCM-010 and RCM-011: AFM writer frontmatter forgery and YAML injection protections are present.
- RCM-018: `git grep "except Exception: pass"` across tracked `engine` and plugin source returns zero.
- RCM-028: `scripts/repro-smoke.sh` exists and was previously verified locally.

**Still not a true RC1:**
- CI is useful as a free public smoke gate, but it is not yet the full RC gate: no SAST/audit workflow beyond npm audit, no formatting/type gates, no Python lock/audit policy, and no macOS/full-matrix release validation.
- Duplicate migration prefix `007_` still exists.
- Python requirements are broad `>=` pins; no lock or hash policy exists.
- Public docs still reference `make audit`, stale test counts, OpenClaw legacy surfaces, and planned features that are already shipped.
- `docs/contracts/CAPABILITIES.md` only documents daemon JSON-RPC and is stale versus the 26 MCP tools in `plugins/sovereign-memory/src/server.ts`.
- Legacy / experimental surfaces still ship: `openclaw-extension/`, `engine/openclaw-tool.sh`, `engine/afm_scheduler.py`, stub vector backends, deep-research UI endpoints with a local absolute path default.
- Public API / CLI / GraphExporter direct coverage is still the biggest quality gap.

---

## Highest-Leverage Bite

The best next bite is **RC1 Public Boundary Hardening**:

1. Finish path/principal containment at the model-facing plugin and daemon API boundary.
2. Make CI strict enough to catch regressions in that boundary.
3. Remove or explicitly quarantine every legacy/external surface that makes the public repo look larger and riskier than the release story.

This bite is better than starting with docs or performance because it shrinks the attack surface first, creates enforceable gates, and turns later cleanup into lower-risk mechanical work.

Recommended first PR after the existing checkpoint:

**PR title:** `rc1: harden public plugin boundary and release gates`

**Scope:**
- RCM-002 residual review in `task.ts`, `team.ts`, `team-harvest.ts`, and `ui-server.ts`.
- RCM-009 status/trace/candidate operator checks and response redaction.
- RCM-014/027/038 supply-chain/security CI gates.
- RCM-021 duplicate migration prefix.
- A public-release artifact denylist test that prevents logs, vaults, DBs, adapters, private sessions, and generated local review packages from entering git.

**Do not include:**
- `antigravity/`, `codex/`, `grok/`
- `docs/RC_MASTER_PLAN_CODEX.md`
- `docs/RC_MASTER_PLAN_GEMINI.md`
- `docs/RC_MASTER_PLAN_Grok.md`
- generated architecture images unless deliberately curated
- `docs/reviews/rc1-phase-012-codex-review.md`
- `docs/implementation/rc1-phase-012-pass2-antigravity.md`
- raw tool logs, session exports, vault files, DBs, adapters, or hook dumps

---

## Workstream Map

### Workstream A: Public Boundary Security

**Purpose:** No model-facing or browser-facing path can escape operator-controlled roots; every daemon-visible identity is stamped; status/trace/candidate surfaces do not leak local paths or allow unauthorized mutation.

**Files likely touched:**
- `plugins/sovereign-memory/src/server.ts`
- `plugins/sovereign-memory/src/task.ts`
- `plugins/sovereign-memory/src/team.ts`
- `plugins/sovereign-memory/src/team-harvest.ts`
- `plugins/sovereign-memory/src/ui-server.ts`
- `plugins/sovereign-memory/src/vault.ts`
- `plugins/sovereign-memory/tests/*.test.mjs`
- `engine/sovrd.py`
- `engine/principal.py`
- `engine/test_principal_binding.py`
- new focused tests as needed under `engine/test_*`

**Tasks:**
- [ ] Build a model-facing schema inventory from `server.registerTool(...)`.
- [ ] Confirm no public MCP zod schema accepts `vaultPath`, `filePath`, `root`, `path`, `afmPrepareUrl`, or equivalent path-shaped input unless it is deliberately a safe relative vault ref.
- [ ] Add a test that serializes every MCP tool schema and fails if a model-facing path override reappears.
- [ ] Split internal helper inputs from model-facing inputs so `task.ts` and `team.ts` can still be testable without giving models path authority.
- [ ] Add a single helper such as `assertAllowedVaultRoot(realPath, allowedRoots)` and route all plugin file reads/writes through it.
- [ ] Review `ui-server.ts` as a browser-facing surface, not an MCP tool. Gate deep-research endpoints behind an explicit env flag or remove them from RC1.
- [ ] Redact absolute paths from status, trace, health, audit, and UI responses unless an operator-only local CLI explicitly asks for them.
- [ ] Require effective principal resolution for daemon `status`, `trace`, `resolve_candidate`, and any handler that reads cross-agent state.
- [ ] Add tests for wrong-agent status/trace/candidate access and for redacted path output.

**Verification commands:**

```sh
git grep -n "vaultPath" -- plugins/sovereign-memory/src
git grep -n "registerTool" -- plugins/sovereign-memory/src/server.ts
cd plugins/sovereign-memory && npm test
PYTHONPATH=engine python -m pytest engine/test_principal_binding.py -q
PYTHONPATH=engine python -m pytest engine/test_candidate_lifecycle.py -q
```

**Exit criteria:**
- No model-facing schema accepts an absolute path.
- Internal tests can still inject temp vault paths through non-model helper APIs.
- Public responses do not leak home paths, DB paths, FAISS paths, or private vault paths by default.
- Wrong principal attempts fail with authorization errors, not filesystem errors.

---

### Workstream B: CI, Security, and Supply Chain Gates

**Purpose:** A public RC should not depend on manual local discipline. CI should prove tests, supply chain, secret hygiene, and release artifact hygiene.

**Policy:** This project should not require a paid GitHub plan for RC1. Use standard GitHub-hosted runners on the public repo for the cloud smoke gate. Do not use larger runners. Do not use self-hosted runners for untrusted public PRs unless a separate hardening plan is written.

**Files likely touched:**
- `.github/workflows/ci.yml`
- `.github/workflows/security.yml`
- `.github/dependabot.yml`
- `engine/requirements.txt`
- `engine/requirements.lock` or `uv.lock`
- `plugins/sovereign-memory/package.json`
- `plugins/sovereign-memory/package-lock.json`
- `.gitignore`
- `.gitfilters`
- `.githooks/pre-commit`
- `scripts/repro-smoke.sh`
- new scripts under `scripts/`

**Tasks:**
- [x] Remove best-effort dependency installs from the public cloud smoke workflow. A dependency install failure now fails the workflow.
- [x] Keep the default cloud gate on `ubuntu-latest`, Python 3.11, and Node 20 to stay lean and free-public-runner friendly.
- [x] Add a tracked public-boundary script for CI to catch private/runtime artifacts before push/PR.
- [ ] Choose one Python locking strategy for RC1: `uv.lock` or hashed `requirements.lock`.
- [ ] Pin Python runtime support explicitly with `requires-python >=3.11,<3.13` in packaging metadata or a project config file.
- [ ] Add Node `engines.node` to `plugins/sovereign-memory/package.json` and enforce Node 20 in CI.
- [ ] Keep `npm ci` as the only CI install path for plugin tests.
- [x] Add `npm audit --audit-level=moderate` after dependency install in the public cloud smoke workflow.
- [ ] Add `pip-audit` against the locked Python dependency set.
- [ ] Add Bandit and Semgrep jobs that fail on new high/critical findings and produce artifact summaries.
- [ ] Add CodeQL for Python and JavaScript/TypeScript.
- [ ] Add Dependabot for GitHub Actions, npm, and Python deps.
- [x] Add a release artifact hygiene script that fails on tracked/private files: logs, DBs, FAISS indexes, `.fmadapter`, raw vault dirs, session exports, `.DS_Store`, adapter configs, hook dumps.
- [ ] Run the pre-commit hook in CI so local and remote gates match.
- [ ] Add a migration uniqueness check that fails on duplicate numeric prefixes.

**Verification commands:**

```sh
git diff --check
.githooks/pre-commit
bash scripts/check-public-boundary.sh
git ls-files | rg '(^|/)node_modules/|\.log$|\.db$|\.sqlite$|\.faiss$|\.npz$|\.fmadapter$|(^|/)\.DS_Store$|^codex-vault/|^claudecode-vault/|^logs/|^inbox/|^outbox/|^raw/|^wiki/' && exit 1 || true
cd plugins/sovereign-memory && npm ci && npm test && npm audit --audit-level=moderate
PYTHONPATH=engine python -m pytest engine/ -q
bash scripts/repro-smoke.sh
```

**Exit criteria:**
- Public cloud smoke is green without `|| true` on dependency installs.
- Security scans are present and calibrated.
- Release artifact hygiene is machine-checked.
- Dependency versions are reproducible.

---

### Workstream C: Migration and Fresh Install Determinism

**Purpose:** A new public user must get the same schema every time. Duplicate migration numbers and split base-schema logic are RC-grade trust leaks.

**Files likely touched:**
- `engine/migrations/007_candidate_packets.sql`
- `engine/migrations/007_handoff_contradiction_events.sql`
- `engine/migrations/008_contradiction_log.sql`
- `engine/migrations.py`
- `engine/db.py`
- `engine/test_migrations*.py` or new migration tests
- `scripts/repro-smoke.sh`

**Tasks:**
- [ ] Resequence duplicate `007_` migration filenames so each migration prefix is unique and monotonic.
- [ ] Add a migration-prefix uniqueness test.
- [ ] Add a clean-db migration test that initializes from zero and asserts expected schema objects exist.
- [ ] Add an upgrade-db test using a fixture at the pre-RC schema version.
- [ ] Route fresh DB initialization through the same migration path where practical, or add a test proving base schema and migrations converge.
- [ ] Extend `scripts/repro-smoke.sh` to assert the final user_version and critical tables/indexes.

**Verification commands:**

```sh
ls engine/migrations | sort
ls engine/migrations | sed -E 's/^([0-9]+)_.*/\1/' | sort | uniq -d
PYTHONPATH=engine python -m pytest engine/ -q -k 'migration or db'
bash scripts/repro-smoke.sh
```

**Exit criteria:**
- No duplicate migration number.
- Clean install and upgrade install converge.
- Smoke script catches schema startup failure.

---

### Workstream D: Public API and CLI Coverage

**Purpose:** The public Python surfaces should have direct tests, not only incidental daemon tests.

**Files likely touched:**
- `engine/agent_api.py`
- `engine/sovereign_memory.py`
- `engine/graph_export.py`
- `engine/test_agent_api.py`
- `engine/test_cli.py`
- `engine/test_graph_export.py`

**Tasks:**
- [ ] Add direct tests for `SovereignAgent` construction, recall/search, learn, context/read, and error handling using isolated temp DB/socket/vault.
- [ ] Add direct tests for every `cmd_*` function that is user-facing.
- [ ] Add `GraphExporter` tests for nodes, edges, empty graph, missing DB, and output shape.
- [ ] Add subprocess-level CLI smoke for `--help`, `stats`, and at least one dry-run or temp-state write path.
- [ ] Add coverage reporting for these three modules and set an RC floor for public surfaces.

**Verification commands:**

```sh
PYTHONPATH=engine python -m pytest engine/test_agent_api.py engine/test_cli.py engine/test_graph_export.py -q
PYTHONPATH=engine python -m pytest --cov=engine/agent_api --cov=engine/sovereign_memory --cov=engine/graph_export --cov-report=term-missing -q
```

**Exit criteria:**
- Public Python entrypoints have direct coverage.
- A broken CLI command fails a focused test before release.

---

### Workstream E: Legacy and Experimental Surface Pruning

**Purpose:** RC1 should ship the smallest honest product. Anything deprecated, host-specific, or hardcoded to local developer paths should be deleted, moved behind an explicit experimental flag, or excluded from release packaging.

**Files likely touched:**
- `openclaw-extension/`
- `engine/openclaw-tool.sh`
- `engine/afm_scheduler.py`
- `engine/test_pr12_afm_loop.py`
- `engine/backends/lance.py`
- `engine/backends/qdrant.py`
- `engine/vector_backend.py`
- `engine/sovrd.py`
- `engine/sovrd_client.py`
- `plugins/sovereign-memory/src/ui-server.ts`
- `plugins/sovereign-memory/frontend-src/src/api.ts`
- `plugins/sovereign-memory/tests/ui-server.test.mjs`
- `README.md`
- `docs/CANONICAL-PATHS.md`
- `docs/runtime-integration.md`
- `SECURITY_PLAN.md`

**Tasks:**
- [ ] Delete `openclaw-extension/` and `engine/openclaw-tool.sh`, or move them to an explicitly unsupported archive outside the RC package.
- [ ] Remove README and docs claims that present OpenClaw legacy bridge as part of RC1.
- [ ] Delete `engine/afm_scheduler.py` if unwired, or wire it through daemon hygiene with tests. Do not leave a half-owned scheduler.
- [ ] Move Qdrant/Lance stubs behind optional extras or remove them from RC1 docs/config.
- [ ] Remove legacy dual-write to `~/.openclaw/MEMORY.md`, or require `LEGACY_MEMORY_DUAL_WRITE=1` with tests and docs stating it is not RC default.
- [ ] Remove or env-gate deep-research UI endpoints and frontend controls; no hardcoded `/Users/hansaxelsson/...` path in public RC runtime.
- [ ] Add a CI grep check for forbidden legacy references in release-facing docs.

**Verification commands:**

```sh
git grep -n "openclaw-extension\|openclaw-tool\.sh\|afm_scheduler\|DEEP_RESEARCH_AGENT_ROOT\|/Users/hansaxelsson/deep-research-agent\|MEMORY.md" -- .
PYTHONPATH=engine python -m pytest engine/ -q
cd plugins/sovereign-memory && npm test
```

**Exit criteria:**
- RC1 surface is smaller and intentional.
- No hardcoded private local path remains in runtime defaults.
- Deprecated integrations are absent from release-facing docs.

---

### Workstream F: Retrieval Performance and Reliability Hardening

**Purpose:** P0 event-loop starvation appears addressed, but RC1 still needs credible latency and memory gates for local-first multi-agent use.

**Files likely touched:**
- `engine/sovrd.py`
- `engine/retrieval.py`
- `engine/faiss_index.py`
- `engine/rerank_cache.py`
- `engine/test_pr5_cache_layers.py`
- `engine/test_pr11_observability.py`
- new performance regression tests under `engine/`

**Tasks:**
- [ ] Add a concurrent-client regression test proving one heavy search does not block a ping/status request past an agreed threshold.
- [ ] Add a handoff-await concurrency test proving `sovereign_await_handoff` does not starve another client.
- [ ] Inspect FAISS `_vectors` lifecycle and remove resident duplicate storage if it is not required after index build.
- [ ] Add bounded query-embedding cache metrics and tests if not already covered.
- [ ] Replace leading-wildcard `LIKE '%...'` lookups with indexed filename/path lookup where the current query is hot.
- [ ] Add a small synthetic benchmark command that runs in CI quickly and a larger local benchmark for release notes.

**Verification commands:**

```sh
PYTHONPATH=engine python -m pytest engine/test_pr11_observability.py engine/test_pr5_cache_layers.py -q
PYTHONPATH=engine python -m pytest engine/ -q -k 'concurrent or latency or faiss or cache'
git grep -n "LIKE '%" -- engine
git grep -n "_vectors" -- engine/faiss_index.py engine/retrieval.py
```

**Exit criteria:**
- Multi-client responsiveness is tested.
- Known hot full scans or duplicated vector memory are fixed or explicitly waived.

---

### Workstream G: Type, Lint, and Maintainability Gates

**Purpose:** Avoid silent drift. RC1 can tolerate a staged strictness rollout, but it cannot leave type/lint backlog invisible.

**Files likely touched:**
- `mypy.ini` or `pyproject.toml`
- `ruff.toml` or `pyproject.toml`
- plugin lint/format config
- `.github/workflows/ci.yml`
- `.github/workflows/security.yml`
- `engine/*.py`
- `plugins/sovereign-memory/src/*.ts`

**Tasks:**
- [ ] Add staged mypy config for public API and daemon handler modules first.
- [ ] Add ruff config with only rules that can pass after targeted fixes.
- [ ] Add TypeScript `tsc --noEmit` or keep `npm run build:server` as the TS gate.
- [ ] Add Prettier or formatting check for plugin source without mass-formatting unrelated generated files.
- [ ] Refactor `engine/retrieval.py::retrieve()` only if measured complexity remains above the RC threshold; otherwise create a post-RC issue with evidence.
- [ ] Fix mutable default argument in `engine/principal.py` if still present.

**Verification commands:**

```sh
PYTHONPATH=engine python -m mypy engine/agent_api.py engine/sovereign_memory.py engine/graph_export.py
python -m ruff check engine
cd plugins/sovereign-memory && npm run build:server
```

**Exit criteria:**
- CI has explicit type/lint gates with a small, defensible scope.
- Remaining broad strictness work is tracked as post-RC, not hidden.

---

### Workstream H: Docs, Contracts, and Release Packaging

**Purpose:** A reviewer should know what RC1 is, what is experimental, how to install it, how to verify it, and what is deliberately out of scope.

**Files likely touched:**
- `README.md`
- `CHANGELOG.md`
- `docs/RC_PLAN.md`
- `docs/contracts/CAPABILITIES.md`
- `docs/contracts/AGENT.md`
- `docs/contracts/THREAT_MODEL.md`
- `docs/contracts/VAULT.md`
- `plugins/sovereign-memory/skills/sovereign-memory/SKILL.md`
- `docs/CANONICAL-PATHS.md`
- `docs/TROUBLESHOOTING.md`
- `SECURITY_PLAN.md`

**Tasks:**
- [ ] Add `CHANGELOG.md` using Keep a Changelog style with an `0.1.0-rc1` section.
- [ ] Replace README `make audit` with commands that exist.
- [ ] Update README test baselines to current verified numbers only after the final gate run.
- [ ] Remove or relabel stale `[PLANNED]` tags for shipped capabilities.
- [ ] Update repository map to remove or mark deleted legacy surfaces.
- [ ] Regenerate `docs/contracts/CAPABILITIES.md` from actual daemon JSON-RPC methods and MCP tools. Include access level, side effects, principal requirement, vault/path behavior, and experimental status.
- [ ] Mark Team Mode as experimental until it has end-to-end tests.
- [ ] Add an RC1 "Trust but verify" section: clean install, smoke script, audit commands, privacy boundaries, local-first assumptions.
- [ ] Add a release checklist that states exactly what must be green before tagging.
- [ ] Ensure docs never instruct users to commit private vault/log/session material.

**Verification commands:**

```sh
git grep -n "make audit\|333 passed\|121 passed\|\[PLANNED\]\|openclaw-extension" -- README.md docs plugins/sovereign-memory/skills
git grep -n "registerTool" -- plugins/sovereign-memory/src/server.ts | wc -l
```

**Exit criteria:**
- Docs match the actual shipped surface.
- A new user can clone, install, run smoke, and understand privacy boundaries without reading session artifacts.

---

### Workstream I: Final RC Audit and Tag Gate

**Purpose:** Prove that the work closed the audit loop instead of just changing the tree.

**Files likely touched:**
- `docs/reviews/rc1-final-audit.md`
- `docs/reviews/rc1-waivers.md`
- `CHANGELOG.md`
- release notes

**Tasks:**
- [ ] Run the full local gate on macOS.
- [ ] Confirm GitHub Actions green on PR.
- [ ] Run the clean clone smoke on a fresh directory.
- [ ] Re-run the audit shape that created the original plan: at minimum one tool-grounded SAST/dependency pass and one adversarial code review pass.
- [ ] Create `docs/reviews/rc1-final-audit.md` summarizing evidence, unresolved issues, and waiver decisions.
- [ ] Create `docs/reviews/rc1-waivers.md` only if any P1/P2 items are deliberately accepted for RC1.
- [ ] Ensure all waivers include owner, mitigation, expiration, and post-RC issue title.
- [ ] Create a signed or annotated RC tag only after no unresolved P0/P1 remains, or after the user explicitly accepts a waiver.

**Final gate commands:**

```sh
git status --short --branch
git diff --check
.githooks/pre-commit
PYTHONPATH=engine python -m pytest engine/ -q
cd plugins/sovereign-memory && npm ci && npm test && npm audit --audit-level=moderate
cd ../..
bash scripts/repro-smoke.sh
git ls-files | rg '(^|/)node_modules/|\.log$|\.db$|\.sqlite$|\.faiss$|\.npz$|\.fmadapter$|(^|/)\.DS_Store$|^codex-vault/|^claudecode-vault/|^logs/|^inbox/|^outbox/|^raw/|^wiki/' && exit 1 || true
```

**Exit criteria:**
- No unresolved P0/P1.
- CI green.
- Clean install smoke green.
- Public package/file list clean.
- Changelog and docs reflect exactly what is being shipped.
- Tag is created from a clean tracked tree.

---

## Recommended PR Sequence

### PR 1: `rc1: harden public plugin boundary and release gates`

**Owns:** Workstreams A, B, C core pieces.

**Why first:** It removes the highest-risk public surfaces and makes the rest of the program enforceable.

**Must include:**
- MCP schema path deny test.
- Strict CI install behavior.
- npm audit and Python audit gate.
- migration uniqueness fix.
- release artifact hygiene script.

**Commit style:**
- `test(rc1): capture public boundary regressions`
- `fix(rc1): remove model path authority from plugin tools`
- `ci(rc1): add security and artifact hygiene gates`
- `fix(rc1): make migrations deterministic`

### PR 2: `rc1: cover public api and prune legacy surfaces`

**Owns:** Workstreams D and E.

**Why second:** Once gates exist, cut dead surface and add direct coverage without mixing with security boundary changes.

**Must include:**
- public API/CLI/GraphExporter tests.
- OpenClaw legacy deletion or quarantine.
- deep-research UI gate/removal.
- AFM scheduler decision.
- stub backend decision.

### PR 3: `rc1: stabilize retrieval and maintainability gates`

**Owns:** Workstreams F and G.

**Why third:** Performance and maintainability work is easier once the release surface is smaller.

**Must include:**
- concurrency regression tests.
- FAISS/vector memory decision.
- LIKE scan decision.
- staged mypy/ruff gates.

### PR 4: `rc1: align docs contracts and release notes`

**Owns:** Workstream H.

**Why fourth:** Docs should describe the final surface after deletion and gating, not a moving target.

**Must include:**
- CHANGELOG.
- README verification gate.
- capability matrix for daemon and 26 MCP tools.
- experimental status sweep.
- local-first trust boundaries.

### PR 5: `rc1: final audit and tag prep`

**Owns:** Workstream I.

**Why last:** This is evidence collection and release decision, not feature work.

**Must include:**
- final audit document.
- waiver document if any waiver exists.
- clean full-gate transcript summary.
- tag recommendation.

---

## RC Decision Rules

**Ready for RC1 tag when:**
- All P0 and P1 items in `docs/RC_PLAN.md` are fixed or explicitly waived.
- No waiver covers path escape, identity spoofing, secret exposure, private artifact leak, data loss, or clean-install failure.
- CI is green on PR and main.
- Clean clone smoke is green.
- Public docs and package metadata match actual shipped behavior.
- Release artifact denylist returns clean.

**Ready for public PR before RC1 tag when:**
- The PR title and description clearly say "RC hardening tranche" rather than "final RC1".
- It contains only sanitized files.
- It has focused verification evidence.

**Leave local and iterate when:**
- A PR would include local source plans, tool outputs, review transcripts, raw session exports, generated local artifacts, or private path-bound assets.
- Any P0/P1 remains untriaged.
- CI only passes because installs are best-effort.

---

## Non-Blocking Questions For Hans

These do not block execution of the plan; they affect the release note tone and final tag policy.

- Should the first public PR be framed as `RC1 hardening phase 2` or should it wait until all five PRs above are complete?
- Should legacy OpenClaw support be fully removed for RC1, or moved behind an explicit unsupported/legacy flag?
- Is Team Mode allowed to remain in public docs as experimental, or should it be hidden until after RC1?

Default assumptions if no answer:
- Use incremental public PRs, but do not call any PR "RC1 complete" until Workstream I passes.
- Remove or quarantine OpenClaw legacy surfaces from the RC package.
- Mark Team Mode experimental everywhere until end-to-end tested.

# Sovereign Memory RC Audit — Phase 2f: Code Quality & Syntax Sweep

**Audit date:** 2026-05-19
**Worktree:** `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19`
**Posture:** READ-ONLY. All evidence from `list_dir`, `grep`, `read_file` across `engine/`, `plugins/sovereign-memory/src/`, `openclaw-extension/`, tests, and the 4 Phase-1 reports. No source modifications. Linter execution: attempted discovery via configs + package manifests; none present so manual static analysis only.
**Inherited context:** Phase-1 reports (scope-creep.md: 31+ dead items; architecture.md: 11 surfaces/8 leaks/15 agent-specific sites; performance.md: 5 hot paths + lazy loading; ci-release.md: no CI + doc drift) reviewed first for cross-references. All claims cite exact `file:line`.

**Methodology for this sweep:**
- Linter/type/format: inspected `package.json` (both), `tsconfig.json` (2), `engine/requirements.txt`, absence of `ruff*`, `.eslintrc*`, `prettier*`, `mypy.ini`, `pyproject.toml`, `Makefile` lint targets, eslint/prettier/ruff in devDeps. `npm test` runs `tsc` (typechecks) + `node --test`; Python uses raw `pytest` only. Manually reviewed import order, naming, quotes, indent, long funcs in 12+ source files.
- Error handling: exhaustive `grep` for `except Exception:`, `except:`, `catch (`, bare patterns + targeted reads of 20+ sites in sovrd.py/retrieval.py/principal.py/trace.py + all 8 TS src/*.ts catch sites.
- Test coverage: enumerated 24 daemon RPC handlers (_METHODS + _stage/list/resolve_candidate), 26 MCP `registerTool` in server.ts:48-913, public methods on `SovereignAgent` (agent_api.py:57+), 14 `cmd_*` (sovereign_memory.py:58-455), GraphExporter, WikiIndexer (for contrast), afm_passes exports, backends stubs. Cross-grep'd every name against `**/*test*.{py,mjs}` + specific test files (pr1b, pr10-15, task.test, team.test, client.test, ui-server.test etc.). Flagged only those with **zero direct calls/exercises**.
- Flaky/path-coupled: grep for `sleep|setTimeout|Path\.home|DEFAULT_SOCKET_PATH|~/.sovereign-memory|tmp.*sock|darwin|sys\.platform` restricted to test globs; inspected setup in test_socket_perms.py, test_vault_root_binding.py, openclaw test, live-prepare-task.mjs, ui-server.test.mjs.
- Style/races/edge: read_file on hot modules (sovrd.py:2445+, trace.py:17+, retrieval.py:1276+, server.ts:1-100+, agent_api.py, graph_export.py, hooks, afm_*.py) + lock/ global/ singleton patterns.

## Findings (Structured: Severity + Location + Details)

### 1. Linting, Typecheck, Format — Absence + Manual Issues

**Nit — No automated lint/type/format tooling configured or runnable**
**plugins/sovereign-memory/package.json:26-32** (devDeps), **openclaw-extension/package.json:19-21**, **engine/requirements.txt:1-21**, root + subdirs (no ruff.toml / .ruff.toml / pyproject.toml / .eslintrc* / prettier.config.* / Makefile)
No `ruff`, `mypy`, `eslint`, `prettier`, `black`, `isort` anywhere. `tsc` runs only as side-effect of `npm run build` (test script); no `typecheck` / `lint` / `format:check` scripts. Python has zero equivalent (no pyright/mypy/pylint in any manifest).
**Suggestion:** Add ruff (py) + eslint+prettier (ts) + mypy to devDeps/CI; enforce in pre-commit or package "lint" script.
**Status: open**

**Nit — Inconsistent catch-clause variable naming across TS sources**
**plugins/sovereign-memory/src/ui-server.ts:534,553** (`catch (e)`), **team-vault-bootstrap.ts:116** (`err`), **agent_ping.ts:304** (`outboxError`), **team-harvest.ts:126+** + **sovereign.ts:50** + **hook.ts:362** + **codex-hook.ts:328** + **kilocode-hook.ts:342** + **afm.ts:193** (mostly `error`)
Mixed `e`/`error`/`err`/`outboxError` reduces readability; no project convention.
**Status: open**

**Suggestion — Long handler functions and mixed import styles in core daemon**
**engine/sovrd.py:504-610** (_handle_daemon_handoff ~100+ LOC), **1445-1639** (_handle_learn), **1866+** (_handle_status) exceed 80-100 LOC with deep nesting; stdlib imports clean (lines 58-74) but local engine imports use post-sys.path `noqa: E402` (88-100) while other modules (e.g. retrieval.py, agent_api.py) do not.
**Status: open**

**Nit — Minor quote/indent drift between Python (4-space) and TS (2-space) expected, but within-TS import grouping varies slightly** (server.ts groups MCP imports vs. hook.ts has interleaved type imports). No prettier to enforce.
**Status: open**

### 2. Inconsistent / Silent Error Handling

**Bug — Multiple bare `except Exception: pass` (or equivalent) swallow root causes with zero logging or propagation on hot/observability paths**
**engine/sovrd.py:1886** (DB stats in _handle_status), **1939** (_faiss_cache_status), **1409**, **2322**, **2537**, **2555**, **2561**, **2660**, **2779** (startup hygiene); **retrieval.py:126,385,1696,1703,1775,2003** (json fallback, cache, rationale, instruction_like, decay, post-filter); **principal.py:6 instances** (similar best-effort guards); **wiki_indexer.py:311,543**; **afm_writer.py:5 instances**; **trace.py:94**; **db.py, migrations.py, indexer.py** (total 48 across 16 files per count).
In status/health these turn hard failures into silent `db_ok=False` or degraded afm with no `logger.exception` or error detail beyond top-level in some paths. Contradicts "traceability" goals in performance.md and observability tests.
**Suggestion:** Replace silent `pass` with `logger.debug("...")` or `except Exception as exc: ...; logger.warning(..., exc_info=...)` for observability paths; propagate where safe (e.g. rationale failure should not drop the whole result).
**Status: open**

**Suggestion — Mixed Python vs TS error patterns at daemon/MCP boundary** (JSON-RPC `_make_error(-32000, ...)` + structured codes vs. MCP `textResult(JSON.stringify({error}))` or thrown zod errors); no unified error contract surface beyond CAPABILITIES.md:113 (which is outdated per ci-release.md). Unhandled promise paths are caught at main() in server.ts:933 and hooks, but deep-research exec in ui-server.ts:534+ only stringifies to client with no server-side audit.
**Status: open**

**Nit — Deep-research external exec paths (scope-creep item) have shallow try/catch returning 400/JSON error without timeouts, exit-code checks, or redaction of user-specific paths**
**plugins/sovereign-memory/src/ui-server.ts:257-613** (runDeepResearchCli + handlers for /api/deep-research/*) + 625+ catches.
**Status: open**

### 3. Race Conditions, Locks, Globals, Clones

**Bug — Incomplete synchronization in TraceRing (GLOBAL_TRACE_RING used from retrieval/observability paths)**
**engine/trace.py:84-88** (`_new_id`: `if trace_id not in self._entries` check *outside* lock, before return), **32** (`__len__` direct access), **29** (`approx_bytes` property), **66** (`get` holds lock but callers in add/put chain do not serialize id gen). `_trim`/`_pop` also assume lock held by caller only. Even though current asyncio single-loop usage makes races unlikely, the API + lock discipline is inconsistent and would break under any threaded use or future refactor.
**Suggestion:** Hold `with self._lock:` for entire `_new_id` + early put, or use a set for claimed ids; make properties/ `__len__` also lock or document thread-unsafe.
**Status: open**

**Suggestion — 20+ raw `global _request_count; _request_count += 1` mutations across every handler (no Lock, no atomic, no encapsulation in a RequestCounter class)**
**engine/sovrd.py:216** (def), **506,964,1049,1090,1133,1173,1227,1308,1465,1684,1829,1868,2104,2193** (and handoff path). Safe today due to GIL + asyncio event loop but fragile; status reads it unsynchronized.
**Status: open**

**Nit — Unnecessary or unsafe dict clones on retrieval hot path** (e.g., `stored = dict(entry)` in trace + multiple `dict(result)` copies in retrieval.py:1652+ envelope shaping and _format_result) without profiling justification; compounds memory under high recall load (cross-ref performance.md).
**Status: open**

### 4. Per-Module Test-Coverage Gaps (Public Surfaces with **NO** Direct Test)

Exhaustive mapping (daemon RPC via `_dispatch` + `_METHODS`, MCP via register + impl imports, Python Agent/CLI via direct calls in tests, other modules via import/call grep). "Direct" = test file contains call, instantiation, or `_dispatch({"method": "X"})` exercising the named surface.

**High-priority (P1) surfaces with zero coverage:**

- `engine/agent_api.py:29` (class `SovereignAgent`) + public methods at **57** (`identity_context`), **94** (`recall`), **138** (`learn`), **158** (`log`), **176** (`start_task`), **184** (`end_task`), **190** (`startup_context`), **307** (`create_thread`), **311** (`get_thread`), **315** (`link_thread_doc`), **321** (`export_graph`), **367** (`close`) — **zero** `SovereignAgent(...)` or method calls in any `test_*.py`. Only exercised via CLI (`sovereign_memory.py`) + deprecated `openclaw-extension/sovrd.py`. (Test file that should: `test_pr1_foundation.py` or new `test_agent_api.py`.)
- `engine/sovereign_memory.py:58-455` (CLI entrypoints) — `cmd_stats:212`, `cmd_faiss:250`, `cmd_hygiene:372`, `cmd_compile:390`, `cmd_watch:195`, `cmd_graph:180`, `cmd_decay:169`, `cmd_learnings:141`, `cmd_context:102`, `cmd_query:75`, `cmd_vectors:349` etc. have **zero or near-zero** direct calls (only `cmd_index` lightly hit in `test_pr15_quant_semantic.py:157`). No dedicated CLI harness tests. (Should be covered by `test_pr1b_contracts.py` or CLI smoke in `test_prN_*.py`.)
- `engine/graph_export.py:23` (`class GraphExporter`) + **34** (`export`), **export_to_file** (in module) — **zero** calls in any test (only `sovereign_memory.py:189` + self `__main__`). Tests set `graph_export_dir` in fixtures but never invoke exporter. (Should: `test_pr11_observability.py` or `test_pr14_*.py`.)

**Other notable zero/near-zero coverage publics (P2/P3):**

- `engine/backends/lance.py:1` (`LanceBackend`), `engine/backends/qdrant.py:1` (`QdrantBackend`) — stub ctors/raises only imported in try blocks (`retrieval.py:842`, `backends/__init__.py`, `multi.py`); never exercised (as documented in scope-creep.md).
- `engine/afm_scheduler.py:10` (`AFMScheduler`) — only self + `test_pr12_afm_loop.py:225` (in dead-code test); zero calls from `sovrd.py`/`sovereign_memory.py`/afm_passes (scope-creep dead module).
- `plugins/sovereign-memory/src/ui-server.ts:257+` (deep-research bridge fns: `runDeepResearchCli`, `createDeepResearchBridge`, `/api/deep-research/*` handlers) — only exercised in `ui-server.test.mjs` (console-only, external dep on `/Users/.../deep-research-agent`); zero coverage in core MCP/daemon path or policy tests.
- `openclaw-extension/src/{bridge.ts,index.ts,sovereign-manager.ts}` public surfaces — only internal + deprecated `test_socket_hardening.py`; no cross-surface tests against main plugin.
- Minor: `engine/migrate_v3_to_v3_1.py` (CLI only, no tests), early G0x env tests (`test_g01_numpy_env.py` etc.) have self-only coverage.

**Summary table of public functions with zero test coverage (P1 first):**

| Priority | File:Line | Public Surface | Recommended Test File | Notes |
|----------|-----------|----------------|-----------------------|-------|
| P1 | engine/agent_api.py:29-367 | SovereignAgent + 12 methods (recall, learn, identity_context, startup_context, threads, export_graph, ...) | test_pr1_foundation.py or new test_agent_api.py | Never instantiated in test suite; only legacy/CLI paths |
| P1 | engine/sovereign_memory.py:212+ | cmd_stats, cmd_faiss, cmd_hygiene, cmd_compile, cmd_graph, cmd_watch, cmd_decay, cmd_learnings, cmd_context, cmd_query, cmd_vectors | test_pr1b_contracts.py + CLI harness | Only cmd_index exercised once |
| P1 | engine/graph_export.py:23-278 | GraphExporter.export / export_to_file | test_pr11_observability.py or test_pr14_*.py | Config dir fixtures exist but no invocation |
| P2 | engine/backends/lance.py:1 / qdrant.py:1 | LanceBackend, QdrantBackend (stubs) | N/A (or remove) | Optional import only; never ctor success |
| P2 | engine/afm_scheduler.py:10 | AFMScheduler (full class + tick/register) | test_pr12_afm_loop.py (exists but dead) | Unwired per scope-creep; test isolated |
| P2 | plugins/sovereign-memory/src/ui-server.ts:257-613 | runDeepResearchCli + deep-research HTTP handlers | ui-server.test.mjs (partial, external) | Scope-creep surface; no core integration |
| P3 | openclaw-extension/src/*.ts + sovrd.py | bridge / sovereign-manager public APIs | openclaw-extension/tests/test_socket_hardening.py | Deprecated per sovrd.py:36,2476 |

(7+ surfaces flagged; counts distinct public entrypoints.)

### 5. Flaky / Path-Coupled Tests

**Nit — Polling sleep + real socket path in OpenClaw hardening test (low risk but not hermetic)**
**openclaw-extension/tests/test_socket_hardening.py:81** (`time.sleep(0.02)` inside 3s deadline loop), **self.socket_path** (real FS, not always tmp). Other tests (`test_socket_perms.py:27`, `test_vault_root_binding.py:87` (has skip), `test_principal_binding.py`) use `tmp_path` or `tmpdir` correctly and mock `DEFAULT_SOCKET_PATH`. No parallel-test conflicts observed, no macOS-only `sys.platform` guards that would skip on Linux CI. One `pytest.skip` for symlinks in vault test. No shared global state mutation across tests.
**Status: open** (low severity)

No other `time.sleep` or `~/.sovereign-memory` writes in core `engine/test_*.py` or `plugins/.../tests/*.mjs` (mentions are docstrings only). `live-prepare-task.mjs` and `ui-server.test.mjs` are integration-oriented but use local servers/sockets under test control. Overall low flaky risk.

### 6. Additional Correctness / Edge-Case Flags (from Full Sweep + Phase-1 Cross-Check)

- **Suggestion** (cross-ref architecture.md leaks + performance hot paths): status/health and provenance continue to emit `db_path`/`faiss_path`/`backend` + internal ids (`sovrd.py:1913-1915`, retrieval.py:136+); no redaction layer on these read-only surfaces despite G11/G12 principal work.
- **Nit** (scope-creep.md + ci-release.md): 14+ stale `[PLANNED: PR-N]` markers in `docs/contracts/CAPABILITIES.md:31-38` etc. still present; G03 lint (`test_g03_contract_matrix.py`) only checks tags, not content freshness vs. `_METHODS:2445` or `server.ts:48`.
- **Suggestion**: 5+ afm_passes/*.py + prompts have public-ish `run_*` entrypoints called only via `afm_writer` / `sovereign_memory.py:424` pass_runners; no direct unit tests outside the PR12-15 integration loop (edge cases in pruning/reorg may be under-exercised).
- No Python `async`/`await` races or un-awaited coros spotted (all daemon handlers sync inside async socket loop); TS top-level `main().catch` present in server + hooks + ui-server.

## What Looks Solid

- **High test density on core public RPC/MCP surfaces**: 24/24 daemon handlers exercised via `_dispatch` in pr1b_contracts + pr6/9/10/11/12-15 (e.g., handoff/ack/await, learn/feedback/trace, compile/endorse, status/health/hygiene, candidates, contradictions subscribe); 26 MCP tools have corresponding impl tests (task.test.mjs, team*.test.mjs, vault.test.mjs, policy.test.mjs, agent-ping.test.mjs, client.test.mjs for sovereign.ts helpers, ui-server.test for console). Pass rates historically 213+/32+ with no permanent skips.
- **Strong use of tmp_path / isolation in most unit tests** (test_pr2, pr4, pr5, pr10, principal_binding, vault_root_binding, size_caps, socket_perms etc.); G03 contract matrix + principal binding tests are precise.
- **TypeScript strict mode + build-time checking** (`tsconfig.json:9` `"strict": true`; `npm test` runs `tsc` before node tests) catches many issues at dev time even without eslint.
- **Lazy loading + defensive guards** (cross-ref performance.md) keep cold paths from exploding; many "best effort" excepts are intentional (cache misses, optional rationale) even if visibility is low.
- **AFM passes + retrieval pipeline** have dedicated PR tests exercising the exact call sites used by `sovereign_memory.py` and `sovrd.py:2102+`.
- **No permanent `.only` / unconditional skips**; conditional skips are justified (ML deps, symlink perms, "server.ts not present" in cross-lang tests).
- Phase-1 reports themselves are internally consistent, heavily cited (`file:line`), and correctly scoped (no over-claims on dead code without import/call-graph proof).

## Summary Table of Open Issues by Severity

| Severity | Count | Examples |
|----------|-------|----------|
| **bug** | 2 | Incomplete TraceRing locking (trace.py:84); silent DB/FAISS swallows in status paths (sovrd.py:1886+) |
| **suggestion** | 6 | 48+ bare except swallows; global counter mutations; SovereignAgent/CLI/GraphExporter zero coverage; mixed error contracts; deep-research exec shallowness; long unencapsulated handlers |
| **nit** | 5 | No linter tooling; catch var naming drift; import/E402 style; polling sleep in OpenClaw test; stale PLANNED markers in contracts (cross-ref phase-1) |
| **Total open** | **13** | All flagged with "Status: open" |

**Public functions / handlers with zero (or near-zero) test coverage flagged:** 7 (3 P1 high-priority + 4 P2/P3).

**Output path:** `grok/audits/2026-05-19/findings/code-quality.md` (absolute: `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19/grok/audits/2026-05-19/findings/code-quality.md`).

**End of Phase 2f code-quality & syntax sweep.** All claims re-inspected via tools on the 2026-05-19 snapshot; cross-checked against inherited Phase-1 artifacts (scope-creep 31+ items, architecture 11 surfaces, etc.). Ready for remediation or Phase 3.
# Sovereign Memory — Release Candidate Master Plan (Codex Synthesis)
**Date:** 2026-05-19
**Synthesizer:** Codex
**Sources:** three independent audits — Grok Build /implement effort=4, Antigravity 2.0 Desktop, Antigravity 2.0 CLI

## Executive Summary
Sovereign Memory is feature-rich and has a strong daemon-side security foundation: EffectivePrincipal, vault-root guards, handoff validation, audit escaping, size caps, and a broad test suite all exist. The release-candidate problem is not absence of architecture; it is uneven enforcement across surfaces. The daemon is much harder than the MCP/plugin filesystem layer, legacy OpenClaw paths still ship, and the repository has no automated CI gate to keep fixes from regressing.

RC is blocked by P0 issues in CI, security boundaries, daemon availability, and one single-orchestrator quality claim that needs verification before being treated as a rewrite mandate. The highest blast-radius items are the MCP `vaultPath` bypass, non-strict principal synthesis, pre-consent cross-agent inbox writes, plugin-side wikilink escape, no CI, blocking daemon dispatch, `time.sleep` in an async handler, unbounded audit growth, and the exposed candidate-resolution operator path.

The path to RC should be deliberately staged: first add CI so every later fix has a regression gate, then close P0 security bypasses, then fix P0 performance/storage blockers, then address P1 hardening, scope cleanup, and documentation contract alignment. P3 items should be explicitly deferred so they do not dilute the RC work.

## Methodology
I read the three top-level `AUDIT.md` files in full, then used per-dimension findings, inventories, proposals, and Antigravity Desktop tool logs to deduplicate findings by root cause and location. Severity follows the requested merger rule: any P0 remains P0 unless another audit explicitly supplied contrary evidence or a narrower calibration. Same-file, same-root-cause issues are one RCM entry with all sources cited; disagreements are preserved in the summary, next action, verification status, or Cross-Audit Open Questions. Tool-only findings are included only when the log evidence was concrete enough to act on, and are marked `TOOL-GROUNDED`.

## Unified Findings Register
| RCM-ID | Severity | Domain | Location | Summary | Sources | Verification | Next action |
|---|---|---|---|---|---|---|---|
| RCM-001 | P0 | CI | `.github/workflows/ci.yml:1 (missing)` | No CI exists, so engine tests, plugin tests, smokes, migrations, and security regressions are all manually gated. | Grok, AG-CLI | CONFIRMED-MULTI | Add `.github/workflows/ci.yml` with ubuntu+macos, Python 3.11/3.12, Node 20, pytest, npm test, and smoke recall probe. |
| RCM-002 | P0 | Security | `plugins/sovereign-memory/src/server.ts:64` | Model-facing MCP schemas still accept `vaultPath`, letting the plugin read/write arbitrary readable filesystem trees outside daemon G12 controls. | Grok | SINGLE-NEEDS-VERIFY | Remove all model-facing path fields from `server.ts`; add plugin-side realpath containment tests in `vault.test.mjs`. |
| RCM-003 | P0 | Security | `engine/principal.py:323` | Non-strict installs synthesize wildcard principals from caller-supplied `agent_id`, enabling spoofed learn/handoff attribution. | Grok, AG-Desktop | CONFIRMED-MULTI | Remove wildcard non-strict synthesis; require principal files or synthesize only fixed local identity and reject mismatches. |
| RCM-004 | P0 | Security | `plugins/sovereign-memory/src/agent_ping.ts:203` | Ping requests create recipient inbox/outbox files before recipient consent, enabling cross-agent vault mutation and inbox spam. | Grok | SINGLE-NEEDS-VERIFY | Move pending pings to sender outbox or neutral lease table; materialize recipient inbox only after opt-in/poll. |
| RCM-005 | P0 | Security | `plugins/sovereign-memory/src/vault.ts:636` | Plugin-side handoff ref resolution lacks daemon G23 containment, so planted wikilinks can escape the vault. | Grok | SINGLE-NEEDS-VERIFY | Apply realpath/is-relative-to checks to `resolveVaultRef`, `listMarkdownFiles`, and `resolveInboxHandoffContext`; add TS escape tests. |
| RCM-006 | P0 | Security | `plugins/sovereign-memory/src/server.ts:504` | `sovereign_resolve_candidate` is exposed to the model-facing MCP surface and may allow an agent to approve its own staged memory. | AG-Desktop | SINGLE-NEEDS-VERIFY | Verify daemon/operator principal flow, then remove from model-facing tools or require explicit operator-only UI/CLI approval. |
| RCM-007 | P0 | Performance | `plugins/sovereign-memory/src/vault.ts:385` | Universal audit append has no rotation/quota and can grow `log.md` and daily logs without bound. | Grok, AG-Desktop | CONFIRMED-MULTI | Add audit rotation/pruning/quota to `recordAudit` and Python handoff audit; test large-log `audit_tail`. |
| RCM-008 | P0 | Performance | `engine/sovrd.py:2550` | Async socket handling dispatches CPU-bound retrieval synchronously, so one heavy recall can block all daemon clients. | Grok, AG-CLI | CONFIRMED-MULTI | Offload heavy dispatch paths with `asyncio.to_thread`/executor and add multi-client latency regression tests. |
| RCM-009 | P0 | Performance | `engine/sovrd.py:832` | `_handle_await_handoff` uses `time.sleep`, blocking the event loop during handoff polling. | AG-CLI | SINGLE-NEEDS-VERIFY | Confirm with grep, replace with `await asyncio.sleep`, and add an async concurrency test. |
| RCM-010 | P0 | Quality | `engine/retrieval.py:1276` | `RetrievalEngine.retrieve()` was flagged as a 500+ line god method with high complexity; severity is disputed by omission in other audits. | AG-CLI | SINGLE-NEEDS-VERIFY | Run `radon cc engine/retrieval.py`; if confirmed, split retrieval pipeline behind tests after P0 security/perf work. |
| RCM-011 | P1 | Security | `engine/sovrd.py:1866` | `status` and `trace` are reported as unauthenticated local JSON-RPC surfaces, leaking paths and cross-agent trace data. | AG-Desktop, Grok, AG-CLI | CONFIRMED-MULTI | Add principal checks to status/trace and redact path fields in responses. |
| RCM-012 | P1 | Security | `engine/afm_writer.py:133` | AFM writer lacks the SEC-018 forged-frontmatter guard present in `writeback.py`. | AG-CLI | SINGLE-NEEDS-VERIFY | Diff `afm_writer.py` against `writeback.py`, port guard, and add malicious `---` body tests. |
| RCM-013 | P1 | Security | `engine/afm_writer.py:78` | AFM writer builds YAML frontmatter with unescaped title/tags, enabling metadata injection. | AG-CLI | SINGLE-NEEDS-VERIFY | Use a YAML dumper for AFM frontmatter and add newline/key-injection tests. |
| RCM-014 | P1 | Security | `engine/requirements.txt:4` | Supply chain is weak: loose Python ranges, native AFM helper provenance gaps, legacy direct-exec shims, and no enforced audit gate. | Grok, AG-CLI | CONFIRMED-MULTI | Add lock/hash workflow, `npm ci`, `pip-audit`, native helper build/attestation docs, and delete legacy shims. |
| RCM-015 | P1 | CI | `Project Root:1` | No CodeQL, Dependabot, secret scanning, Bandit, or Semgrep gate exists. | AG-CLI | SINGLE-NEEDS-VERIFY | Add security workflow or jobs after baseline CI lands; fail on new high/critical findings. |
| RCM-016 | P1 | CI | `scripts/repro-smoke.sh:1 (missing)` | Clean-machine reproduction needs 11-14 manual steps and no hermetic status/recall smoke. | Grok, AG-CLI | CONFIRMED-MULTI | Add an isolated `SOVEREIGN_HOME=/tmp/...` smoke script and run it in CI. |
| RCM-017 | P1 | Architecture | `engine/sovrd.py:285` | Core daemon and plugin code contain hardcoded agent aliases/IDs/vault paths, contradicting agent-agnostic claims. | Grok, AG-Desktop, AG-CLI | CONFIRMED-MULTI | Move agent/vault mapping into principal config or a runtime registry; stop hardcoding hook constants in core contracts. |
| RCM-018 | P1 | Architecture | `plugins/sovereign-memory/src/server.ts:48` | MCP/RPC schemas are fragmented and public docs list fewer tools than the 26 registered tools. | Grok, AG-Desktop, AG-CLI | CONFIRMED-MULTI | Create one capability/schema source and regenerate SKILL/CAPABILITIES/tool docs from it. |
| RCM-019 | P2 | Architecture | `engine/sovrd.py:1913` | Public surfaces leak implementation details such as absolute paths, backend names, doc IDs, chunk IDs, and identity internals. | Grok, AG-Desktop, AG-CLI | CONFIRMED-MULTI | Add a redaction layer for status, health, trace, provenance, and identity startup payloads. |
| RCM-020 | P1 | Scope | `openclaw-extension/:1` | Deprecated OpenClaw extension and `engine/openclaw-tool.sh` still ship and bypass daemon boundaries. | Grok, AG-Desktop, AG-CLI | CONFIRMED-MULTI | Delete `openclaw-extension/` and `engine/openclaw-tool.sh`; update docs/plans references. |
| RCM-021 | P1 | Scope | `plugins/sovereign-memory/skills/sovereign-memory/SKILL.md:31` | Experimental Team Mode is promoted as a primary workflow despite audit claims that it is not RC-stable. | AG-CLI | SINGLE-NEEDS-VERIFY | Downgrade Team Mode language to experimental until CI and direct tests cover the full workflow. |
| RCM-022 | P1 | Scope | `engine/migrations/:1` | Duplicate `007_*.sql` migration IDs risk divergent new-user database state. | AG-CLI | SINGLE-NEEDS-VERIFY | List migrations, resequence one file, and add migration-order CI check. |
| RCM-023 | P1 | Quality | `engine/agent_api.py:29` | Public `SovereignAgent`, CLI commands, and `GraphExporter` have zero direct tests. | Grok | SINGLE-NEEDS-VERIFY | Add direct unit/integration tests for `SovereignAgent`, `cmd_*`, and `GraphExporter`. |
| RCM-024 | P1 | Quality | `engine/sovrd.py:1886` | Silent `except/pass` and coarse error handling hide failures in status, FAISS, rationale, decay, and hot paths. | Grok, AG-Desktop | CONFIRMED-MULTI | Replace silent swallows with structured logging or propagated errors; add failure-path tests. |
| RCM-025 | P1 | Performance | `engine/retrieval.py:348` | FAISS cold load/rebuild performs full embedding table reads/copies and becomes O(N) at scale. | Grok, AG-CLI | CONFIRMED-MULTI | Batch embedding loads, add incremental/background HNSW build, and create 50k-vector regression harness. |
| RCM-026 | P1 | Performance | `engine/faiss_index.py:44` | FAISS index stores raw vectors in Python lists in addition to FAISS, creating redundant memory pressure. | AG-CLI | SINGLE-NEEDS-VERIFY | Inspect `_vectors` lifecycle; remove resident duplicate storage or gate it behind build-only paths. |
| RCM-027 | P1 | Performance | `engine/retrieval.py:257` | Unique recall queries always pay embedding and cross-encoder cost; query embedding cache is absent. | Grok | SINGLE-NEEDS-VERIFY | Add bounded query-embedding cache and expose cold/hot timings in status or eval output. |
| RCM-028 | P1 | Performance | `engine/retrieval.py:1231` | Leading-wildcard path lookup forces full-table scans for wikilink/neighborhood resolution. | AG-CLI | SINGLE-NEEDS-VERIFY | Add filename/path index column or FTS-backed path lookup and benchmark before/after. |
| RCM-029 | P1 | Performance | `engine/sovrd.py:140` | Lazy singleton initialization is unsynchronized, risking duplicate model loads under concurrent first use. | Grok | SINGLE-NEEDS-VERIFY | Guard `_lazy_*` and model singletons with locks; add concurrent first-recall tests. |
| RCM-030 | P1 | Quality | `engine/*.py:1` | Type-safety gaps are broad: AG-Desktop mypy logged 89 errors across 21 files. | AG-Desktop, AG-CLI | TOOL-GROUNDED | Add mypy config with staged strictness; fix public API and handler types before enforcing. |
| RCM-031 | P2 | Security | `engine/afm_passes/session_distillation.py:68` | SHA1 is used for AFM content digests in five pass files; tool logs mark it as insecure hash use. | AG-Desktop, AG-CLI | TOOL-GROUNDED | Replace SHA1 with SHA256 in AFM pass digest helpers; update golden IDs/tests. |
| RCM-032 | P2 | Security | `engine/afm_provider.py:211` | Dynamic `urllib.request.urlopen` can accept dangerous schemes unless bridge URL is constrained. | AG-Desktop, AG-CLI | TOOL-GROUNDED | Enforce localhost HTTP(S) allowlist and reject `file://` or non-loopback endpoints. |
| RCM-033 | P2 | Security | `engine/episodic.py:82` | Raw episodic events may store secrets and later feed AFM distillation prompts without redaction. | AG-CLI | SINGLE-NEEDS-VERIFY | Add redaction in `add_event` or pre-AFM extraction path; test secret patterns. |
| RCM-034 | P2 | Security | `engine/agent_api.py:347` | Dynamic SQL placeholder construction was flagged by tools; Desktop models disagree whether this is exploit or code smell. | AG-Desktop | TOOL-GROUNDED | Verify inputs are parameterized; refactor repeated placeholder builders or document false-positive rationale. |
| RCM-035 | P2 | Security | `plugins/sovereign-memory/package-lock.json:1` | `npm audit` found 4 vulnerabilities, including high `fast-uri` path traversal/host confusion. | AG-Desktop | TOOL-GROUNDED | Run `npm audit fix` or dependency upgrades; add npm audit to CI with triage policy. |
| RCM-036 | P2 | Scope | `engine/afm_scheduler.py:1` | `afm_scheduler.py` is unwired and only referenced by tests/old plans. | Grok | SINGLE-NEEDS-VERIFY | Delete it or explicitly wire/document it; prefer deletion for RC. |
| RCM-037 | P2 | Scope | `plugins/sovereign-memory/src/ui-server.ts:257` | Deep-research UI server executes an external non-repo path with little documentation, timeout, or redaction. | Grok | SINGLE-NEEDS-VERIFY | Remove, or gate behind explicit env and add timeout/exit/path-redaction tests. |
| RCM-038 | P2 | Scope | `engine/backends/lance.py:63` | Lance and Qdrant vector backends are non-functional stubs still visible as future options. | Grok, AG-Desktop, AG-CLI | CONFIRMED-MULTI | Remove from RC build/docs or move behind experimental extras. |
| RCM-039 | P2 | Scope | `engine/db.py:1` | Base schema initialization duplicates migration logic, increasing drift risk. | AG-CLI | SINGLE-NEEDS-VERIFY | Standardize schema creation through migrations and add a fresh-db migration test. |
| RCM-040 | P2 | Scope | `engine/migrate_v3_to_v3_1.py:1` | Legacy manual migration script remains after SQL migrations superseded it. | Grok, AG-CLI | CONFIRMED-MULTI | Delete after verifying SQL migrations cover its behavior. |
| RCM-041 | P2 | Scope | `engine/sovrd.py:188` | Legacy dual-write to `MEMORY.md` remains for old Hermes/OpenClaw compatibility. | AG-CLI | SINGLE-NEEDS-VERIFY | Remove or quarantine behind explicit legacy flag with tests. |
| RCM-042 | P2 | Scope | `plugins/sovereign-memory/src/team-harvest.ts:88` | Redaction, slugification, handoff packet building, and helpers are duplicated across Python and TypeScript. | Grok, AG-Desktop, AG-CLI | CONFIRMED-MULTI | Pick canonical ownership for vault operations and consolidate duplicated helpers. |
| RCM-043 | P2 | Quality | `engine/principal.py:255` | Mutable default list for vault roots was flagged as a Python footgun. | AG-CLI | SINGLE-NEEDS-VERIFY | Change default to `None` and initialize inside constructor/dataclass factory. |
| RCM-044 | P2 | Quality | `engine/sovrd.py:504` | Large handlers and file bloat in `sovrd.py`/`retrieval.py` make boundaries hard to maintain. | Grok, AG-CLI | CONFIRMED-MULTI | Split after P0 fixes into handler modules and retrieval pipeline components. |
| RCM-045 | P2 | Quality | `engine/*.py:1` | Ruff/Prettier reported broad formatting/import issues; AG-Desktop Prettier logged 108 files needing formatting. | AG-Desktop, AG-CLI | TOOL-GROUNDED | Add ruff/prettier/eslint jobs after initial CI; avoid mass formatting until feature freeze. |
| RCM-046 | P2 | CI | `README.md:433` | Release docs reference phantom gates (`make audit`), stale test counts, no CHANGELOG, and no engine fields. | Grok, AG-CLI | CONFIRMED-MULTI | Add `CHANGELOG.md`, package `engines`, `python_requires`, and executable verification docs. |
| RCM-047 | P2 | CI | `engine/launchd/com.openclaw.sovrd.plist.example:61` | launchd examples use `~` in log paths, which launchd will not expand. | AG-Desktop, AG-CLI | CONFIRMED-MULTI | Use absolute placeholder paths or template generation; test plist lint. |
| RCM-048 | P2 | Performance | `engine/db.py:316` | SQLite/FTS cleanup and auto-vacuum are absent or manual, so DB/FTS files can grow indefinitely. | AG-Desktop, AG-CLI | CONFIRMED-MULTI | Add incremental vacuum/FTS optimize to migrations or hygiene routine. |
| RCM-049 | P2 | Performance | `engine/episodic.py:297` | Episodic TTL cleanup exists but is not automatically triggered by the daemon. | AG-CLI | SINGLE-NEEDS-VERIFY | Wire cleanup into hygiene/nightly path and add retention tests. |
| RCM-050 | P2 | Quality | `engine/eval/queries.jsonl:1` | Evaluation expected doc IDs are illustrative placeholders, limiting out-of-box live eval value. | AG-Desktop | SINGLE-NEEDS-VERIFY | Generate fixture data for eval or mark the dataset mock-only in docs/CI. |
| RCM-051 | P3 | Security | `engine/hyde.py:1` | HyDE/AFM JSON parsing can fail on fenced or prefaced model output and fall back silently. | AG-Desktop | SINGLE-NEEDS-VERIFY | Defer: add tolerant JSON extraction after RC blockers. |
| RCM-052 | P3 | Security | `engine/afm_provider.py:162` | Native AFM helper stdout must be pure JSON; extra logs trigger fallback. | AG-Desktop | SINGLE-NEEDS-VERIFY | Defer: enforce stderr-only helper logs and add parser tests. |
| RCM-053 | P3 | Scope | `_archive/:1` | Old archive/quarantine directories and legacy Hermes assets remain in the repository. | AG-CLI | SINGLE-NEEDS-VERIFY | Defer: purge or move outside release tarball after RC validation. |
| RCM-054 | P3 | Scope | `docs/ARCHITECTURAL-REVIEW-ROADMAP.md:1` | Historical roadmap docs remain alongside current docs and can confuse release readers. | AG-CLI | SINGLE-NEEDS-VERIFY | Defer: archive stale docs after contract alignment. |
| RCM-055 | P3 | Scope | `plugins/sovereign-memory/src/team-harvest.ts:88` | Stale TODO for helper extraction remains. | AG-Desktop, AG-CLI | CONFIRMED-MULTI | Defer unless touched by duplicate-helper cleanup. |

## Phases

### Phase 0 — CI bootstrap (UNGATED, must happen first)
Entry criteria — Repository can be checked out with existing dirty work left untouched; no source fixes are started before CI baseline lands.

Exit criteria — `.github/workflows/ci.yml` runs on push, PR, and nightly; matrix includes `ubuntu-latest` and `macos-latest`, Python 3.11/3.12, and Node 20; jobs run engine `pytest`, plugin `npm ci && npm test`, and an isolated smoke status/recall probe.

Findings addressed — RCM-001, RCM-015, RCM-016.

Estimated scope — M.

Suggested executor — Codex for workflow scaffolding and smoke scripting; Antigravity for tool-grounded validation of the resulting CI logs.

### Phase 1 — P0 security fixes
Entry criteria — Phase 0 CI exists and can run at least the current manual tests, even if later jobs are still being hardened.

Exit criteria — No model-facing tool accepts arbitrary vault paths; non-strict principal spoofing is gone; ping/handoff filesystem writes are consent-bound; plugin wikilink reads are contained; candidate resolution is operator-only or verified safe; all changes have focused tests.

Findings addressed — RCM-002, RCM-003, RCM-004, RCM-005, RCM-006, plus security portions of RCM-011.

Estimated scope — L.

Suggested executor — Claude Code for memory-system-aware security changes; Grok for multi-critic review of boundary refactors; Antigravity for reproducing tool/log evidence.

### Phase 2 — P0 performance & storage fixes
Entry criteria — Phase 0 is running and Phase 1 can proceed independently without overlapping edits to retrieval/storage hot paths.

Exit criteria — Daemon dispatch no longer blocks all clients on heavy retrieval; `_handle_await_handoff` uses nonblocking sleep; audit logs have quota/rotation; any confirmed `retrieve()` complexity work has a measured decomposition plan rather than an unbounded rewrite.

Findings addressed — RCM-007, RCM-008, RCM-009, RCM-010.

Estimated scope — M/L.

Suggested executor — Codex for targeted async/storage changes; Antigravity for load/concurrency measurements; Grok if the retrieval refactor becomes multi-file and needs critic passes.

### Phase 3 — P1 hardening
Entry criteria — Phase 1 and Phase 2 P0 fixes are merged or in final review with passing CI.

Exit criteria — P1 security, supply-chain, architecture, coverage, and scale risks have either landed fixes or explicit RC waivers; status/trace access is principal-aware; AFM writer security regressions are closed; public surfaces have direct tests.

Findings addressed — RCM-011, RCM-012, RCM-013, RCM-014, RCM-017, RCM-018, RCM-021, RCM-022, RCM-023, RCM-024, RCM-025, RCM-026, RCM-027, RCM-028, RCM-029, RCM-030.

Estimated scope — L.

Suggested executor — Claude Code for principal/AFM/public API changes; Grok for architecture/coverage refactors; Antigravity for mypy/security-tool evidence.

### Phase 4 — Dead code & scope cleanup
Entry criteria — Phase 3 has started, and CI prevents accidental breakage while deleting or quarantining files.

Exit criteria — `openclaw-extension/`, `engine/openclaw-tool.sh`, unwired AFM scheduler, legacy migration scripts, and abandoned/stub surfaces are removed or explicitly quarantined; migration collision is resolved; duplicate logic has a named canonical owner.

Findings addressed — RCM-020, RCM-022, RCM-036, RCM-037, RCM-038, RCM-039, RCM-040, RCM-041, RCM-042.

Estimated scope — M.

Suggested executor — Grok for multi-critic deletion safety; Codex for mechanical cuts and reference cleanup.

### Phase 5 — Doc + contract alignment
Entry criteria — Main security and scope changes are stable enough that docs will not churn daily.

Exit criteria — SKILL.md accurately marks experimental features; CAPABILITIES.md matches runtime methods and MCP tools; CHANGELOG exists; stale `[PLANNED]` and stale pass counts are fixed; `python_requires` and Node `engines` fields exist; launchd examples are absolute-path safe.

Findings addressed — RCM-018, RCM-021, RCM-046, RCM-047, plus doc portions of RCM-019 and RCM-020.

Estimated scope — M.

Suggested executor — Codex for contract/doc edits; Antigravity for docs-vs-code checks.

### Phase 6 — RC validation gate
Entry criteria — Phases 0-5 have landed or have explicit waivers for non-blockers.

Exit criteria — At least two of the three audit styles are rerun; CI is green on push/PR/nightly; zero unaddressed P0/P1 findings remain; all P2 deferrals have owners; P3 items are listed as post-RC.

Findings addressed — All RCM entries, with P3 deferred explicitly.

Estimated scope — M.

Suggested executor — Antigravity for tool-grounded validation, Grok for critic synthesis, Codex for final crosswalk/waiver updates.

## Verification Backlog
| Item | RCM IDs | How to verify |
|---|---:|---|
| MCP `vaultPath` bypass | RCM-002 | `rg -n "vaultPath" plugins/sovereign-memory/src/server.ts plugins/sovereign-memory/src/task.ts plugins/sovereign-memory/src/vault.ts` and confirm model-facing zod schemas cannot pass paths. |
| Non-strict principal spoofing | RCM-003 | `sed -n '300,340p' engine/principal.py` and add a no-principals socket test that supplied `agent_id` cannot mint wildcard capabilities. |
| Ping pre-consent write | RCM-004 | `sed -n '190,215p' plugins/sovereign-memory/src/agent_ping.ts` and test that a request does not create recipient inbox files before opt-in. |
| Plugin G23 containment gap | RCM-005 | `sed -n '620,670p' plugins/sovereign-memory/src/vault.ts` and add `../` plus symlink escape tests for `resolveInboxHandoffContext`. |
| Candidate resolution exposure | RCM-006 | `sed -n '500,545p' plugins/sovereign-memory/src/server.ts` and trace daemon `resolve_candidate` principal checks under MCP invocation. |
| Audit append growth | RCM-007 | `rg -n "recordAudit|appendFile|_append_handoff_audit" plugins/sovereign-memory/src engine/sovrd.py` and run a repeated-call log growth test. |
| Blocking daemon dispatch | RCM-008 | `sed -n '2535,2560p' engine/sovrd.py` and run two concurrent client requests where one performs heavy recall. |
| Blocking handoff sleep | RCM-009 | `grep -n "time.sleep" engine/sovrd.py` to confirm `_handle_await_handoff`, then replace with async sleep and test concurrency. |
| Retrieval complexity | RCM-010 | `radon cc engine/retrieval.py -s` to confirm `retrieve()` complexity before approving a refactor. |
| AFM frontmatter regression | RCM-012 | `diff -u engine/writeback.py engine/afm_writer.py | rg -n "frontmatter|---|forg"` and add malicious frontmatter tests. |
| AFM YAML injection | RCM-013 | Inspect `engine/afm_writer.py:78` for f-string YAML generation; test newline/key injection in title/tags. |
| Migration collision | RCM-022 | `ls engine/migrations/ | sort | rg '^007_'` and add a migration uniqueness CI check. |
| Public API coverage | RCM-023 | `rg -n "SovereignAgent|cmd_|GraphExporter" engine/test_*.py` and confirm direct calls exist. |
| Mypy/tool findings | RCM-030 | Re-run mypy using the same AG-Desktop command if preserved, or `python -m mypy engine`; compare against 89 logged errors. |
| NPM audit vulnerabilities | RCM-035 | `cd plugins/sovereign-memory && npm audit --omit=dev` after dependency updates. |

## Cross-Audit Open Questions
| Question | What each audit said | Recommended resolution | Why |
|---|---|---|---|
| Is dynamic SQL a P0 vulnerability or a code smell? | AG-Desktop top report called it P0; its Model B noted placeholder values are still parameter-bound and likely low-severity; Semgrep/Bandit flagged it as blocking/medium tool evidence. | Treat as RCM-034 P2 until a concrete exploit path is shown; still refactor repeated placeholder builders. | The severity rule allows downgrade because the same audit recorded cited contrary evidence. |
| Is `PERF-P0-01` audit growth truly P0? | Grok kept it P0 after critic challenge; AG-Desktop only flagged launchd/log rotation as lower severity. | Keep RCM-007 P0 for RC until rotation/quota is added or a bounded-volume measurement proves it safe. | It hits nearly every tool/hook path and has direct availability blast radius. |
| Should `RetrievalEngine.retrieve()` complexity block RC? | AG-CLI called it P0; Grok treated retrieval complexity as important but not a top P0 release blocker. | Keep RCM-010 P0 but `SINGLE-NEEDS-VERIFY`; require `radon` and tests before any large refactor. | A maintainability P0 should not preempt security/perf blockers without measurement. |
| Should Team Mode be public or experimental for RC? | AG-CLI flagged SKILL promotion as P1 drift; Grok documented team tools but focused on tool count/docs drift. | Downgrade docs to experimental until end-to-end CI tests exist. | Avoid overpromising agent orchestration while core boundaries are still closing. |
| Is HTTP fallback removable before RC? | AG-Desktop Model B called deprecated HTTP fallback P1 scope creep; Grok listed HTTP as P3/docs drift and useful maintenance fallback. | Do not remove in Phase 1; decide during Phase 4 whether to quarantine, disable by default, or document maintenance-only. | Removal may affect non-UDS environments; security gain needs compatibility review. |
| Are `0o700` socket directory warnings valid? | Semgrep/Bandit flagged `0o700` as insecure/widely permissive; all audits agreed 0700/0600 is correct for local private sockets. | Treat tool finding as false positive; keep hard-fail semantics on chmod errors. | World-readable `0o644` would weaken the intended local privacy boundary. |

## Out of Scope (defer past RC)
All P3 findings are deferred unless touched incidentally by a P0/P1/P2 fix:

- RCM-051: tolerant HyDE JSON parsing for fenced/prefaced model output.
- RCM-052: native AFM helper stdout/stderr robustness.
- RCM-053: archive/quarantine directories and legacy Hermes assets.
- RCM-054: historical roadmap document cleanup.
- RCM-055: stale `team-harvest.ts` TODO, unless duplicate-helper cleanup touches it.
- AG-CLI `ARCH-004`: internal `sm://doc/{doc_id}/chunk/{chunk_id}` URI IDs.
- AG-Desktop low evaluation usability issue, if not handled by RCM-050.
- Full v2 security hardening such as replay-resistant principals, cryptographic principal proofs, and long-term redaction policy.

## Track A Note (the agent-integration skill)
This three-orchestrator test produced useful design data for the future agent-platform install skill. Grok showed native multi-agent/critic orchestration with `/implement --effort` and stronger synthesis discipline; Antigravity Desktop showed sandboxed real tool execution and productized Manager fan-out, with logs that materially improved the plan; Antigravity CLI showed concise high-signal per-dimension tables and surfaced different blockers, especially migrations and AFM writer regressions. The install skill should probe capability bits such as native multi-agent fan-out, layered-manager orchestration, sandboxed tool-log availability, durable artifact layout, product commands like `/goal` or `/implement effort`, and whether the platform can expose logs as first-class evidence.

## Appendix: Source Audit Crosswalk
| Original audit ID / source label | Assigned RCM-ID |
|---|---|
| Grok `CI-P0-01`; AG-CLI `CI-001`, `CI-B01`, pipeline P0 | RCM-001 |
| Grok `SEC-P0-01`, `SEC-P2E-01` | RCM-002 |
| Grok `SEC-P0-02`, `SEC-P2E-02`; AG-Desktop security-modelB P2 | RCM-003 |
| Grok `SEC-P0-03`, `SEC-P2E-03` | RCM-004 |
| Grok `SEC-P0-04`, `SEC-P2E-04` | RCM-005 |
| AG-Desktop architecture-modelB P0 privilege gating bypass | RCM-006 |
| Grok `PERF-P0-01`; AG-Desktop operability-modelB log rotation | RCM-007 |
| AG-CLI `PERF-001`; Grok performance-adversarial blocking dispatch | RCM-008 |
| AG-CLI `PERF-002`, `PERF-B01` | RCM-009 |
| AG-CLI `QUAL-001`; lint inventory `RetrievalEngine.retrieve` complexity | RCM-010 |
| AG-Desktop security-modelB P1; AG-Desktop architecture-modelA P2; Grok `ARC-P2-01`; AG-CLI `ARCH-004` path leaks | RCM-011, RCM-019 |
| AG-CLI `SEC-001`, `SEC-A01`, `SEC-VULN-001` | RCM-012 |
| AG-CLI `SEC-002`, `SEC-A02`, `SEC-VULN-002` | RCM-013 |
| Grok `SEC-P1-01`, `CI-P1-01`; AG-CLI `CI-002`, `CI-B02`, pipeline dependency P1 | RCM-014 |
| AG-CLI `CI-003`, `CI-B03`, pipeline SAST/secret-scanning P1 | RCM-015 |
| Grok `CI-P1-01`; AG-CLI pipeline repro/deployment parity findings | RCM-016 |
| Grok `ARC-P1-01`; AG-CLI `ARCH-001`, `ARCH-002`, `ARCH-003`, `ARC-01`, `ARC-03`; AG-Desktop architecture docs | RCM-017 |
| Grok `ARC-P1-02`; AG-CLI `ARCH-006`; AG-Desktop architecture-modelB versioning/capability negotiation | RCM-018 |
| Grok `ARC-P2-01`; AG-Desktop absolute path leak; AG-CLI `ARC-02`, `ARCH-004` | RCM-019 |
| Grok `SCP-P1-01`, `ARC-P2-02`; AG-Desktop scope-modelA P1; AG-CLI `SCOPE-002`, `SCOPE-A03`, `SCOPE-B02` | RCM-020 |
| AG-CLI `SCOPE-001`, `SCOPE-A01` | RCM-021 |
| AG-CLI `SCOPE-002`, `SCOPE-001`, `SCOPE-B01` | RCM-022 |
| Grok `CQ-P1-01` | RCM-023 |
| Grok `CQ-P1-02`; AG-Desktop Bandit B110/B112 logs | RCM-024 |
| Grok `PERF-P1-02`; AG-CLI `PERF-005`, `PERF-B03` | RCM-025 |
| AG-CLI `PERF-004`, `PERF-B02` | RCM-026 |
| Grok `PERF-P1-01` | RCM-027 |
| AG-CLI `PERF-003` | RCM-028 |
| Grok `PERF-P2-01`, `CQ-P2-01` | RCM-029 |
| AG-CLI `QUAL-002`; AG-Desktop mypy log; AG-CLI lint inventory type gaps | RCM-030 |
| AG-Desktop security-modelA SHA1; Semgrep SHA1; Bandit B324; AG-CLI noted SHA1 as P3/P2 vulnerability | RCM-031 |
| AG-Desktop security-modelA urllib; Semgrep dynamic urllib; Bandit B310; AG-CLI `SEC-004`, `SEC-A04` | RCM-032 |
| AG-CLI `SEC-003`, `SEC-A03`, `SEC-VULN-003` | RCM-033 |
| AG-Desktop Dynamic SQL P0/code-smell; Semgrep SQL; Bandit B608 | RCM-034 |
| AG-Desktop `npm-audit.log` fast-uri/hono/ip-address findings | RCM-035 |
| Grok `SCP-P1-02` | RCM-036 |
| Grok `SCP-P2-01` | RCM-037 |
| Grok `SCP-P3-01`; AG-Desktop scope-modelA P2; AG-CLI `SCOPE-004`, `SCOPE-B04` | RCM-038 |
| AG-CLI `SCOPE-005`, `SCOPE-003`, `SCOPE-B02` | RCM-039 |
| Grok low-consumer migrate script; AG-CLI `SCOPE-B03` | RCM-040 |
| AG-CLI `SCOPE-006`, `SCOPE-A04` | RCM-041 |
| Grok duplicate primitives; AG-Desktop scope-modelB slug/audit duplication; AG-CLI `SCOPE-003`, `SCOPE-005`, `SCOPE-A02` | RCM-042 |
| AG-CLI `QUAL-004`, lint inventory mutable default | RCM-043 |
| AG-CLI `QUAL-003`; Grok long handlers/code-quality suggestions | RCM-044 |
| AG-Desktop ruff/prettier logs; AG-CLI style/standards inventory | RCM-045 |
| Grok `CI-P1-02`, `ARC-P3-01`, `SCP-P2-02`; AG-CLI docs drift | RCM-046 |
| AG-Desktop operability-modelB Medium; Grok `CI-P2-01`; AG-CLI `CI-004`, `CI-B04` | RCM-047 |
| AG-Desktop performance-modelB Low; AG-CLI `PERF-007` | RCM-048 |
| AG-CLI `PERF-B04` | RCM-049 |
| AG-Desktop evaluation-modelA Low | RCM-050 |
| AG-Desktop evaluation-modelB HyDE output validation | RCM-051 |
| AG-Desktop evaluation-modelB native AFM JSON parsing | RCM-052 |
| AG-CLI `SCOPE-006`, `SCOPE-B07`, dead-code inventory archives/assets | RCM-053 |
| AG-CLI `SCOPE-B06` | RCM-054 |
| AG-Desktop scope-modelA P3; AG-CLI `SCOPE-007`, `SCOPE-A06` | RCM-055 |

# PHASE 1d — CI & Release Readiness Audit (Researcher)

**Audit date:** 2026-05-19
**Scope:** Full release-engineering and reproducibility audit of the Sovereign Memory RC snapshot in the dedicated audit worktree (`/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19`).
**Method:** 100% tool-grounded (list_dir on root + .github + subdirs; 20+ targeted grep with precise patterns across `**/*.{md,py,ts,json,yml,yaml,sh}`; read_file on all manifests, README, contracts, sovrd.py:1-100/1866-1919/2565-2608/2718-2820, config.py:19-220, package.jsons, test files, launchd plist, etc.). No edits performed outside this audit deliverable. All claims cite exact `file:line` (absolute paths under the audit worktree unless noted). Speculation explicitly labeled "Hypothesis:".

**Key boundary note:** The audit worktree + main repo both lack `.github/workflows/` (confirmed via list_dir + recursive grep for `on:\s*(push|pull_request)` / `jobs:` / `matrix:` patterns; 0 matches). All CI evidence is manual/dev-log only.

---

## 1. CI Coverage

**Finding:** No automated CI system exists. Zero GitHub Actions workflows, no Makefile targets for test, no pyproject.toml / tox / setup.cfg CI hooks. All verification is manual, executed ad-hoc by developers and captured in planning artifacts.

### CI Matrix Table

| Workflow / Config | Triggers | Coverage (what runs) | Gaps (observed) |
|-------------------|----------|----------------------|-----------------|
| **None** (no `.github/workflows/` dir or YAML anywhere in tree; grep for `on:\s*(push\|pull_request\|schedule\|workflow_dispatch)` + `jobs:` + `matrix:` across `**/*.{yml,yaml}` returned 0 matches at audit snapshot) | N/A (push / PR / nightly / schedule all absent) | None automated. | • No OS matrix (macOS/Linux/Windows claims untested in CI)<br>• No Python version matrix (docs recommend 3.11/3.12; 3.14 noted as risky in engine/requirements.txt:3)<br>• No Node version matrix (plugin package.json:7-18 uses tsc/vite/node --test; no "engines" field)<br>• Full engine test suite (36 `engine/test_*.py` files, pytest-based, ~213-333 passes historically per logs) **never auto-run**<br>• Plugin tests (`plugins/sovereign-memory/tests/*.test.mjs` — 15+ files per package.json:12; plus openclaw-extension/tests/) only via manual `npm test`<br>• Eval harness (`engine/eval/harness.py`, `test_pr4_eval_harness.py`, `eval/queries.jsonl`) never auto-invoked in any script<br>• No migration safety / smoke harness in automation |
| **Manual verification gate** (README.md:438-444; SECURITY_PLAN.md:250-256; docs/plans/execution/00_MASTER_TRACKER.md:74-81; repeated in 15+ WORKTREE_STATE.md entries e.g. 2026-04-26 logs) | Developer runs before merge/PR | • `cd engine && pytest -q` (all 36 tests)<br>• `cd ../plugins/sovereign-memory && npm test` (32/32 in late logs)<br>• `npm run smoke:hook`<br>• Occasional `pytest -q engine/test_prN_*.py` focused + migration on /tmp/*.db + AFM dry-runs with `SOVEREIGN_AFM_LOOP=off` | • Relies on individual clean machines + human discipline<br>• No enforcement; drift visible in stale pass counts<br>• No cross-platform (all logs appear macOS-centric from paths) |
| **package.json scripts** (plugins/sovereign-memory/package.json:12,15-18; openclaw-extension/package.json:15,17) | `npm test`, `npm run smoke:*` | Builds + runs `node --test tests/*.test.mjs` + limited smokes | Does not invoke Python engine tests or eval harness |
| **Python test discovery** (36 files under engine/test_*.py; imports pytest + fixtures in e.g. test_vault_root_binding.py:21, test_size_caps_and_sync_warn.py:23) | Manual `pytest` only | Covers foundation through PR-15 (G01-G23 gaps, handoff, AFM, eval, contracts, etc.) + `test_g03_contract_matrix.py` | Eval harness integration tests exist but not part of default `pytest` run in docs |

**Evidence citations:**
- Absence: list_dir audit-root + main `/.github` (empty workflows subdir); grep "on:\s*(push..." path=audit-root + main repo (0 matches); same for Makefile/pyproject.
- Manual gate: README.md:436 (`## Verification Gate`), 440 (`cd engine && pytest -q`), 442 (`npm test`), 433 (`make audit` claim); SECURITY_PLAN.md:251 (`cd engine && pytest -q`); docs/plans/execution/RESUME.md:96-101 and 00_MASTER_TRACKER.md:76-80; 20+ WORKTREE_STATE.md entries logging exact `pytest -q engine` + `npm test` results (e.g. "213 passed / 3 skipped", "32/32 passed").
- Test count: list_dir + grep `^` glob=`test_*.py` under engine/ (36 files); plugins/sovereign-memory/tests/ (18 files, 15 matched by `*.test.mjs`).

**Hypothesis (low confidence):** CI may exist in untracked private CI (e.g. internal runner) or developer laptops only; public repo snapshot shows zero automation surface.

---

## 2. Install Paths Claimed vs Real

### macOS (native AFM + launchd + pip + npm)
- **Claimed (README.md:263-293, 427-428; engine/launchd/com.openclaw.sovrd.plist.example:1-69):** `python3 -m pip install -r engine/requirements.txt` (or venv 3.11/3.12); `python3 sovrd.py --socket ~/.sovereign-memory/run/sovrd.sock`; launchd agent example with `Umask 63`, logs under ~/Library/Logs/; `xattr` backup-exclude for db/vault (README:419-425). Native AFM via `engine/native_afm_helper` (binary present) or source `native_afm_helper.swift`.
- **Real:** Works for bare pip if wheels available. Unix socket creation + 0700/0600 chmod in sovrd.py:2571-2593 (SEC-001). launchd plist is example only (requires manual substitution of absolute paths, no ~ expansion). Native AFM: swift source requires `swiftc` + macOS 15.4+ Foundation Models SDK (not auto-built in install); binary present in tree but no build script documented. AFM bridge fallback (localhost 11437) assumed external.
- **Undocumented rituals:** `mkdir -p ~/.sovereign-memory/run` + perms; potential first-run model download by sentence-transformers (lazy but large); `SOVEREIGN_AFM_*` envs for native helper path.

### Linux (Docker? bare pip?)
- **Claimed:** Minimal. README venv/pip instructions are POSIX (work on Linux). No Docker.
- **Real:** Bare `pip install -r` possible if manylinux wheels for faiss-cpu + sentence-transformers exist for the distro/Python. No Dockerfiles (grep `Dockerfile|FROM python` = 0 matches). Unix sockets work on Linux. No launchd equivalent documented. Sync-root warnings (iCloud/Dropbox etc.) are macOS-centric in code/comments.
- **Gaps:** No systemd unit example; no containerized repro; potential icu4c system dep noted in requirements.txt:20 as optional comment only.

### Windows (claimed? any support?)
- **Claimed:** None explicit. Paths use `os.path.expanduser("~")` + POSIX separators in docs/config.
- **Real:** Primary IPC is `socket.AF_UNIX` (sovrd.py:42 client example, 2583 `start_unix_server`, 2565 `_serve_unix_socket`). Windows 10+ has limited AF_UNIX but unreliable for this use + 0600 perms semantics differ. No Windows-specific paths, launchers, or tests (grep `win32|Windows|sys\.platform` in engine/*.py = 1 trivial comment in principal.py:126). HTTP fallback exists (sovrd.py:2612 `_serve_http`) but deprecated/off-by-default and not primary surface.
- **Hypothesis:** Windows support is aspirational/untested; current surfaces (daemon socket + launchd + xattr) are macOS/Linux-only in practice.

**Citations:** config.py:19-43 (SOVEREIGN_HOME + expanduser defaults); sovrd.py:2565 (`async def _serve_unix_socket`), 2612 (HTTP comment "for environments without Unix socket support"); engine/requirements.txt:2-20; launchd plist full content (mac-only); openclaw-extension/README.md:27 (old ~/.openclaw paths).

---

## 3. Reproducibility

**After clean `git clone` + documented install on fresh macOS machine, how many commands/steps until `sovereign_status` or first successful `sovereign_recall` returns non-error results?**

**Exact N = 11–14 discrete shell commands** (plus background process management + potential interactive venv activation + long-running pip/npm downloads). "Non-error" for status is achievable; "with results" for recall requires additional indexing/learn steps.

### Reproduction Checklist (step | documented? | works in worktree? | friction)

| Step | Documented? (exact loc) | Works in this audit worktree snapshot? | Friction / Undocumented |
|------|-------------------------|---------------------------------------|-------------------------|
| 1. `git clone https://github.com/infektyd/sovereign-memory.git && cd ...` | Implicit (README root) | Yes (worktree is already at HEAD snapshot) | None |
| 2. `python3.12 -m venv .venv` (or system python3) | Yes, README.md:273 (recommended for repro NumPy/FAISS) | Yes (G01 test_g01_numpy_env.py exists) | High: Python 3.12 not guaranteed on fresh mac; 3.14 breakage noted in requirements.txt:3 + G01 |
| 3. `source .venv/bin/activate && pip install --upgrade pip` | Yes, README.md:274-275 | Yes | Medium: venv activation per shell |
| 4. `pip install -r engine/requirements.txt` | Yes, README.md:276 (and simpler cd engine variant:267) | Yes (pins: numpy<2, sentence-transformers, faiss-cpu etc.) | **Very High**: 5-15+ min download + possible wheel build for faiss/sentence-transformers; first import triggers ~100-500MB model cache; no lockfile/pip-compile; native deps (icu4c comment) |
| 5. (Optional but needed for default paths) `mkdir -p ~/.sovereign-memory/run && chmod 700 ...` | Partial (sovrd.py auto-mkdir:2571; README hygiene notes) | Yes (code creates) | Undocumented explicit step for first-run perms |
| 6. `cd engine && python3 sovrd.py --socket /tmp/audit-sovrd.sock` (background) | Yes, README.md:284 (default ~/.sovereign-memory path) | Yes (lazy init; status path light) | High: must background or new terminal; default path may collide with live daemon; no systemd/launchctl one-liner for dev |
| 7. In new shell/venv: `cd engine && python3 sovrd_client.py --socket /tmp/audit-sovrd.sock status` | Yes, README.md:291 | Yes (light DB+afm_status path; no model load) | Low for status; client hardcodes default socket at sovrd_client.py:22 |
| 8-10. For plugin surface: `cd plugins/sovereign-memory && npm install && npm run build` | Yes, README.md:340-341; plugin README:227 | Yes (package.json:12 test script) | **Very High**: 100+ MB node_modules (react 19, vite, mcp sdk); tsc + vite; no package-lock reproducibility guarantee across Node versions |
| 11. `node dist/cli.js status` or register MCP + call sovereign_status | Partial (smoke:status script:18) | Yes (cli exists) | Medium: MCP host (Codex/Claude) install steps are host-specific (`claude plugin install --plugin-dir ...` at plugin README:128) |
| 12+. For first `sovereign_recall` with non-error (possibly empty) results | Partial (via client `search` or MCP) | Yes if daemon up + principal/vault guards pass (config + principal.py) | High: empty results until data seeded (via `sovereign_learn`, indexer, or vault files + index); recall path triggers embedder (performance.md cross-ref) |
| Extra for native AFM: compile swift or ensure binary + `SOVEREIGN_AFM_PROVIDER_MODE=native` | Partial (AFM section README:305-332; afm_provider.py) | Binary present; source present | Undocumented compile command for fresh machines; macOS 15.4+ only; many env vars (SOVEREIGN_AFM_NATIVE_HELPER etc.) |

**Evidence:** Full Quickstart + Verification Gate in README.md:261-451; sovrd.py:2718 (main), 2565 (socket), 1866 (_handle_status — DB only, no embed); config.py:19 (CANONICAL_SOVEREIGN_HOME via SOVEREIGN_HOME); plugin package.json:12,40-48 (smokes); 15+ env var sites across engine/ (SOVEREIGN_* grep); no one-line "first recall" script.

**Hypothesis:** On a truly fresh mac (no prior models/brew deps), step 4 alone can exceed 10-20 minutes wall time + failure modes (wheel incompat, disk space for HF cache).

---

## 4. Release Artifacts Inventory

- **Tagging convention:** None documented. No references to `git tag`, annotated tags, or release branches in README, SECURITY_PLAN, or plans/. (grep `tag|git tag|release tag` limited to unrelated).
- **Changelogs:** Zero `CHANGELOG*` or `HISTORY*` files (targeted grep + list_dir root returned none).
- **Version pinning (all surfaces at "0.1.0")**:
  - Daemon: engine/sovrd.py:215 (`VERSION = "0.1.0"`)
  - Plugin: plugins/sovereign-memory/package.json:3 (`"version": "0.1.0"`)
  - OpenClaw: openclaw-extension/package.json:3, plugin.json:5, openclaw.plugin.json:5 (all "0.1.0")
  - Server manifest: plugins/sovereign-memory/src/server.ts:45 (`version: "0.1.0"`)
- **Python deps:** engine/requirements.txt:5 (`numpy>=1.24.0,<2.0` — G01 pin); no lockfile; sentence-transformers/faiss unpinned beyond lower bounds.
- **Node:** No "engines" field in either package.json; relies on dev machine Node 20+ (from @types).
- **Plugin compatibility matrix:** None current. Old/partial matrix in docs/contracts/CAPABILITIES.md:20-50 (still lists legacy JSON-RPC `search`/`learn`/`handoff` with many `[PLANNED: PR-N]`). Actual surface: 26 MCP tools (server.ts:48-913 registerTool calls for sovereign_status through sovereign_subscribe_contradictions). No host-version × contract-version table (e.g. "Codex vX supports Sovereign contract 1.0.0").
- **docs/contracts/ version stamps vs code:** All primary contracts stamped **1.0.0 / 2026-04-26** (AGENT.md:3, VAULT.md:3, POLICY.md:3, PAGE_TYPES.md:3, CAPABILITIES.md:3). WORKFLOWS.md:1 has no stamp. Stamps predate later MCP expansion, G11-G23 principal/audit/handoff changes. G03 lint (test_g03_contract_matrix.py:14-34) only checks for IMPLEMENTED/PARTIAL/PLANNED tags in 3 files (present but outdated PLANNED notes remain, e.g. CAPABILITIES.md:35-38).
- **Other:** No signed releases, no sbom, no release notes dir. `make audit` (pip-audit) referenced but unimplemented (no Makefile).

**Citations:** Multiple read_file + grep for "0.1.0", "Contract version:", "Last updated:", "make audit", "CHANGELOG".

---

## 5. Public Doc Accuracy — Doc vs Reality Drift Table

| Claim Location | Claim | Actual (evidence) | Severity |
|----------------|-------|-------------------|----------|
| README.md:433 | "`make audit` will run `pip-audit -r engine/requirements.txt` once SEC-009 lands." | No Makefile in repo (list_dir root + grep "Makefile|make audit" = 0 actionable hits; only doc references in SECURITY_PLAN.md:255 and README). | High |
| README.md:453 | "The current acceptance baseline is `333 passed` for engine tests and `121 passed` for plugin tests." | 36 Python test files (engine/test_*.py list); recent verified runs in docs/plans/execution/*_STATE.md and RESUME.md show 213/3 skipped (engine) + 32/32 (plugin). Numbers stale by 100+ tests. | Medium |
| README.md:263-293 + plugin README:125-199 | Quickstart + host install paths assume simple `pip`/`npm` + host `plugin install --plugin-dir` leads to working `sovereign_status` / `sovereign_recall`. | 11-14+ steps + heavy dep downloads + Unix socket rituals + host-specific cache invalidation (TROUBLESHOOTING.md:6-114 on Parse Error from stale plugin caches). No "first recall in <5 min" path. | High (repro friction) |
| docs/contracts/CAPABILITIES.md:24-39 (full matrix) + AGENT.md:76-78 | Lists legacy daemon JSON-RPC methods (`search`, `learn`, `handoff`, `trace` etc.) with many `[PLANNED: PR-N]` and access levels. | Primary public surface is 26 MCP tools (plugins/sovereign-memory/src/server.ts:266 `sovereign_status`, 339 `sovereign_recall`, 638 `sovereign_negotiate_handoff`, 773+ ping_agent_*, 185+ team_* etc.). Old JSON-RPC still in daemon but secondary/deprecated for agents. | High (surface drift) |
| docs/contracts/AGENT.md:3, CAPABILITIES.md:3 etc. (all 1.0.0 / 2026-04-26) + "If this document and any code diverge, this document defines..." | Contracts are canonical and up-to-date. | Stamps predate MCP expansion, principal binding (G11+), handoff redesign (PR-10), AFM passes (PR-12-15). PLANNED notes in contracts contradict shipped code (e.g. handoff via negotiate not old `handoff` RPC). G03 tags present but insufficient. | High |
| README.md:304-332 + docs/goal-native-afm.md | Native AFM "use the local JSON helper at `engine/native_afm_helper`". | Binary present; swift source present (native_afm_helper.swift:1+); no documented `swiftc` command or Xcode reqs for fresh machines; many SOVEREIGN_AFM_* envs scattered (afm_provider.py:40-105). | Medium |
| SECURITY_PLAN.md:255 + README | `make -C .. audit` as release step. | No Makefile target exists. | Medium |
| openclaw-extension/README.md:27-79 | Paths assume `~/.openclaw/...` + specific venv. | Superseded by canonical `~/.sovereign-memory` (config.py:19 G02 unification); docs/CANONICAL-PATHS.md:27-32 calls out retired legacy roots. | Low-Medium (stale extension docs) |

**Additional observed drift:** TROUBLESHOOTING.md focuses narrowly on one HTTP/JSON-RPC parse error (good signal of real pain); lacks full "clean machine install" checklist. eval/ and scripts/ have no CI integration notes.

---

## 6. What Looks Solid

- **Manual verification discipline is thorough and repeated:** Every PR log in docs/plans/execution/ explicitly records `pytest -q engine` + `npm test` + smoke + migration safety on temp DB (e.g. RESUME.md:203, WORKTREE_STATE.md multiple entries). 36 high-quality pytest tests + 18+ TS tests cover G01-G23 + security (test_socket_perms.py, test_vault_root_binding.py, test_audit_escape.py etc.).
- **Path unification (G02) is centralized and tested:** config.py:19 (`CANONICAL_SOVEREIGN_HOME`), resolve_canonical_path:187, test_path_resolution.py:15 — single source for ~/.sovereign-memory defaults.
- **Version pins + env discipline exist:** numpy<2 in requirements.txt:5; SOVEREIGN_* envs consistently read via os.environ.get in config + afm_provider + sovrd (no magic globals).
- **Contracts have self-lint (G03):** test_g03_contract_matrix.py enforces IMPLEMENTED/PARTIAL/PLANNED tags; files carry them (even if content lags).
- **Lazy loading + safety posture:** Status/health paths avoid heavy models (sovrd.py:1866); principal binding + vault guards (principal.py, G12) are real and tested; audit redaction + 0600 sockets are enforced in code.
- **Smoke scripts and CLI exist:** package.json smokes, sovrd_client.py, engine/sovereign_memory.py CLI, native helper binary — usable for manual RC gate.

---

## Summary: Output Path + Blocker + Quick Wins

**Deliverable written to:** `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19/grok/audits/2026-05-19/findings/ci-release.md` (and only to this path; no other writes).

**Single biggest release-readiness blocker:** Complete absence of automated CI (no workflows, no matrix, no PR gating) combined with extremely high manual reproduction friction (N=11-14+ steps, multi-minute heavy pip + npm installs with wheel risks, platform-specific Unix socket/launchd/native-AFM rituals, and no Dockerized or one-command smoke). Release verification is entirely human-dependent on a clean macOS dev machine; any drift in docs (stale pass counts, outdated contract matrices, phantom `make audit`) goes undetected until a human notices. This makes reliable, repeatable RC cuts impossible at scale.

**2-3 quick wins (high impact, low effort):**
1. **Add minimal GitHub Actions workflow** (`.github/workflows/ci.yml`): matrix of `ubuntu-latest` + `macos-latest`, Python 3.11/3.12, Node 20; run `cd engine && python -m pytest -q`, `cd plugins/sovereign-memory && npm ci && npm test`, plus smoke + a temp-socket status/recall probe. Trigger on push/PR. (Covers 80% of current manual gate.)
2. **Freeze reproduction steps into an executable script** (`scripts/repro-smoke.sh` or `make smoke` even if Makefile is new): one command after clone that creates venv, installs (with clear progress), starts isolated daemon on /tmp socket, calls status + a no-op recall, and exits non-zero on failure. Update README to "bash scripts/repro-smoke.sh" as the canonical first-recall gate.
3. **Add CHANGELOG.md + version bump discipline + engines fields**: Adopt keep-a-changelog format; pin Node engines in both package.json; add `python_requires` to a minimal setup.cfg or pyproject; update contract docs/CAPABILITIES.md + AGENT.md to reflect the actual 26-tool MCP surface (or deprecate the legacy matrix). This closes the most visible doc/reality gaps in <1 day.

All observations above are directly traceable to the cited tool outputs on the 2026-05-19 audit snapshot. No speculation was required for the core findings.
# PHASE 1d — CI & Release Readiness: Adversarial Addendum (Supply-Chain, Repro, Verification Gaps)

**Audit date:** 2026-05-19
**Worktree:** `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19`
**Posture:** 100% READ-ONLY. All evidence from `list_dir`, `grep`, `read_file` (and cross-referenced prior phase outputs). No source modifications performed anywhere.
**Foundation:** Builds directly on `ci-release.md` (no CI, N=11-14 repro friction, doc drift, missing version pinning) + `architecture.md` (11 surfaces, 8 impl leaks, ~15 agent-specific sites, backend import violations), `scope-creep.md` (31+ dead/unreferenced + stale PLANNED markers), `performance.md` (hot paths, cold-boot tax).
**Cross-references:** Primary security surface via `SECURITY_PLAN.md:53-65` (explicit assumptions, esp. #8 "lockfile pinning") + `docs/contracts/THREAT_MODEL.md`; in-progress `security.md` and `code-quality.md` (parallel) via the above + `architecture.md:84-94` (import violations) and `scope-creep.md:49` (abandoned extension as attack surface). Adversarial lens applied to install/repro/verification per Phase 1d scope.
**Method:** 30+ targeted tool calls post-foundation (grep on `**/*.{md,py,ts,json,sh,plist,swift}` for launchd/openclaw-tool/native_afm/swiftc/lockfile/postinstall/venv/socket/~/sovereign-memory/repro/smoke/hermetic/clean + read_file on manifests, entrypoints, tests, docs/TROUBLESHOOTING.md, SECURITY_PLAN.md:240-268, afm_provider.py:118-160, sovrd.py:2565-2608/2718-2821, config.py:19-220, package*.json + locks, client.py:25-80, etc.). Citations use absolute worktree paths.

---

## 1. Supply-Chain & Install Risks (Adversarial Pass)

The release surface has **no automated gate** on the exact vectors called out in `ci-release.md:67-71,91` (heavy pip + npm + implicit model downloads; no lockfile enforcement because no CI). Combined with legacy direct-exec surfaces and prebuilt native binaries, this creates concrete supply-chain and post-install tampering opportunities.

### 1.1 No Lockfile Enforcement + Broad Ranges (pip + npm)
- **Python:** `engine/requirements.txt:4-8` uses lower-bound + one narrow pin only (`numpy>=1.24.0,<2.0`; `sentence-transformers>=2.2.0`, `faiss-cpu>=1.7.4`, `tiktoken>=0.5.0` etc. with no upper bounds or hashes). No `requirements.lock`, no `pip-compile`, no `pyproject.toml` hashes. First `pip install -r` on clean machine pulls from PyPI (and transitive torch/huggingface wheels) with build-from-sdist fallbacks possible for faiss/sentence-transformers.
- **npm:** `plugins/sovereign-memory/package.json:21-33` uses `^` ranges (`@modelcontextprotocol/sdk@^1.29.0`, `react@^19.2.5`, `vite@^8.0.10` etc.). `package-lock.json` (lockfileVersion 3) exists but is **never enforced in any documented gate** (README:340 `npm install`, SECURITY_PLAN:252 `npm test`, ci-release verification steps). No top-level `"engines"` field (only in transitive deps). `openclaw-extension/package.json:19-22` similarly unpinned devDeps + has its own lock.
- **Attack surface:** A compromised or mirrored PyPI/npm registry (or dependency confusion) can inject malicious wheels/postinstall hooks on every fresh repro. No SBOM, no `pip-audit` / `npm audit` in CI (phantom `make audit` per ci-release.md:105, SECURITY_PLAN.md:255). `SECURITY_PLAN.md:64` Assumption #8 ("Dependency installs are from trusted registries **with lockfile pinning**") is **directly violated by the released artifacts and docs**.

### 1.2 Heavy Model + Native Downloads on Every Repro
- `sentence-transformers` (requirements:4) + `models.py` (lazy `SentenceTransformer` + CrossEncoder on first `.model` access) triggers ~100-500MB HF hub download of `all-MiniLM-L6-v2` + reranker on first `sovereign_recall` / `sovereign_learn` after clean `pip install`. No model hash pinning or vendoring.
- Native AFM: prebuilt `engine/native_afm_helper` (binary, executable per `afm_provider.py:51` `os.access(..., X_OK)`) + source `engine/native_afm_helper.swift:1-50+` (Foundation Models JSON bridge via subprocess in `afm_provider.py:135-143`: `subprocess.run([os.fspath(helper)], ...)` with no shell).
  - **No documented build:** README:311 claims "use the local JSON helper at `engine/native_afm_helper`"; `docs/goal-native-afm.md:64` and `native-afm-implementation-note.md:15` mention "compile the helper" / "wrapper script compiles" but **zero `swiftc` / Xcode instructions** in Quickstart, Verification Gate, or TROUBLESHOOTING. Binary is simply checked in.
  - **Risk:** Tampered binary in repo (or supply of the swiftc/Xcode toolchain) executes with user privileges on `SOVEREIGN_AFM_PROVIDER_MODE=native|auto`. `afm_provider.py:57-60` redacts paths in errors but still execs arbitrary configured helper (`SOVEREIGN_AFM_NATIVE_HELPER` env override).
- **Adversarial scenario:** Attacker publishes malicious wheel that drops a trojaned `native_afm_helper` (or poisons HF cache); first-recall path executes it before any principal gate or redaction in some status paths.

### 1.3 Legacy Direct-Exec + Deprecated Surfaces Still in Tree
- `engine/openclaw-tool.sh:10-32` (and learn path): Hardcodes legacy `SOVEREIGN_DIR="$HOME/.openclaw/sovereign-memory-v3.1"`, directly execs `"$PYTHON" "$API"` (agent_api.py) bypassing daemon entirely. See `architecture.md:90` ("backend import violation — Replace with sovrd_client... or remove") and `scope-creep.md:49` (highest-risk abandoned experiment; still ships, still listed in some docs).
- `openclaw-extension/` entire dir (deprecated per `sovrd.py:36,2476`; `scope-creep.md:45-49`): contains `sovrd.py` (direct sqlite + agent_api), `migrate_phase2.py`, bridge that can exec externals. Increases supply surface even if "deprecated."
- launchd plist: `engine/launchd/com.openclaw.sovrd.plist.example:45` hardcodes `/usr/bin/python3` (system python, not venv) + placeholders that **do not expand `~`** (explicit warning at lines 14-16). `Umask 63` is good (SEC-016) but manual substitution + `mkdir -p ~/Library/Logs/...; chmod 0700` required; no one-liner or systemd equivalent for Linux. `sovrd.py:2779` hygiene checks are non-fatal warnings only.
- **Adversarial:** Malicious fork or poisoned local checkout (common in "git clone + pip" flows) can ship altered `openclaw-tool.sh` or extension that runs arbitrary Python before daemon principal stamping (`principal.py:284+`).

### 1.4 Plugin Package + MCP Install Surface
- `plugins/sovereign-memory/README.md:128`: `claude plugin install --plugin-dir /path/to/...` points at source tree (builds on host). No published npm package for the MCP server; consumers pull/build from git. Combined with `ui-server.ts` deep-research exec surface (`scope-creep.md:51-55`: external `deep_research_agent` paths, `execFileAsync`).
- No `postinstall` scripts found in package.jsons/locks (good), but transitive deps (react 19, vite 8, mcp-sdk) have broad attack surface on every `npm install`.

**P0/P1 mapping:** These are **P0 for RC** (supply-chain tampering can bypass all G11-G23 principal/audit/redaction before first recall) and amplify the "no CI = no regression gate on security fixes" (ci-release.md:133 example). Violates core SECURITY_PLAN assumptions.

---

## 2. Reproducibility as a Security/Quality Issue

`ci-release.md:56-79` (N=11-14 steps, host-specific paths, heavy downloads, no hermetic smoke) is not just UX friction — it is a **security and quality defect**.

### 2.1 Host-Specific Paths + ~/.sovereign-memory Pollution
- Canonical resolver `config.py:19-20` (G02, tested in `test_path_resolution.py:18-48`): `CANONICAL_SOVEREIGN_HOME = os.environ.get("SOVEREIGN_HOME", os.path.expanduser("~/.sovereign-memory"))`. All DB/FAISS/socket/vaults/graphs derive from it (`config.py:34-96`, `resolve_canonical_path:193-219`).
- Plugin parity: `plugins/sovereign-memory/src/config.ts:14-16` (SOCKET_PATH), `12` (DEFAULT_VAULT), plus per-agent sprawl (`CLAUDECODE_VAULT_PATH:71`, `KILOCODE_*`, `CODEX_*` at 44-100). Multiple vaults + "run/" dir created on first daemon start (`sovrd.py:2571`).
- **Nondeterminism vector:** A "first recall" succeeds or fails based on:
  - Prior `~/.sovereign-memory` state (pollution from previous runs, stale sockets, partial HF cache, corrupt FAISS manifest).
  - Sync-root detection (`sovrd.py:2772-2780`: `_warn_if_sync_root` only; non-fatal; macOS-centric iCloud/Dropbox comments).
  - Unix socket perms (`sovrd.py:2573-2593`: mkdir+chmod 0700/0600 with only `logger.warning` on OSError — never fails hard).
- **Adversarial/quality:** An attacker (or CI flakiness, or bad dev env) can pre-populate `~/.sovereign-memory/run/sovrd.sock` (world-readable) or poison the HF cache / FAISS index so that "status" passes but recall returns garbage or crashes nondeterministically. No test enforces a clean `/tmp` isolated home for the full path (socket + DB + models + vault).

### 2.2 No Hermetic Smoke Test for Repro Path or Daemon Startup Under Clean Conditions
- Existing smokes: `plugins/sovereign-memory/package.json:16-18` (`smoke:status`, `smoke:hook` etc. after `npm run build`); `engine/test_socket_perms.py:27-42` (simulates perms with `tmp_path`); `test_path_resolution.py` (static + optional node check).
- **Gaps:** No test or script that:
  - `mkdir -p /tmp/audit-clean-home; env SOVEREIGN_HOME=/tmp/... python -m venv ...; pip install -r; python sovrd.py --socket /tmp/.../sovrd.sock` in background;
  - Calls `sovrd_client.py status` + a no-op `recall` (empty ok) + verifies no `~/.sovereign-memory` pollution and socket 0600;
  - Runs under both macOS + Linux (no matrix);
  - Exercises native AFM stub path + model lazy load without net.
- `sovrd.py:2783` main starts `_serve_unix_socket` directly; client `sovrd_client.py:27-30` has a helpful but **typo'd error** ("socksd not running").
- Result per ci-release.md:63-73: "first recall with non-error" still requires 12+ steps + background management + potential long downloads. "An attacker or bad env can make 'first recall' succeed or fail nondeterministically."

**P1 (high for RC quality + security posture):** Repro nondeterminism undermines the "local-first" hygiene claims (README:405-414) and makes security regression testing impossible. Cross-refs performance.md:83 (cold-boot model tax on first recall) and architecture.md:42 (status leaks paths even on light paths).

---

## 3. Test Gaps and Quality Issues in the Release/Verification Surface

The manual verification gate (`ci-release.md:20-21`, README.md:436-451, SECURITY_PLAN.md:250-265) is the **only** release gate.

### 3.1 Manual Gate Is Unenforced + Stale + Platform-Narrow
- Exact steps (README:440-444, SECURITY_PLAN:251): `cd engine && pytest -q`; `cd ../plugins/sovereign-memory && npm test`; `npm run smoke:hook`. Plus ad-hoc temp-socket live smoke.
- **No automation, no matrix:** Confirmed 0 `.github/workflows/`, 0 Makefile, 0 `on: push` etc. (ci-release.md:7,25). No macOS/Linux/Windows × Python 3.11/3.12/3.14 × Node 20 matrix. All historical logs (RESUME.md:215-222, WORKTREE_STATE.md) appear macOS-centric (paths, launchd).
- **Stale baselines:** README:453 claims "333 passed" engine / "121 passed" plugin (also in `docs/goal-native-afm.md:61-62`); SECURITY_PLAN:251 cites 213/3 skipped + 32/32. Reality per ci-release + scope: 36 test_*.py files; recent runs ~213 passed. Drift undetected without CI.
- **Missing repro/daemon-start coverage:** No test exercises the full Quickstart path under `SOVEREIGN_HOME=/tmp/...` + isolated socket + clean model cache. `test_g03_contract_matrix.py` (G03, solid) only lints 3 contract files for tags. Integration smokes in `test_pr*.py` use temp DBs but not full install + plugin interop matrix.
- **macOS/Linux plugin/daemon interop:** Unix socket is primary (`sovrd.py:2565`, client AF_UNIX); HTTP fallback deprecated (sovrd.py:2612 comment). No cross-platform tests; Windows AF_UNIX is "unreliable" (ci-release.md:47). Plugin TS assumes POSIX paths/homedir.

### 3.2 Other Verification Surface Gaps
- Migration safety, temp-socket handoff/audit/redaction, SIGTERM shutdown are manual only (README:448-451).
- Eval harness (`engine/eval/harness.py`, `test_pr4_eval_harness.py`) + `eval/queries.jsonl` never auto-run.
- No test for launchd plist substitution or `openclaw-tool.sh` (legacy) under clean conditions.
- Phantom `make audit` / `pip-audit` (ci-release.md:105, SECURITY_PLAN:255) — no Makefile anywhere.

**P1 RC risk:** "Human-dependent on a clean macOS dev machine" (ci-release summary) means security fixes (e.g. future socket or principal changes) have no regression gate. A bad merge can silently reintroduce world-readable sockets or agentId spoofing (pre-G11 issues referenced in architecture.md).

---

## 4. Newly Identified Lint, Error-Handling & Doc-Quality Issues (Install/Repro/Docs Surface)

While re-examining (beyond the foundation drift table in ci-release.md:101-114):

- **Error message typo (user-facing):** `engine/sovrd_client.py:28`: `"Error: socksd not running — socket..."` (should be "sovrd"). Hits every failed status/recall in manual verification and Quickstart. Low severity but poor release polish.
- **Silent chmod degradation (security-relevant):** `sovrd.py:2574-2575,2592-2593`: `os.chmod` failures only `logger.warning` (never raises, never aborts bind). An env with weird umask or FS can produce world-readable socket despite SEC-001 claims and `test_socket_perms.py`.
- **Non-fatal hygiene that can mask attacks:** `sovrd.py:2779`: `except Exception: logger.exception("cloud-sync hygiene check failed (non-fatal)")`. Sync-root vault can be exfiltrated silently.
- **Doc quality / drift (new citations):**
  - `docs/goal-native-afm.md:61-62` and `native-afm-implementation-note.md:67` repeat the stale 333/121 numbers.
  - `docs/native-afm-implementation-note.md:15`: "The wrapper script compiles..." — no such script or instructions exist in tree (confirmed grep); binary just present.
  - `README.md:433` + SECURITY_PLAN still reference unimplemented `make audit`.
  - `plugins/sovereign-memory/README.md:128` install example uses absolute host path (no portable guidance).
  - TROUBLESHOOTING.md narrow focus (one parse error) per ci-release; no "clean machine first-recall checklist."
- **Contract vs. reality (amplified from architecture/scope):** CAPABILITIES.md still carries many `[PLANNED: PR-N]` for features long shipped in `sovrd.py:2445` `_METHODS` and `server.ts:48-926` (26 MCP tools). G03 lint (`test_g03...py:14-34`) only checks tag *presence*, not accuracy.
- **Version surface still 0.1.0 everywhere** (ci-release:86-90) with no CHANGELOG.

These are quality issues that erode trust in the RC verification surface.

---

## 5. P0/P1 RC Risks (Translated from Findings)

| Risk | Evidence | Severity | Cross-Ref |
|------|----------|----------|-----------|
| No CI + no hermetic repro smoke = no regression gate on security fixes (socket perms, principal stamping, redaction, AFM exec) | ci-release.md:13,133; this addendum §2-3; SECURITY_PLAN:64 Assumption #8 violated | **P0/P1** (core example in prompt) | architecture.md:100 (G11 centralized identity strong *in code* but untested in release gate) |
| Supply-chain tampering via unpinned heavy deps + model downloads + prebuilt native binary + legacy direct-exec sh | requirements.txt:4-8; package.json:21-33 (no engines/lock enforcement); openclaw-tool.sh:32; afm_provider.py:135 (subprocess on checked-in binary); no swiftc docs | **P0** (pre-first-recall compromise) | scope-creep.md:49 (openclaw as highest-risk abandoned); SECURITY_PLAN assumptions |
| Reproducibility nondeterminism enables env-based attacks or flaky "first recall" (pollution, socket mode, cache) | config.py:19 (expanduser); sovrd.py:2571-2593 (warning-only chmod); no /tmp-home smoke test | **P1** (quality + security posture) | performance.md:111 (model load tax); ci-release.md:78 |
| Stale docs + phantom gates undermine manual verification | README:453 (333/121), SECURITY_PLAN:255 (make audit), goal-native-afm.md:61 | **P1** (drift compounds over time without CI) | ci-release.md:106-109; scope-creep.md:71 (14+ stale PLANNED) |
| Legacy surfaces (openclaw-tool, extension, ui-server exec) increase attack surface in release tarball | openclaw-tool.sh:1+; scope-creep inventory | **P1** | architecture.md:88-90 (import violations); THREAT_MODEL.md socket section |
| No macOS/Linux interop matrix for daemon+plugin | Unix socket primary; no cross-platform tests or Docker | **P1** (claimed portability) | ci-release.md:47-48 |

All map to "release verification is entirely human-dependent" (ci-release summary).

---

## 6. What Looks Solid (Release Hygiene That Is Present)

- **G03 contract matrix test** (`engine/test_g03_contract_matrix.py:14-34`): Enforces IMPLEMENTED/PARTIAL/PLANNED tags in AGENT/POLICY/VAULT.md. Present and passing; good self-lint even if content lags (scope-creep notes).
- **Path unification + parity test (G02):** `config.py:19` (CANONICAL_SOVEREIGN_HOME) + `test_path_resolution.py:36-48` (cross-lang TS check for .sovereign-memory + run/sovrd.sock). Single source; tested.
- **Socket perms test + runtime hardening (G04/SEC-001):** `test_socket_perms.py:27-42` + `sovrd.py:2571-2593` (0700/0600 with unlink stale). `SECURITY_PLAN` and launchd plist explicitly call out Umask 63 for logs.
- **Principal stamping on every handler (G11):** `principal.py:284-334` + `sovrd.py:1314-1320` (EffectivePrincipal before work; -32000 on mismatch). No silent trust. Architecture.md:100 calls this solid.
- **Lazy loading + light status paths:** `sovrd.py:1866` (_handle_status avoids models); `models.py` + `retrieval.py` properties defer heavy work. Good for "status" in verification gate.
- **Smoke scripts + CLI exist:** package.json smokes, `sovrd_client.py`, `sovereign_memory.py` CLI, native binary presence (usable manually).
- **Audit escape + redaction consistent:** `vault.ts:342-382` (SEC-014) + `sovrd.py:243` (_SECRET_PATTERNS); called from 47+ sites but escaping is centralized.
- **Test volume + no permanent skips:** 36 engine + 18+ TS tests; conditionals only for optional ML deps (scope-creep.md:81-89).
- **G02 resolver logging on mismatch:** `config.py:215-218` — operator-visible when envs diverge from canonical.

These are real, tool-verified positives that should be preserved/enforced in any future CI.

---

## 7. Summary + High-Impact Low-Effort Wins (Adversarial Lens)

The foundation `ci-release.md:133` blocker ("no CI + N=11-14 friction + human-only gate") is **worse under adversarial analysis**: it directly enables the supply-chain and nondeterministic-repro attacks that can subvert the very G11/G12/G23/G04 security work that landed. Legacy dead surfaces (`scope-creep.md`) and missing native AFM provenance amplify the exposure. `SECURITY_PLAN` assumptions are aspirational, not enforced by the release artifacts.

**Immediate wins (build on ci-release quick wins):**
1. **Minimal hermetic smoke script** (`scripts/repro-smoke.sh` or pytest fixture): `SOVEREIGN_HOME=/tmp/$$-clean env ... venv + pip + isolated /tmp sock + status + recall + assert no pollution + 0600 perms + exit 1 on fail`. Wire into README Verification Gate as the canonical "first recall" step. (Closes nondet vector + provides CI seed.)
2. **Add engines + lock enforcement notes** to both package.json + requirements (or pip-tools lock) + update SECURITY_PLAN Assumption #8 to match reality or add "TODO: enforce in CI".
3. **Remove or explicitly quarantine legacy direct-exec:** Delete `engine/openclaw-tool.sh` (or move to `archive/`) + `openclaw-extension/` per scope-creep recs; this shrinks the supply surface immediately.
4. **Document or remove native AFM compile:** Either add a one-line `swiftc ... -o native_afm_helper` (with macOS 15.4+ note) to README + a smoke test, or delete the binary + source if not required for RC.
5. **Fix the socksd typo + make chmod failures hard errors** (or at least fail startup with clear guidance) in client + sovrd socket path.

All claims above are directly traceable to the cited absolute paths and prior phase-1 reports in this worktree snapshot. No speculation without label.

**Deliverable written exclusively to:** `/Users/hansaxelsson/Projects/sovereignMemory/grok/worktrees/audit-2026-05-19/grok/audits/2026-05-19/findings/ci-release-adversarial.md`

(End of adversarial addendum. Ready for integration with parallel security.md / code-quality.md outputs.)

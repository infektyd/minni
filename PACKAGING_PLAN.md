# Minni — Packaging & Presentation Plan (for maintainer approval)

Status: **PROPOSED — awaiting maintainer approval before implementation.**
Scope: packaging and presentation only. No engine, retrieval, governance, or benchmark
logic changes. Every code-adjacent hook is flagged below and isolated in its own PR.

---

## 0. Repo facts this plan is built on (verified against the tree)

- **No `pyproject.toml` exists.** Python deps live in `engine/requirements.txt`, unpinned
  (`>=` bounds), with no lockfile. There is no installable Python package, no console
  script, and no `minni` CLI — the daemon is `python engine/minnid.py`, the client is
  `python engine/minnid_client.py`.
- `engine/` is a **flat module layout** (`db.py`, `models.py`, `config.py`, imported as
  top-level names). A naive wheel would install `db`, `models`, `config` into
  site-packages root — unshippable to PyPI without a package-layout change, which is
  engine-adjacent churn. This constrains the v0.1 channel choice (see §1).
- **Python 3.14 pin** lives in `.python-version`, `Makefile` (hard version check),
  `ci.yml`, and README. A feature grep found nothing 3.14-specific (only 3.10+
  `match/case`), so the pin appears to be policy, not necessity — but per the brief the
  pin is **kept intact**; we solve it with interpreter provisioning, not relaxation.
- Node side: `plugins/minni/package.json` (`minni-multi-plugin` 0.1.0, lockfile present,
  no `bin`/`files`/`exports`). Adapter manifests exist for claude-code, codex, gemini,
  kilocode (no `.grok-plugin` dir; grok is wired via `propagate.py --platform grok`).
- Versions already agree at **0.1.0** (daemon `VERSION` constant, all plugin manifests) —
  good; the first tag can be `v0.1.0` with no version bumps.
- CI (`ci.yml`) already runs lint boundary checks, both test suites, and the hermetic
  smoke `scripts/repro-smoke.sh` on clean Ubuntu. **No release workflow, no version tags.**
- `repro-smoke.sh` verifies, in an isolated `$MINNI_HOME`: venv exists → daemon starts →
  DB file created (migrations applied) → `status` RPC returns `daemon`+`engine` keys →
  `search` RPC returns `results` → real `~/.minni` was not touched. A `doctor` command
  can reuse exactly these probes.
- Embedding models (`all-MiniLM-L6-v2`, `ms-marco-MiniLM-L-6-v2`, `nli-deberta-v3-small`,
  ~320 MB total) download lazily inside `engine/models.py` singletons on first retrieval
  call, with no Minni-controlled progress output.
- README is substantive (356 lines) and already contains the comparison section, the
  "evidence, not instruction" framing, Quickstart, and honest caveats — but it **ends
  with a leftover editorial note** ("Want me to write this to README.md on a fresh
  branch…?") that must be removed. Six purpose-built SVGs sit unused in
  `docs/readme-assets/`.
- Hygiene present: `LICENSE` (MIT), `CODEOWNERS`, `FUNDING.yml`, `dependabot.yml`,
  `pr-hygiene.yml`. Missing: `CHANGELOG.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, issue/PR templates, demo recording.
- **Inconsistencies found:** (a) root `.claude-plugin/plugin.json` says `Apache-2.0`
  while `plugins/minni/.claude-plugin/plugin.json` and `LICENSE` say MIT; (b)
  `.gitignore` covers `engine/venv/` but not the actual `engine/.venv/`; (c)
  `bench/README.md` mentions a "3.11 venv" that contradicts the repo's own 3.14 setup;
  (d) README uses two different "four verb" sets (Recall/Learn/Plan/Handoff vs the
  `prepare_task → prepare_outcome → plan → learn` lifecycle spine).
- **No honestly citable benchmark numbers exist** (the only membench report is an
  explicitly labeled stub fixture). The README/docs will therefore contain none.

---

## 1. Distribution channels — recommendation and why

**Primary channel for v0.1.0: hardened source install (`git clone` + one command), with
`uv` provisioning Python 3.14 automatically.**

Why not the alternatives first:

- **PyPI wheel (`pipx install minni`)** — blocked by the flat `engine/` module layout
  (top-level `db`/`models`/`config` would collide in site-packages). Fixing that means
  moving engine files into a `minni/` package and rewriting imports — mechanical but
  large and squarely engine-adjacent. **Deferred to v0.2 as a maintainer decision**
  (see §6). The pyproject metadata we add now is written so the later rename is the
  only remaining step.
- **`curl | sh` bootstrap** — adds an opaque trust step for a security-conscious
  project whose audience reads shell scripts skeptically. Not needed once `uv` handles
  the interpreter.
- **Docker image** — offered as the *secondary* "evaluate in one command" channel
  (`docker run` → daemon + `doctor` pass), because CI already proves the Linux path.
  But Minni's real habitat is the developer's own machine (launchd, per-agent vaults,
  local editors), so the container is a demo/eval vehicle, not the recommended install.

The `uv` choice directly solves the #1 blocker: `uv` downloads and manages CPython 3.14
itself (`uv python install 3.14` happens implicitly via `requires-python`), so the pin
stays intact **and** stops being an adoption filter. `make setup` keeps its current
system-python path and gains a uv path when system `python3` is too old — no behavior
change for existing users.

**npm publication** of `minni-multi-plugin`: deferred (plugins are installed from the
repo via `propagate.py` / marketplace.json today; publishing to npm adds a supply-chain
surface with no current consumer). Metadata (`files`, `bin`, `exports`) is completed now
so the package *could* be published later without changes.

## 2. Release & version scheme

- **SemVer, pre-1.0**: `v0.1.0` first tag (matches every version constant already in the
  tree — no bumps needed). Breaking changes allowed in minors until 1.0, stated in
  CHANGELOG header.
- `CHANGELOG.md` in Keep-a-Changelog format, seeded from git history (grouped by the
  existing conventional-commit prefixes).
- **Release workflow** (`.github/workflows/release.yml`): on tag `v*` — run the full CI
  job including `repro-smoke.sh` as a **release gate**, build Python sdist (wheel once
  PyPI path unblocks) + `npm pack` tarball + (if approved) Docker image, attach all
  artifacts to a GitHub Release with generated notes. Secrets expected (documented, not
  stored): none for GitHub Releases; `PYPI_API_TOKEN` (or trusted publishing) and
  `NPM_TOKEN` only if/when those channels are approved.
- Reproducibility: commit a compiled Python lockfile (`engine/requirements.lock` via
  `uv pip compile`, hash-pinned) used by CI and `make setup`; `package-lock.json`
  already exists.

## 3. First-run install (Section 1 of the brief — done first)

Target: clean machine → passing doctor in <10 min, unattended.

1. **`pyproject.toml`** (in `engine/`, layout-safe): name, description, readme, MIT
   license, classifiers (Development Status :: 4 - Beta), `requires-python = ">=3.14"`,
   dependencies (from requirements.txt, pinned), and
   `[project.scripts] minni = "minni_cli:main"`.
2. **`minni` CLI — new file `engine/minni_cli.py`** (thin, packaging-only; no engine
   imports beyond `minnid_client._rpc`):
   - `minni up` — start `minnid.py` detached, write a PID file under `~/.minni/run/`,
     print the socket path and a first-run banner; `--foreground` passthrough.
   - `minni down` — SIGTERM via the PID file (daemon already handles SIGTERM cleanly).
   - `minni status` — the existing `status` RPC, rendered in plain language.
   - `minni doctor` — reuses the smoke script's exact probes: venv/interpreter ok,
     socket exists with 0600 perms (run dir 0700), daemon `status` RPC returns
     `daemon`+`engine`, `search` round-trips with a `results` key, embedding models
     present in the HF cache (with size note if absent). Plain-language PASS/FAIL lines.
   - `make doctor` target wrapping it. `repro-smoke.sh` stays the CI oracle; doctor
     imports the same client calls so they can't drift apart.
   - ⚠ Flagged packaging hooks (each isolated, minimal): the PID file in `minni up`
     (new file next to the socket, no daemon changes), and the CLI module itself.
3. **First-run download visibility** — one-time stderr banner before the lazy model
   load: "First run: downloading ~320 MB of embedding models to ~/.cache/huggingface;
   this happens once." ⚠ This touches `engine/models.py` (a log line around the three
   singletons, no logic change) — **isolated in its own PR and explicitly flagged; will
   not merge without your sign-off on the diff.**
4. **`make setup` uv path** — if system `python3` < 3.14 and `uv` is available (or
   installable via the documented one-liner), create `engine/.venv` with
   `uv venv --python 3.14`. Existing behavior unchanged when system python suffices.
5. **Acceptance** — a clean-container run (local) of the Quickstart ends in a passing
   `minni doctor`; CI gains a step asserting `make doctor` passes after the smoke test.

## 4. Repo hygiene

- Add: `CONTRIBUTING.md` (real workflow: `make setup/check/smoke`, hooksPath, PR
  hygiene rules that `pr-hygiene.yml` already enforces), `CODE_OF_CONDUCT.md`
  (Contributor Covenant), `SECURITY.md` (short reporting policy → points to
  `SECURITY_PLAN.md`), `.github/ISSUE_TEMPLATE/` (bug + install-problem + feature),
  `.github/PULL_REQUEST_TEMPLATE.md`.
- Badges: CI status, license, latest release, supported Python — real signals only.
- Fixes: add `engine/.venv/` (or `.venv/`) to `.gitignore`; reconcile the
  Apache-2.0/MIT manifest mismatch (→ MIT, matching `LICENSE`, **confirm**); correct the
  stale "3.11 venv" note in `bench/README.md` (doc-only).

## 5. Presentation

- **README rewrite** in the brief's order, building on (not replacing) the existing
  text: hero → problem → what Minni is → comparison **as a table** (content already
  exists as prose) → Quickstart (`git clone` → `make setup` → `minni up` → wire one
  runtime → `minni recall`-style cited result) → honest pre-v1 status → license/links.
  Surface **"Recall is evidence, not instruction"** as its own short section (the
  memory-poisoning defense, enforced at the data layer). Use the existing
  `docs/readme-assets/` SVGs (hero + memory-layers). Remove the leftover editorial tail.
  ᛗ/Minni branding kept consistent across README, docs, and package metadata.
- **Demo**: asciinema cast (checked in + linked GIF) of the four verbs in ~60 s.
  ⚠ Needs your call on the canonical verb set (see §6).
- **Docs restructure** under `docs/`: `docs/index.md` (map), Concepts, Install &
  Troubleshooting (absorb `TROUBLESHOOTING.md`), per-runtime setup pages (Claude Code /
  Codex / Gemini / Grok — thin pages wrapping `propagate.py` usage), Security model
  (links `SECURITY_PLAN.md` + `docs/contracts/THREAT_MODEL.md`), Architecture (one
  request-flow diagram: agent → MCP plugin → socket → daemon → recall/learn/handoff →
  Markdown+SQLite). MkDocs site via GitHub Pages **optional, last, only if everything
  else lands** — the docs tree must read well as plain GitHub Markdown regardless.

## 6. Maintainer decisions of record (resolved 2026-07-02)

1. **PyPI path**: the `engine/` → `minni/` package rename is **approved for v0.2**;
   explicitly out of scope for this effort.
2. **`engine/models.py` banner**: **approved.** PR 3 proceeds; the diff is still posted
   on the PR for review visibility, per the engine-firewall rule.
3. **License of record**: **MIT** everywhere (matches `LICENSE`).
4. **Canonical four verbs**: **recall → learn → approve → handoff.** The governance
   gate ("approve") is the differentiator and leads the README/demo; the plan/lifecycle
   spine (`prepare_task → prepare_outcome → plan → learn`) remains documented as the
   session lifecycle, distinct from the four verbs.
5. **Docker eval image**: **included.** `Dockerfile` is checked in and CI-built/validated;
   release workflow publishes the image to GHCR using the built-in `GITHUB_TOKEN` (no
   extra secrets). Never built on the maintainer's machine (storage constraint).
6. **Package names** (verified against the registries on 2026-07-02): PyPI `minni` is
   available — reserve at first v0.2 publish; PyPI `minnid` also free as fallback. npm
   `minni` is **taken** by an unrelated package, so the plugin keeps its existing name
   `minni-multi-plugin` (available).

## 7. PR sequence (one concern per PR, in order)

| PR | Concern | Files (add ➕ / modify ✏️) |
|----|---------|---------------------------|
| 1 | Hygiene quick fixes | ✏️ `.gitignore` (`.venv/`), ✏️ root `.claude-plugin/plugin.json` (license), ✏️ `bench/README.md` (3.11 note), ✏️ `README.md` (remove editorial tail only) |
| 2 | Install path & CLI | ➕ `engine/pyproject.toml`, ➕ `engine/minni_cli.py`, ➕ `engine/requirements.lock`, ✏️ `Makefile` (uv path, `doctor` target), ✏️ `engine/requirements.txt` (pins), ✏️ `.github/workflows/ci.yml` (doctor step) |
| 3 | ⚠ Flagged engine hook | ✏️ `engine/models.py` (first-run download banner only) — merges only with explicit approval |
| 4 | Release engineering | ➕ `CHANGELOG.md`, ➕ `.github/workflows/release.yml`, ✏️ `plugins/minni/package.json` (`files`/`bin`/`exports`), then tag `v0.1.0` |
| 5 | Hygiene files | ➕ `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `.github/ISSUE_TEMPLATE/*`, `.github/PULL_REQUEST_TEMPLATE.md` |
| 6 | README & docs | ✏️ `README.md` (full rewrite), ➕ `docs/index.md` + concepts/install/per-runtime/architecture pages, ✏️ badge row |
| 7 | Demo | ➕ `docs/readme-assets/demo.cast` (+ GIF), ✏️ `README.md` (embed) |
| 8 | (optional, if approved) Docker eval image | ➕ `Dockerfile`, ✏️ `release.yml` |

Definition of done tracks the brief verbatim; §8 below records the outcome.

---

## 8. Outcome record (2026-07-02)

All planned work shipped the same day, as ten merged PRs plus the `v0.1.0` tag:

| PR | Delivered |
|----|-----------|
| #107 | Hygiene quick fixes (license → MIT, `.venv/` ignore, README tail, bench doc) + this plan |
| #108 | `minni` CLI (`up`/`down`/`status`/`doctor`), `engine/pyproject.toml`, uv-compiled `requirements.lock`, uv-managed interpreter path in `make setup`, doctor gate in CI, 10 CLI tests |
| #110 | The one flagged engine diff: first-run model-download announcements in `engine/models.py` (approved §6.2) |
| #111 | `CHANGELOG.md`, tag-gated `release.yml` (smoke + check as release gates, sdist + npm tarball on GitHub Releases, zero extra secrets), completed npm metadata |
| #109 + #116 | CONTRIBUTING (with the **memory firewall**, widened per Codex review to plugin model-facing paths), CoC, SECURITY.md → SECURITY_PLAN.md, issue/PR templates |
| #112 | Docker eval image (engine-only, non-root, lazy models), CI-built + in-container smoke, GHCR publish on tags |
| #113 | Audit-grounded README rewrite (4 wrong + 3 stale claims fixed, comparison table, evidence section) + `docs/` tree (concepts, install, architecture with literal MCP tool list, security, per-runtime pages) |
| #114 | Live asciinema demo (cast + GIF): doctor → learn-stages → approve → recall-with-`<EVIDENCE>`; handoff shown as default-deny |
| #115 | Client fix: staged candidates reported as staged, not "Stored learning #?" |

**Channel decisions as implemented:** hardened source install (uv-provisioned 3.14)
as primary; Docker/GHCR as the eval channel; GitHub Releases for artifacts; PyPI and
npm publishing deliberately absent (§6.1/§6.6).

**Deferred to the maintainer / v0.2:**
- The `engine/` → `minni/` package rename that unblocks `pipx install minni`
  (approved in principle for v0.2; PyPI name `minni` was free as of 2026-07-02).
- npm publication (name `minni` is taken; `minni-multi-plugin` metadata is
  publish-ready if ever wanted).
- A two-runtime handoff segment for the demo — needs real seeded identities via
  `minni-install`; a wildcard principal file did not grant `handoff` in testing,
  so the demo honestly shows the default-deny instead.
- MkDocs/GitHub Pages site (plan §5 marked it optional-last; the docs tree reads
  fine as plain GitHub Markdown).
- `docs/readme-assets/*.svg` are stale per the maintainer and unused — delete or
  regenerate at leisure.
- The engine venv on the primary dev machine predates the 3.14 floor (3.13.14);
  the next `make setup` will rebuild it (multi-GB model/dep download).

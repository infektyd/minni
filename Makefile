# Minni — unified root entrypoint.
#
# One command surface that wraps BOTH surfaces (the Python engine package under
# src/minni/ and the Node/TS plugin under plugins/minni/) plus the membench
# harness. Every target calls the same commands documented in README.md so the
# Makefile never drifts from the docs.
#
#   make setup   - create .venv + install deps (editable minni package), npm ci the plugin
#   make lint    - ruff (engine) + eslint (plugin)
#   make typecheck - tsc --noEmit (plugin)
#   make build   - build the plugin (tsc + vite)
#   make test    - full engine pytest + plugin test suite (heavy)
#   make check   - fast validation gate (lint + typecheck + plugin build/test
#                  + scoped engine pytest); stamps .git/.proven-shas for pre-push
#   make attest  - full rootside attestation (check + smoke + check-versions + public boundary)
#   make promote - verify proven tree-SHA receipt, push main to origin, and sync-root
#   make smoke   - hermetic engine repro smoke (scripts/repro-smoke.sh)
#   make daemon  - run the minnid daemon on the default socket
#   make help    - list targets
#
# Tooling: the venv lives at .venv (repo root; v0.2 moved it out of the old
# engine/ dir) and is built with the system python3 when it matches the
# .python-version pin (3.14) on major.minor; when it does not and uv is
# installed, setup provisions a uv-managed Python at the pinned version instead. The editable install (`pip install -e .`) is what
# makes `import minni` and the `minni`/`minnid` console scripts work for normal
# single-checkout use. Test targets additionally set PYTHONPATH=src so a
# worktree's pytest does not silently import minni from another checkout's
# editable install (#258). Ruff runs from that venv so local hooks, CI, and
# agent shells share the same dependency set.

VENV_PY ?= .venv/bin/python
PYTHON_FOR_VENV ?= python3
RUFF ?= $(VENV_PY) -m ruff
PLUGIN_DIR := plugins/minni
SOCKET ?= $(HOME)/.minni/run/minnid.sock

# Scoped engine pytest for `make check`: a fast, model-free core that exercises
# daemon import, dispatch, status, learn/read, and the observability surface.
# Override to widen, e.g. `make check CHECK_PYTEST="tests -q"` for the full suite.
CHECK_PYTEST ?= tests/test_g01_numpy_env.py tests/test_obs.py tests/test_pr11_observability.py tests/test_minni_cli.py tests/test_worktree_import_resolution.py tests/test_promote_proven.py tests/test_python_version_gate.py tests/test_diagnose_legacy_aliases_uri.py tests/test_harness_envelope_parity.py tests/test_read_recent_learning_ids.py -q

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "Minni targets:"
	@echo "  setup      .venv + deps + editable minni package, plugin npm ci"
	@echo "  lint       ruff (engine) + eslint (plugin)"
	@echo "  typecheck  tsc --noEmit (plugin)"
	@echo "  build      build the plugin (tsc + vite)"
	@echo "  test       full engine pytest + plugin test (heavy)"
	@echo "  test-engine  full engine pytest suite (override ENGINE_PYTEST)"
	@echo "  check      fast gate: lint + typecheck + plugin build/test + scoped engine pytest"
	@echo "  attest     full rootside attestation (scripts/promote-proven.sh --attest)"
	@echo "  promote    promote proven local main to origin and sync-root (scripts/promote-proven.sh)"
	@echo "  coverage   plugin (node) + engine (pytest-cov) coverage with floors"
	@echo "  smoke      hermetic engine repro smoke"
	@echo "  daemon     run the minnid daemon (SOCKET=$(SOCKET))"
	@echo "  doctor     verify the install end to end (daemon must be running)"
	@echo "  bench      run the membench fixture end-to-end"

# ── Setup ────────────────────────────────────────────────────────────────
.PHONY: setup venv
# The interpreter gate (`make venv`, which setup runs first): the venv's Python
# must match .python-version exactly on major.minor
# (scripts/python_version_gate.py). A minimum is not enough: a newer system
# python3 builds a venv the locked wheels cannot install into. When system
# python3 does not match and uv (https://docs.astral.sh/uv/) is installed, uv
# provisions the pinned Python so newcomers never install an interpreter by
# hand. An unreadable or malformed pin (gate exit 2) stops setup before any
# existing .venv is touched. Deps install from the compiled lockfile
# (requirements.lock) for reproducibility; requirements.txt stays the
# human-edited source spec. The final editable install is load-bearing: it is
# how `import minni` resolves and how the `minni` / `minnid` console scripts
# land in the venv.
PY_PIN := $(shell tr -d '[:space:]' < .python-version 2>/dev/null)

venv:
	sys_rc=0; $(PYTHON_FOR_VENV) scripts/python_version_gate.py 2>/dev/null || sys_rc=$$?; \
	if [ $$sys_rc -eq 2 ]; then \
	  $(PYTHON_FOR_VENV) scripts/python_version_gate.py; \
	  echo "make setup stopped: fix .python-version (or restore scripts/python_version_gate.py); .venv was not touched."; \
	  exit 1; \
	elif [ $$sys_rc -eq 0 ]; then \
	  true; \
	elif command -v uv >/dev/null 2>&1; then \
	  echo "system python3 does not match .python-version ($(PY_PIN)) — uv will provision Python $(PY_PIN) for .venv"; \
	else \
	  echo "Python $(PY_PIN) (.python-version) is required for the Minni venv."; \
	  echo "Install Python $(PY_PIN), or install uv (https://docs.astral.sh/uv/) and re-run make setup — uv downloads the interpreter for you."; \
	  exit 1; \
	fi
	venv_rc=1; if [ -x .venv/bin/python ]; then venv_rc=0; .venv/bin/python scripts/python_version_gate.py || venv_rc=$$?; fi; \
	if [ $$venv_rc -eq 0 ]; then \
	  echo ".venv already uses Python $(PY_PIN)"; \
	elif $(PYTHON_FOR_VENV) scripts/python_version_gate.py 2>/dev/null; then \
	  echo "recreating .venv with $(PYTHON_FOR_VENV)"; rm -rf .venv && $(PYTHON_FOR_VENV) -m venv .venv; \
	else \
	  echo "recreating .venv with uv-managed Python $(PY_PIN)"; rm -rf .venv && uv venv --seed --python "$(PY_PIN)" .venv; \
	fi

setup: venv
	.venv/bin/python -m pip install --upgrade pip && .venv/bin/python -m pip install -r requirements.lock && .venv/bin/python -m pip install --no-deps -e .
	cd $(PLUGIN_DIR) && npm ci
	git config core.hooksPath .githooks

# ── Lint / typecheck ──────────────────────────────────────────────────────
.PHONY: lint lint-engine lint-plugin
lint: lint-engine lint-plugin

lint-engine:
	$(RUFF) check src/minni tests

lint-plugin:
	cd $(PLUGIN_DIR) && npm run lint

.PHONY: typecheck
typecheck:
	cd $(PLUGIN_DIR) && npm run typecheck

# ── Build ─────────────────────────────────────────────────────────────────
.PHONY: build
build:
	cd $(PLUGIN_DIR) && npm run build

# ── Test ──────────────────────────────────────────────────────────────────
ENGINE_PYTEST ?= tests -q

.PHONY: test test-engine test-plugin
test: test-engine test-plugin

# Full engine suite is heavy (loads embedding/FAISS models). Override the scope
# with ENGINE_PYTEST, e.g. `make test-engine ENGINE_PYTEST="tests/test_obs.py -q"`.
# PYTHONPATH=src (#258): an editable install from another checkout must not win
# over this tree when pytest is launched via Make from a git worktree. Bare
# `python -m pytest` (no Make / no PYTHONPATH) can still hit a wrong-tree
# editable; that path is not covered here (conftest/pyproject pythonpath are
# mechanical-gate surfaces for a follow-up).
test-engine:
	PYTHONPATH=src:$$PYTHONPATH $(VENV_PY) -m pytest $(ENGINE_PYTEST)

test-plugin:
	cd $(PLUGIN_DIR) && npm test

# ── Coverage ────────────────────────────────────────────────────────────────
# Plugin uses node's built-in coverage with conservative line/branch/function
# floors. Engine uses pytest-cov against the full suite (fail_under in
# .coveragerc). To scope the engine run, override COV_PYTEST and disable
# the floor, e.g.:
#   make coverage COV_PYTEST="tests/test_obs.py --cov-fail-under=0"
COV_PYTEST ?= tests -q

.PHONY: coverage coverage-plugin coverage-engine
coverage: coverage-plugin coverage-engine

coverage-plugin:
	cd $(PLUGIN_DIR) && npm run coverage

coverage-engine:
	PYTHONPATH=src:$$PYTHONPATH $(VENV_PY) -m pytest --cov=minni --cov-report=term-missing $(COV_PYTEST)

# ── Validation gate ────────────────────────────────────────────────────────
# Fast, deterministic gate suitable for pre-push / CI. Runs both surfaces'
# static gates plus a scoped engine pytest (not the full model-loading suite).
.PHONY: check
check: lint typecheck build
	cd $(PLUGIN_DIR) && npm test
	PYTHONPATH=src:$$PYTHONPATH $(VENV_PY) -m pytest $(CHECK_PYTEST)
	@bash scripts/promote-proven.sh --stamp-only make-check

.PHONY: attest promote
attest:
	@bash scripts/promote-proven.sh --attest

promote:
	@bash scripts/promote-proven.sh

# ── Engine runtime ─────────────────────────────────────────────────────────
.PHONY: smoke
smoke:
	bash scripts/repro-smoke.sh

.PHONY: daemon start
daemon start:
	$(VENV_PY) -m minni.minnid --socket $(SOCKET)

# Health check a non-contributor can read: same probes as scripts/repro-smoke.sh
# (status shape + recall round-trip) plus socket perms and model-cache presence.
.PHONY: doctor
doctor:
	$(VENV_PY) -m minni.minni_cli --socket $(SOCKET) doctor

# ── membench (s7) — one-command orchestration ──────────────────────────────
#
# `make bench` runs the WHOLE benchmark on the public synthetic FIXTURE end-to-end
# (Layer 1 + Layer 2 stub + significance + efficiency + report) and writes the
# artifacts to the gitignored bench/results/ dir. Fully offline by default.
#
# Reproducibility: two `make bench` runs produce a BYTE-IDENTICAL Layer-1
# scorecard JSON (results/layer1_scorecard.json) — Layer-2 is CI-only.

# PR92-2: reuse VENV_PY rather than re-declaring the same interpreter path.
PYTHON ?= $(VENV_PY)
BENCH_DIR := bench
OUT ?= $(BENCH_DIR)/results

# Run the FIXTURE end-to-end. HF_HUB_OFFLINE keeps the embedding adapter off the
# network (weights are cached locally); the harness makes no network call.
.PHONY: bench
bench:
	cd $(BENCH_DIR) && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
		PYTHONPATH=. "../$(PYTHON)" -m membench.run_bench --out "../$(OUT)"

# Re-run twice and diff the Layer-1 scorecard JSON to prove byte-reproducibility.
.PHONY: bench-repro
bench-repro:
	cd $(BENCH_DIR) && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=. \
		../$(PYTHON) -m membench.run_bench --out /tmp/membench_repro_a >/dev/null
	cd $(BENCH_DIR) && HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=. \
		../$(PYTHON) -m membench.run_bench --out /tmp/membench_repro_b >/dev/null
	@diff /tmp/membench_repro_a/layer1_scorecard.json \
		/tmp/membench_repro_b/layer1_scorecard.json \
		&& echo "Layer-1 scorecard JSON is BYTE-IDENTICAL across two runs."

.PHONY: bench-test
bench-test:
	$(PYTHON) -m pytest -q $(BENCH_DIR)

# ── Wheel payload staging (issue #142) ─────────────────────────────────────
.PHONY: stage-payload release-wheel check-versions check-deployments
# Report which deployed plugin builds have drifted behind source. A copied
# dist carries no record of its origin, so drift is silent; this makes it loud.
check-deployments:
	$(VENV_PY) scripts/check_deployments.py --strict

stage-payload:
	cd $(PLUGIN_DIR) && npm ci && npm run build && npm run build:server:bundle
	$(VENV_PY) scripts/stage_payload.py

release-wheel: stage-payload
	@test -f src/minni/plugin_payload/payload-manifest.json || \
	  (echo "release-wheel: missing payload-manifest.json — run make stage-payload first" >&2; exit 1)
	@$(VENV_PY) -c "import json,re,sys; \
	  v=re.search(r'^version\\s*=\\s*[\"\\']([^\"\\']+)[\"\\']', \
	  open('pyproject.toml').read(), re.M).group(1); \
	  m=json.load(open('src/minni/plugin_payload/payload-manifest.json')); \
	  sys.exit(0 if m.get('version')==v else \
	  (_ for _ in ()).throw(SystemExit(f'release-wheel: manifest version {m.get(\"version\")!r} != pyproject {v!r}')))"
	$(VENV_PY) -m build

check-versions:
	$(VENV_PY) scripts/check_versions.py

# ── Root sync (issue #232-#234 follow-through; audit GA1-3/GA5-1) ──────────
# Bring the LIVE install up to origin/main: fetch + ff-only (refuses on dirty
# or diverged local state), refresh the editable install, rebuild the plugin,
# redeploy every platform surface, restart minnid, then verify with the
# version/deployment checkers. `make sync-root DRY_RUN=1` prints the plan.
.PHONY: sync-root
sync-root:
	bash scripts/update_root.sh $(if $(filter 1 yes true,$(DRY_RUN)),--dry-run,)

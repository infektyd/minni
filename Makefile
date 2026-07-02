# Minni — unified root entrypoint.
#
# One command surface that wraps BOTH surfaces (the Python engine under engine/
# and the Node/TS plugin under plugins/minni/) plus the membench harness. Every
# target calls the same commands documented in README.md so the Makefile never
# drifts from the docs.
#
#   make setup   - create engine venv + install deps, npm ci the plugin
#   make lint    - ruff (engine) + eslint (plugin)
#   make typecheck - tsc --noEmit (plugin)
#   make build   - build the plugin (tsc + vite)
#   make test    - full engine pytest + plugin test suite (heavy)
#   make check   - fast validation gate (lint + typecheck + plugin build/test
#                  + scoped engine pytest); this is what CI/pre-push should run
#   make smoke   - hermetic engine repro smoke (scripts/repro-smoke.sh)
#   make daemon  - run the minnid daemon on the default socket
#   make help    - list targets
#
# Tooling: the engine venv lives at engine/.venv and is built with the system
# python3, which is the supported Python 3.14 path for this repo; when system
# python3 is older than 3.14 and uv is installed, setup provisions a uv-managed
# Python 3.14 instead (the version floor itself never changes). Ruff runs from
# that venv so local hooks, CI, and agent shells share the same dependency set.

# Engine venv python. Two forms for two working dirs (PR92-2): VENV_PY is
# repo-root-relative (daemon + bench, run from the repo root); ENGINE_VENV_PY is
# the same interpreter addressed from inside engine/ (ruff, run after `cd engine`).
VENV_PY ?= engine/.venv/bin/python
PYTHON_FOR_VENV ?= python3
ENGINE_VENV_PY ?= .venv/bin/python
RUFF ?= $(ENGINE_VENV_PY) -m ruff
PLUGIN_DIR := plugins/minni
SOCKET ?= $(HOME)/.minni/run/minnid.sock

# Scoped engine pytest for `make check`: a fast, model-free core that exercises
# daemon import, dispatch, status, learn/read, and the observability surface.
# Override to widen, e.g. `make check CHECK_PYTEST="-q"` for the full suite.
CHECK_PYTEST ?= test_g01_numpy_env.py test_obs.py test_pr11_observability.py test_minni_cli.py -q

.DEFAULT_GOAL := help

.PHONY: help
help:
	@echo "Minni targets:"
	@echo "  setup      engine venv + deps, plugin npm ci"
	@echo "  lint       ruff (engine) + eslint (plugin)"
	@echo "  typecheck  tsc --noEmit (plugin)"
	@echo "  build      build the plugin (tsc + vite)"
	@echo "  test       full engine pytest + plugin test (heavy)"
	@echo "  test-engine  full engine pytest suite (override ENGINE_PYTEST)"
	@echo "  check      fast gate: lint + typecheck + plugin build/test + scoped engine pytest"
	@echo "  coverage   plugin (node) + engine (pytest-cov) coverage with floors"
	@echo "  smoke      hermetic engine repro smoke"
	@echo "  daemon     run the minnid daemon (SOCKET=$(SOCKET))"
	@echo "  doctor     verify the install end to end (daemon must be running)"
	@echo "  bench      run the membench fixture end-to-end"

# ── Setup ────────────────────────────────────────────────────────────────
.PHONY: setup
# The interpreter gate: system python3 >= 3.14 is the classic path; when it is
# older, uv (https://docs.astral.sh/uv/) provisions a managed Python 3.14 so
# newcomers never install an interpreter by hand. The 3.14 floor itself is
# unchanged. Deps install from the compiled lockfile (requirements.lock) for
# reproducibility; requirements.txt stays the human-edited source spec. The
# final editable install exposes the `minni` CLI (up/down/status/doctor) inside
# the venv without touching engine import paths.
setup:
	cd engine && if $(PYTHON_FOR_VENV) -c "import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)" 2>/dev/null; then \
	  true; \
	elif command -v uv >/dev/null 2>&1; then \
	  echo "system python3 is older than 3.14 — uv will provision Python 3.14 for engine/.venv"; \
	else \
	  echo "Python 3.14+ is required for the Minni engine venv."; \
	  echo "Install Python 3.14, or install uv (https://docs.astral.sh/uv/) and re-run make setup — uv downloads the interpreter for you."; \
	  exit 1; \
	fi
	cd engine && if [ -x .venv/bin/python ] && .venv/bin/python -c "import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)"; then \
	  echo "engine/.venv already uses Python 3.14+"; \
	elif $(PYTHON_FOR_VENV) -c "import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)" 2>/dev/null; then \
	  echo "recreating engine/.venv with $(PYTHON_FOR_VENV)"; rm -rf .venv && $(PYTHON_FOR_VENV) -m venv .venv; \
	else \
	  echo "recreating engine/.venv with uv-managed Python 3.14"; rm -rf .venv && uv venv --seed --python 3.14 .venv; \
	fi
	cd engine && .venv/bin/python -m pip install --upgrade pip && .venv/bin/python -m pip install -r requirements.lock && .venv/bin/python -m pip install --no-deps -e .
	cd $(PLUGIN_DIR) && npm ci
	git config core.hooksPath .githooks

# ── Lint / typecheck ──────────────────────────────────────────────────────
.PHONY: lint lint-engine lint-plugin
lint: lint-engine lint-plugin

lint-engine:
	cd engine && $(RUFF) check .

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
ENGINE_PYTEST ?= -q

.PHONY: test test-engine test-plugin
test: test-engine test-plugin

# Full engine suite is heavy (loads embedding/FAISS models). Override the scope
# with ENGINE_PYTEST, e.g. `make test-engine ENGINE_PYTEST="test_obs.py -q"`.
test-engine:
	cd engine && PYTHONPATH=. .venv/bin/python -m pytest $(ENGINE_PYTEST)

test-plugin:
	cd $(PLUGIN_DIR) && npm test

# ── Coverage ────────────────────────────────────────────────────────────────
# Plugin uses node's built-in coverage with conservative line/branch/function
# floors. Engine uses pytest-cov against the full suite (fail_under in
# engine/.coveragerc). To scope the engine run, override COV_PYTEST and disable
# the floor, e.g.:
#   make coverage COV_PYTEST="test_obs.py --cov-fail-under=0"
COV_PYTEST ?= -q

.PHONY: coverage coverage-plugin coverage-engine
coverage: coverage-plugin coverage-engine

coverage-plugin:
	cd $(PLUGIN_DIR) && npm run coverage

coverage-engine:
	cd engine && PYTHONPATH=. .venv/bin/python -m pytest --cov=. --cov-report=term-missing $(COV_PYTEST)

# ── Validation gate ────────────────────────────────────────────────────────
# Fast, deterministic gate suitable for pre-push / CI. Runs both surfaces'
# static gates plus a scoped engine pytest (not the full model-loading suite).
.PHONY: check
check: lint typecheck build
	cd $(PLUGIN_DIR) && npm test
	cd engine && PYTHONPATH=. .venv/bin/python -m pytest $(CHECK_PYTEST)

# ── Engine runtime ─────────────────────────────────────────────────────────
.PHONY: smoke
smoke:
	bash scripts/repro-smoke.sh

.PHONY: daemon start
daemon start:
	$(VENV_PY) engine/minnid.py --socket $(SOCKET)

# Health check a non-contributor can read: same probes as scripts/repro-smoke.sh
# (status shape + recall round-trip) plus socket perms and model-cache presence.
.PHONY: doctor
doctor:
	$(VENV_PY) engine/minni_cli.py --socket $(SOCKET) doctor

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

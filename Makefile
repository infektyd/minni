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
# Tooling: the engine venv lives at engine/.venv (Python 3.11/3.12 — see
# .python-version). ruff is resolved from PATH (see engine/requirements.txt).

VENV_PY ?= engine/.venv/bin/python
RUFF ?= ruff
PLUGIN_DIR := plugins/minni
SOCKET ?= $(HOME)/.minni/run/minnid.sock

# Scoped engine pytest for `make check`: a fast, model-free core that exercises
# daemon import, dispatch, status, learn/read, and the observability surface.
# Override to widen, e.g. `make check CHECK_PYTEST="-q"` for the full suite.
CHECK_PYTEST ?= test_obs.py test_pr11_observability.py -q

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
	@echo "  bench      run the membench fixture end-to-end"

# ── Setup ────────────────────────────────────────────────────────────────
.PHONY: setup
setup:
	cd engine && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
	cd $(PLUGIN_DIR) && npm ci

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

# ── membench (s7) — one-command orchestration ──────────────────────────────
#
# `make bench` runs the WHOLE benchmark on the public synthetic FIXTURE end-to-end
# (Layer 1 + Layer 2 stub + significance + efficiency + report) and writes the
# artifacts to the gitignored bench/results/ dir. Fully offline by default.
#
# Reproducibility: two `make bench` runs produce a BYTE-IDENTICAL Layer-1
# scorecard JSON (results/layer1_scorecard.json) — Layer-2 is CI-only.

PYTHON ?= engine/.venv/bin/python
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

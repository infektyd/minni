# membench (s7) — one-command orchestration.
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

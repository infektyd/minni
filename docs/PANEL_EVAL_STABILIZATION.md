# Panel Eval Stabilization Verification

Use these commands from the repository root. They avoid private vault/log/DB
material and are intended to be repeatable locally and in CI.

## Focused Python Tests

```bash
python3 -m pytest \
  engine/test_pr2_envelope.py::TestFAISSPersistence \
  engine/test_pr4_eval_harness.py \
  engine/test_pr10_handoff.py
```

## Plugin Tests

```bash
npm --prefix plugins/minni test
```

## Reviewed Seed Validation

```bash
python3 -m engine.eval.harness validate \
  --path eval/reviewed_seed.jsonl \
  --min-reviewed 30
```

The reviewed seed is deliberately smaller than the future gate corpus. Do not
use it as a substitute for the 300-query gate.

## Non-Gated Smoke Eval

```bash
python3 -m engine.eval.harness run \
  --queries eval/reviewed_seed.jsonl \
  --config baseline \
  --retrievers mock \
  --mock
```

The smoke eval uses fixture doc IDs from the reviewed seed and does not touch
the live Sovereign Memory database.

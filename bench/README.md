# membench — Minni memory benchmark harness

Scrutiny-proof benchmark for the Minni local-first agent-memory system. The
authoritative methodology is the design spec:
`docs/superpowers/specs/2026-06-15-membench-design.md`.

`bench/` is **isolated** from `src/minni/` and `plugins/` (fairness §7.5): nothing
here imports a private `minni.*` / `plugins.*` module. The `minni` adapter
talks to Minni only through the **public Unix-socket JSON-RPC protocol**, the
same interface any external client uses.

## Status

Slice **s1** (skeleton + first real round-trip). Shipped:

- `membench/contract.py` — the `MemoryAdapter` contract + data types (§3).
- `membench/corpus.py` — `FrozenCorpus` loader: content-hash fail-closed +
  path-traversal guard at both `doc_ids()` build and `read()` (§5).
- `membench/tokenizer.py` — canonical pinned tokenizer (`cl100k_base`, §7.8).
- `membench/config.py` — pinned fields (§7.7); credentials by env-var NAME only.
- `membench/runner_layer1.py` — harness-owned token counting + budget abort.
- `membench/adapters/stub.py` — deterministic in-memory adapter (proves the
  contract green without any external service).
- `membench/adapters/minni_adapter.py` — the Minni adapter over an **isolated
  throwaway daemon** (own temp `MINNI_HOME` + temp socket). NEVER touches the
  operator's live daemon/DB/socket.
- `membench/fixtures/corpus_synthetic/` — small public synthetic corpus, hash
  pinned in `config.FIXTURE_CORPUS_HASH`.

Baseline adapters, scorer/metrics, Layer 2, judge, and gold labels are later
slices (s2–s7).

## Running the tests

Use the repo's engine venv (built by `make setup` with Python 3.14) rather than a
bare `python3`, so faiss/embedding imports resolve against the pinned dependencies:

```sh
cd ~/Projects/Minni && .venv/bin/python -m pytest -q bench
```

`tiktoken` (the canonical tokenizer) must be installed in `.venv`.

# Retrieval evaluation

The `fixture` command runs the actual `RetrievalEngine` against a new disposable
SQLite/FTS database and real markdown files. It never opens the live Minni database
or vault. Paths in `fixtures/retrieval.json` map to IDs allocated by the temporary
DB; expected IDs are resolved from that map, not fabricated by a mock searcher.
The report keeps stable relative refs after the temporary directory is deleted.

```sh
PYTHONPATH=src .venv/bin/python -m minni.eval.harness fixture \
  --repeats 3 --output /tmp/minni-fixture.json
```

The default `lexical-deadline` profile deliberately supplies an already-expired
model deadline. This exercises production FTS ranking, lifecycle filters,
principal eligibility, result formatting and the real model-deadline fallback
without loading models or accessing the network. Degradation is expected and
reported. Its latency is **lexical fallback latency**, not hybrid latency.

To exercise actual embedding, FAISS and cross-encoder reranking using cached
models, run:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=src \
  .venv/bin/python -m minni.eval.harness fixture --profile hybrid \
  --repeats 3 --output /tmp/minni-fixture-hybrid.json
```

Without the offline environment flags, the model library can download missing
models. Hybrid evaluation refuses an index with no semantic chunks; a degraded
query also makes its summary fail. Both profiles exit 3 for failed expectations.
Unexpected indexing/runtime exceptions fail the command rather than produce a
passing report. Query expansion and HyDE are disabled in both profiles.

The corpus is **machine-curated synthetic data**, grounded in the public agent
and lifecycle contracts and the actual principal read policy. It is not human
reviewed and does not measure representative private-memory quality. The five
cases cover shared evidence, cross-project recall, own/foreign privacy, lifecycle
exclusion, and denied-only results. Project directories are ordinary paths within
one allowed corpus, not authorization boundaries. Foreign private and blocked
notes are forbidden regardless of whether a relevant own-agent result exists.
Every document has an independent `expected_eligible` annotation for the fixed
principal and default lifecycle options. All ineligible documents are forbidden
in every case, in addition to case-specific exclusions; unknown returned IDs
also fail. The oracle does not call the production read policy to decide whether
its own result is correct. New documents require an explicit eligibility label.
Hybrid retrieval may return unrelated eligible documents for a denied-only
phrase; that case checks exclusion, not semantic abstention or recall.

Reports include source revision/dirty state, fixture hash, dependency/model
names, query options, real document IDs, returned/missing/forbidden refs,
recall at each case's limit, MRR, stage timing and degradation flags. Empty
expected sets are excluded from aggregate recall/MRR. Repetitions are labeled
starting at zero: setup/indexing is measured separately; the first query may
include reranker initialization, while later repeats can hit caches. Percentiles
use nearest ranks over all query runs and are descriptive only: this tiny corpus
is not a production latency benchmark. Engine stage timings can overlap
(`embedding_ms` currently includes semantic search); do not sum them.

For a quality study, curate additional public or locally retained questions,
review their source refs and eligible expectations independently, then compare
identical corpus hashes/options/models on the same hardware. Keep private
corpora and reports outside version control. The existing `run --mock` and
`reviewed_seed.jsonl` are legacy harness/scoring smokes with mock-only IDs; their
scores do not establish retrieval quality. The fixture command is separate from
the legacy 300-reviewed-query feature gate and makes no claim to satisfy it.

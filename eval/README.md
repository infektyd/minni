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

## Public repository corpus

`fixtures/public_repo.json` adds 20 machine-reviewed questions over 18 verbatim,
coherent excerpts from public Minni documentation (3,086 words). It includes
paraphrases, concrete operational questions, answers requiring multiple sources,
and false premises that should retrieve correcting evidence. Neighboring topics
remain in the corpus as plausible distractors; two excerpts are distractor-only.
No private memories, model-generated facts, or observed retrieval outputs were
used to establish the expected sources.

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=src \
  .venv/bin/python -m minni.eval.harness fixture \
  --corpus eval/fixtures/public_repo.json --profile hybrid \
  --repeats 3 --output /tmp/minni-public-repo.json
```

Use `--profile lexical-deadline` for the deliberately degraded, model-free
comparison. Do not rewrite expectations or tune thresholds to make its lexical
scores pass: ordinary questions and paraphrases can expose retrieval misses.

Each document records its public source path, inclusive line range, full source
blob SHA-256, excerpt SHA-256, and `git_blob_oid`. The original `source_revision`
is historical annotation context. Verification checks the blob and its source
path in retained Git history, so squash merging does not require keeping the
original PR commit reachable. Text is copied exactly, including Markdown and
newlines. Each question's `expected_refs`
resolve to those source records; `expected_answer` explains the independent
relevance judgment. The answer text is explanatory, **not an answer-generation
metric**. `hard_negative_refs` identify authorized, topically similar distractors,
not forbidden content: returning one is not a privacy violation. False-premise
questions have positive correcting sources rather than artificial empty-answer
requirements. All corpus documents are deliberately eligible for the fixture's
fixed Codex principal; use the separate synthetic corpus for authorization and
lifecycle exclusion tests.

This is **machine-reviewed public-document retrieval**, not human-reviewed ground
truth, a representative private-memory study, or a production latency benchmark.
It measures retrieval of the pinned documentation, not independent proof that
all documentation describes deployed behavior. The source contracts may contain
historical or planned behavior outside the selected excerpts. Keep this corpus
separate from the synthetic corpus when reporting quality; they test different
things. Later documentation changes do not silently change the snapshot: review
new source text, hashes, and expectations explicitly when refreshing it.

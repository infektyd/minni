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

## Compare a candidate against the baseline

Validate a reviewed corpus using the same strict checks as a quality run:

```sh
PYTHONPATH=src .venv/bin/python -m minni.eval.harness validate \
  --quality-gate --path /path/to/reviewed-queries.jsonl
```

This requires unique query strings, explicit class labels, exact integer IDs,
and nonnegative integer `budget_tokens` when supplied (default 4096).
Plain `validate` retains the legacy validation contract.

Use a reviewed corpus with real document judgments for the retrieval backend:

```sh
PYTHONPATH=src .venv/bin/python -m minni.eval.harness run \
  --queries /path/to/reviewed-queries.jsonl \
  --config no-expand,with-expand --retrievers minnid \
  --quality-gate --quality-baseline no-expand --quality-candidate with-expand
```

The default check requires a **5% relative** gain in mean recall@5, with no
regression in any query class (`notes`). For example, 0.40 to 0.42 meets the
improvement threshold; a declining class still fails. A zero baseline needs a
strictly positive candidate score; both zero fails. Reports must contain the
same unique queries, exact integer document judgments, and class metadata.
Missing, nonfinite, or out-of-range scores fail. A whole class without judgments
cannot disappear from the check; partially unjudged queries remain explicitly
listed as unevaluable. Negative/privacy probes require a separate outcome
contract and are not certified by recall scores.

The existing corpus validator requires at least 300 queries with explicit JSON
`"reviewed": true`, relevance metadata, answer rubric, and privacy expectation.
Malformed raw IDs and invalid comparison arguments are rejected before
retriever initialization. Quality mode currently supports recall only; graded
nDCG comparisons require preserving additional comparable judgment evidence.
The default policy is recall@5 with `--min-improvement 0.05`; other supported K
values or thresholds are custom comparisons, not evidence for that policy.

A failed invocation exits 2; failed comparison exits 3 and writes a quality-gate
JSON report. Success writes the report with compared means, class results,
excluded queries, and limitations. The check computes no confidence intervals
and does not assess latency, answer quality, or consumer resistance to hostile
recalled content. `--mock` exercises plumbing only. Existing fixture studies and
placeholder seed IDs do not become a reviewed real-world quality study merely
because the comparison code exists.

The legacy `--gate` Minni-versus-ripgrep loss-rate check is separate and keeps its
20% rule. Ungated runs remain available for smaller exploratory datasets.

Quality comparisons require one document-ID retriever and two distinct configs.
Both configs must set `use_hyde=False` explicitly — HyDE on either side
(including the `with-hyde` config) is rejected before any retrieval work, so a
two-dimension change can never certify as expansion evidence. `no-expand`
disables query expansion; `with-expand` sets `expand=True`, which uses the
engine’s `query_expand_default` mode (rule or AFM; unsupported defaults fall
back to rule).
It does not force AFM expansion or guarantee extra variants for every query. The legacy
`baseline` config retains its existing defaults. Report names resolve by complete
config identity, so `baseline` never aliases `fp32-baseline`. Report names must
also be unique case-insensitively (`minnid,MINNID` share one backend and one
report file on case-insensitive filesystems) and must not collide with the
`gate` / `quality-gate` artifact names.

Quality mode rejects malformed JSONL, missing or blank query-class labels,
retrieval exceptions, and unsupported config options. An absent or null
`expected_doc_ids` field is malformed evidence, never an empty judgment: only
an explicitly present `[]` marks an unevaluable probe. It cannot be combined with
the legacy `--gate`. These
checks validate the comparison inputs; passing synthetic tests is not evidence
of improved real retrieval quality.

Real acceptance requires a positively recorded frozen snapshot with the
same explicit identity on both sides. Absent provenance, a missing or
`"unknown"` snapshot, frozen-without-identity, a snapshot mismatch, or mixed
evidence kinds all fail — nothing is inferred from absence, so a bare report
without provenance fails rather than passing as synthetic. Positively labeled
mock evidence keeps the numeric comparison but never certifies: the decision
is forced to fail with a synthetic-plumbing reason and evidence label (exit
3). Matching snapshot strings are packet identity, not authenticated proof of
a frozen corpus. Rerun both configs against a frozen snapshot with a recorded
identity (snapshot support lands separately) before gating; this gate invents
no frozen proof.

Gate artifacts (`*-gate.json`, `*-quality-gate.json`) carry their own
`provenance` block — query digest, code revision, corpus snapshot, gate inputs,
and the recorded decision — so a retained artifact identifies its evidence
when copied independently.

## Private-study preparation runbook (no corpus collected yet)

Hans chose private day-to-day cross-project memories as the study target. No
private corpus has been collected, and nothing below reads, exports, or
benchmarks live memories. This section prepares the procedure so a later,
separately authorized collection step can run it without improvising.

### 1. Freeze an eligible corpus before scoring anything

- Copy the selected memories into a **frozen snapshot directory** with a
  recorded identity (e.g. a manifest of file paths plus SHA-256 per file and
  one manifest digest). The snapshot is read-only for the whole study.
- Every document carries an explicit eligibility annotation for the fixed
  study principal (like the fixture's `expected_eligible`), decided before
  retrieval runs, not derived from retrieval output.
- Keep the snapshot and all study reports **outside version control**
  (e.g. under Minni's private data dir or `/tmp`), never in `eval/`.
- Refreshing the corpus means a new snapshot with a new identity and new
  review; never silently swap files under a recorded digest.

### 2. Scope reads and govern access

- Run retrieval under a **least-privilege principal** whose allowed roots
  cover only the frozen snapshot, with `update_access=False`, writeback
  disabled, and no daemon side effects. Record the principal id,
  capabilities, and allowed roots in the report.
- The study harness must open the snapshot database only; it must not open
  the live vault for reads, writes, or metadata.

### 3. Review authentically, not via the legacy boolean

- The existing `"reviewed": true` flag is a gate-shape marker: it says a row
  has the required fields. It is **not** evidence that a human judged
  relevance, wrote an answer rubric, or set a privacy expectation.
- Authentic review means independent reviewers apply a written rubric to
  each query, record relevance grades and privacy expectations per document,
  adjudicate disagreements, and log the review method and date. The
  provenance block records `human_review: not-established` until that
  process exists; do not relabel it by hand.

### 4. Why the legacy `run` still accesses DEFAULT_CONFIG

- `RealSearcher` wraps `RetrievalEngine` over the mutable `DEFAULT_CONFIG`
  live database for convenient smoke and comparison plumbing. That is why
  ordinary `run` commands touch the live engine.
- Treat those reports as **comparison plumbing over mutable content**, not
  study evidence: the backend is recorded as live-mutable with snapshot
  `unknown`, never as frozen or safe. The fixture command is the model for
  study isolation (disposable database, fixed principal, recorded hashes).

### 5. Keep private reports out of the repo

```sh
PYTHONPATH=src .venv/bin/python -m minni.eval.harness run \
  --queries /path/to/reviewed-queries.jsonl \
  --config no-expand,with-expand --retrievers minnid \
  --quality-gate --quality-baseline no-expand --quality-candidate with-expand \
  --output-dir /private/study-reports
```

`--output-dir` defaults to `eval/reports`, preserving existing behavior; pass
an outside-the-repo directory for anything private. A new directory (explicit
or default) is created with mode `0700`; a pre-existing group/other-writable
directory fails fast with exit 2 before any retrieval work, instead of running
the study and then writing zero reports. An existing explicit directory must
already be owned by you and private. Shared report directories such as `/tmp`
itself are rejected; use a dedicated child directory. JSON and Markdown
reports are written with mode `0600`. Repeated config/retriever combinations
(including case-insensitive collisions) are rejected before a run starts.
Every JSON report carries
a `provenance` block: a digest of the exact parsed queries scored
(`loaded_queries_digest`), the separately observed query-file bytes with
explicitly unverified correspondence, code revision/dirty state,
requested/effective retrieval settings (options an adapter swallows without
effect are listed under `ignored_by_backend`, never claimed as compared;
harness envelope defaults such as `update_access` appear as effective only
for backends that actually consume them — the live engine alone),
config/dependency metadata when
importable (model names are configured defaults, not observed inference),
principal availability, run order and timing caveats (searcher construction
happens before, and outside, the measured per-query timing), and
backend-specific corpus identity (live databases stay `unknown`, never
hashed; file baselines and placeholders get their own labels). The Markdown
comparison adds a short Run Provenance section derived from the actually
constructed backends, not from CLI flags alone. Provenance describes how a
report was produced; it is not a passing certification, and `unknown` means
unverifiable, not safe.

`fixture --output` writes a single `0600` file, so the documented
`/tmp/minni-fixture.json` paths keep working: a sticky shared parent such as
`/tmp` is accepted for one private file (the sticky bit stops other users
renaming or replacing it), while a non-sticky shared parent is rejected. The
destination is preflighted before the fixture runs, so an unusable path exits
2 instead of discarding a completed evaluation.

`fp32-baseline`, `int8-quantized`, and `with-semantic-merge` are placeholder
ablations without implemented option changes and are rejected in quality mode.
They remain available for legacy descriptive reports. Quality mode also rejects
pairs with identical effective options after accounting for the engine’s
`expand=True` default; distinct names alone do not
establish a feature comparison.
## Bounded study snapshot (authorized-export packet in, frozen corpus out)

`src/minni/eval/study_snapshot.py` is the snapshot foundation for the
private-memory campaign. It collects nothing: the only input is a bounded,
explicit **authorized-export packet** (principal/store/source identity plus
record content) supplied by the parent, which connects the governed export
separately. Arbitrary paths and vault dumps are never accepted.

Packet shape (`packet_version: "minni-study-export-v1"`):

- `principal.agent_id`, `store.{store_id, origin}`, `source.origin`,
  `authorization.claimed` — a supplied claim recorded as provenance, never
  authentication proof and never independently verified permission.
- `records[]` — each with a `(store, source_doc_id)` tuple identity (the
  same document number in two stores names two documents), relative `.md`
  `artifact_path` (no absolutes, no `..`), `text` plus matching
  `content_sha256`, `content_kind: original|excerpt` (excerpts must cite a
  `source_locator`), `review_state: machine_proposed` with
  `human_reviewed: false`, source-ownership `agent`, `privacy_level`, clear
  `origin`, original lifecycle `page_status`/`page_type`, an explicit boolean
  `expected_eligible`, and optional scalar-only `source_detail`
  (cross-project eligibility is annotated before retrieval, never inferred
  from it; project directories are ordinary paths, not authorization
  boundaries).

Hard input bounds (1000 records, 100k chars / 400k UTF-8 bytes per text,
5M chars total, plus length caps on every metadata string, capability list,
and scalar-only finite `source_detail`) fire before any hashing, writes, or
DB work. Validation then rejects tampered manifests — the digest binds
canonical source/principal/authorization metadata AND lifecycle fields, so
swapping any of them invalidates the snapshot — tampered content, duplicate
`(store, source_doc_id)` tuples, unsafe artifact paths (canonical segments
only: `a/./n.md`, `a//n.md`, and trailing-slash aliases are rejected),
unbound extra fields, missing excerpt/original labels, any human-reviewed
claim, and malformed fields. Identical bytes under separate ownership are
allowed and linked through a shared content group (`content_groups` in the
manifest), never silently conflated. Machine judgments are never labeled
human-reviewed: original lifecycle/privacy provenance is preserved in
`source_provenance`, the study judgment lives separately in
`study_judgment`.

```sh
PYTHONPATH=src .venv/bin/python - <<'EOF'
import json
from pathlib import Path
from minni.eval.study_snapshot import (
    prepare_snapshot, materialize_snapshot_db,
)
packet = json.loads(Path("/private/study-export/packet.json").read_text())
dest = Path("/private/study-snapshots/study-01")  # 0700 dirs / 0600 files
manifest = prepare_snapshot(packet, dest)      # no DB, engine, or model imports
info = materialize_snapshot_db(dest)           # disposable lexical FTS corpus
print(manifest["snapshot_id"], info["document_ids"])
EOF
```

`prepare_snapshot` freezes vault files, a deterministic opaque remapping
(`study-0001…`, sorted by store/source identity), and `snapshot.json` whose
`snapshot_id` derives from the manifest digest only — snapshot IDs are never
assigned to the live corpus. Destinations that are, contain, or sit inside
live/default paths are rejected before anything is written, and preparation
refuses a non-empty destination so a second packet can never mix bytes into
an existing snapshot. Frozen files and metadata re-validate on every
materialization and every search (`verify_snapshot`): symlinks in any path
component (including vault ancestors and the snapshot root/outputs),
tampered bytes, inconsistent mappings, unmapped vault files, invented
snapshot IDs, edited identity mirrors, and digest mismatches all fail, and
`mapping.json` carries the manifest/snapshot IDs so outputs can never mix
across snapshots. Reads use strict JSON (no NaN/Infinity) with size
preflights before any bytes load. `materialize_snapshot_db` runs once per
prepared directory, mirrors the fixture's isolated construction with every
DB/index/vault path inside the snapshot directory, preserves original
ownership/lifecycle/privacy metadata per record, disables writeback, and
loads no model; `check_materialized` re-binds every `document_ids` entry to
the actual immutable SQLite rows and FTS text over a read-only handle
(runtime access counters are excluded, so normal governed search effects
never read as tampering). Refreshing the corpus means a new snapshot
directory, never silent file swaps.

Run governed retrieval over the frozen corpus with the isolated backend:

```sh
PYTHONPATH=src .venv/bin/python -m minni.eval.harness run \
  --queries /private/study-queries.jsonl --retrievers snapshot \
  --snapshot-dir /private/study-snapshots/study-01 \
  --output-dir /private/study-reports
```

The snapshot retriever requires `--snapshot-dir`, opens only that directory
under a least-privilege principal scoped to the snapshot vault, and never
instantiates the live `DEFAULT_CONFIG`, a retrieval engine, or a model.
Retrieval is an explicit offline lexical baseline (FTS5 MATCH with the
engine's default lifecycle exclusions plus the central read gate) — not a
full-engine quality comparison — and takes no deadline, so expiry semantics
cannot empty its results. Provenance labels the verified snapshot ID plus its manifest digest
(failing closed to `unknown`/unfrozen without a verified ID)
with supplied (not verified) authorization; the snapshot backend is excluded
from quality-gate config comparisons. The search path is fully read-only;
no zero-write forensic claim is made beyond that.
`sm_export_pack` stays what it is (shared snippets under an export
capability, not a corpus snapshot) and no capability is bypassed.

Scope honesty: a bounded packet study only — not representative
private-memory quality, not a retrieval-performance claim, not a
default-change signal. Decisive acceptance stays with the parent.

Unresolved (parent-owned): the governed daemon export that produces the
packet, and the collection limits for the real day-to-day memory corpus,
are not implemented here — this module only validates, freezes, and serves
whatever bounded packet the parent supplies.

# Minni Typed Memory Graph — Design Spec

**Date:** 2026-07-09
**Status:** approved design, pre-implementation
**Approach:** A — "Extend the spine" (extend `memory_links` and existing machinery; additive schema only)
**Provenance:** drafted by GPT-5.6 Sol (max effort, repo-grounded), red-teamed by Grok 4.5
(8 findings, 2 critical — folded in below), arbitrated and synthesized by Claude Fable
against pre-registered positions. Approved by Hans 2026-07-09.

## Locked decisions

1. **Retrieval substrate first.** The graph exists to improve recall quality; visualization
   is deferred (Phase 4, outside this spec's acceptance gate).
2. **Edges are inferred automatically at write-time** (durable learning commit),
   Supermemory-style.
3. **Nodes are memories** (learnings + wiki docs), not extracted entities.
4. **Additive schema only.** Extend `memory_links`; no parallel graph store.
5. **Fail-loud commit behavior** (user-approved behavior change): if edge inference cannot
   run (local model down/timeout/invalid output), durable promotion fails loudly and the
   candidate stays staged. No silent edge-less commits, no async catch-up queue.

## 1. Edge model and schema

### 1.1 Typed vocabulary

All inferred edges use the newly committed memory as source and a pre-existing memory as
target. The classifier picks exactly one type or `none` per pair; no redundant `relates`
beside a stronger edge.

| Type | Semantics | Direction | Lifecycle effect |
|---|---|---|---|
| `updates` | Source replaces/materially revises target's claim under same applicability conditions. Recency alone is insufficient. | new → old | May auto-supersede at the higher mutation threshold (§2.4) |
| `extends` | Compatible; adds detail, scope, evidence, or procedure. | extension → base | none |
| `contradicts` | Claims cannot both be true under materially overlapping conditions. | stored in detection direction; traversed both ways | unresolved `contradiction_log` row; never auto-resolves |
| `relates` | Useful navigational relationship; same-topic alone is usually `none`. | symmetric; traversed both ways | none |
| `wikilink` / `derived_from` | existing explicit types | unchanged | unchanged |

`confidence` (calibrated classifier probability the type is correct) and `weight`
(operational retrieval multiplier, default 1.0, adjustable by consolidation) are distinct
columns; read scoring uses both. Existing explicit edges behave as confidence 1.0.

### 1.2 Canonical learning nodes — N:1 by design

Learnings currently have two document representations: the synthetic `learning://<id>`
alias created by `writeback.add_derived_from_edges()` (writeback.py:163-247) and the
content-hash-addressed `_durable/*.md` document indexed for retrieval
(minnid.py:327-353). This spec unifies new writes on the `_durable` document as the
canonical graph node.

**Red-team critical amendment (Grok F1):** because `_durable_doc_path` is keyed on
`(agent_id, content)`, distinct live learnings with identical content map to the SAME
physical document row as steady-state behavior, not a legacy artifact. The mapping is
therefore **many-to-one by design**:

- New join table `learning_documents(learning_id INTEGER NOT NULL REFERENCES
  learnings(learning_id), doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE
  CASCADE, PRIMARY KEY(learning_id, doc_id))`.
- **No** `UNIQUE` index on a `documents.learning_id` column; no such column at all.
- `documents` gains `memory_kind TEXT` and `memory_uri TEXT` (unique partial index on
  `memory_uri` where not null). `memory_uri` remains `learning://<learning_id>` for the
  most recent mapping; traversal and repair must tolerate N learnings per node.
- `add_derived_from_edges()` receives the canonical doc_id instead of creating an alias.
- Legacy `learning://` alias rows remain valid. A repair pass maps only provable 1:1
  aliases (copy edges, mark alias superseded); ambiguous content-deduplicated cases are
  reported via health diagnostics, never guessed.

Typed inference operates only over `memory_kind IN ('learning','wiki')`.

### 1.3 Migration `016_typed_memory_graph.sql` (Phase 1, slimmed)

```sql
ALTER TABLE documents ADD COLUMN memory_kind TEXT;
ALTER TABLE documents ADD COLUMN memory_uri TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_memory_uri
    ON documents(memory_uri) WHERE memory_uri IS NOT NULL;

CREATE TABLE IF NOT EXISTS learning_documents (
    learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
    doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    created_at REAL,
    PRIMARY KEY (learning_id, doc_id)
);

ALTER TABLE memory_links ADD COLUMN confidence REAL;
ALTER TABLE memory_links ADD COLUMN inference_method TEXT;
ALTER TABLE memory_links ADD COLUMN model_id TEXT;
ALTER TABLE memory_links ADD COLUMN prompt_version TEXT;
ALTER TABLE memory_links ADD COLUMN inference_run_id TEXT;
ALTER TABLE memory_links ADD COLUMN evidence_json TEXT;
ALTER TABLE memory_links ADD COLUMN inferred_at REAL;
ALTER TABLE memory_links ADD COLUMN edge_status TEXT NOT NULL DEFAULT 'active';

CREATE INDEX IF NOT EXISTS idx_memory_links_target_active
    ON memory_links(target_doc_id, edge_status, link_type, source_doc_id);
CREATE INDEX IF NOT EXISTS idx_memory_links_source_active
    ON memory_links(source_doc_id, edge_status, link_type, target_doc_id);

ALTER TABLE contradiction_log ADD COLUMN source_doc_id INTEGER
    REFERENCES documents(doc_id) ON DELETE SET NULL;
ALTER TABLE contradiction_log ADD COLUMN target_doc_id INTEGER
    REFERENCES documents(doc_id) ON DELETE SET NULL;
ALTER TABLE contradiction_log ADD COLUMN edge_run_id TEXT;
ALTER TABLE contradiction_log ADD COLUMN confidence REAL;
ALTER TABLE contradiction_log ADD COLUMN resolution_status TEXT DEFAULT 'unresolved';

-- Grok F7: legacy rows distinguishable from new unresolved detections
UPDATE contradiction_log SET resolution_status = 'legacy_unclassified'
    WHERE resolution_status = 'unresolved';

CREATE INDEX IF NOT EXISTS idx_contradiction_graph_pair
    ON contradiction_log(source_doc_id, target_doc_id, resolution_status);

UPDATE memory_links SET
    confidence = COALESCE(confidence, 1.0),
    inference_method = COALESCE(inference_method, CASE link_type
        WHEN 'wikilink' THEN 'explicit_wikilink'
        WHEN 'derived_from' THEN 'writeback_evidence'
        ELSE 'legacy' END);
```

**Deferred (Grok F8):** the `memory_link_inference_runs` ledger table moves to Phase 2/3;
Phase 1 logs run metadata (including `no_candidates` and error statuses) to the
application logger and audit tail only. Idempotency does not need the table — it is
enforced by deterministic `run_id` + the composite PK on `memory_links`.

`evidence_json` is bounded provenance, not copied content: chunk IDs, hashes of the exact
excerpts shown to the classifier, shortlist cosine, rationale capped at 280 chars.
Rehydration goes through normal read authorization; hash mismatch marks the edge `stale`.
Application-level validation enforces vocabulary, finite [0,1] confidence/weight, and
valid statuses (no CHECK constraints — would require table rebuild).

### 1.4 Schema-readiness gate (promoted to Phase 1 hard requirement — Grok F5)

`SovereignDB._init_schema()` currently treats migration failure as non-fatal
(db.py:397-411) and `_execute_tolerant` (migrations.py:387-426) can leave partial column
sets on drifted schemas. Therefore: at daemon startup and on first graph-enabled request
per store, a readiness probe compares `PRAGMA table_info(memory_links)` (and
`learning_documents`, `contradiction_log`) against the expected 016 column set.

- Probe fails → graph features disabled for that store with `graph_status='schema_missing'`
  in traces and health output.
- `schema_missing` and `degraded` (runtime graph-query failure, §3.4) are **distinct
  statuses** — a broken migration must never present as a healthy-but-degraded graph.

## 2. Write path — inference at durable learning commit

### 2.1 One commit coordinator

All durable learning paths converge on one coordinator (today there are four divergent
paths: writeback.store_learning, governance.handle_learn(force), resolve_candidate,
AFM consolidation). Signature sketch:

```
commit_learning_with_graph(request, principal) -> LearningCommitResult
infer_typed_edges(new_memory, candidates, run_id) -> EdgeInferenceBatch
```

Embeddings, candidate shortlist, and the model call all happen OUTSIDE the write lock
(prepare-before-transaction, mirroring retrieval.py:642-685). One short `BEGIN IMMEDIATE`
then atomically inserts: learning, canonical document + `learning_documents` row,
FTS/chunks, typed edges, lifecycle mutations, contradiction rows. FAISS refresh after
commit. No model call ever runs while holding a SQLite write transaction.

### 2.2 Candidate shortlist

1. Query existing FAISS with the new learning's embedding: top 48 chunks.
2. Dedup to documents by max chunk similarity.
3. Exclude self, non-memory kinds, terminal/blocked nodes, and any document the
   committing principal cannot read.
4. Keep ≤12 documents with cosine ≥ 0.42 (recall-oriented floor, slightly under
   graph_export's 0.45; the model is the precision gate).
5. Send ≤8 highest-scoring pairs in ONE batched local-model call (excerpts ≤~220 tokens
   each, inside the 3,200-token AFM input budget).

No candidates over the floor → commit normally, record `no_candidates` in the audit log.

### 2.3 Local classification contract

New operation class `edge_inference`, **hardcoded local-only**.

**Red-team critical amendment (Grok F2):** `default_provider_chain()` seeds
`local_only=True` only for the literal `"retrieval"`; unknown operations fall back to a
cloud-eligible `OperationPolicy()` (model_provider.py:217, :247-256). Implementation MUST
add `"edge_inference": OperationPolicy(local_only=True)` to the unconditional seed dict
(not config-driven), plus a test asserting `providers_for("edge_inference")` never
returns a non-local provider regardless of config content. "Local" = native AFM or
loopback bridge only.

Model receives numbered, escaped evidence excerpts + timestamps, page types, status,
applies_when. Content is untrusted evidence, never instructions; output is schema-only.
Per pair it returns: pair id, label (`updates|extends|contradicts|relates|none`),
direction, finite confidence in [0,1], supporting excerpt indices, bounded rationale.
Missing/duplicate/unknown fields, invalid evidence refs, or truncation invalidate the
ENTIRE batch — partial graph commits are forbidden.

### 2.4 Confidence thresholds (calibration starting points, not measured facts)

| Type | Persist edge | Additional action |
|---|---:|---|
| `updates` | ≥ 0.88 | auto-supersede only at ≥ 0.96 |
| `contradicts` | ≥ 0.88 | unresolved `contradiction_log` row |
| `extends` | ≥ 0.82 | none |
| `relates` | ≥ 0.78 | none |

Auto-supersession requires the target to be an active learning owned by the **same agent
in the same store** (Grok F6: cross-store supersession attempts are structurally excluded,
not raced — they downgrade to a pending `graph_update_review` entry in
`consolidation_actions`, as do wiki targets, already-superseded targets, other agents'
learnings, and would-be cycles). The supersession transaction sets
`learnings.superseded_by` + status, the canonical document's `superseded_by` +
`page_status`, and keeps the `updates` edge active as historical provenance.

`contradicts` inserts doc IDs (and learning IDs when available) into `contradiction_log`.
Raw detection does NOT emit `contradiction_events` — those retain their meaning of
resolved supersession (governance.py:379-489).

### 2.5 Idempotency and failure behavior

`run_id` = deterministic hash over source fingerprint, ordered candidate IDs+hashes,
model identity/revision, prompt version. Re-running: upserts matching edges via the
composite PK, updates confidence/provenance only on successful runs, marks
previously-inferred-now-absent edges from that source `stale`, never touches
`wikilink`/`derived_from` (mirrors wiki_indexer's link-type-scoped pruning,
wiki_indexer.py:563-601).

Pre-commit recheck of target existence/status/privacy/evidence hashes; one full retry on
race; second mismatch → `edge_candidates_changed`.

**Fail-loud boundary (approved):** if candidates exist and the local model is
unavailable/times out/violates the contract — no durable learning, no edges, candidate
stays staged, sanitized error recorded, RPC returns structured
`edge_inference_unavailable | edge_inference_timeout | edge_inference_invalid_output`.
A model outage delays durable promotion; it never loses the proposed memory and never
silently commits an edge-less learning.

Warm-path latency targets (unverified until measured against live AFM; §7 P1 exit
criteria include measuring them): candidate lookup p95 ≤ 50ms; single batched model call
p95 ≤ 1.2s, hard timeout 2.0s; added commit latency p95 ≤ 1.5s, p99 ≤ 2.2s; exactly one
model call per commit.

## 3. Read path — graph-aware recall

### 3.1 Pipeline placement and privacy ordering

Expansion happens after first-pass RRF fusion and **before** cross-encoder reranking, so
graph-only neighbors compete in the precision stage with real query-relevance scores:

1. FTS + FAISS → 2. RRF merge → 3. `expand_typed_graph(top 8 seeds)` —
   **privacy gate INSIDE expansion** → 4. union (cap below) → 5. cross-encoder rerank →
   6. lifecycle filters, evidence envelopes, token budgeting.

**Red-team critical amendment (Grok F3):** `can_read_document()` is called at candidate
production time inside `expand_typed_graph` — before any neighbor text is hydrated or
enters the rerank batch. This is mandatory because graph neighbors are the first
candidates in the pipeline NOT pre-scoped by the SQL agent/workspace filters; the
existing post-rerank gate (retrieval.py:2388) is insufficient for them. Acceptance test:
"graph candidates are privacy-filtered before entering the rerank batch."
With `principal=None`, graph expansion is disabled entirely (no legacy ungated behavior).

**Cap (Grok F4):** ≤12 graph-derived candidates TOTAL per query, post-union across all
query variants combined. Rerank batch grows from ~20 to ≤32; this enlarged batch is part
of the latency budget and must be measured at P1 exit (read p95 regression ≤20% overall).

### 3.2 Traversal and scoring

Both directions via the new source/target indexes. Type factors and depth caps:

| Type | factor | max depth |
|---|---:|---:|
| updates | 1.00 | 2 |
| contradicts | 0.95 | 1 |
| extends | 0.85 | 2 |
| derived_from | 0.75 | 2 |
| wikilink | 0.70 | 2 |
| relates | 0.55 | 1 |

Path score = seed first-pass score × type factor × weight × confidence × 0.65 per hop
after the first; max-scoring path wins per node; this is candidate-generation provenance —
the reranker does final ordering. Hard guards: depth ≤2 (Phase 1 ships 1-hop only),
≤6 neighbors per expanded node, ≤12 total, visited-set keyed by (store, doc_id), no
self-edges, deterministic ordering (path score, then doc_id). Edges and traversal are
**store-local** (per-vault SQLite or shared store); daemon-level merging happens after,
as today. Cross-store edges are out of scope (integer doc IDs are store-local).

### 3.3 Supersession and contradiction handling

Expansion runs before terminal-status removal so an old lexical hit can lead to its
readable successor. Default recall suppresses the superseded node and returns the
successor with provenance naming the replacement path; `include_superseded=true` keeps
the old node at ×0.15 with `recommended_action='ignore'`. If the successor is
unauthorized, neither its existence nor ID is disclosed.

A returned node with an authorized unresolved contradiction gains an additive
`contradictions` sidecar (separately wrapped evidence, confidence, model provenance,
graph path); recommended action becomes `follow_up` unless instruction-like handling
already escalates. Both sides may also compete as normal rerank candidates.

### 3.4 Provenance and degradation

Graph-derived results add: `retrieval_origin='graph'`, `graph_rank`, `graph_score`,
`graph_paths` (full edge chain with types, confidences, run IDs, model identity),
`seed_doc_id`, and a human rationale ("one-hop incoming `updates` edge from lexical
seed"). Extends the provenance assembled at retrieval.py:2519-2543. Neighbor content
passes through the same instruction-like detection and escaped evidence envelope as
direct hits; denied nodes are indistinguishable from absent nodes, but withheld-neighbor
COUNTS appear in provenance ("1 neighbor withheld").

Runtime graph-query failure → baseline results + `graph_status='degraded'` + trace error.
Distinct from `schema_missing` (§1.4). Never masquerades as successful graph recall.

## 4. Consolidation interplay

Existing candidate consolidation runs every 15 minutes (config.py:181-197) and is left
untouched. New separate daily `graph_maintenance` pass:

- No age-based edge decay (node decay already affects retrieval; decay.py:33-100).
- Mark edges `stale` when cited evidence hashes no longer match.
- Retain `updates` edges as history after supersession.
- Reclassify low-confidence active `relates` edges; upgrade only when the new type clears
  its normal threshold; mark the old edge stale rather than keeping redundant types.
- Re-infer edges for successors rather than copying from superseded memories.
- Record `graph_revalidate | graph_upgrade | graph_stale | graph_conflict` in
  `consolidation_actions` (migration 014 table).
- Physical pruning: only stale low-value `relates` edges after a 30-day audit window;
  doc deletion already cascades edges via FK.

A future `/dream` (currently a design sketch only) may PROPOSE graph mutations through
actions; it never mutates edges directly ("dreams propose; waking endorses").

## 5. Security and privacy

- An edge may connect different privacy levels only if the committing principal can read
  both endpoints; edge existence is sensitive metadata and follows target authorization
  at read time.
- Foreign private/local-only targets excluded by the central gate (principal.py:675-706).
- Classifier I/O: escaped, numbered, evidence-only; schema-constrained output; no tool
  or action channel. Instruction-like flags propagate through traversal
  (contract: docs/contracts/AGENT.md:113-132).
- **Pre-existing hole closed in Phase 1:** `GraphExporter.export()` and
  `SovereignAgent.export_graph()` (agent_api.py:321-363) emit nodes/edges without
  `can_read_document()`; both gain a required principal and per-node gating before typed
  edges exist for them to leak.
- Cross-store edges deferred entirely (see §3.2).

## 6. Testing and acceptance

**Unit/migration:** 016 applies idempotently to shared + every per-vault DB; legacy edge
types preserved with timestamps; `EXPLAIN QUERY PLAN` proves backlink index use; all four
durable-write entry points route through the coordinator; model-output validation rejects
malformed/forged batches; repeated runs produce no duplicate edges/contradiction rows;
supersession is atomic, same-agent/same-store-only, cycle-safe; failed model call leaves
zero partial state; readiness probe distinguishes `schema_missing` from `degraded`;
`providers_for("edge_inference")` is local-only under adversarial configs.

**Property tests** (random cyclic/high-degree graphs): caps never exceeded; per-store
dedup holds; no mirror-edge amplification; no unauthorized ID/URI/path/text/count/
contradiction metadata ever appears; graph-disabled output is bit-identical to baseline;
physical row order never changes ranked output.

**Differential eval** (frozen DB, same model + token budget, graph on/off) using the
existing harness (src/minni/eval/) extended with `graph_enabled`. Ship criteria:

- +5% absolute Recall@5 on the graph-dependent split (multi-hop, no lexical overlap).
- No regression on any query class (existing gate rule, harness.py:114-118); overall
  nDCG@10 and token-budget Recall@5 do not decrease.
- `updates`/`contradicts` precision ≥ 0.90 each; macro-F1 ≥ 0.80; false-positive rate on
  `none` pairs ≤ 0.05 (frozen labeled pair set, never tuned on the retrieval test set).
- ≥95% of gold contradiction pairs surface the other side when either side is retrieved.
- Zero privacy leaks across deterministic tests + 10,000 randomized traversals.
- Read p95 regression ≤20% including the enlarged rerank batch; graph SQL p95 ≤ 15ms.
- Write latency within §2.5 budgets, measured against live AFM.

## 7. Phasing

**P1 — smallest shippable retrieval slice:** migration 016 + readiness gate; canonical
learning nodes (`learning_documents` join table) for new commits; single commit
coordinator; local classifier with hardcoded local-only policy; persisted typed edges +
provenance; 1-hop expansion before rerank behind a feature flag; privacy gating inside
expansion; export_graph principal gating; eval harness extension. NO auto-supersession.
*Exit: schema/idempotency/privacy tests green + graph-on/off eval gate + measured
latency budgets.*

**P2 — lifecycle semantics:** high-confidence same-agent/same-store `updates`
supersession; extended contradiction_log integration + sidecars + subscription support;
legacy `learning://` repair report; `memory_link_inference_runs` ledger table if run
observability proves needed.
*Exit: atomic-chain, stale-belief, contradiction-fanout, adversarial-ownership tests.*

**P3 — depth and maintenance:** selective two-hop traversal; daily graph_maintenance
pass; evidence-hash revalidation; relates upgrades; wiki indexing stamps
`memory_kind='wiki'` + stable memory URIs.
*Exit: multi-hop eval split, bounded-latency, consolidation-action audit tests.*

**P4 — outside this spec's gate:** privacy-gated graph export for UI; Memory Board
visualization of real typed edges; optional /dream proposal integration.

## 8. Open questions and risks

1. **Threshold calibration is unverified** — freeze a labeled pair set before tuning;
   never tune on the final retrieval test set.
2. **Local model latency is unverified** — prototype one native/loopback batch call and
   measure before committing to the 2.0s timeout.
3. **Canonical-node backfill ambiguity** — repair only provable 1:1 aliases; report the
   rest via health diagnostics.
4. **Blocking promotion changes current fail-open behavior** — approved deliberately
   (locked decision 5); staging is the durability buffer. Revisit only if AFM outages
   prove frequent enough to hurt in practice.
5. **Cross-store edges** — deferred; would need stable global memory URIs and a
   daemon-level edge registry (outside Approach A).

# Minni Typed Memory Graph — Design & Migration Acceptance Addendum

**Date:** 2026-09-05
**Status:** Proposed Reconciliation & Migration Acceptance Specification (Pending Independent Parent Acceptance)
**Baseline SHA:** `2af2d888`
**Parent Document:** [2026-07-09-typed-memory-graph-design.md](2026-07-09-typed-memory-graph-design.md)
**Authors:** Antigravity (Autonomous Peer) in collaboration with Codex; approved core design by Hans (2026-07-09).

---

## 1. Executive Summary & Audit Drift Reconciliation

This addendum reconciles the approved Minni Typed Memory Graph specification (`2026-07-09-typed-memory-graph-design.md`) with current `main` at commit `2af2d888`. Hans approved the original architecture and instructed that it be carried forward into implementation, not silently parked or watered down.

An independent audit of the current codebase identified six concrete drift vectors between the 2026-07-09 spec and `main`. This section establishes the authoritative reconciliation for each:

| # | Drift Finding | Root Cause on `main` (`2af2d888`) | Authoritative Resolution |
|---|---|---|---|
| **1** | **Migration 016 is occupied; sequence is now 020** | `016_normalize_document_timestamps.sql` through `020_thread_delivery_cursors.sql` have landed since July 2026. | Assign migration number **`021_typed_memory_graph.sql`**. Update all references, migration runner checks, and documentation. |
| **2** | **Readiness probe must validate required schema semantics** | `_execute_tolerant` in `migrations.py` skips missing tables/columns without raising; checking only names permits drifted/wrong-definition UNIQUE/FK/PK/indexes. | Expand `graph_readiness_probe()` to validate **all 18 added/defined columns, 4 tables, 5 secondary indexes, composite PK `(learning_id, doc_id)`, and CASCADE foreign keys** across `documents`, `learning_documents`, `memory_links`, and `contradiction_log`. |
| **3** | **Denied-neighbor counts contradict denied-node indistinguishability** | Original §3.4 stated: *"denied nodes are indistinguishable from absent nodes, but withheld-neighbor COUNTS appear in provenance ('1 neighbor withheld')"*. Disclosing the count leaks the existence and topology of unauthorized edges. | **Enforce strict indistinguishability**: Caller-facing result envelopes and human rationales **must not** disclose withheld neighbor counts. Unauthorized neighbors are dropped silently during graph expansion, exactly matching absent nodes. Internal audit logs retain diagnostic counters under operator privilege only. |
| **4** | **Many-learnings-to-one canonical document requires aggregate liveness** | Content-keyed durable document paths (`_durable_doc_path`) map multiple distinct learnings with identical content to a single `documents` row (`learning_documents` join). If one learning is superseded, naively setting `documents.page_status = 'superseded'` kills other active learnings sharing the node. | Implement **Deterministic Aggregate Liveness**: The canonical `documents` row remains active (`page_status = 'accepted'`) as long as **at least one** associated learning in `learning_documents` remains active. Retirement occurs only when **all** linked learnings are inactive; the lifecycle table below selects `superseded`, `expired`, or `rejected`, with deterministic successor resolution ($\max(superseded\_by)$) where applicable. Zero mappings preserve existing document state without resurrection. |
| **5** | **Standing repair of already-durable projections vs. new promotions** | Conflating the fail-loud rule for new promotions with background repair would violate Minni's core durability guarantee, while claiming embedder outage is "successful" misrepresents partial projection state. | **Decompose repair outcomes**: <br>• *Preserved Durable Truth*: The existing `learnings` row is immutable committed truth.<br>• *Projection Repair*: Reports `complete` (doc+vectors), `incomplete_lexical_only` (embedder offline; degraded), or `failed` (I/O error).<br>• *Deferred Typed Edges*: Edges deferred (`edges_deferred='degraded'`) if classifier offline without blocking projection repair.<br>• *New Promotion*: Fail-loud. Candidate stays staged; zero durable writes. |
| **6** | **Phase 1 new learning vs. Phase 3 wiki indexing** | Potential confusion over whether wiki pages participate in graph edges in Phase 1 before receiving schema stamps. | **Retain Intentional Phase Boundary**: In Phase 1, edge inference is strictly **learning-to-learning**. Candidate shortlist is filtered to `memory_kind = 'learning'`. Wiki pages are excluded from Phase 1 candidate shortlisting and formally enter the graph in Phase 3 when `wiki_indexer.py` stamps `memory_kind = 'wiki'` and stable URIs. |

---

## 2. Locked Decisions (Preserved Without Regression)

The original approved decisions remain intact; the last item states the reconciled rollout mechanism:

1. **Additive `memory_links`**: The memory graph is an extension of the existing `memory_links` table. No parallel graph database, separate vector store, or secondary document table is introduced.
2. **Memories as Nodes**: Graph nodes are physical memories (learnings and wiki documents), not synthetic extracted entity abstractions.
3. **Local-Only Inference at Durable Promotion**: Edge classification is hardcoded to `local_only=True` via `OperationPolicy`. No cloud model provider may ever be invoked for edge inference, regardless of system configuration.
4. **Fail-Loud New Promotion**: If edge inference cannot execute successfully during new learning promotion (due to local model timeout, offline provider, or contract violation), durable promotion is aborted, and the proposal remains staged in `candidate_packets`. No silent edge-less promotions or unmonitored async catch-up queues are permitted.
5. **Existing Cross-Project Authorized Recall**: The security boundary defined by `can_read_document()`, workspace scoping, and principal capability verification is preserved bit-for-bit. Graph traversal operates strictly within store-local boundaries and enforces privacy gating before hydration.
6. **Decoupled Operational Toggles**: Write-time edge classification (`config.graph_classification_enabled`) and read-time graph expansion (`config.graph_expansion_enabled`) are independent flags.


---

## 3. Phased Implementable Plan

### Phase 1: Core Substrate & 1-Hop Read Expansion (Smallest Shippable Slice)

*Goal: Establish durable schema, atomic write coordinator, local classifier, and privacy-safe 1-hop recall.*

1. **Database & Schema Substrate (`P1.1`)**:
   - Deliver `src/minni/migrations/021_typed_memory_graph.sql`.
   - Update `src/minni/migrations.py` with `_migration_present_in_schema(conn, 21)` and `_verify_migration_021_graph_schema()`.
   - Implement `src/minni/graph_readiness.py` providing `check_graph_readiness(conn)` to probe tables, columns, and indexes.
2. **Local Inference Policy & Operational Toggles (`P1.2`)**:
   - Update `src/minni/model_provider.py`: Unconditionally seed `"edge_inference": OperationPolicy(local_only=True)` in `default_provider_chain()`. Add unit assertions that cloud providers are never eligible.
   - Implement batched edge classification prompt (`prompts/edge_inference_v1.txt`) within AFM token budget (≤3,200 tokens).
   - Establish two decoupled operational configuration flags:
     - `config.graph_classification_enabled`: Governs write-time edge inference at durable promotion (default `True`).
     - `config.graph_expansion_enabled`: Governs read-time 1-hop neighbor traversal in `retrieve()` (default `False` during bootstrap).
3. **Single Write Coordinator (`P1.3`)**:
   - Implement `commit_learning_with_graph()` in `src/minni/graph_coordinator.py`:
     - Phase A: Candidate shortlist via FAISS (top 48 chunks → top 12 docs, cosine ≥ 0.42) + local model batch inference outside the write lock.
       - **Phase 1 Filter**: Restrict candidates strictly to canonical **learnings** (`memory_kind = 'learning'`, `page_type = 'learning'`). Wiki documents are excluded in Phase 1 and enter candidate shortlisting in Phase 3.
     - Phase B: Atomic `BEGIN IMMEDIATE` transaction writing: learning, canonical document, `learning_documents` join row, FTS5/chunk_embeddings, typed `memory_links` edges.
     - Phase C: Post-commit FAISS refresh.
   - Unify entry points: `resolve_candidate` (accept), `handle_learn` (force), and AFM consolidation.
4. **1-Hop Retrieval Expansion (`P1.4`)**:
   - Integrate `expand_typed_graph()` in `src/minni/retrieval.py` between RRF fusion (Step 3) and Cross-Encoder reranking (Step 4), gated behind `config.graph_expansion_enabled`.
   - Enforce internal privacy check: `can_read_document()` inside expansion before hydration.
   - Hard candidate caps: ≤8 seeds, ≤6 neighbors/node, ≤12 total graph candidates.
   - Strict indistinguishability: Withheld neighbors are dropped silently; no caller-facing withheld counts.
5. **Security Gating on Graph Export (`P1.5`)**:
   - Patch `GraphExporter.export()` and `SovereignAgent.export_graph()` to require an `EffectivePrincipal` and filter nodes/edges with `can_read_document()`.

*Phase 1 Exit Gate (Explicit Quantitative Gates — Preserved from Spec §6):*
1. **Migration & Common Verifier**:
   - Migration `021_typed_memory_graph.sql` applies idempotently to shared and every per-vault DB; schema matches normative contract bit-for-bit.
   - Unified verifier (`verify_graph_schema`) accurately flags `ready`, `schema_missing`, and `schema_drifted` across all test matrix conditions (`TC-READY-01` through `07`).
2. **Quantitative Retrieval Differential Eval Gate** (Membench harness `src/minni/eval/`, frozen DB, same model + token budget, graph on vs. off):
   - **+5% absolute Recall@5** on the graph-dependent split (multi-hop traversal, zero lexical overlap).
   - **Zero regression** on any existing query class (`harness.py:114-118`).
   - Overall **nDCG@10** and token-budget **Recall@5** do not decrease.
   - Read latency p95 regression **$\le 20\%$** including the enlarged rerank batch; graph SQL traversal p95 **$\le 15$ms**.
3. **Quantitative Classifier Quality Gate** (Frozen labeled pair set, never tuned on retrieval test set):
   - **Precision $\ge 0.90$** on `updates` and `contradicts` pairs individually.
   - **Macro-F1 $\ge 0.80$** across all edge classes (`updates`, `extends`, `contradicts`, `relates`, `none`).
   - **False positive rate $\le 0.05$** on `none` pairs (critical protection against spurious edge pollution).
   - **$\ge 95\%$** of gold contradiction pairs surface the counter-edge when either side is retrieved.
   - Single batched classification latency p95 **$\le 1.2$s**, hard timeout **2.0s** measured against live AFM/loopback; write commit latency addition p95 **$\le 1.5$s**, p99 **$\le 2.2$s**.
4. **Durability & Fail-Loud Gate**:
   - Candidate promotion aborts with zero durable writes on classifier timeout, outage, or malformed batch (proposal stays staged in `candidate_packets`).
   - Standing repair decomposes cleanly: embedder outage yields `incomplete_lexical_only` without touching durable `learnings` rows; classifier outage yields `edges_deferred='degraded'`.
5. **Security & Privacy Gate**:
   - Zero privacy leaks across deterministic test suite + 10,000 randomized graph traversals.
   - Strict indistinguishability verified: caller-facing result envelopes disclose zero withheld neighbor counts.
   - `export_graph` and `GraphExporter.export()` enforce principal gating with `can_read_document()`.

---

### Phase 2: Lifecycle Semantics & Contradiction Governance

*Goal: Enable automatic supersession, contradiction surfacing, and legacy alias repair.*

1. **High-Confidence Auto-Supersession (`P2.1`)**:
   - In write coordinator: If inferred edge is `updates` with confidence ≥ 0.96, and target is an active learning owned by the **same agent in the same store**, execute atomic supersession.
   - Update target learning (`status='superseded'`, `superseded_by=new_lid`).
   - Evaluate N:1 aggregate liveness for target document: Only update `documents.page_status='superseded'` if all attached learnings are now superseded.
   - Cross-agent, cross-store, or wiki targets downgrade to `graph_update_review` in `consolidation_actions`.
2. **Contradiction Subsystem Integration (`P2.2`)**:
   - When edge is `contradicts` (confidence ≥ 0.88), insert row into `contradiction_log` with `resolution_status='unresolved'`.
   - Update `retrieval.py`: Returned nodes with unresolved contradictions attach an evidence sidecar (`recommended_action='follow_up'`).
   - Integrate with `minni_subscribe_contradictions`.
3. **Legacy Alias Repair Diagnostic (`P2.3`)**:
   - Add offline diagnostic in `health.py` identifying 1:1 legacy `learning://<id>` alias documents and proposing migration to canonical `_durable` nodes.

---

### Phase 3: Traversal Depth & Graph Maintenance

*Goal: Expand traversal to 2 hops, implement daily maintenance, and index wiki graph metadata.*

1. **Bounded 2-Hop Traversal (`P3.1`)**:
   - Enable 2-hop expansion for `updates`, `extends`, and `derived_from` edges with 0.65 hop decay factor.
2. **Daily Graph Maintenance Sweep (`P3.2`)**:
   - Verify cited evidence hashes; mark edges `stale` upon mismatch.
   - Reclassify low-confidence active `relates` edges.
   - Prune stale `relates` edges after a 30-day grace period.
3. **Wiki Page Graph Indexing (`P3.3`)**:
   - In `wiki_indexer.py`: Stamp `documents.memory_kind = 'wiki'` and maintain deterministic `memory_uri = 'wiki://<path>'`.
   - Activate wiki documents as both targets and sources of typed memory graph edges.

---

### Phase 4: Visualization & Memory Board (Out of Scope for Core Engine Gate)

*Goal: Surface typed edges and contradiction clusters in the Web Console / Memory Board UI.*

---

## 4. Transaction and State-Transition Tables

### 4.1 Candidate, Learning, and Edge State Transitions

| Entity | Initial State | Trigger Event | Guard Condition | Target State | Transaction Scope |
|---|---|---|---|---|---|
| **Candidate Packet** | `proposed` | `resolve_candidate(accept)` | Model inference OK | `accepted` | `BEGIN IMMEDIATE` (Atomic with learning write) |
| **Candidate Packet** | `proposed` | `resolve_candidate(accept)` | Model unavailable / timeout | `proposed` (stays staged) | None (Aborted before transaction) |
| **Candidate Packet** | `proposed` | `resolve_candidate(reject)` | Operator authorized | `rejected` | `BEGIN IMMEDIATE` |
| **Learning** | `(none)` | Promotion commit | Candidate accepted | `active` (`status=NULL`, `superseded_by=NULL`) | `BEGIN IMMEDIATE` |
| **Learning** | `active` | Inferred `updates` (conf ≥ 0.96) | Same agent, same store, active | `superseded` (`superseded_by=new_lid`) | `BEGIN IMMEDIATE` |
| **Memory Edge** | `(none)` | Inference commit | Conf ≥ threshold, valid evidence | `active` | `BEGIN IMMEDIATE` |
| **Memory Edge** | `active` | Maintenance pass | Target deleted / evidence hash mismatch | `stale` | Maintenance txn |
| **Contradiction** | `(none)` | Inferred `contradicts` (conf ≥ 0.88) | Document pair valid | `unresolved` | `BEGIN IMMEDIATE` |
| **Contradiction** | `unresolved` | `resolve_contradiction` | Operator or owning agent resolved | `resolved` | `BEGIN IMMEDIATE` |

---

### 4.2 N:1 Aggregate Liveness Truth Table (Canonical Document Lifecycle)

Because `_durable_doc_path` hashes `(agent_id, content)`, multiple distinct learnings $L_1, L_2, \dots, L_k$ can map to the exact same physical document row $D$. The lifecycle of $D$ is governed by the aggregate state of all attached learnings in `learning_documents`.

**Aggregate Invariant:**
$$\text{Liveness}(D) = \bigvee_{L \in \text{Learnings}(D)} \Big( \text{status}(L) \notin \{\text{'rejected'}, \text{'expired'}, \text{'superseded'}\} \land \text{superseded\_by}(L) \text{ IS NULL} \Big)$$

**Deterministic Successor Resolution Rule:**
When all attached learnings are inactive, if at least one learning was superseded, the document's `superseded_by` pointer is deterministically resolved to the canonical document of the designated successor learning.
In Minni's schema, `learnings.learning_id` is an `INTEGER PRIMARY KEY AUTOINCREMENT`. Monotonically increasing `learning_id` values define database insertion sequence rather than real-world physical event chronology. Choosing the maximum successor ID:
$$S^* = \max \left\{ L.\text{superseded\_by} \colon L \in \text{Learnings}(D), L.\text{superseded\_by} \text{ IS NOT NULL} \right\}$$
provides a **chosen deterministic stable tie-break** across multiple candidate successors, avoiding non-deterministic query ordering. The document pointer is then resolved via:
$$\text{doc.superseded\_by} = \text{canonical doc\_id of } S^* \text{ in } \texttt{learning\_documents}$$

| Scenario | State of Attached Learnings | Document `page_status` | Document `superseded_by` | In Recall Pool? | Deterministic Semantic Rationale |
|---|---|---|---|---|---|
| **Initial Commit** | $L_1$ active | `accepted` | `NULL` | **Yes** | Single learning mapped to canonical doc. |
| **Duplicate Ingestion** | $L_1$ active, $L_2$ active | `accepted` | `NULL` | **Yes** | Identical content committed again; both learnings live. |
| **Partial Supersession** | $L_1$ superseded by $S_1$; $L_2$ active | `accepted` | `NULL` | **Yes** | $L_1$ is replaced, but $L_2$ remains valid. Document **must remain active** so $L_2$ is recallable. |
| **Full Supersession** | $L_1$ superseded by $S_1$; $L_2$ superseded by $S_2$ | `superseded` | Canonical `doc_id` of $\max(S_1, S_2)$ | **No** (unless `include_superseded`) | All attached learnings dead. Successor learning deterministically selected via stable autoincrement ID tie-break. |
| **Partial Expiry** | $L_1$ expired; $L_2$ active | `accepted` | `NULL` | **Yes** | Document survives while any learning is non-expired. |
| **Mixed Superseded + Expired** | $L_1$ superseded by $S_1$; $L_2$ expired | `superseded` | Canonical `doc_id` of $S_1$ | **No** (unless `include_superseded`) | Supersession provides replacement knowledge; takes precedence over bare expiry. |
| **Mixed Superseded + Rejected** | $L_1$ superseded by $S_1$; $L_2$ rejected | `superseded` | Canonical `doc_id` of $S_1$ | **No** (unless `include_superseded`) | Supersession provides replacement knowledge; takes precedence over rejection. |
| **Full Expiry** | $L_1$ expired; $L_2$ expired | `expired` | `NULL` | **No** | All attached learnings expired; no successor exists. |
| **Full Rejection** | $L_1$ rejected; $L_2$ rejected | `rejected` | `NULL` | **No** | All attached learnings rejected; no successor exists. |
| **Mixed Expired + Rejected** | $L_1$ expired; $L_2$ rejected | `expired` | `NULL` | **No** | Neither is superseded; expiry takes precedence over bare rejection. |
| **Zero Attached Learnings** | No rows in `learning_documents` for `doc_id` | *Preserved* (existing row status) | *Preserved* (existing row pointer) | Follows existing status | Non-learning document or detached doc; strictly preserves existing row state without resurrection. |

---

### 4.3 Atomic Transaction Boundary & Failure Matrix

```
[ Incoming Request: Promotion or Repair ]
                   │
                   ▼
       Is operation New Promotion or Standing Repair?
        ├── New Promotion ──────────────┐
        └── Standing Repair ────┐       │
                                │       │
                                ▼       ▼
```

| Operational Condition | New Promotion (`resolve_candidate`, `handle_learn`) | Standing Repair (`reconstruct_learning_projections`) |
|---|---|---|
| **Condition A: Graph Enabled & Ready** (`graph_status = 'ready'`) | **Atomic Success**: Pre-compute shortlist + model inference outside txn. `BEGIN IMMEDIATE` writes learning + doc + join + chunks + edges. Commit. Return `ok`. | **Projection Complete + Edges**: Reconstructs doc, join row, chunks. Pre-computes edges. Commits all. Return `complete`. |
| **Condition B: Model Failure / Timeout** (`graph_status = 'degraded'`) | **Fail-Loud**: Txn aborted before write. Candidate stays `proposed`. Return error: `edge_inference_timeout` or `edge_inference_unavailable`. | **Fail-Open (Durability First)**: Reconstructs doc, join row, chunks. Edges deferred (`edges_deferred='degraded'`). Returns `complete_edges_deferred`. If embedder down: returns `incomplete_lexical_only`. Learning is untouched. |
| **Condition C: Schema Missing** (`graph_status = 'schema_missing'`) | **Fail-Loud**: Txn aborted. Candidate stays `proposed`. Return error: `edge_inference_schema_missing`. Prevents edge-less data drift. | **Fail-Open (Fallback Projection)**: Reconstructs baseline doc and chunks. Skips join row and edges. Returns `incomplete_schema_missing` (truth preserved). |
| **Condition D: Feature Disabled** (`graph_classification_enabled = False`) | **Baseline Promotion**: Writes learning + doc + chunks via standard baseline path. Zero graph inference attempted. Return `ok`. | **Baseline Repair**: Reconstructs baseline doc + chunks. Zero graph inference attempted. Returns `complete_edges_disabled`. |


---

## 5. Migration Partial Failure, Retry, and Compatibility Contract

### 5.1 Migration Script Design (`021_typed_memory_graph.sql`)

The migration is strictly additive and designed to execute cleanly under SQLite's transaction model:

```sql
-- Migration 021: Minni Typed Memory Graph Schema
-- Target: SQLite shared database and per-vault stores

-- 1. Extend documents for memory typing and stable URIs
ALTER TABLE documents ADD COLUMN memory_kind TEXT;
ALTER TABLE documents ADD COLUMN memory_uri TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_memory_uri
    ON documents(memory_uri) WHERE memory_uri IS NOT NULL;

-- 2. Join table for N:1 Canonical Learning Documents
CREATE TABLE IF NOT EXISTS learning_documents (
    learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
    doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    created_at REAL,
    PRIMARY KEY (learning_id, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_learning_documents_doc_id
    ON learning_documents(doc_id);

-- 3. Extend memory_links for typed edge attributes
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

-- 4. Extend contradiction_log for graph document pairing
ALTER TABLE contradiction_log ADD COLUMN source_doc_id INTEGER
    REFERENCES documents(doc_id) ON DELETE SET NULL;
ALTER TABLE contradiction_log ADD COLUMN target_doc_id INTEGER
    REFERENCES documents(doc_id) ON DELETE SET NULL;
ALTER TABLE contradiction_log ADD COLUMN edge_run_id TEXT;
ALTER TABLE contradiction_log ADD COLUMN confidence REAL;
ALTER TABLE contradiction_log ADD COLUMN resolution_status TEXT DEFAULT 'unresolved';

-- Legacy rows classified explicitly so they are distinguishable from new detections
UPDATE contradiction_log SET resolution_status = 'legacy_unclassified'
    WHERE resolution_status = 'unresolved' AND source_doc_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_contradiction_graph_pair
    ON contradiction_log(source_doc_id, target_doc_id, resolution_status);

-- Backfill existing explicit links
UPDATE memory_links SET
    confidence = COALESCE(confidence, 1.0),
    inference_method = COALESCE(inference_method, CASE link_type
        WHEN 'wikilink' THEN 'explicit_wikilink'
        WHEN 'derived_from' THEN 'writeback_evidence'
        ELSE 'legacy' END)
    WHERE confidence IS NULL OR inference_method IS NULL;
```

### 5.2 Interaction with `migrations.py` and `_execute_tolerant`

In `src/minni/migrations.py`, the runner applies statements using `_execute_tolerant()`, which catches `sqlite3.OperationalError` and ignores "duplicate column" (for idempotency) and "no such table/column" on partial test schemas.

**The Partial Schema Risk:** If an `ALTER TABLE` statement fails due to an unexpected constraint or missing baseline table, `_execute_tolerant` could skip it without aborting `_flush_batch()`, causing migration 021 to be stamped as applied in `schema_migrations` even though the schema is incomplete.

**The Mitigation (Two-Tier Contract):**
1. **Runner Schema Probe (`_migration_present_in_schema`)**:
   ```python
   if version == 21:
       return verify_graph_schema(conn).ready
   ```
2. **Post-Batch Strict Validation Hook (`_verify_migration_021_graph_schema`)**:
   Executed immediately after statement flushing for version 21 within `_flush_batch()`. It runs the full readiness check. If the base tables are incomplete, it returns false, skips the `schema_migrations` stamp, and commits the batch without 021; the runner retries on next startup once the base schema is supplied. If the existing tables are present but drifted, it raises a hard exception, forcing `conn.rollback()` so that `schema_migrations` is NOT stamped.

### 5.3 Retry and Multi-Store Compatibility

- **Crash / Lock during Migration**: SQLite rollback leaves `schema_migrations` and `PRAGMA user_version` unchanged. Subsequent boot retries version 21 automatically.
- **Per-Vault Isolation**: Each agent vault (`~/.minni/vaults/<agent>/minni.db`) runs migrations independently on first contact via `SovereignDB._init_schema()`.
- **Pre-021 Database Backward Compatibility**: If a database is opened by code where graph features are enabled, but the schema has not yet migrated, the readiness probe flags `graph_status = 'schema_missing'`. All reads gracefully fall back to baseline hybrid search (FTS5 + FAISS); new promotions fail loud with clear diagnostics.

---

## 6. Privacy and N:1 Lifecycle Acceptance Matrix

### 6.1 Denied-Node Indistinguishability Specification

**Theorem (Graph Indistinguishability):**
Let $G = (V, E)$ be the stored memory graph. For any requesting principal $P$, let $V_{\text{denied}} = \{v \in V \mid \neg \text{can\_read\_document}(P, \text{ws}, v)\}$. Let $G_P = (V \setminus V_{\text{denied}}, E_P)$ be the subgraph observable by $P$.

*Security Invariant:* The output of `retrieve(query, principal=P)` on graph $G$ must be **strictly bit-identical** to the output of `retrieve(query, principal=P)` executed on a hypothetical database containing only $G_P$.

*Corollaries:*
1. **No Withheld Counters**: Provenance metadata returned to $P$ must never contain counts of suppressed neighbors (e.g., `"1 neighbor withheld"`). Emitting this count proves the existence of an edge $(u, v)$ where $v \in V_{\text{denied}}$, violating indistinguishability.
2. **Expansion Filtering**: Candidate documents produced during graph traversal must pass `can_read_document(P, ws, doc)` **before** entering the candidate pool and before any chunk text is fetched or sent to the cross-encoder reranker.
3. **No Dangling Structural Hints**: Path provenance in search results (`graph_paths`) must terminate strictly at authorized nodes.

### 6.2 Acceptance Verification Suite

| Test ID | Category | Target Invariant | Assertion / Verification Command |
|---|---|---|---|
| `TEST-MG-01` | Migration | Idempotency | Apply 021 twice to fresh and legacy DBs; verify `PRAGMA integrity_check` is `ok` and schema matches exact column specification. |
| `TEST-MG-02` | Readiness | Fault Isolation | Create DB missing `learning_documents`; verify `check_graph_readiness()` returns `(False, "schema_missing: table 'learning_documents' missing")` and `graph_status` is `schema_missing`. |
| `TEST-MG-03` | Inference | Local-Only Enforcement | Invoke `providers_for("edge_inference")` under configuration with active cloud providers. Assert returned provider list contains only local/loopback instances. |
| `TEST-MG-04` | Durability | Fail-Loud Promotion | Simulate local model timeout (mock latency > 2.0s). Attempt `resolve_candidate(accept)`. Assert candidate remains `proposed`, no learning is inserted, and response code is `edge_inference_timeout`. |
| `TEST-MG-05` | Durability | Standing Repair Safety | Trigger `reconstruct_learning_projections` with local model mock down. Assert existing durable learnings are **not** modified/deleted, document projection is reconstructed, and `edges_deferred='degraded'` is logged. |
| `TEST-MG-06` | Privacy | Indistinguishability | Populate Node A (public) linked to Node B (private to Agent X). Query as Agent Y. Assert Node A is returned with `graph_paths` showing no reference or count of Node B. Compare caller-visible graph content against a DB where Node B was never inserted, excluding volatile timing/request identifiers; authorized content and topology must be identical. |
| `TEST-MG-07` | Lifecycle | N:1 Aggregate Liveness | Commit two identical learnings $L_1$ and $L_2$ mapping to canonical Doc $D$. Supersede $L_1$. Query recall pool. Assert Doc $D$ is **still returned** as active. Supersede $L_2$. Assert Doc $D$ is now excluded. |
| `TEST-MG-08` | Retrieval | Multi-hop Expansion | Query requiring 1-hop traversal to connect non-lexical match. Assert Recall@5 increases without precision regression on baseline query set. |

---

## 7. Normative Schema & Lifecycle Contract (Phase 1 Acceptance Specification)

*Note: Per task instructions, no production schema or application code is modified in this design assignment. The following contract defines the exact normative specification, unified verifier rules, and concrete acceptance cases that govern Phase 1 implementation and verification.*

### 7.1 Unified Verifier Architecture Contract

To eliminate divergence between migration execution and runtime execution, **one common verifier** (`src/minni/graph_readiness.py:verify_graph_schema(conn)`) must govern both:
1. **Migration Completion Detection**: Used by `src/minni/migrations.py:_migration_present_in_schema(conn, 21)` to determine if migration 021 has been applied.
2. **Read/Write Runtime Readiness**: Used by `SovereignDB._init_schema()` and `graph_coordinator` to gate write-time edge inference and read-time graph expansion.

Migration 021 is considered complete, and runtime components are considered ready, if and only if `verify_graph_schema(conn)` evaluates to `ready`.

#### Verification Status Classification
- **`schema_missing`**: One or more of the 4 required tables (`documents`, `learning_documents`, `memory_links`, `contradiction_log`) are absent from `sqlite_master`.
- **`schema_drifted`**: All 4 tables exist, but any structural or semantic attribute diverges from the normative contract:
  - Column name, declared affinity/type, nullability constraint, or default value mismatch.
  - Primary key shape violation on `learning_documents` (count $\ne 2$, incorrect column order, or extra PK column).
  - Foreign key target, column mapping, or `ON DELETE` action mismatch (`CASCADE`, `SET NULL`).
  - Secondary index absence, non-uniqueness, missing/altered `WHERE` partial clause, or mismatched column sequence.
- **`ready`**: All 18 added/defined columns, 4 tables, 5 secondary indexes, composite primary key, and foreign key actions match the normative specification bit-for-bit.

---

### 7.2 Normative Schema Specification (Authoritative 18-Column / 4-Table Contract)

The common verifier must validate the database schema against the following exact specifications:

#### 1. Table `documents` (Extended)
- **Required Columns (2 added)**:
  | Column | Declared Type | Nullable | Default | Normative Invariant Role |
  |---|---|---|---|---|
  | `memory_kind` | `TEXT` | Yes (`notnull=0`) | `NULL` | Discriminator (`'learning'` in P1; `'wiki'` in P3). |
  | `memory_uri` | `TEXT` | Yes (`notnull=0`) | `NULL` | Stable global URI identifier. |
- **Secondary Index (1)**:
  - `idx_documents_memory_uri`:
    - `is_unique`: `True`
    - `indexed_columns`: `['memory_uri']`
    - `partial_predicate`: `WHERE memory_uri IS NOT NULL` (verified via `sqlite_master.sql`)

#### 2. Table `learning_documents` (New Join Table)
- **Required Columns (3 defined)**:
  | Column | Declared Type | Nullable | PK Sequence | Normative Invariant Role |
  |---|---|---|---|---|
  | `learning_id` | `INTEGER` | No (`notnull=1`) | `1` (1st PK col) | FK targeting `learnings(learning_id)`. |
  | `doc_id` | `INTEGER` | No (`notnull=1`) | `2` (2nd PK col) | FK targeting `documents(doc_id)`. |
  | `created_at` | `REAL` | Yes (`notnull=0`) | `0` (Non-PK) | Record creation timestamp. |
- **Primary Key Shape (Strict Contract)**:
  - PK column count **must equal 2**.
  - PK column order **must strictly be** `['learning_id', 'doc_id']`.
  - Schema is rejected as `schema_drifted` if PK count $> 2$ or column sequence is inverted.
- **Foreign Key Constraints (Strict Contract)**:
  - `doc_id -> documents(doc_id)`: `on_delete = 'CASCADE'` (deleting a canonical doc cascades join rows).
  - `learning_id -> learnings(learning_id)`: `on_delete = 'NO ACTION'` or `'RESTRICT'` (preserves durable learning).
- **Secondary Index (1)**:
  - `idx_learning_documents_doc_id`: Non-unique, exact column sequence `['doc_id']`.

#### 3. Table `memory_links` (Extended)
- **Required Columns (8 added)**:
  | Column | Declared Type | Nullable | Default | Normative Invariant Role |
  |---|---|---|---|---|
  | `confidence` | `REAL` | Yes (`notnull=0`) | `NULL` | Finite model confidence $\in [0.0, 1.0]$. |
  | `inference_method` | `TEXT` | Yes (`notnull=0`) | `NULL` | Provenance label (`local_afm_v1`, `explicit_wikilink`, etc.). |
  | `model_id` | `TEXT` | Yes (`notnull=0`) | `NULL` | Classifier model ID string. |
  | `prompt_version` | `TEXT` | Yes (`notnull=0`) | `NULL` | Prompt template version identifier. |
  | `inference_run_id` | `TEXT` | Yes (`notnull=0`) | `NULL` | Deterministic run ID for idempotency. |
  | `evidence_json` | `TEXT` | Yes (`notnull=0`) | `NULL` | Bounded JSON evidence hashes and excerpt refs. |
  | `inferred_at` | `REAL` | Yes (`notnull=0`) | `NULL` | Timestamp of classification. |
  | `edge_status` | `TEXT` | No (`notnull=1`) | `'active'` | Edge lifecycle status (`active`, `stale`). |
- **Secondary Indexes (2 covering)**:
  - `idx_memory_links_target_active`: Non-unique, exact column sequence `['target_doc_id', 'edge_status', 'link_type', 'source_doc_id']`.
  - `idx_memory_links_source_active`: Non-unique, exact column sequence `['source_doc_id', 'edge_status', 'link_type', 'target_doc_id']`.

#### 4. Table `contradiction_log` (Extended)
- **Required Columns (5 added)**:
  | Column | Declared Type | Nullable | Default | Foreign Key | Normative Invariant Role |
  |---|---|---|---|---|---|
  | `source_doc_id` | `INTEGER` | Yes (`notnull=0`) | `NULL` | `documents(doc_id) ON DELETE SET NULL` | Originating document node. |
  | `target_doc_id` | `INTEGER` | Yes (`notnull=0`) | `NULL` | `documents(doc_id) ON DELETE SET NULL` | Contradicted document node. |
  | `edge_run_id` | `TEXT` | Yes (`notnull=0`) | `NULL` | None | Inference run correlation ID. |
  | `confidence` | `REAL` | Yes (`notnull=0`) | `NULL` | None | Contradiction confidence score. |
  | `resolution_status` | `TEXT` | Yes (`notnull=0`) | `'unresolved'` | None | Status (`unresolved`, `resolved`, `legacy_unclassified`). |
- **Foreign Key Constraints (Strict Contract)**:
  - `source_doc_id -> documents(doc_id)`: `on_delete = 'SET NULL'`.
  - `target_doc_id -> documents(doc_id)`: `on_delete = 'SET NULL'`.
- **Secondary Index (1)**:
  - `idx_contradiction_graph_pair`: Non-unique, exact column sequence `['source_doc_id', 'target_doc_id', 'resolution_status']`.

---

### 7.3 Operational Policies & Decoupled Configuration Contract

1. **Unconditional Local-Only Seeding (`src/minni/model_provider.py`)**:
   `default_provider_chain()` unconditionally seeds `"edge_inference": OperationPolicy(local_only=True)`.
2. **Immutable Safety Override**:
   Any configuration entry attempting to map `edge_inference` with `localOnly: false` is forcibly overridden to `local_only = True` by the provider loader. Graph edge classification is structurally barred from cloud routing.
3. **Decoupled Operational Flags (`src/minni/config.py`)**:
   - `config.graph_classification_enabled: bool = True`: Controls write-time candidate shortlisting and edge inference during promotion.
   - `config.graph_expansion_enabled: bool = False`: Controls read-time 1-hop neighbor traversal in `retrieve()`. Remains `False` during initial Phase 1 deployment until the differential evaluation gate is cleared.

---

### 7.4 N:1 Aggregate Liveness & Successor Tie-Break Contract

`src/minni/graph_lifecycle.py:evaluate_canonical_doc_liveness(conn, doc_id)` executes under the following normative contract:

1. **State Preservation (Non-Resurrection)**:
   - Queries `SELECT page_status, superseded_by FROM documents WHERE doc_id = ?`.
   - If document does not exist, raises `DocumentNotFoundError`.
   - If zero mappings exist in `learning_documents` for `doc_id` (e.g. non-learning document or detached row), **strictly preserves existing document row state**, returning `(current_status, current_superseded_by)`. Zero mappings never resurrect retired, expired, or rejected documents to `'accepted'`.
2. **Aggregate Liveness**:
   - If at least one attached learning is active (`superseded_by IS NULL` and `status NOT IN ('rejected', 'expired', 'superseded')`), returns `('accepted', None)`.
3. **Deterministic Precedence Hierarchy (All Attached Learnings Inactive)**:
   - **Precedence 1: Superseded**: If $\ge 1$ attached learning has `superseded_by IS NOT NULL`:
     - *Stable Tie-Break*: In SQLite, `learnings.learning_id` is an `INTEGER PRIMARY KEY AUTOINCREMENT`. Increasing IDs reflect database insertion order, not real-world event chronology. Choosing the maximum superseded ID:
       $$S^* = \max \left\{ L.\text{superseded\_by} \colon L \in \text{Learnings}(D), L.\text{superseded\_by} \text{ IS NOT NULL} \right\}$$
       provides a **chosen deterministic stable tie-break** across multiple candidate successors.
     - Resolves the canonical document of $S^*$ via `SELECT doc_id FROM learning_documents WHERE learning_id = S* ORDER BY doc_id ASC LIMIT 1`.
     - Returns `('superseded', canonical_doc_id)`.
   - **Precedence 2: Expired**: If no attached learning was superseded, but $\ge 1$ has `status == 'expired'`:
     - Returns `('expired', None)`.
   - **Precedence 3: Rejected**: If all attached learnings have `status == 'rejected'`:
     - Returns `('rejected', None)`.

---

### 7.5 Concrete Acceptance Test Matrix (Phase 1 Gate)

| Test ID | Category | Injection / Precondition | Expected Output / Assertion |
|---|---|---|---|
| `TC-READY-01` | Readiness | Clean DB migrated via `021_typed_memory_graph.sql`. | `status == 'ready'`, `missing_items == []`. |
| `TC-READY-02` | Readiness | Fresh DB missing table `learning_documents`. | `status == 'schema_missing'`, `missing_items == ['table:learning_documents']`. |
| `TC-READY-03` | Readiness | `memory_links.edge_status` created without `NOT NULL DEFAULT 'active'`. | `status == 'schema_drifted'`, flags nullability/default mismatch. |
| `TC-READY-04` | Readiness | `learning_documents` created with 3-column PK `(learning_id, doc_id, created_at)`. | `status == 'schema_drifted'`, flags PK length and shape violation. |
| `TC-READY-05` | Readiness | `idx_memory_links_target_active` created with swapped column sequence. | `status == 'schema_drifted'`, flags column sequence mismatch. |
| `TC-READY-06` | Readiness | `learning_documents` created without `ON DELETE CASCADE` on `doc_id`. | `status == 'schema_drifted'`, flags missing CASCADE foreign key. |
| `TC-READY-07` | Readiness | `idx_documents_memory_uri` created without `UNIQUE` or missing partial `WHERE` clause. | `status == 'schema_drifted'`, flags unique/partial index predicate violation. |
| `TC-LIFE-01` | Lifecycle | Doc $D$ with single active learning $L_1$. | Returns `('accepted', None)`. |
| `TC-LIFE-02` | Lifecycle | Doc $D$ with duplicate active learnings $L_1, L_2$. $L_1$ superseded by $S_1$; $L_2$ active. | Returns `('accepted', None)` (liveness preserved; $L_2$ recallable). |
| `TC-LIFE-03` | Lifecycle | Doc $D$ with $L_1$ superseded by $S_1=100$ and $L_2$ superseded by $S_2=250$. | Returns `('superseded', doc_of(250))` (stable autoincrement tie-break). |
| `TC-LIFE-04` | Lifecycle | Doc $D$ with $L_1$ superseded by $S_1$ and $L_2$ expired. | Returns `('superseded', doc_of(S1))` (supersession takes precedence over expiry). |
| `TC-LIFE-05` | Lifecycle | Doc $D$ with $L_1$ expired and $L_2$ rejected. | Returns `('expired', None)` (expiry takes precedence over rejection). |
| `TC-LIFE-06` | Lifecycle | Doc $D$ with $L_1$ rejected and $L_2$ rejected. | Returns `('rejected', None)`. |
| `TC-LIFE-07` | Lifecycle | Doc $D$ with `page_status='expired'` and 0 rows in `learning_documents`. | Returns `('expired', None)` (preserves existing state; zero resurrection). |
| `TC-LIFE-08` | Lifecycle | Doc $D$ with `page_status='superseded'`, pointer 42, and 0 rows in `learning_documents`. | Returns `('superseded', 42)` (preserves existing pointer; zero resurrection). |

---

## 8. Locked Invariant Decisions vs. Parent Acceptance Limits

To avoid reopening approved decisions or inventing unneeded blockers, choices are categorized into approved invariant decisions (locked by Hans) and precise design-level acceptance limits owned by the parent orchestrator:

### 8.1 Approved Invariant Decisions (Locked by Hans)

1. **Auto-Supersession Threshold (≥ 0.96)**: High-confidence threshold for auto-superseding same-agent, same-store learnings is locked at ≥ 0.96. Lower scores in [0.88, 0.96) persist the `updates` edge but downgrade to a pending `graph_update_review` action in `consolidation_actions`.
2. **Passive Contradiction Governance**: Inferred contradictions (confidence ≥ 0.88) are logged to `contradiction_log` and surfaced passively via recall sidecars and `minni_subscribe_contradictions`. Raw detection does NOT interrupt sessions or emit intrusive contradiction events.
3. **Additive Schema over `memory_links`**: The typed graph is an extension of existing SQLite tables (`documents`, `learning_documents`, `memory_links`, `contradiction_log`). No external graph database or secondary document store is introduced.
4. **Memories as Graph Nodes**: Nodes are physical memories (learnings, and wiki pages in Phase 3), not synthetic entity extractions.
5. **Hardcoded Local-Only Inference**: Edge inference is structurally bound to local providers via `OperationPolicy(local_only=True)`. Cloud routing is prohibited regardless of configuration.
6. **Fail-Loud New Promotion**: If classifier inference fails on new learning promotion, the candidate remains staged in `candidate_packets`; zero durable learning rows are committed.
7. **Standing Repair Fail-Open Principle**: Standing projection repair never un-durables or deletes existing committed `learnings` rows. Embedder failure yields `incomplete_lexical_only` (not a success); classifier outage yields deferred edges (`edges_deferred='degraded'`).
8. **Decoupled Operational Toggles**: Write-time edge classification (`config.graph_classification_enabled`) and read-time 1-hop expansion (`config.graph_expansion_enabled`) operate as completely independent controls.
9. **N:1 Deterministic Aggregate Liveness**: Canonical documents remain `accepted` while any attached learning is active; all-inactive precedence follows `superseded` (with stable autoincrement tie-break $S^* = \max(superseded\_by)$ canonical doc) > `expired` > `rejected`; zero mappings strictly preserve existing row status without resurrection.
10. **Strict Privacy Indistinguishability**: Denied nodes are silently dropped during graph expansion without disclosing withheld-neighbor counts.
11. **Phase 1 Learning-to-Learning Boundary**: Edge inference in Phase 1 operates strictly on canonical learnings (`memory_kind = 'learning'`). Wiki pages are excluded from Phase 1 candidate shortlisting (neither sources nor targets) until stamped in Phase 3.

### 8.2 Precise Parent Acceptance Limits (Pre-Integration Gate)

Prior to implementing code or migrating production databases, the parent orchestrator independently evaluates and accepts the following concrete limits:

1. **Reconciliation Sign-Off**: Verification that `2026-07-09-typed-memory-graph-design.md` and this acceptance addendum are fully consistent, with zero internal contradictions, exact accounting (18 columns, 4 tables, 5 secondary indexes + composite PK), repository-relative links, and complete semantic specifications.
2. **Common Verifier Validation**: Independent verification that `verify_graph_schema` in `src/minni/graph_readiness.py` accurately identifies `ready`, `schema_missing`, and `schema_drifted` against the full test matrix (`TC-READY-01` through `07`) on SQLite test fixtures.
3. **Migration 021 Idempotency Gate**: Clean, idempotent execution of `src/minni/migrations/021_typed_memory_graph.sql` on shared and per-vault test databases without error or partial state.
4. **Local Classifier Benchmark & Latency Gate**: Verification against live local AFM/loopback provider that single batched candidate classification satisfies:
   - Latency p95 $\le 1.2$s, hard timeout 2.0s.
   - Precision $\ge 0.90$ on `updates` and `contradicts`, macro-F1 $\ge 0.80$, false positive rate $\le 0.05$ on `none` pairs over frozen labeled pairs.
   - 100% compliant JSON schema output before enabling `graph_classification_enabled`.
5. **Differential Retrieval Evaluation (Production Read Toggle Gate)**: Review of the Membench differential evaluation run on the target host, verifying:
   - $\ge +5\%$ absolute Recall@5 on the graph-dependent split.
   - Zero recall regression on existing baseline query classes.
   - Overall nDCG@10 and token-budget Recall@5 non-decreasing.
   - Read latency p95 increase $\le 20\%$ and graph SQL traversal p95 $\le 15$ms before toggling `config.graph_expansion_enabled = True` in production configuration.

# Concepts

## The four verbs

| Verb | What happens | Surface |
|---|---|---|
| **Recall** | Cited, provenance-tagged retrieval across the personal and shared legs | `minni_recall`, `minni_drill`, `minni_route`, `minni_export_pack` |
| **Learn** | Propose, don't write: stages a `candidate_packets` row with status `proposed` and returns a `candidate_id`. No durable memory is written on this path | `minni_learn`, `minni_learning_quality` |
| **Approve** | A later resolution decision — accept / reject / redact / log-only / merge / supersede / do-not-store. Only accepting decisions write or keep a durable learning row, disk note, and index entry | `minni_resolve_candidate` |
| **Handoff** | Explicit cross-agent transfer under a lease; the receiver acks before the sender releases | `minni_negotiate_handoff`, `minni_ack_handoff`, `minni_await_handoff`, `minni_list_pending_handoffs` |

There is exactly one escape around the approve gate: `force=true` on `learn`
writes a durable learning directly, **only** for an operator principal, and is
audit-stamped `FORCE_DURABLE_LEARN`. A non-operator force attempt is denied
with an `operator_only` error.

Alongside the four verbs, sessions carry a lifecycle spine —
`prepare_task → prepare_outcome → plan → learn` — injected via the
`<minni:context>` envelope so agents orient before ambitious work and distill
before context is flushed. Durable, evidence-gated plans
(`minni_plan_*`) survive sessions and compaction.

## Recall is evidence, not instruction

Every recall result is wrapped in an evidence envelope carrying provenance:
source path, owning agent, score, review state, privacy level, and a
personal-vs-shared leg marker (`src: "p"` / `src: "c"` in the RPC payload).
The framing is enforced at the data layer: instruction-like content in stored
documents is detected and reversibly perturbed before it can reach a prompt
with authority, and recalled text is presented as material to weigh, not text
to obey. Combined with the propose→approve gate, this is Minni's
memory-poisoning defense: nothing writes itself into durable memory, and
nothing recalled speaks with the operator's voice.

## Two-tier storage

- **Personal tier** — each agent's vault wiki (`<agent>-vault/wiki/**/*.md`) is
  indexed by the `vault_ingest` pass into that vault's own
  `.index/vault.db` + `vault.faiss` + `vault.manifest.json`.
- **Shared tier** — `~/.minni/minni.db` (SQLite, FTS5, WAL) holds durable
  learnings, candidates, episodic/contradiction events, handoff leases, and
  the pooled document layer, with a shared FAISS index for vectors.

Recall merges the tiers by `scope`: `personal` (caller's index, falling back
to shared), `combined` (all per-vault indexes plus shared), or `both` (the
default — personal and combined merged, deduplicating the caller's own hits).
Learnings always come from the shared DB. Full provenance — owning agent,
source vault, score components, `indexed_at` — is available via `minni_drill`.

Vaults are the human-readable surface: wiki (synthesis pages, handoff notes,
learning notes), inbox (candidate drafts, hook packets), outbox (outgoing
handoffs), logs (append-oriented audit trail). Agents use the plugin/daemon
contracts instead of scraping another agent's private vault directly.

## Retrieval

The retrieval stack is a hybrid pipeline: lexical search (SQLite FTS5/BM25) +
vector search (FAISS) fused with reciprocal-rank fusion, cross-encoder
reranking, optional NLI claim-attribution scoring, MMR-diverse token-budgeted
packing, and progressive depth tiers (`headline` / `snippet` / `chunk` /
`document`) — merged across the personal and shared legs by recall scope.

## The AFM pass pipeline

Background curation runs as discrete passes under `engine/afm_passes/`:

| Pass | Role |
|---|---|
| `vault_ingest` | Builds each agent's personal `.index` from its wiki |
| `inbox_ingest` | Ingests hook-written inbox files into `candidate_packets` |
| `consolidation` | Promote / dedupe / mark-for-review triage of staged candidates |
| `synthesis` | Sourced synthesis pages in the vault wiki |
| `session_distillation` | Distills session transcripts into candidate material |
| `procedure_extraction` | Extracts reusable procedures |
| `inbox_archive`, `pruning`, `reorganization` | Hygiene: archive processed inbox files, age out stale material, reorganize |

Note the division of labor in the learning path: **`inbox_ingest`** moves
inbox files into `candidate_packets`; **`consolidation`** then proposes
promote/dedupe/review decisions that the daemon applies according to the
configured gates. Raw transcripts, status packets, hook envelopes, test junk,
and unverified claims route to review or rejection, not active memory.

## Core invariants

| Invariant | Meaning |
|---|---|
| Identity loads whole | Agent identity and standing rules are not chunked |
| Knowledge loads chunked | Large docs/history are retrieved by need and cited |
| Recall is evidence | Retrieved content is never automatically instruction |
| Learning is proposal-first | `learn` stages a candidate; durable memory requires approval or the audited operator escape |
| Documents are two-tier | Personal `.index` per vault + shared pooled layer, merged by scope |
| Learnings are shared-DB | Durable learnings, candidates, leases, events live in `~/.minni/minni.db` |
| Vaults are per-agent | Shared daemon, separate human-readable workspaces, separate personal indexes |
| Local transport first | Unix socket by default; provider calls are explicit and gated |

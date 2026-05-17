# PR-2: Phase 1 — Persistent FAISS, Result Envelope, Vault Page Schema

> **Scope:** FAISS disk persistence, rich result envelope, vault page schema with frontmatter/status lifecycle.
>
> **Depends on:** PR-1 (migrations runner, model singletons).
>
> **Behavior change:** Cold-start <500ms. Search results gain additive fields. Vault pages gain structured frontmatter.

---

## 1.1 — Persistent FAISS with Manifest

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/faiss_persist.py` | `save(index, manifest_path)` writes `index.faiss` + `index.manifest.json`. `load(manifest_path, expected_db_checksum)` returns index if match, else `None`. |
| **MODIFY** | `engine/faiss_index.py` | `_ensure_loaded()`: attempt `load()` first; on miss, rebuild then `save()`. |

### Manifest: `{embedding_model, vector_dim, chunk_id_order, chunk_count, db_checksum, saved_at}`

### DB Checksum: `SELECT count(*), max(rowid), max(updated_at) FROM chunk_embeddings` — hash it.

### CLI: `python -m engine.sovereign_memory faiss --rebuild`

### Default location: `${SOVEREIGN_DB_PATH%/*}/faiss/`

### Verification

- Cold-start with cache: timed <500ms
- Delete cache → rebuilds and saves automatically
- Manual `faiss --rebuild` works

---

## 1.2 — Result Envelope

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/scoring.py` | `compute_confidence(rrf_score, cross_encoder_score, decay_factor) -> float [0,1]`. Percentile calibration over rolling window (last 1000 results in `score_distribution` table). |
| **CREATE** | `engine/rationale.py` | `explain(result_record) -> str`. Deterministic human-readable line from provenance dict. |
| **CREATE** | `engine/safety.py` | Regex detector for `instruction_like` flag. Imperative voice, "ignore previous" patterns, role-play directives. |
| **CREATE** | `engine/migrations/002_score_distribution.sql` | Rolling-window table for calibration. |
| **MODIFY** | `engine/retrieval.py` | Existing `{text, source, heading, score}` becomes the full envelope (additive). |

### Envelope adds: `confidence`, `provenance` (fts_rank, semantic_rank, rrf_score, cross_encoder_score, decay_factor, agent_origin, age_days, doc_id, chunk_id, backend), `rationale`, `privacy_level`, `source_authority`, `review_state`, `instruction_like`, `wikilink`, `evidence_refs`, `recommended_action`, `recommended_wiki_updates`.

### Constraints: All additive. `null` under degraded mode, never missing. `instruction_like` computed on every chunk.

---

## 1.3 — Vault Page Schema and Frontmatter

### Page types: `entity`, `concept`, `decision`, `procedure`, `session`, `artifact`, `handoff`, `synthesis`

### Status lifecycle: `draft → candidate → accepted → superseded → rejected → expired`

| Status | In Default Recall? |
|--------|-------------------|
| `draft` | No (opt-in) |
| `candidate` | Yes (flagged) |
| `accepted` | Yes |
| `superseded` | No (opt-in) |
| `rejected` | No (opt-in) |
| `expired` | No (opt-in) |

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/wiki_indexer.py` | Read frontmatter. Reject invalid pages (log to `log.md`, exclude from index). |
| **MODIFY** | `engine/indexer.py` | Honor `status` and `privacy`. Skip `blocked` pages. |
| **MODIFY** | `engine/retrieval.py` | Default skip `superseded`/`rejected`. Add `include_superseded`, `include_rejected`, `include_drafts` flags. |
| **MODIFY** | `plugins/sovereign-memory/src/vault.ts` | `writeVaultPage()` accepts `{type, status, privacy, sources, expires, supersededBy}`. Defaults: type from section, status `candidate`, privacy `safe`. |
| **MODIFY** | `engine/seed_identity.py` | Ensure new dirs: `wiki/procedures/`, `wiki/artifacts/`, `wiki/handoffs/`. |

### Back-fill: Pages >30 days with sources → `accepted`. Without sources → `draft`. Emit hygiene report.

---

## PR-2 Completion Checklist

- [ ] FAISS persistence: save/load with manifest, <500ms cold-start
- [ ] `faiss --rebuild` CLI works
- [ ] `scoring.py`, `rationale.py`, `safety.py` created
- [ ] `002_score_distribution.sql` migration works
- [ ] Envelope fields populated (additive, backward-compatible)
- [ ] Degraded mode: `null` not missing
- [ ] Frontmatter read by indexers; invalid pages rejected
- [ ] Default recall skips `superseded`/`rejected`
- [ ] `vault.ts` emits structured frontmatter
- [ ] New vault dirs created on init
- [ ] All existing tests pass

---

## Next Steps

→ [04_PR3_Phase2_Storage_Abstraction.md](./04_PR3_Phase2_Storage_Abstraction.md)

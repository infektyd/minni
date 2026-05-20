# PR-3: Phase 2 — Storage Abstraction (Scale-Agnostic Spine)

> **Scope:** VectorBackend protocol, adapters (faiss-disk default, faiss-mem, stubs for qdrant/lance), multi-backend fan-out, backend registry, SQLite↔vector sync, cross-backend RRF.
>
> **Depends on:** PR-2 (persistent FAISS, envelope with `backend` provenance field).
>
> **Behavior change:** None with defaults. Existing FAISS behavior wrapped in protocol. Multi-backend dormant unless configured.

---

## 2.1 — VectorBackend Protocol

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/vector_backend.py` | Python `Protocol` class defining the interface. |

### Protocol

```python
class VectorBackend(Protocol):
    name: str  # "faiss-mem", "faiss-disk", "qdrant", "lance", ...
    dim: int
    def upsert(self, items: list[VectorItem]) -> None: ...
    def remove(self, chunk_ids: list[int]) -> None: ...
    def search(self, query_vec: np.ndarray, k: int, filter: dict | None) -> list[VectorHit]: ...
    def stats(self) -> dict: ...
```

### Data Types

- `VectorItem = {chunk_id, doc_id, vector, metadata: {agent, layer, source, created_at, privacy_level, status}}`
- `VectorHit = {chunk_id, doc_id, score, backend: str}`

---

## 2.2 — Adapters

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/backends/__init__.py` | Package init. |
| **CREATE** | `engine/backends/faiss_disk.py` | Wraps existing `faiss_index.py` + Phase 1.1 persistence. **New default.** |
| **CREATE** | `engine/backends/faiss_mem.py` | Pure in-memory (current behavior, no persistence). Available as `--vector-backend=faiss-mem`. |
| **CREATE** | `engine/backends/qdrant.py` | Stub: protocol + `raise ImportError("install sovereign-memory[qdrant]")`. Not active. |
| **CREATE** | `engine/backends/lance.py` | Same shape, stubbed. |
| **CREATE** | `engine/backends/multi.py` | Fan-out adapter: wraps N backends, runs `search()` in parallel, returns interleaved hits with `backend` provenance. |

### Constraints

- FAISS backends use post-filter via SQLite for metadata (FAISS doesn't support filtered search).
- Qdrant/Lance stubs document the path but are non-functional without optional deps.
- `multi.py` uses `concurrent.futures.ThreadPoolExecutor` for parallel search.

---

## 2.3 — Backend Registry + Selection

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/migrations/003_backend_state.sql` | `vector_backends(name TEXT PRIMARY KEY, last_synced_chunk_rowid INTEGER, last_synced_at INTEGER, vector_count INTEGER, status TEXT)` |
| **MODIFY** | `engine/config.py` | Add `vector_backends: list[str] = ["faiss-disk"]`. |
| **MODIFY** | `engine/sovrd.py` | Resolve `backend="auto"` using priority cascade. Expose `backend` kwarg on `search()`. |

### Resolution logic for `backend="auto"`:
1. Try freshest backend whose `vector_count` matches `documents` count
2. Fall back to next
3. Full FTS-only as final fallback

### Agent override: `daemon.search(query, backend="qdrant")` or `backend=["faiss-disk", "qdrant"]` (fan-out).

---

## 2.4 — SQLite ↔ Vector Backend Sync

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/vector_sync.py` | Iterates `chunk_embeddings` where `rowid > last_synced_chunk_rowid`, batches into `upsert()`, updates `vector_backends` table. Removes via indexer's delete path. |

### Triggers

- Indexer pass completion
- Daemon idle hook (every 30s if dirty)
- CLI: `python -m engine.sovereign_memory vectors --sync`

### Constraint: Multi-backend sync runs per-backend independently. One slow backend doesn't block others.

---

## 2.5 — Cross-Backend RRF in Retrieval

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/retrieval.py` | Existing FTS+semantic RRF stays. Add Nth input stream when multi-backend active. Each backend contributes ranked list; RRF merges with `1/(k+rank)`. Verify `db.chunk_exists(chunk_id)` before final assembly. `backend` field in `provenance`. |

---

## PR-3 Completion Checklist

- [ ] `engine/vector_backend.py` defines Protocol with VectorItem/VectorHit
- [ ] `engine/backends/` directory with faiss_disk, faiss_mem, qdrant (stub), lance (stub), multi
- [ ] `003_backend_state.sql` migration creates tracking table
- [ ] `config.vector_backends` defaults to `["faiss-disk"]`
- [ ] `backend="auto"` resolution works in sovrd.py
- [ ] `vector_sync.py` syncs SQLite→backend on indexer pass, idle hook, and CLI
- [ ] Cross-backend RRF merges correctly
- [ ] **With multi-backend disabled (default): results bit-identical to pre-PR**
- [ ] **With only faiss-disk: also bit-identical**
- [ ] **With mock stub backend added: RRF merges correctly**
- [ ] All existing tests pass

---

## Next Steps

→ [05_PR4_Phase3_0_Eval_Harness_Policy.md](./05_PR4_Phase3_0_Eval_Harness_Policy.md)

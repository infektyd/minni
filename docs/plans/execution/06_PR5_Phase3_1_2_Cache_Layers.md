# PR-5: Phase 3.1 + 3.2 — Cross-Encoder Cache + Layer-Aware Retrieval

> **Scope:** LRU cache for cross-encoder scores, layer column on documents/chunks, layer-filtered search, chronological retrieval mode.
>
> **Depends on:** PR-2 (envelope, migrations runner).
>
> **Behavior change:** Repeat queries faster (cache). Layer filter opt-in. Defaults unchanged.

---

## 3.1 — Cross-Encoder Cache

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/rerank_cache.py` | `LRUCache((query_hash, chunk_id) → score)` with capacity 1024. Also keyed on cross-encoder model name + version — model swap invalidates cache. |
| **MODIFY** | `engine/retrieval.py` | Before calling cross-encoder, check cache. After scoring, store in cache. |
| **MODIFY** | `engine/indexer.py` | Chunk-delete path emits invalidation event the cache subscribes to. |

### Constraints

- Cache is in-memory, per-process. No persistence needed.
- Key includes model name + version so model swap invalidates.
- Capacity 1024 is sufficient — most queries hit recent chunks.

### Verification

```bash
# Same query twice: second call's cross-encoder time ~0ms
python -c "
import time
from engine.retrieval import hybrid_search
hybrid_search('auth migration')  # warm up
t0 = time.time()
hybrid_search('auth migration')
print(f'Second call: {(time.time()-t0)*1000:.0f}ms')  # expect near-zero CE time
"
```

---

## 3.2 — Layer-Aware Retrieval

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/migrations/004_layer_column.sql` | `ALTER TABLE documents ADD COLUMN layer TEXT DEFAULT NULL` and `ALTER TABLE chunk_embeddings ADD COLUMN layer TEXT DEFAULT NULL`. |
| **MODIFY** | `engine/indexer.py` | Back-fill `layer` based on rules: `whole_document=1 AND agent LIKE 'identity:%'` → `identity`; episodic → keep existing; wiki `type: artifact` → `artifact`; everything else → `knowledge`. |
| **MODIFY** | `engine/retrieval.py` | `search()` accepts `layers: list[str] | None` (None = all, current behavior). Also accepts `sort: "semantic" | "chronological"` (default semantic) and optional `start_date`/`end_date`. |
| **MODIFY** | `engine/sovrd.py` | Expose `layers`, `sort`, `start_date`, `end_date` kwargs on `search()`. |

### Layers

| Layer | Populated by |
|-------|-------------|
| `identity` | `whole_document=1 AND agent LIKE 'identity:%'` |
| `episodic` | Episodic events |
| `knowledge` | Everything else (default) |
| `artifact` | Wiki frontmatter `type: artifact` |

### Chronological Retrieval

When `sort="chronological"`: bypass RRF and semantic ranking entirely. Return strict linear narrative ordered by `created_at`. Useful for timeline reconstruction.

### Constraints

- `layers=None` = all layers = current behavior. Zero change for existing callers.
- `sort` defaults to `"semantic"` = current behavior.
- `start_date`/`end_date` are optional ISO date strings.

### Verification

```bash
# Default (no layer filter) matches current behavior
python -c "
from engine.retrieval import hybrid_search
r1 = hybrid_search('test')
r2 = hybrid_search('test', layers=None)
assert [x['source'] for x in r1] == [x['source'] for x in r2]
print('PASS')
"

# Layer filter returns only matching layers
python -c "
from engine.retrieval import hybrid_search
results = hybrid_search('test', layers=['identity'])
# All results should be identity-layer docs
print(f'{len(results)} identity results')
"
```

---

## PR-5 Completion Checklist

- [ ] `engine/rerank_cache.py` with LRU(1024), model-version-keyed
- [ ] Repeat query shows ~0ms cross-encoder time on second call
- [ ] Chunk deletion invalidates cache entries
- [ ] `004_layer_column.sql` migration adds nullable `layer` column
- [ ] Indexer back-fills `layer` based on existing rules
- [ ] `search()` accepts `layers` filter (default `None` = all)
- [ ] `search()` accepts `sort` (default `"semantic"`)
- [ ] Chronological mode returns time-ordered results
- [ ] `start_date`/`end_date` filtering works
- [ ] All existing tests pass

---

## Next Steps

→ [07_PR6_Phase3_3_Contradictions.md](./07_PR6_Phase3_3_Contradictions.md)

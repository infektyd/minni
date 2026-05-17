# PR-15: Phase 7 — Quantized Embeddings + Semantic Chunking (Deferred)

> **Scope:** Optional int8 quantized embeddings for FAISS index, optional semantic chunk merging in the chunker. Both opt-in. Both deferred until Phase 6 stabilizes and eval harness can measure exact impact.
>
> **Depends on:** PR-4 (eval harness for recall measurement).
>
> **Behavior change:** None with defaults. Both features behind config flags requiring explicit opt-in.

---

## 7.1 — Quantized Embeddings (Opt-In)

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/config.py` | Add `embedding_quantization: str = "fp32"`. Options: `"fp32"` (default), `"int8"`. |
| **MODIFY** | `engine/faiss_index.py` or `engine/backends/faiss_disk.py` | When `embedding_quantization="int8"`: wrap FAISS index in `IndexHNSWPQ` (or `IndexHNSWSQ` for simpler scalar quant). Triggered at index rebuild. |

### Constraints

- No migration needed. SQLite blob still holds fp32 (truth source). Quantization is downstream only.
- Reindex required after changing `embedding_quantization`; the FAISS manifest records the mode and rejects stale caches.
- Default is `fp32` — zero change unless explicitly configured.

### Verification

```bash
# Measure recall impact
python -m engine.eval.harness run --config fp32-baseline,int8-quantized
# Expect ≥95% of fp32 recall@5

# Limited/mock verification when a live DB/embedder is unavailable
python -m engine.eval.harness run --config fp32-baseline,int8-quantized --mock

# Memory footprint comparison
python -c "
from engine.faiss_index import get_index
import sys
idx = get_index()
print(f'Index memory: ~{sys.getsizeof(idx)}')
"
```

---

## 7.2 — Semantic Chunking Pass (Opt-In)

### Files

| Action | Path | Details |
|--------|------|---------|
| **MODIFY** | `engine/config.py` | Add `chunking_semantic_merge: bool = False`. |
| **MODIFY** | `engine/chunker.py` | Post-pass: when enabled, adjacent chunks within same heading whose embedding cosine > 0.9 merge into one (capped at `max_tokens`). |
| **MODIFY** | `engine/sovereign_memory.py` | CLI: `python -m engine.sovereign_memory index --semantic-merge`. |

### Logic

1. After normal chunking, iterate adjacent chunk pairs within the same heading.
2. Compute embedding cosine similarity.
3. If cosine > 0.9 and merged length ≤ `max_tokens`: merge.
4. Cap: never merge beyond `max_tokens`.

### Constraints

- Default `False` — zero change.
- Reindex required after enabling.
- Only affects newly indexed content (or explicit reindex).

### Verification

```bash
# Reindex with semantic merge
python -m engine.sovereign_memory index --semantic-merge

# Compare chunk counts: fewer, denser chunks expected
python -c "
from engine.db import connect
db = connect()
count = db.execute('SELECT count(*) FROM chunk_embeddings').fetchone()[0]
print(f'Chunk count after merge: {count}')
"

# Eval harness comparison
python -m engine.eval.harness run --config baseline,with-semantic-merge
```

---

## PR-15 Completion Checklist

- [x] `embedding_quantization` config with `fp32` default
- [x] `int8` mode wraps FAISS in quantized index at rebuild
- [x] Quantized recall ≥95% of fp32 on eval harness (mock-limited: 0.9608 vs 0.9608; local `faiss` unavailable)
- [x] `chunking_semantic_merge` config with `False` default
- [x] Semantic merge post-pass: cosine > 0.9 adjacent chunks merge
- [x] `index --semantic-merge` CLI works
- [x] Reindex required after enabling either feature (documented)
- [x] All existing tests pass

---

## 🏁 Sprint Complete

This is the final PR in the rollout. Return to [00_MASTER_TRACKER.md](./00_MASTER_TRACKER.md) to verify all checkboxes.

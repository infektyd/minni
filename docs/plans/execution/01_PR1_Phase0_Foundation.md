# PR-1: Phase 0 — Cross-Cutting Foundation

> **Scope:** Schema versioning + migrations runner, module-level model singletons, token-accurate budgeting.
>
> **Depends on:** Nothing. This is the first PR.
>
> **Behavior change:** None. Faster cold-start, accurate token counts. Zero new user-visible features.

---

## 0.1 — Schema Versioning + Migrations Runner

### What to build

A migrations system that reads `PRAGMA user_version` from SQLite and runs pending numbered SQL scripts in a single transaction.

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/migrations.py` | Runner: reads `PRAGMA user_version`, iterates `engine/migrations/*.sql` in order, runs pending ones in a single transaction, bumps `user_version` on success. |
| **CREATE** | `engine/migrations/001_baseline.sql` | No-op script that marks existing schema as version 1. Existing DBs come up at v1 with zero changes; fresh DBs run same baseline. |
| **MODIFY** | `engine/db.py` | Call migrations runner exactly once per process in `db.connect()`. Guard with a module-level flag to prevent re-entry. |

### Constraints

- `migrate_v3_to_v3_1.py` becomes deprecated documentation — do **not** delete it.
- All migrations run in a **single transaction** and bump `user_version` only on success.
- Existing DBs must come up at version 1 with zero row/column changes.

### Verification

```bash
# Fresh DB: runner creates baseline
rm -f /tmp/test_sovereign.db
SOVEREIGN_DB_PATH=/tmp/test_sovereign.db python -c "from engine.db import connect; connect()"
sqlite3 /tmp/test_sovereign.db "PRAGMA user_version;"  # expect: 1

# Existing DB: no changes
cp sovereign_memory.db /tmp/existing.db
SOVEREIGN_DB_PATH=/tmp/existing.db python -c "from engine.db import connect; connect()"
sqlite3 /tmp/existing.db "PRAGMA user_version;"  # expect: 1
# Verify row counts unchanged
```

---

## 0.2 — Module-Level Model Singletons

### What to build

A single shared instance for the embedding model and cross-encoder, replacing scattered `SentenceTransformer(...)` instantiations.

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/models.py` | Exports `get_embedder()` and `get_cross_encoder()`, both `@functools.cache`. Wraps `SentenceTransformer(config.embedding_model)` and `CrossEncoder(config.cross_encoder_model)`. |
| **MODIFY** | `engine/retrieval.py` | Replace all `SentenceTransformer(...)` calls with `get_embedder()`. |
| **MODIFY** | `engine/indexer.py` | Same replacement. |
| **MODIFY** | `engine/wiki_indexer.py` | Same replacement. |
| **MODIFY** | `engine/writeback.py` | Same replacement. |
| **MODIFY** | `engine/episodic.py` | Same replacement. |
| **MODIFY** | `engine/seed_identity.py` | Same replacement. |

### Constraints

- Same model, same calls, just shared. Must produce **numerically identical embeddings**.
- Do not change model names, parameters, or behavior.

### Verification

```python
# Singleton identity check
python -c "from engine.models import get_embedder; a=get_embedder(); b=get_embedder(); assert a is b; print('PASS')"

# Cold-start timing (before/after comparison)
time python -c "from engine.retrieval import hybrid_search; hybrid_search('test query')"
# Expect ~3-5x faster first query vs pre-PR baseline
```

---

## 0.3 — Token-Accurate Budgeting

### What to build

Replace word-count approximation in the chunker with tiktoken-based token counting.

### Files

| Action | Path | Details |
|--------|------|---------|
| **CREATE** | `engine/tokens.py` | `get_encoder()` returns singleton `tiktoken.get_encoding("cl100k_base")`. Helper `count_tokens(text) -> int`. |
| **MODIFY** | `engine/chunker.py` | Replace `len(text.split()) * 0.75` approximation with `count_tokens()`. |

### Constraints

- Existing chunks already in DB are fine — they store text, not counts. No reindex required.
- `tiktoken` is already in the ecosystem (used by LLM clients). If not in `requirements.txt`, add it.

### Verification

```bash
# Reindex a known doc; chunk count may differ slightly (more accurate)
python -m engine.sovereign_memory index --path <known_doc>

# Existing tests still pass
cd engine && pytest -q
```

---

## PR-1 Completion Checklist

- [ ] `engine/migrations.py` exists and runner works on fresh + existing DBs
- [ ] `engine/migrations/001_baseline.sql` is a no-op that sets version 1
- [ ] `engine/db.py` calls runner once per process
- [ ] `engine/models.py` exports cached singletons
- [ ] All 6 files using `SentenceTransformer(...)` now use `get_embedder()`
- [ ] `engine/tokens.py` exists with `count_tokens()`
- [ ] `engine/chunker.py` uses tiktoken instead of word-count approximation
- [ ] `cd engine && pytest -q` — green
- [ ] `cd plugins/sovereign-memory && npm test` — 29 tests pass
- [ ] JSON-RPC contract: all existing methods return unchanged shapes
- [ ] Live recall: known query returns same top result ±1 position

---

## Next Steps

→ [02_PR1b_Phase0_Contracts.md](./02_PR1b_Phase0_Contracts.md) — Agent contract, vault contract, progressive disclosure depth tiers.

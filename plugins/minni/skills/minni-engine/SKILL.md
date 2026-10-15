---
name: minni-engine
description: Development reference for the Minni engine (v3.1/v3.2). Use when modifying the memory system's extraction, reranking, consolidation, or retrieval pipelines.
tags: [memory, llm, faiss, sqlite]
---

# Minni Engine — Development Reference

## File Structure

```
~/Projects/Minni/
├── Makefile               # Root setup/check/test/daemon entrypoint
├── .venv/                 # Python 3.14 venv with all deps (pip install -e .)
├── pyproject.toml         # Package metadata and console scripts (minni, minnid)
├── requirements.txt       # Engine dependencies
├── tests/                 # pytest suite
├── src/minni/             # Minni Python package
│   ├── config.py          # Centralized config (dataclass, MINNI_* overrides)
│   ├── db.py              # SQLite + FTS5 schema and migrations
│   ├── minnid.py          # JSON-RPC daemon over Unix socket
│   ├── retrieval.py       # Hybrid RetrievalEngine orchestrator
│   ├── trie_index.py      # Tier 1 in-memory prefix/token Trie (TrieIndexMixin, <0.5ms)
│   ├── fts_search.py      # Tier 2 SQLite FTS5 BM25 lexical search (FtsSearchMixin, <10ms fast mode)
│   ├── semantic_rerank.py # Tier 3 FAISS + RRF (0.60/0.40) + Cross-Encoder rerank (SemanticRerankMixin)
│   ├── writeback.py       # Write-back memory and learning lifecycle
│   ├── faiss_index.py     # FAISS index manager (faiss-disk / faiss-mem)
│   ├── episodic.py        # Episodic event logging
│   └── indexer.py         # Vault indexing pipeline (including synchronous index_single_file)
└── plugins/minni/         # Multi-host TypeScript plugin
```

## Key Interfaces (don't break these)

### Extraction (`src/minni/extraction.py`)
```python
from minni.extraction import get_extractor
extractor = get_extractor()
facts = extractor.extract_facts(messages)  # List[Dict] with type/content/confidence
available = extractor.is_available()       # bool
```

### Retrieval (`src/minni/retrieval.py` + `trie_index.py`, `fts_search.py`, `semantic_rerank.py`)
```python
from minni.retrieval import RetrievalEngine
engine = RetrievalEngine(db, config)
results = engine.retrieve(query, limit=5, agent_id=None, mode="fast")  # List[Dict]
```

### Writeback (`src/minni/writeback.py`)
```python
from minni.writeback import WriteBackMemory, CATEGORIES
# CATEGORIES: pattern, fix, decision, preference, fact, procedure, architecture, codebase, general
wb = WriteBackMemory(db, config)
learning_id = wb.store_learning(agent_id, content, category="architecture", confidence=0.9)
wb.detect_contradiction(agent_id, learning_id, content)
```

## Model Backend Patterns

### Reranking & Reciprocal Rank Fusion (`src/minni/semantic_rerank.py` & `src/minni/config.py`)
Retrieval fuses lexical and semantic candidates via weighted RRF (`0.60` FTS / `0.40` semantic), reserves the top-10 FTS BM25 slots before Cross-Encoder reranking, and applies a `>= 0.35` post-rerank confidence floor:

1. **Config** (`src/minni/config.py`): `reranker_model`, `rrf_fts_weight` (`0.60`), `rrf_semantic_weight` (`0.40`)
2. **Mixin** (`src/minni/semantic_rerank.py`): `SemanticRerankMixin` handles vector search, weighted RRF, Cross-Encoder reranking, and NLI attribution
3. **Fast Path** (`mode="fast"`): `TrieIndexMixin` (`src/minni/trie_index.py`) + `FtsSearchMixin` (`src/minni/fts_search.py`) return lexical hits in `< 10 ms` without loading neural models
4. **Fallback**: on any reranker error or deadline expiry, return deterministic RRF/FTS candidates

## Config (config.py)

All settings are fields on the `MinniConfig` dataclass. Env var overrides:
- `MINNI_HOME`, `MINNI_VAULT_PATH`, `MINNI_DB_PATH`, `MINNI_FAISS_PATH`
- `MINNI_AFM_PROVIDER_MODE`, `MINNI_AFM_MODE`, `MINNI_AFM_ALLOWED_TARGETS`
- `MINNI_PROVIDERS_CONFIG` and provider-specific secret references

The global `DEFAULT_CONFIG = MinniConfig()` is imported everywhere.

## Testing Changes

```bash
cd ~/Projects/Minni
make setup

# Syntax check
.venv/bin/python -c "import py_compile; py_compile.compile('src/minni/minnid.py', doraise=True)"

# Import check (verify no broken deps)
.venv/bin/python -c "from minni.retrieval import RetrievalEngine; from minni.config import DEFAULT_CONFIG; print('OK')"

# Start the daemon
make daemon

# Check daemon readiness
.venv/bin/python -m minni.minnid_client --socket ~/.minni/run/minnid.sock status
```

## Related Skills

- `minni-ingestion` — Ingest LLM Wiki pages with frontmatter enrichment, wikilink graph extraction, and agent-optimized chunking

## Pitfalls

- **Keep retrieval mixins cohesive** — `RetrievalEngine` (`src/minni/retrieval.py`) inherits from `TrieIndexMixin` (`src/minni/trie_index.py`), `FtsSearchMixin` (`src/minni/fts_search.py`), and `SemanticRerankMixin` (`src/minni/semantic_rerank.py`)
- **Preserve synchronous single-file indexing** — `minni_learn` and `resolve_candidate(accept)` call `index_single_file` / `vault_index_doc` so newly written notes are recallable immediately
- **Don't change response formats** — downstream agents depend on exact JSON shapes
- **FTS5 queries** must be sanitized (no special chars) — use `_sanitize_fts_query()` in `src/minni/fts_search.py` (re-exported on `RetrievalEngine`)
- **Config changes** require updating both the dataclass field AND any code that reads it

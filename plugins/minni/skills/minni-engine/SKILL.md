---
name: minni-engine
description: Development reference for the Minni engine (v3.1/v3.2). Use when modifying the memory system's extraction, reranking, consolidation, or retrieval pipelines.
tags: [openclaw, memory, llm, faiss, sqlite]
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
│   ├── db.py              # SQLite + FTS5 schema
│   ├── minnid.py          # JSON-RPC daemon over Unix socket
│   ├── retrieval.py       # Hybrid FTS5 + FAISS + RRF pipeline
│   ├── writeback.py       # Write-back memory and learning lifecycle
│   ├── faiss_index.py     # FAISS index manager
│   ├── episodic.py        # Episodic event logging
│   └── indexer.py         # Vault indexing pipeline
└── plugins/minni/         # Multi-host TypeScript plugin
```

## Key Interfaces (don't break these)

### Extraction (extraction.py)
```python
from extraction import get_extractor
extractor = get_extractor()
facts = extractor.extract_facts(messages)  # List[Dict] with type/content/confidence
available = extractor.is_available()       # bool
```

### Retrieval (retrieval.py)
```python
from retrieval import RetrievalEngine
engine = RetrievalEngine(db, config)
results = engine.retrieve(query, limit=5, agent_id=None)  # List[Dict]
```

### Writeback (writeback.py)
```python
from writeback import WriteBackMemory
wb = WriteBackMemory(db, config)
learning_id = wb.store_learning(agent_id, content, category="fix", confidence=0.9)
wb.detect_contradiction(agent_id, learning_id, content)
```

## Model Backend Patterns

### Swapping Reranker (V3.2 pattern)
The reranker is an HTTP API call, not a local model:

1. **Config** (`config.py`): `reranker_model`, `openrouter_api_key`, `openrouter_base_url`
2. **Property** (`retrieval.py`): `reranker` returns `True`/`None` (availability flag)
3. **Method** (`_rerank`): HTTP POST to `/v1/rank`, parse `{results: [{index, relevance_score}]}`
4. **Fallback**: on any error, log warning and return candidates unsorted (RRF scores preserved)

To swap to a different reranker API:
- Change `config.reranker_model` and the API URL/headers in `_rerank()`
- Keep the same signature: `_rerank(query, candidates) → candidates`
- Always add `rerank_score` key to each candidate dict

### Swapping Extraction LLM (V3.2 pattern)
Extraction uses the OpenClaw gateway (OpenAI-compatible /v1/chat/completions):

1. **Config** (`config.py`): `extraction_model`, `openclaw_gateway_url`
2. **Method** (`_call_gateway`): HTTP POST to `/v1/chat/completions`
3. **Availability** (`is_available`): HTTP GET to `/v1/models`

To swap to a different LLM backend:
- Change `config.extraction_model` and the URL in `_call_gateway()`
- Keep the same prompt, JSON parsing (3 strategies), and confidence filtering
- Keep `get_extractor()` singleton pattern

### Adding a New Endpoint (app.py)
1. Add Pydantic request/response models
2. Add `@app.post("/your_endpoint")` handler
3. For background work: use `asyncio.create_task()` — never block the response
4. Use `_make_agent(agent_id)` for writeback operations

## Config (config.py)

All settings are fields on the `SovereignConfig` dataclass. Env var overrides:
- `MINNI_HOME`, `MINNI_VAULT_PATH`, `MINNI_DB_PATH`, `MINNI_FAISS_PATH`
- `MINNI_AFM_PROVIDER_MODE`, `MINNI_AFM_MODE`, `MINNI_AFM_ALLOWED_TARGETS`
- `MINNI_PROVIDERS_CONFIG` and provider-specific secret references

The global `DEFAULT_CONFIG = SovereignConfig()` is imported everywhere.

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

- `minni-wiki-ingestion` — Ingest LLM Wiki pages with frontmatter enrichment, wikilink graph extraction, and agent-optimized chunking

## Pitfalls

- **Don't remove `SentenceTransformer`** from retrieval.py — it's the embedding model for FAISS, NOT the reranker
- **Don't inline extraction** in request paths — always use `asyncio.create_task()` (see `_run_extraction` in app.py)
- **Don't change response formats** — downstream agents depend on exact JSON shapes
- **FTS5 queries** must be sanitized (no special chars) — use `_sanitize_fts_query()` in retrieval.py
- **Config changes** require updating both the dataclass field AND any code that reads it

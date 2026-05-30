---
name: minni-engine
description: Development reference for the Minni engine (v3.1/v3.2). Use when modifying the memory system's extraction, reranking, consolidation, or retrieval pipelines.
tags: [openclaw, memory, llm, faiss, sqlite]
---

# Minni Engine — Development Reference

## File Structure

```
~/.openclaw/minni-v3.1/
├── config.py              # Centralized config (dataclass, env var overrides)
├── db.py                  # SQLite + FTS5 schema (learnings, chunk_embeddings, episodic_events)
├── app.py                 # FastAPI service (all /recall, /learn, /process_conversation endpoints)
├── extraction.py          # LLM fact extraction via OpenClaw gateway
├── retrieval.py           # Hybrid FTS5 + FAISS + RRF + Cohere rerank pipeline
├── writeback.py           # Write-back memory, contradiction detection, learning lifecycle
├── consolidation.py       # Idle-time memory compression (background)
├── agent_api.py           # SovereignAgent — agent-facing API
├── chunker.py             # Markdown-aware document chunking
├── faiss_index.py         # FAISS index manager (flat ↔ HNSW auto-switch)
├── episodic.py            # Episodic event logging
├── decay.py               # Memory decay (half-life scoring)
├── indexer.py             # Vault indexing pipeline
├── graph_export.py        # Graph exporter
├── sovereign_memory.py    # CLI entry point
├── venv/                  # Python 3.14 venv with all deps
└── requirements.txt
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
- `SOVEREIGN_VAULT_PATH`, `SOVEREIGN_DB_PATH`, `SOVEREIGN_FAISS_PATH`
- `OPENROUTER_API_KEY` — for reranking API
- `OPENCLAW_GATEWAY_URL` — for LLM extraction (default: `http://localhost:18789`)

The global `DEFAULT_CONFIG = SovereignConfig()` is imported everywhere.

## Testing Changes

```bash
cd ~/.openclaw/workspaces/workspace-forge/minni-v3.1
source venv/bin/activate

# Syntax check
python3 -c "import py_compile; py_compile.compile('extraction.py', doraise=True)"

# Import check (verify no broken deps)
python3 -c "from extraction import get_extractor; from retrieval import RetrievalEngine; from config import DEFAULT_CONFIG; print('OK')"

# Start the service
uvicorn app:app --host 0.0.0.0 --port 8312 --reload

# Test endpoints
curl http://localhost:8312/extraction_status
curl http://localhost:8312/stats
```

## Related Skills

- `minni-wiki-ingestion` — Ingest LLM Wiki pages with frontmatter enrichment, wikilink graph extraction, and agent-optimized chunking

## Pitfalls

- **Don't remove `SentenceTransformer`** from retrieval.py — it's the embedding model for FAISS, NOT the reranker
- **Don't inline extraction** in request paths — always use `asyncio.create_task()` (see `_run_extraction` in app.py)
- **Don't change response formats** — downstream agents depend on exact JSON shapes
- **FTS5 queries** must be sanitized (no special chars) — use `_sanitize_fts_query()` in retrieval.py
- **Config changes** require updating both the dataclass field AND any code that reads it

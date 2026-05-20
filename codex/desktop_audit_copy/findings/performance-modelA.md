# Dimension 5: Performance & Scaling (Model A - Gemini 3.5 Flash)

## Performance Capabilities & Cache Layers

### 1. Vector Search Performance
* **File:** [engine/faiss_index.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/faiss_index.py)
* **Summary:** The engine supports two vector search modes:
  - **FAISS (native C++):** Requires `faiss-cpu`. Enables fast exact/approximate matching.
  - **NumPy Fallback:** If `faiss-cpu` is missing, the code falls back to numpy-based brute-force cosine similarity. While functional for test suites and small databases, numpy brute-force scales poorly ($O(N)$ with document count) and does not support quantization or indexing optimizations.

### 2. Cache Layers
* **File:** [engine/rerank_cache.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/rerank_cache.py)
* **Summary:** The `RerankCache` implements cache lookup for cross-encoder reranking. It caches expensive model scores indexed by query and chunk hashes, and invalidates the cache when chunk content changes. This drastically reduces CPU/GPU usage on repetitive agent queries.

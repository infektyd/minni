# Dimension 5: Performance & Scaling (Model B - Claude 4.6 Sonnet)

## SQLite and Hybrid Search Optimization

### 1. SQLite FTS5 Search Integration
The database layer combines lexical searching (SQLite FTS5) with semantic search (FAISS or NumPy brute force). The results are fused using Reciprocal Rank Fusion (RRF).
- FTS5 leverages a virtual index mapping words to document segments.
- RRF combines rankings by taking the reciprocal sum of ranks (e.g. $1 / (60 + \text{rank})$).
Because SQLite is configured with WAL (Write-Ahead Log) mode, read operations do not block write operations, ensuring search responsiveness during background vector synchronization.

### 2. Scalar Quantization (int8-quantized)
To support scale-out memory configurations, the system implements optional scalar quantization of vector embeddings.
- Converts 32-bit floats to 8-bit integers (`int8`), reducing the memory and disk footprint of FAISS indices by ~75%.
- This allows a developer machine to host larger memory segments locally in memory, at the cost of minor recall degradation (typically $<1\%$).

---

## Findings

### [Low] [Scaling] Absence of SQLite auto-vacuum and regular database optimization
* **File:** [engine/db.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/db.py)
* **Summary:** As agents write, update, and prune learnings, old database records are deleted. SQLite does not automatically reclaim disk space unless `VACUUM` is run, or `auto_vacuum` is enabled on the database connection. The file size of `sovereign_memory.db` will grow indefinitely, even when the actual number of documents remains constant.
* **Recommendation:** Execute `PRAGMA auto_vacuum = INCREMENTAL` during migrations, and run incremental vacuum cycles during nightly hygiene routines.

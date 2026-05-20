# Sovereign Memory Resource Limits & Inventory
**Dimension:** Performance & Footprint
**Date:** 2026-05-19

## 1. Network & IPC Limits
| Limit Name | Value | Location | Description |
|------------|-------|----------|-------------|
| UDS Request Body | 1 MiB | `sovrd.py:2512` | Max size of a single JSON-RPC request. |
| Handoff Await Polling | 50ms | `sovrd.py:832` | Polling interval for pending handoffs (Currently BLOCKING). |

## 2. Retrieval & FAISS Limits
| Limit Name | Value | Location | Description |
|------------|-------|----------|-------------|
| HNSW Threshold | 50,000 | `config.py:62` | Vectors needed to switch from Flat to HNSW index. |
| Search Limit (Top-K) | 20 | `config.py:75` | Max candidates fetched in first-pass search. |
| Context Token Budget | 4,096 | `config.py:100` | Max tokens returned to agent per recall request. |
| Neighborhood Search | 8 | `retrieval.py:1217` | Max distance for wikilink context enrichment. |

## 3. Storage & Cleanup
| Limit Name | Value | Location | Description |
|------------|-------|----------|-------------|
| Episodic TTL | 7 Days | `db.py:186` | Retention period for events (Manual cleanup only). |
| FTS Snippet Size | 280 chars | `retrieval.py:15` | Size of snippets in 'snippet' disclosure tier. |
| Memory per Vector | 1,536 bytes | `faiss_index.py` | 384 dimensions * 4 bytes (fp32). |

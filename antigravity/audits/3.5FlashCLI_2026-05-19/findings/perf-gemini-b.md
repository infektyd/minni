# Performance Audit Findings: Sovereign Memory (Agent B Findings)
**Dimension:** Performance & Footprint
**Model:** Gemini 3.5 Flash (High)
**Date:** 2026-05-19

| ID | Severity | Location | Summary | Evidence | Next Action |
|----|----------|----------|---------|----------|-------------|
| PERF-B01 | P0 | `sovrd.py:832` | **Event Loop Blocked by sleep**. `_handle_await_handoff` uses `time.sleep` instead of `await asyncio.sleep`. | Blocks entire daemon for all clients during handoff await. | Replace with `await asyncio.sleep(0.05)`. |
| PERF-B02 | P1 | `faiss_index.py:44` | **Redundant Resident Memory Bloat**. Stores raw vectors in a Python list *in addition* to the FAISS index. | `self._vectors` consumes 1.5GB+ per 1M vectors. | Remove `_vectors` and retrieve from FAISS if needed. |
| PERF-B03 | P1 | `retrieval.py:348` | **Memory Spike on Cold Start**. Uses `c.fetchall()` to load all embeddings. | Loads entire embedding table into memory at once. | Switch to cursor iteration (batching). |
| PERF-B04 | P2 | `episodic.py:297` | **Indefinite Disk Growth**. Episodic cleanup is manual and never triggered by the engine. | `cleanup_expired` is defined but never called. | Add automatic background cleanup task to daemon. |

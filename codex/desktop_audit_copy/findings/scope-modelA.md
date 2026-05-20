# Dimension 2: Scope Creep & Dead Code (Model A - Gemini 3.5 Flash)

## Findings

### [P1] [Dead Code] Deprecated openclaw-extension Directory
* **File:** [openclaw-extension/](file:///Users/hansaxelsson/Projects/sovereignMemory/openclaw-extension)
* **Summary:** The entire `openclaw-extension/` directory (which contains a duplicate `sovrd.py` daemon, outdated TS files, scripts, and package files) is obsolete and has been superseded by the `plugins/sovereign-memory` workspace.
* **Evidence:** Comment in `engine/sovrd.py` line 36 and line 2476 stating it is deprecated.
* **Recommendation:** Completely delete the `openclaw-extension/` directory.

### [P2] [Dead Code] Stubbed Unused Vector Backends (LanceDB & Qdrant)
* **File:** [engine/backends/lance.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/backends/lance.py) and [engine/backends/qdrant.py](file:///Users/hansaxelsson/Projects/sovereignMemory/engine/backends/qdrant.py)
* **Summary:** The `LanceBackend` and `QdrantBackend` classes are non-functional, protocol-conforming stubs that raise `ImportError` or `NotImplementedError` when called. FAISS (`faiss_disk.py` / `faiss_mem.py`) is the only active vector backend.
* **Evidence:** `engine/backends/lance.py` lines 63-78, `engine/backends/qdrant.py` lines 62-78.
* **Recommendation:** Remove `lance.py` and `qdrant.py` from `engine/backends/` until they are actively implemented, reducing package size and testing overhead.

### [P3] [Stale TODO] Stale helper extraction TODO in team-harvest.ts
* **File:** [plugins/sovereign-memory/src/team-harvest.ts](file:///Users/hansaxelsson/Projects/sovereignMemory/plugins/sovereign-memory/src/team-harvest.ts#L88)
* **Summary:** Stale TODO comment to extract the shared `postJson` helper.
* **Evidence:** `plugins/sovereign-memory/src/team-harvest.ts` line 88.
* **Recommendation:** Extract the helper or remove/update the comment.

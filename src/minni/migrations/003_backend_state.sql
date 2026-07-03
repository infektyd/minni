-- 003_backend_state.sql
-- PR-3: Backend state tracking table for the VectorBackend abstraction.
-- Additive only — no DROP, no destructive ALTER.
-- The migrations runner bumps user_version to 3 (highest known migration) on success.
--
-- vector_backends tracks one row per registered vector backend:
--   name                   : Backend identifier ("faiss-disk", "qdrant", etc.)
--   last_synced_chunk_rowid: The chunk_embeddings.rowid of the last synced chunk.
--                            Used by vector_sync.py to sync only new rows.
--   last_synced_at         : Unix timestamp of last successful sync.
--   vector_count           : Number of vectors currently in this backend.
--   status                 : "ok" | "dirty" | "empty" | "error"
--                            "dirty" triggers idle-hook re-sync.

CREATE TABLE IF NOT EXISTS vector_backends (
    name TEXT PRIMARY KEY,
    last_synced_chunk_rowid INTEGER DEFAULT 0,
    last_synced_at INTEGER DEFAULT 0,
    vector_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'empty'
);

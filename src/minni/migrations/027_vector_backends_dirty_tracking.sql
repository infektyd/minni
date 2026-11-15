-- 027_vector_backends_dirty_tracking.sql
-- Charts 5 & 6: Automatic SQLite dirty tracking and monotonic dirty sequence
-- for registered vector backends when chunk_embeddings rows are deleted or
-- updated (including via ON DELETE CASCADE from documents).
-- Additive only — no DROP, no CASE..END inside trigger bodies.

CREATE TABLE IF NOT EXISTS vector_backends (
    name TEXT PRIMARY KEY,
    last_synced_chunk_rowid INTEGER DEFAULT 0,
    last_synced_at INTEGER DEFAULT 0,
    vector_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'empty',
    dirty_seq INTEGER NOT NULL DEFAULT 0,
    synced_dirty_seq INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE vector_backends ADD COLUMN dirty_seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE vector_backends ADD COLUMN synced_dirty_seq INTEGER NOT NULL DEFAULT 0;

CREATE TRIGGER IF NOT EXISTS trg_chunk_embeddings_ad_vector_dirty
AFTER DELETE ON chunk_embeddings
BEGIN
    UPDATE vector_backends SET status = 'dirty', dirty_seq = dirty_seq + 1;
END;

CREATE TRIGGER IF NOT EXISTS trg_chunk_embeddings_au_vector_dirty
AFTER UPDATE OF embedding, chunk_text ON chunk_embeddings
BEGIN
    UPDATE vector_backends SET status = 'dirty', dirty_seq = dirty_seq + 1;
END;

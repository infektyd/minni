-- Migration 021: Minni Typed Memory Graph Schema
-- Target: SQLite shared database and per-vault stores

-- 1. Extend documents for memory typing and stable URIs
ALTER TABLE documents ADD COLUMN memory_kind TEXT;
ALTER TABLE documents ADD COLUMN memory_uri TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_memory_uri
    ON documents(memory_uri) WHERE memory_uri IS NOT NULL;

-- 2. Join table for N:1 Canonical Learning Documents
CREATE TABLE IF NOT EXISTS learning_documents (
    learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
    doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    created_at REAL,
    PRIMARY KEY (learning_id, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_learning_documents_doc_id
    ON learning_documents(doc_id);

-- 3. Extend memory_links for typed edge attributes
ALTER TABLE memory_links ADD COLUMN confidence REAL;
ALTER TABLE memory_links ADD COLUMN inference_method TEXT;
ALTER TABLE memory_links ADD COLUMN model_id TEXT;
ALTER TABLE memory_links ADD COLUMN prompt_version TEXT;
ALTER TABLE memory_links ADD COLUMN inference_run_id TEXT;
ALTER TABLE memory_links ADD COLUMN evidence_json TEXT;
ALTER TABLE memory_links ADD COLUMN inferred_at REAL;
ALTER TABLE memory_links ADD COLUMN edge_status TEXT NOT NULL DEFAULT 'active';

CREATE INDEX IF NOT EXISTS idx_memory_links_target_active
    ON memory_links(target_doc_id, edge_status, link_type, source_doc_id);
CREATE INDEX IF NOT EXISTS idx_memory_links_source_active
    ON memory_links(source_doc_id, edge_status, link_type, target_doc_id);

-- 4. Extend contradiction_log for graph document pairing
ALTER TABLE contradiction_log ADD COLUMN source_doc_id INTEGER
    REFERENCES documents(doc_id) ON DELETE SET NULL;
ALTER TABLE contradiction_log ADD COLUMN target_doc_id INTEGER
    REFERENCES documents(doc_id) ON DELETE SET NULL;
ALTER TABLE contradiction_log ADD COLUMN edge_run_id TEXT;
ALTER TABLE contradiction_log ADD COLUMN confidence REAL;
ALTER TABLE contradiction_log ADD COLUMN resolution_status TEXT DEFAULT 'unresolved';

-- Legacy rows classified explicitly so they are distinguishable from new detections
UPDATE contradiction_log SET resolution_status = 'legacy_unclassified'
    WHERE resolution_status = 'unresolved' AND source_doc_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_contradiction_graph_pair
    ON contradiction_log(source_doc_id, target_doc_id, resolution_status);

-- Backfill existing explicit links
UPDATE memory_links SET
    confidence = COALESCE(confidence, 1.0),
    inference_method = COALESCE(inference_method, CASE link_type
        WHEN 'wikilink' THEN 'explicit_wikilink'
        WHEN 'derived_from' THEN 'writeback_evidence'
        ELSE 'legacy' END)
    WHERE confidence IS NULL OR inference_method IS NULL;

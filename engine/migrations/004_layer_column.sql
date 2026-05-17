-- PR-5: Layer-aware retrieval metadata.
-- Additive and nullable so existing callers and old rows continue to work.

ALTER TABLE documents ADD COLUMN layer TEXT DEFAULT NULL;
ALTER TABLE chunk_embeddings ADD COLUMN layer TEXT DEFAULT NULL;

UPDATE documents
SET layer = CASE
    WHEN whole_document = 1 AND agent LIKE 'identity:%' THEN 'identity'
    WHEN lower(COALESCE(page_type, '')) = 'artifact' THEN 'artifact'
    ELSE 'knowledge'
END
WHERE layer IS NULL;

UPDATE chunk_embeddings
SET layer = (
    SELECT COALESCE(documents.layer, 'knowledge')
    FROM documents
    WHERE documents.doc_id = chunk_embeddings.doc_id
)
WHERE layer IS NULL;

CREATE INDEX IF NOT EXISTS idx_doc_layer ON documents(layer);
CREATE INDEX IF NOT EXISTS idx_chunk_layer ON chunk_embeddings(layer);

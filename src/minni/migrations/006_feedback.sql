-- PR-9: agent feedback on recall results.
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    query_hash TEXT,
    query_text TEXT,
    doc_id INTEGER,
    chunk_id INTEGER,
    agent_id TEXT,
    useful BOOLEAN,
    created_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_feedback_agent_query_doc
    ON feedback(agent_id, query_hash, doc_id, created_at);

CREATE INDEX IF NOT EXISTS idx_feedback_created
    ON feedback(created_at);

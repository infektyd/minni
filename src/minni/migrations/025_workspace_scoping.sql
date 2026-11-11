-- Migration 025: workspace_id scoping on documents and learnings + backfill
--
-- Schema parity across fresh DBs and existing DBs where documents.workspace_id
-- already exists (TEXT DEFAULT '') while learnings.workspace_id does not yet
-- exist. _execute_tolerant ignores 'duplicate column name' on ALTER TABLE.

ALTER TABLE documents ADD COLUMN workspace_id TEXT NOT NULL DEFAULT '';
UPDATE documents SET workspace_id = '' WHERE workspace_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_documents_workspace ON documents(workspace_id);

ALTER TABLE learnings ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default';
UPDATE learnings SET workspace_id = 'default' WHERE workspace_id IS NULL OR workspace_id = '';
CREATE INDEX IF NOT EXISTS idx_learn_workspace ON learnings(workspace_id);

-- Backfill learnings.workspace_id from accepted candidate_packets rows when
-- learnings.workspace_id is still empty/'default' and a matching accepted
-- candidate_packets row carries a concrete workspace_id.
UPDATE learnings
SET workspace_id = (
    SELECT cp.workspace_id
    FROM candidate_packets AS cp
    WHERE cp.status = 'accepted'
      AND cp.principal = learnings.agent_id
      AND cp.content = learnings.content
      AND cp.workspace_id IS NOT NULL
      AND TRIM(cp.workspace_id) NOT IN ('', 'default')
    ORDER BY COALESCE(cp.resolved_at, cp.proposed_at) DESC, cp.candidate_id DESC
    LIMIT 1
)
WHERE (learnings.workspace_id IS NULL OR learnings.workspace_id IN ('', 'default'))
  AND EXISTS (
      SELECT 1
      FROM candidate_packets AS cp
      WHERE cp.status = 'accepted'
        AND cp.principal = learnings.agent_id
        AND cp.content = learnings.content
        AND cp.workspace_id IS NOT NULL
        AND TRIM(cp.workspace_id) NOT IN ('', 'default')
  );

-- Backfill learnings.workspace_id from linked documents when learning_documents
-- connects an un-scoped learning to an already workspace-scoped document.
UPDATE learnings
SET workspace_id = (
    SELECT d.workspace_id
    FROM learning_documents AS ld
    JOIN documents AS d ON d.doc_id = ld.doc_id
    WHERE ld.learning_id = learnings.learning_id
      AND d.workspace_id IS NOT NULL
      AND TRIM(d.workspace_id) NOT IN ('', 'default')
    ORDER BY d.doc_id DESC
    LIMIT 1
)
WHERE (learnings.workspace_id IS NULL OR learnings.workspace_id IN ('', 'default'))
  AND EXISTS (
      SELECT 1
      FROM learning_documents AS ld
      JOIN documents AS d ON d.doc_id = ld.doc_id
      WHERE ld.learning_id = learnings.learning_id
        AND d.workspace_id IS NOT NULL
        AND TRIM(d.workspace_id) NOT IN ('', 'default')
  );

-- Propagate backfilled learnings.workspace_id onto linked _durable/ documents
-- when the typed memory graph join table (learning_documents) is present.
UPDATE documents
SET workspace_id = (
    SELECT l.workspace_id
    FROM learning_documents AS ld
    JOIN learnings AS l ON l.learning_id = ld.learning_id
    WHERE ld.doc_id = documents.doc_id
      AND l.workspace_id IS NOT NULL
      AND TRIM(l.workspace_id) NOT IN ('', 'default')
    ORDER BY l.learning_id DESC
    LIMIT 1
)
WHERE (documents.workspace_id IS NULL OR documents.workspace_id IN ('', 'default'))
  AND EXISTS (
      SELECT 1
      FROM learning_documents AS ld
      JOIN learnings AS l ON l.learning_id = ld.learning_id
      WHERE ld.doc_id = documents.doc_id
        AND l.workspace_id IS NOT NULL
        AND TRIM(l.workspace_id) NOT IN ('', 'default')
  );

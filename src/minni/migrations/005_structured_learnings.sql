-- 005_structured_learnings.sql
-- PR-6: Structured Learnings + Contradiction Detection.
-- Additive only — no DROP, no destructive ALTER.
-- Adds new nullable columns to the learnings table to support:
--   - Structured assertions (machine-comparable distillation of content)
--   - Conditional applicability (applies_when scoping)
--   - Evidence document references (evidence_doc_ids as JSON array)
--   - Explicit contradiction tracking (contradicts_id FK to another learning)
--
-- Each ALTER TABLE is a separate statement so _execute_tolerant handles
-- "duplicate column name" errors idempotently when this runs on a DB that
-- already has these columns (e.g. applied twice, or added in _init_schema).

ALTER TABLE learnings ADD COLUMN assertion TEXT;
ALTER TABLE learnings ADD COLUMN applies_when TEXT;
ALTER TABLE learnings ADD COLUMN evidence_doc_ids TEXT;
ALTER TABLE learnings ADD COLUMN contradicts_id INTEGER REFERENCES learnings(learning_id);
ALTER TABLE learnings ADD COLUMN status TEXT DEFAULT 'active';

CREATE INDEX IF NOT EXISTS idx_learn_status ON learnings(status);
CREATE INDEX IF NOT EXISTS idx_learn_contradicts ON learnings(contradicts_id);

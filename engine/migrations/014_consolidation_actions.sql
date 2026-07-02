-- Issue #119 (defect 2): the AFM consolidation pass and the daemon's durable
-- promote/dedup/review helpers (minnid_runtime/afm.py) read and write
-- consolidation_actions, but no migration created it — only test fixtures did
-- (and long-lived installs that got it from earlier ad-hoc creation). On a
-- fresh MINNI_HOME an operator wet-run failed with
-- "no such table: consolidation_actions". Schema matches the test fixtures
-- (test_inbox_ingest.py) and every INSERT/SELECT in the engine.

CREATE TABLE IF NOT EXISTS consolidation_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type TEXT NOT NULL,
    source_event_id INTEGER,
    target_learning_id INTEGER,
    superseded_learning_id INTEGER,
    claim TEXT,
    category TEXT,
    confidence REAL,
    status TEXT DEFAULT 'pending',
    detail TEXT,
    created_at REAL NOT NULL
);

-- The consolidation drain's re-examine fence and the review-marker dedup both
-- probe WHERE action_type='afm_review' AND claim=?; index that lookup.
CREATE INDEX IF NOT EXISTS idx_consolidation_actions_type_claim
    ON consolidation_actions (action_type, claim);

-- G3: daemon delivery cursors only. Not graph state.
-- Journal seq rebuilds the pending notification queue. This table stores
-- last_delivered_seq per subscriber/plan. No slices, deps, claims, or evidence.

CREATE TABLE IF NOT EXISTS thread_delivery_cursors (
    subscriber_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    last_delivered_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subscriber_id, plan_id)
);

CREATE INDEX IF NOT EXISTS idx_thread_delivery_cursors_plan
    ON thread_delivery_cursors(plan_id);

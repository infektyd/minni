-- G3: unused in production. Honesty: minnid does not write this table.
-- Live delivery cursors live in plugin .runtime/thread-relay/cursors.json
-- written on journal append. This schema is leftover, not a live store.
-- No slices, deps, claims, or evidence.

CREATE TABLE IF NOT EXISTS thread_delivery_cursors (
    subscriber_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    last_delivered_seq INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subscriber_id, plan_id)
);

CREATE INDEX IF NOT EXISTS idx_thread_delivery_cursors_plan
    ON thread_delivery_cursors(plan_id);

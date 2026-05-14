-- PR-16: handoff leases and contradiction fanout.

CREATE TABLE IF NOT EXISTS handoff_leases (
    lease_id TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    task TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    contradicts_id INTEGER,
    inbox_path TEXT,
    outbox_path TEXT,
    created_at REAL NOT NULL,
    expires_at REAL
);

CREATE INDEX IF NOT EXISTS idx_handoff_to_status ON handoff_leases(to_agent, status);

CREATE TABLE IF NOT EXISTS learning_reads (
    learning_id INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    read_at REAL NOT NULL,
    source TEXT,
    PRIMARY KEY(learning_id, agent_id, read_at)
);

CREATE INDEX IF NOT EXISTS idx_learning_reads_agent_time ON learning_reads(agent_id, read_at);

CREATE TABLE IF NOT EXISTS contradiction_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    superseded_learning_id INTEGER NOT NULL,
    new_learning_id INTEGER NOT NULL,
    originating_agent TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contradiction_events_created ON contradiction_events(created_at);
CREATE INDEX IF NOT EXISTS idx_contradiction_events_superseded ON contradiction_events(superseded_learning_id);

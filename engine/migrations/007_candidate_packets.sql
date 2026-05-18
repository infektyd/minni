-- G14: Candidate staging table (SEC-003 / P1 keystone "human governs persistence")
-- Schema per design doc: candidate_id, principal (stamped), workspace_id, layer,
-- privacy_level, content, evidence_refs (JSON), derived_from (JSON),
-- instruction_like, status CHECK (proposed/accepted/rejected/redacted/expired/merged/superseded),
-- resolved_at, resolved_by, resolution_reason, proposed_at.
-- Index (principal, status, proposed_at DESC) for console listing.

CREATE TABLE IF NOT EXISTS candidate_packets (
    candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    principal TEXT NOT NULL,                 -- EffectivePrincipal.agent_id (server-stamped)
    workspace_id TEXT NOT NULL DEFAULT 'default',
    layer TEXT,
    privacy_level TEXT,
    content TEXT NOT NULL,
    evidence_refs TEXT,                      -- JSON array of doc refs / evidence
    derived_from TEXT,                       -- JSON lineage / source info
    instruction_like INTEGER DEFAULT 0,      -- boolean-ish for evidence-only fence hint
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'accepted', 'rejected', 'redacted', 'expired', 'merged', 'superseded')),
    proposed_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT,                        -- principal that resolved
    resolution_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_candidate_principal_status_time
    ON candidate_packets (principal, status, proposed_at DESC);

CREATE INDEX IF NOT EXISTS idx_candidate_status
    ON candidate_packets (status);

-- Note: rejected/redacted/expired rows preserved for audit (never auto-deleted in P1).
-- Only 'accepted' rows result in durable learnings INSERT (via resolve path).

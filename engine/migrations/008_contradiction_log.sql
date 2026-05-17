-- G18: Contradiction log table (P1)
-- Records every contradiction detected by writeback detector.
-- Links to candidate resolution for surfacing on console cards.
-- Fields per design: memory_a_id, memory_b_id (the conflicting learnings/candidates),
-- detected_at, detection_method, resolution_id (FK to candidate_packets when resolved).

CREATE TABLE IF NOT EXISTS contradiction_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_a_id INTEGER,                     -- learning_id or candidate_id of first side
    memory_b_id INTEGER,                     -- learning_id or candidate_id of second side
    detected_at REAL NOT NULL,
    detection_method TEXT DEFAULT 'cosine',  -- e.g. 'cosine', 'assertion_match'
    resolution_id INTEGER,                   -- candidate_packets.candidate_id that addressed it (nullable until resolved)
    FOREIGN KEY (resolution_id) REFERENCES candidate_packets(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_contradiction_detected
    ON contradiction_log (detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_contradiction_resolution
    ON contradiction_log (resolution_id);

-- When a candidate is resolved (accepted/rejected), the detector can link prior contradictions.
-- Excluded from recall: only accepted candidates become learnings; contradictions on proposed candidates are shown in UI for human review.

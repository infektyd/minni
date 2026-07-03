-- 002_score_distribution.sql
-- PR-2: Rolling-window score distribution table for confidence calibration.
-- Additive only — no DROP, no destructive ALTER.
-- Bumps user_version to 2 via the PR-1 migrations runner.
--
-- ALTER TABLE ADD COLUMN statements are idempotent: the migrations runner
-- silently ignores "duplicate column name" errors, so this script is safe
-- to apply to DBs where these columns were already added by _init_schema.

CREATE TABLE IF NOT EXISTS score_distribution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_score REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'rrf',
    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
);

CREATE INDEX IF NOT EXISTS idx_score_dist_kind ON score_distribution(kind, created_at);

ALTER TABLE documents ADD COLUMN page_status TEXT DEFAULT 'candidate';
ALTER TABLE documents ADD COLUMN privacy_level TEXT DEFAULT 'safe';
ALTER TABLE documents ADD COLUMN page_type TEXT;
ALTER TABLE documents ADD COLUMN superseded_by INTEGER;
ALTER TABLE documents ADD COLUMN expires_at REAL;
ALTER TABLE documents ADD COLUMN evidence_refs TEXT;

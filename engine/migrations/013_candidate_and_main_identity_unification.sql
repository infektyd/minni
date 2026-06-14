-- Migration 013: complete the agent-identity unification that 012 started.
-- Operator statement (Hans, 2026-06-11): claudecode / claude-code / main are all
-- the same agent platform — historical main-tagged rows are claude-code writes
-- collapsed by the old legacy_agent_ids alias config. Going forward, 'main' is
-- reserved for genuine operator context (vault-isolation branch).
-- Pre-migration backup: ~/.minni/backups/minni-pre-013.db (2026-06-11).
--
-- candidate_packets was missed by 012 (it retagged learnings only).
UPDATE candidate_packets SET principal = 'claude-code'
  WHERE principal IN ('claudecode', 'main');

-- learnings: fold historical main rows into claude-code (012 handled claudecode).
-- The 011 FTS triggers keep learnings_fts in sync with this UPDATE.
UPDATE learnings SET agent_id = 'claude-code' WHERE agent_id = 'main';

-- Test-run leakage: pytest runs fell through to the live db before the
-- _stage_candidate temp-db fix (2026-06-11). These rows are fixtures, not memory.
DELETE FROM candidate_packets WHERE principal = 'test';
DELETE FROM learnings WHERE agent_id = 'test';

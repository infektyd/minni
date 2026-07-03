-- Migration 013: complete the agent-identity unification that 012 started.
-- Operator statement (Hans, 2026-06-11): claudecode / claude-code / main are all
-- the same agent platform — historical main-tagged rows are claude-code writes
-- collapsed by the old legacy_agent_ids alias config. Going forward, 'main' is
-- reserved for genuine operator context (vault-isolation branch).
-- Pre-migration backup: ~/.minni/backups/minni-pre-013.db (2026-06-11).
--
-- SECURITY (M1, 2026-07-02): the main -> claude-code rewrite for
-- candidate_packets.principal and learnings.agent_id is NOT unconditional
-- SQL here. It only applies to Hans's already-migrated 2026-06-11 database,
-- where 'main' is known (operator-asserted) to be a legacy claude-code
-- alias. Applying that rewrite unconditionally to any un-migrated DB would
-- silently reattribute a genuine operator 'main' principal's memory to
-- claude-code on a fresh install where no such alias ever existed. The
-- rewrite is therefore performed in Python by run_migrations() (see
-- migrations.py, _apply_migration_013_legacy_main_rewrite), gated on the
-- MINNI_LEGACY_MAIN_IS_CLAUDE_CODE=1 environment flag. See
-- docs/CANONICAL-PATHS.md and SECURITY_PLAN.md decision log for details.
--
-- The claudecode -> claude-code fold (candidate_packets only; 012 handled
-- learnings) is NOT the disputed alias — 'claudecode' has never been a
-- valid operator principal, only a legacy slug variant of the same agent
-- id, so it is safe to always fold here.
UPDATE candidate_packets SET principal = 'claude-code'
  WHERE principal = 'claudecode';

-- Test-run leakage: pytest runs fell through to the live db before the
-- _stage_candidate temp-db fix (2026-06-11). These rows are fixtures, not memory.
DELETE FROM candidate_packets WHERE principal = 'test';
DELETE FROM learnings WHERE agent_id = 'test';

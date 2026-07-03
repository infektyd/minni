-- Migration 012: agent_id tag hygiene — canonicalize historical learnings rows.
-- Requires 011 (FTS sync triggers) so these UPDATEs propagate to learnings_fts.
-- Pre-migration backup: ~/.minni/backups/minni-pre-tag-hygiene.db (2026-06-11).
--
-- claudecode → claude-code: rows written by the inbox-ingest path, which derived
-- the agent id by literally stripping '-vault' from the vault dir name instead of
-- inverting _default_agent_vault (fixed alongside this migration).
UPDATE learnings SET agent_id = 'claude-code' WHERE agent_id = 'claudecode';

-- grok / grok-4.3 → grok-build: pre-canonicalization self-identifications from the
-- Grok agent, before grok-build entered platform_agent_ids. Reversible via backup.
UPDATE learnings SET agent_id = 'grok-build' WHERE agent_id IN ('grok', 'grok-4.3');

-- learning_id=2: pre-G11 CLI corruption swapped --agent value and content; the
-- row belongs to syntra while the real learning text is currently in agent_id.
UPDATE learnings SET content = agent_id, agent_id = 'syntra' WHERE agent_id LIKE 'Discord bot tags%';

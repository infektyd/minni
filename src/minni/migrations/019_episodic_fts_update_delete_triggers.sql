-- Migration 019: give episodic_events the UPDATE and DELETE FTS triggers that
-- learnings has had since migration 011.
--
-- Issue #287, found during adversarial review of PR #279. episodic_events
-- carried an AFTER INSERT trigger only, while learnings carries insert, update
-- AND delete. The asymmetry was latent — nothing in src/ UPDATEs
-- episodic_events today — but the failure mode is silent and the metric added
-- in #279 cannot see it: episodic_index_coverage checks that an event_id is
-- PRESENT in the index, not that the indexed text still matches the row.
--
-- Reproduced against the shipped schema:
--
--   UPDATE episodic_events SET content='revised beta text' WHERE event_id=1
--   -> events content: revised beta text
--   -> fts    content: original alpha text
--   -> MATCH 'beta'  = 0 rows   (the current text is unfindable)
--   -> MATCH 'alpha' = 1 row    (the stale text still matches)
--   -> episodic_index_ratio: 1.0 (reports healthy)
--
-- and for deletes, an event removed by any path that does not hand-delete its
-- index row first leaves an orphan behind. episodic.py's two prune paths
-- (trim_recall_traces, cleanup_expired) do delete FTS rows explicitly, so this
-- trigger is belt-and-braces for them — but it is the only thing protecting a
-- third prune path written later.
--
-- These are CREATE TRIGGER IF NOT EXISTS, matching the base schema in
-- db._init_schema, so this migration is idempotent and a fresh database (which
-- gets the triggers from _init_schema directly) sees a no-op.
--
-- Note the UPDATE trigger is DELETE-then-conditional-INSERT rather than
-- learnings' UPDATE-in-place. episodic's insert trigger is guarded on
-- `WHEN NEW.content IS NOT NULL`, so an UPDATE setting content to NULL must
-- REMOVE the index row; an in-place UPDATE would write NULL content into fts5
-- and keep a row the insert path would never have created.

CREATE TRIGGER IF NOT EXISTS trg_episodic_fts_update
AFTER UPDATE OF agent_id, content ON episodic_events
BEGIN
    DELETE FROM episodic_fts WHERE event_id = OLD.event_id;
    INSERT INTO episodic_fts(event_id, agent_id, content)
    SELECT NEW.event_id, NEW.agent_id, NEW.content
    WHERE NEW.content IS NOT NULL;
END;

CREATE TRIGGER IF NOT EXISTS trg_episodic_fts_delete
AFTER DELETE ON episodic_events
BEGIN
    DELETE FROM episodic_fts WHERE event_id = OLD.event_id;
END;

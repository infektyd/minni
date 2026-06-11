-- Migration 011: FTS sync triggers for learnings_fts
-- The INSERT trigger (trg_learnings_fts_insert) already exists in db._init_schema.
-- UPDATE and DELETE on learnings silently desynced the standalone fts5 table without these.

CREATE TRIGGER IF NOT EXISTS trg_learnings_fts_update
AFTER UPDATE OF agent_id, category, content ON learnings
BEGIN
    UPDATE learnings_fts
    SET agent_id = NEW.agent_id,
        category = NEW.category,
        content = NEW.content
    WHERE learning_id = OLD.learning_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_learnings_fts_delete
AFTER DELETE ON learnings
BEGIN
    DELETE FROM learnings_fts WHERE learning_id = OLD.learning_id;
END;
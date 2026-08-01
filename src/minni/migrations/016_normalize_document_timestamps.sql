-- Migration 016: normalize non-numeric documents.indexed_at / last_modified /
-- last_accessed, and stop any writer from re-poisoning them.
--
-- Audit R0. SQLite's REAL *affinity* accepts a non-numeric string verbatim, so
-- a writer that handed an ISO-8601 string to indexed_at left TEXT sitting in a
-- REAL column. Readers then blow up on the whole result set, not the one row:
-- retrieval._filter_candidates raises ValueError out through handle_search
-- (-32000, entire recall aborted) and decay.run_decay raises TypeError inside
-- its transaction (entire decay pass aborted).
--
-- Repair: parse ISO-8601 back to an epoch. SQLite's strftime('%s', ...) reads a
-- tz-less timestamp as UTC, which is exactly what minni.timestamps.parse_epoch
-- does — the two sides must agree or a repaired row would not equal a
-- re-parsed one. The trailing 'Z' is stripped explicitly rather than relying on
-- the host SQLite being new enough to accept it.
--
-- Values that parse to NULL (genuine garbage) fall back to 0.0. That is a
-- deliberate, visible sentinel: it reads as "epoch zero, needs attention"
-- rather than quietly inventing a plausible date.
--
-- Prevention: normalizing triggers below. Every in-tree writer passes
-- time.time(), so the realistic poison source is an out-of-tree script or a
-- future caller — a Python-side guard alone cannot cover those. The triggers
-- fire only on the WHEN clause (typeof mismatch), so the steady-state cost on
-- correct writes is one typeof() per row. SQLite runs with recursive_triggers
-- OFF by default, so the corrective UPDATE inside a trigger body does not
-- re-enter the UPDATE trigger.

UPDATE documents
   SET indexed_at = COALESCE(CAST(strftime('%s', replace(replace(indexed_at, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
 WHERE indexed_at IS NOT NULL
   AND typeof(indexed_at) NOT IN ('integer', 'real');

UPDATE documents
   SET last_modified = COALESCE(CAST(strftime('%s', replace(replace(last_modified, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
 WHERE last_modified IS NOT NULL
   AND typeof(last_modified) NOT IN ('integer', 'real');

UPDATE documents
   SET last_accessed = COALESCE(CAST(strftime('%s', replace(replace(last_accessed, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
 WHERE last_accessed IS NOT NULL
   AND typeof(last_accessed) NOT IN ('integer', 'real');

-- Trigger bodies deliberately use three WHERE-guarded UPDATEs instead of one
-- UPDATE with CASE expressions: the migration runner's statement splitter
-- (migrations.py::_split_statements) tracks BEGIN/END depth by token, so a
-- CASE ... END inside a trigger body closes the body early and the script is
-- cut in half. No CASE here, no END except the trigger's own.

CREATE TRIGGER IF NOT EXISTS trg_documents_normalize_ts_insert
AFTER INSERT ON documents
WHEN (NEW.indexed_at IS NOT NULL AND typeof(NEW.indexed_at) NOT IN ('integer', 'real'))
  OR (NEW.last_modified IS NOT NULL AND typeof(NEW.last_modified) NOT IN ('integer', 'real'))
  OR (NEW.last_accessed IS NOT NULL AND typeof(NEW.last_accessed) NOT IN ('integer', 'real'))
BEGIN
    UPDATE documents
       SET indexed_at = COALESCE(CAST(strftime('%s', replace(replace(indexed_at, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND indexed_at IS NOT NULL
       AND typeof(indexed_at) NOT IN ('integer', 'real');

    UPDATE documents
       SET last_modified = COALESCE(CAST(strftime('%s', replace(replace(last_modified, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND last_modified IS NOT NULL
       AND typeof(last_modified) NOT IN ('integer', 'real');

    UPDATE documents
       SET last_accessed = COALESCE(CAST(strftime('%s', replace(replace(last_accessed, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND last_accessed IS NOT NULL
       AND typeof(last_accessed) NOT IN ('integer', 'real');
END;

CREATE TRIGGER IF NOT EXISTS trg_documents_normalize_ts_update
AFTER UPDATE OF indexed_at, last_modified, last_accessed ON documents
WHEN (NEW.indexed_at IS NOT NULL AND typeof(NEW.indexed_at) NOT IN ('integer', 'real'))
  OR (NEW.last_modified IS NOT NULL AND typeof(NEW.last_modified) NOT IN ('integer', 'real'))
  OR (NEW.last_accessed IS NOT NULL AND typeof(NEW.last_accessed) NOT IN ('integer', 'real'))
BEGIN
    UPDATE documents
       SET indexed_at = COALESCE(CAST(strftime('%s', replace(replace(indexed_at, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND indexed_at IS NOT NULL
       AND typeof(indexed_at) NOT IN ('integer', 'real');

    UPDATE documents
       SET last_modified = COALESCE(CAST(strftime('%s', replace(replace(last_modified, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND last_modified IS NOT NULL
       AND typeof(last_modified) NOT IN ('integer', 'real');

    UPDATE documents
       SET last_accessed = COALESCE(CAST(strftime('%s', replace(replace(last_accessed, 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND last_accessed IS NOT NULL
       AND typeof(last_accessed) NOT IN ('integer', 'real');
END;

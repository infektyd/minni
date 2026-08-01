-- Migration 016: normalize non-numeric documents.indexed_at / last_modified /
-- last_accessed, and stop any writer from re-poisoning them.
--
-- Audit R0. One `documents` row (an identity envelope) carried `indexed_at` AND
-- a writer that handed an ISO-8601 string to indexed_at left TEXT sitting in a
-- REAL column. Readers then blow up on the whole result set, not the one row:
-- retrieval._filter_candidates raises ValueError out through handle_search
-- (-32000, entire recall aborted) and decay.run_decay raises TypeError inside
-- its transaction (entire decay pass aborted).
--
-- Repair, two forms, applied in order so the first never gets a chance to lose
-- what the second could have kept:
--
--   1. Numeric TEXT (e.g. a caller that stored str(time.time())). CAST(... AS
--      REAL) parses this directly and losslessly. This form is recoverable by
--      minni.timestamps.parse_epoch's first branch (`float(text)`) — the SQL
--      path must recover it too, or a value Python would keep is silently
--      swept into the "needs attention" bucket below. Grok review flagged
--      this: without step 1, strftime('%s', '1700000000.5') returns NULL (this
--      SQLite build does not read a bare numeric string as a Julian day; it
--      returns NULL), so the row would fall through to the 0.0 sentinel even
--      though it was perfectly recoverable.
--
--   2. ISO-8601 (naive or offset-qualified, trailing 'Z' or '+HH:MM'/'-HH:MM').
--      strftime('%s', ...) on this SQLite build (3.53+) resolves the offset
--      before converting, matching minni.timestamps.parse_epoch's
--      datetime.fromisoformat + timestamp() path. The trailing 'Z' is
--      stripped explicitly rather than relying on the host SQLite accepting
--      it as a zone designator.
--
-- Values that parse to NULL by both forms (genuine garbage) fall back to 0.0.
-- That is a deliberate, visible sentinel: it reads as "epoch zero, needs
-- attention" rather than quietly inventing a plausible date.
--
-- Prevention: normalizing triggers below. Every in-tree writer passes
-- time.time(), so the realistic poison source is an out-of-tree script or a
-- future caller — a Python-side guard alone cannot cover those. The triggers
-- fire only on the WHEN clause (typeof mismatch), so the steady-state cost on
-- correct writes is one typeof() per row. SQLite runs with recursive_triggers
-- OFF by default, so the corrective UPDATE inside a trigger body does not
-- re-enter the UPDATE trigger.
--
-- Trigger bodies (and the bulk repairs below) deliberately use WHERE-guarded
-- UPDATEs instead of one UPDATE with CASE expressions: the migration runner's
-- statement splitter (migrations.py::_split_statements) tracks BEGIN/END
-- depth by token, so a CASE ... END inside a trigger body closes the body
-- early and the script is cut in half. No CASE anywhere in this file, no END
-- except each trigger's own.

-- Step 1: numeric TEXT epochs (whole string is digits/decimal-point only).
UPDATE documents
   SET indexed_at = CAST(indexed_at AS REAL)
 WHERE typeof(indexed_at) = 'text'
   AND indexed_at GLOB '[0-9]*'
   AND indexed_at NOT GLOB '*[^0-9.]*';

UPDATE documents
   SET last_modified = CAST(last_modified AS REAL)
 WHERE typeof(last_modified) = 'text'
   AND last_modified GLOB '[0-9]*'
   AND last_modified NOT GLOB '*[^0-9.]*';

UPDATE documents
   SET last_accessed = CAST(last_accessed AS REAL)
 WHERE typeof(last_accessed) = 'text'
   AND last_accessed GLOB '[0-9]*'
   AND last_accessed NOT GLOB '*[^0-9.]*';

-- Step 2: everything still TEXT is either ISO-8601 or genuine garbage.
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

CREATE TRIGGER IF NOT EXISTS trg_documents_normalize_ts_insert
AFTER INSERT ON documents
WHEN (NEW.indexed_at IS NOT NULL AND typeof(NEW.indexed_at) NOT IN ('integer', 'real'))
  OR (NEW.last_modified IS NOT NULL AND typeof(NEW.last_modified) NOT IN ('integer', 'real'))
  OR (NEW.last_accessed IS NOT NULL AND typeof(NEW.last_accessed) NOT IN ('integer', 'real'))
BEGIN
    UPDATE documents
       SET indexed_at = CAST(indexed_at AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(indexed_at) = 'text'
       AND indexed_at GLOB '[0-9]*'
       AND indexed_at NOT GLOB '*[^0-9.]*';

    UPDATE documents
       SET last_modified = CAST(last_modified AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(last_modified) = 'text'
       AND last_modified GLOB '[0-9]*'
       AND last_modified NOT GLOB '*[^0-9.]*';

    UPDATE documents
       SET last_accessed = CAST(last_accessed AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(last_accessed) = 'text'
       AND last_accessed GLOB '[0-9]*'
       AND last_accessed NOT GLOB '*[^0-9.]*';

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
       SET indexed_at = CAST(indexed_at AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(indexed_at) = 'text'
       AND indexed_at GLOB '[0-9]*'
       AND indexed_at NOT GLOB '*[^0-9.]*';

    UPDATE documents
       SET last_modified = CAST(last_modified AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(last_modified) = 'text'
       AND last_modified GLOB '[0-9]*'
       AND last_modified NOT GLOB '*[^0-9.]*';

    UPDATE documents
       SET last_accessed = CAST(last_accessed AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(last_accessed) = 'text'
       AND last_accessed GLOB '[0-9]*'
       AND last_accessed NOT GLOB '*[^0-9.]*';

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

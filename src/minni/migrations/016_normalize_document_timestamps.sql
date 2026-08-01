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
--   1. Numeric TEXT (e.g. a caller that stored str(time.time())), optionally
--      whitespace-padded. TRIM + CAST(... AS REAL) parses this directly and
--      losslessly. This form is recoverable by minni.timestamps.parse_epoch's
--      first branch (`text.strip()` then `float(text)`) — the SQL path must
--      recover it too, or a value Python would keep is silently swept into
--      the "needs attention" bucket below.
--
--      The numeric guard requires the TRIMmED string to be a single
--      well-formed non-negative integer or decimal (at most one '.', digits
--      on both sides of it, no other characters). Two grok-review rounds
--      shaped this:
--        - round 1: without step 1 at all, a bare numeric string fell all
--          the way to the 0.0 sentinel (this SQLite build's strftime('%s', a
--          plain number) returns NULL rather than misreading it as a Julian
--          day, but the effect — losing a recoverable value — was the same).
--        - round 2: a *loose* step-1 guard (any mix of digits and dots) let
--          garbage like '1700000000.5.9' or '12..3' through; CAST(... AS
--          REAL) takes the longest leading numeric prefix and stores a
--          plausible-looking but WRONG epoch — exactly the "quietly
--          inventing a plausible date" failure the 0.0 sentinel exists to
--          avoid, just via a different route. The guard below requires
--          EXACTLY one decimal point when one is present, so multi-dot
--          garbage fails step 1 and falls through to step 2 (which fails it
--          too, landing on the visible 0.0 sentinel — matching
--          parse_epoch('1700000000.5.9') -> None).
--
--   2. ISO-8601 (naive or offset-qualified, trailing 'Z' or '+HH:MM'/'-HH:MM'),
--      optionally whitespace-padded (TRIMmed before strftime, same reasoning
--      as step 1 — round 3 of grok-review caught this: a leading/trailing
--      space made strftime('%s', ...) return NULL even though
--      minni.timestamps.parse_epoch recovers the value via strip(), so the
--      row fell to the 0.0 sentinel instead of its real, recoverable epoch).
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
--
-- Not handled: a leading '-' (a negative epoch, i.e. before 1970). parse_epoch
-- technically recovers "-5" via float(); the SQL guard below does not treat
-- it as numeric, so it falls to step 2 and then the 0.0 sentinel. A live
-- negative epoch is not a realistic value for this column, so the asymmetry
-- is accepted rather than chased.

-- Step 1: numeric TEXT epochs — TRIMmed string is a bare integer or a decimal
-- with exactly one '.' and digits on both sides. No CASE (see note above).
UPDATE documents
   SET indexed_at = CAST(TRIM(indexed_at) AS REAL)
 WHERE typeof(indexed_at) = 'text'
   AND (
     (TRIM(indexed_at) GLOB '[0-9]*' AND TRIM(indexed_at) NOT GLOB '*[^0-9]*')
     OR (TRIM(indexed_at) GLOB '[0-9]*.[0-9]*'
         AND TRIM(indexed_at) NOT GLOB '*.*.*'
         AND TRIM(indexed_at) NOT GLOB '*[^0-9.]*')
   );

UPDATE documents
   SET last_modified = CAST(TRIM(last_modified) AS REAL)
 WHERE typeof(last_modified) = 'text'
   AND (
     (TRIM(last_modified) GLOB '[0-9]*' AND TRIM(last_modified) NOT GLOB '*[^0-9]*')
     OR (TRIM(last_modified) GLOB '[0-9]*.[0-9]*'
         AND TRIM(last_modified) NOT GLOB '*.*.*'
         AND TRIM(last_modified) NOT GLOB '*[^0-9.]*')
   );

UPDATE documents
   SET last_accessed = CAST(TRIM(last_accessed) AS REAL)
 WHERE typeof(last_accessed) = 'text'
   AND (
     (TRIM(last_accessed) GLOB '[0-9]*' AND TRIM(last_accessed) NOT GLOB '*[^0-9]*')
     OR (TRIM(last_accessed) GLOB '[0-9]*.[0-9]*'
         AND TRIM(last_accessed) NOT GLOB '*.*.*'
         AND TRIM(last_accessed) NOT GLOB '*[^0-9.]*')
   );

-- Step 2: everything still TEXT is either ISO-8601 or genuine garbage.
UPDATE documents
   SET indexed_at = COALESCE(CAST(strftime('%s', replace(replace(TRIM(indexed_at), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
 WHERE indexed_at IS NOT NULL
   AND typeof(indexed_at) NOT IN ('integer', 'real');

UPDATE documents
   SET last_modified = COALESCE(CAST(strftime('%s', replace(replace(TRIM(last_modified), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
 WHERE last_modified IS NOT NULL
   AND typeof(last_modified) NOT IN ('integer', 'real');

UPDATE documents
   SET last_accessed = COALESCE(CAST(strftime('%s', replace(replace(TRIM(last_accessed), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
 WHERE last_accessed IS NOT NULL
   AND typeof(last_accessed) NOT IN ('integer', 'real');

CREATE TRIGGER IF NOT EXISTS trg_documents_normalize_ts_insert
AFTER INSERT ON documents
WHEN (NEW.indexed_at IS NOT NULL AND typeof(NEW.indexed_at) NOT IN ('integer', 'real'))
  OR (NEW.last_modified IS NOT NULL AND typeof(NEW.last_modified) NOT IN ('integer', 'real'))
  OR (NEW.last_accessed IS NOT NULL AND typeof(NEW.last_accessed) NOT IN ('integer', 'real'))
BEGIN
    UPDATE documents
       SET indexed_at = CAST(TRIM(indexed_at) AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(indexed_at) = 'text'
       AND (
         (TRIM(indexed_at) GLOB '[0-9]*' AND TRIM(indexed_at) NOT GLOB '*[^0-9]*')
         OR (TRIM(indexed_at) GLOB '[0-9]*.[0-9]*'
             AND TRIM(indexed_at) NOT GLOB '*.*.*'
             AND TRIM(indexed_at) NOT GLOB '*[^0-9.]*')
       );

    UPDATE documents
       SET last_modified = CAST(TRIM(last_modified) AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(last_modified) = 'text'
       AND (
         (TRIM(last_modified) GLOB '[0-9]*' AND TRIM(last_modified) NOT GLOB '*[^0-9]*')
         OR (TRIM(last_modified) GLOB '[0-9]*.[0-9]*'
             AND TRIM(last_modified) NOT GLOB '*.*.*'
             AND TRIM(last_modified) NOT GLOB '*[^0-9.]*')
       );

    UPDATE documents
       SET last_accessed = CAST(TRIM(last_accessed) AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(last_accessed) = 'text'
       AND (
         (TRIM(last_accessed) GLOB '[0-9]*' AND TRIM(last_accessed) NOT GLOB '*[^0-9]*')
         OR (TRIM(last_accessed) GLOB '[0-9]*.[0-9]*'
             AND TRIM(last_accessed) NOT GLOB '*.*.*'
             AND TRIM(last_accessed) NOT GLOB '*[^0-9.]*')
       );

    UPDATE documents
       SET indexed_at = COALESCE(CAST(strftime('%s', replace(replace(TRIM(indexed_at), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND indexed_at IS NOT NULL
       AND typeof(indexed_at) NOT IN ('integer', 'real');

    UPDATE documents
       SET last_modified = COALESCE(CAST(strftime('%s', replace(replace(TRIM(last_modified), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND last_modified IS NOT NULL
       AND typeof(last_modified) NOT IN ('integer', 'real');

    UPDATE documents
       SET last_accessed = COALESCE(CAST(strftime('%s', replace(replace(TRIM(last_accessed), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
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
       SET indexed_at = CAST(TRIM(indexed_at) AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(indexed_at) = 'text'
       AND (
         (TRIM(indexed_at) GLOB '[0-9]*' AND TRIM(indexed_at) NOT GLOB '*[^0-9]*')
         OR (TRIM(indexed_at) GLOB '[0-9]*.[0-9]*'
             AND TRIM(indexed_at) NOT GLOB '*.*.*'
             AND TRIM(indexed_at) NOT GLOB '*[^0-9.]*')
       );

    UPDATE documents
       SET last_modified = CAST(TRIM(last_modified) AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(last_modified) = 'text'
       AND (
         (TRIM(last_modified) GLOB '[0-9]*' AND TRIM(last_modified) NOT GLOB '*[^0-9]*')
         OR (TRIM(last_modified) GLOB '[0-9]*.[0-9]*'
             AND TRIM(last_modified) NOT GLOB '*.*.*'
             AND TRIM(last_modified) NOT GLOB '*[^0-9.]*')
       );

    UPDATE documents
       SET last_accessed = CAST(TRIM(last_accessed) AS REAL)
     WHERE doc_id = NEW.doc_id
       AND typeof(last_accessed) = 'text'
       AND (
         (TRIM(last_accessed) GLOB '[0-9]*' AND TRIM(last_accessed) NOT GLOB '*[^0-9]*')
         OR (TRIM(last_accessed) GLOB '[0-9]*.[0-9]*'
             AND TRIM(last_accessed) NOT GLOB '*.*.*'
             AND TRIM(last_accessed) NOT GLOB '*[^0-9.]*')
       );

    UPDATE documents
       SET indexed_at = COALESCE(CAST(strftime('%s', replace(replace(TRIM(indexed_at), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND indexed_at IS NOT NULL
       AND typeof(indexed_at) NOT IN ('integer', 'real');

    UPDATE documents
       SET last_modified = COALESCE(CAST(strftime('%s', replace(replace(TRIM(last_modified), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND last_modified IS NOT NULL
       AND typeof(last_modified) NOT IN ('integer', 'real');

    UPDATE documents
       SET last_accessed = COALESCE(CAST(strftime('%s', replace(replace(TRIM(last_accessed), 'Z', ''), 'T', ' ')) AS REAL), 0.0)
     WHERE doc_id = NEW.doc_id
       AND last_accessed IS NOT NULL
       AND typeof(last_accessed) NOT IN ('integer', 'real');
END;

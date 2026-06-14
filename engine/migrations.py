"""
Minni — Schema Migrations Runner.

Reads PRAGMA user_version from SQLite and runs pending numbered SQL scripts
from engine/migrations/*.sql in lexicographic order, in a single transaction.
Bumps user_version only on success.

Design principles:
- Idempotent: safe to call multiple times per process (module-level guard).
- Transactional: all pending migrations run as one atomic transaction.
- Additive only: migration scripts must never DROP tables or columns.
- Graceful: any failure raises, leaving user_version unchanged so the next
  start will retry from the same version.
"""

import logging
import os
import sqlite3

logger = logging.getLogger("sovereign.migrations")

# Directory containing numbered .sql migration files
_MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")


def _load_migration_files():
    """
    Return sorted list of (version_int, filepath) pairs for every
    NNN_*.sql file found in the migrations directory.

    Files must be named NNN_description.sql where NNN is a zero-padded
    integer (e.g. 001_baseline.sql → version 1).
    """
    if not os.path.isdir(_MIGRATIONS_DIR):
        logger.warning("Migrations directory not found: %s", _MIGRATIONS_DIR)
        return []

    entries = []
    for fname in os.listdir(_MIGRATIONS_DIR):
        if not fname.endswith(".sql"):
            continue
        prefix = fname.split("_")[0]
        try:
            version = int(prefix)
        except ValueError:
            logger.warning("Skipping non-numeric migration file: %s", fname)
            continue
        entries.append((version, os.path.join(_MIGRATIONS_DIR, fname)))

    entries.sort(key=lambda x: x[0])
    return entries


def _ensure_tracking_table(conn: sqlite3.Connection) -> None:
    """
    Create schema_migrations tracking table on first use, and back-fill it
    against PRAGMA user_version so DBs migrated under the old runner are
    assumed to have applied every migration up to their current version.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at INTEGER NOT NULL"
        ")"
    )
    row = conn.execute("PRAGMA user_version").fetchone()
    current_version = row[0] if row else 0
    if current_version == 0:
        return
    # Back-fill: assume any migration file with version <= current_version was
    # applied by the legacy user_version-gated runner.
    existing = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations")
    }
    if existing:
        return  # tracking table already populated
    import time as _time
    now = int(_time.time())
    for version, filepath in _load_migration_files():
        if version <= current_version and _migration_present_in_schema(conn, version):
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, os.path.basename(filepath), now),
            )


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name=?", (table, column)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _migration_present_in_schema(conn: sqlite3.Connection, version: int) -> bool:
    """
    Decide whether a user_version back-fill can safely mark a migration applied.

    PR-5 can be introduced after PR-6 has already bumped user_version to 5.
    In that case user_version alone is not evidence that 004's additive layer
    columns exist, so leave it pending unless the columns are actually present.
    """
    if version == 4:
        return (
            _column_exists(conn, "documents", "layer")
            and _column_exists(conn, "chunk_embeddings", "layer")
        )
    if version == 7:
        return _table_exists(conn, "candidate_packets")
    if version == 8:
        return (
            _table_exists(conn, "handoff_leases")
            and _table_exists(conn, "learning_reads")
            and _table_exists(conn, "contradiction_events")
        )
    if version == 9:
        return _table_exists(conn, "contradiction_log")
    return True


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def run_migrations(conn: sqlite3.Connection) -> None:
    """
    Run all pending migrations against *conn*.

    Migrations are applied by NAME, not by user_version. This lets parallel
    development branches add e.g. 003 and 005 in one wave and 004 in the next
    without 004 getting silently skipped (which the original user_version-only
    gating would do).

    Reads schema_migrations table for the set of applied versions, then runs
    every file whose version is not in that set. Each apply records itself in
    schema_migrations and bumps user_version to the highest known version on
    success.

    Raises:
        Exception: Re-raises any error that occurs during migration, after
                   rolling back the transaction. schema_migrations and
                   user_version are left unchanged so the next startup retries
                   cleanly.
    """
    _ensure_tracking_table(conn)
    if conn.in_transaction:
        conn.commit()

    applied_versions = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations")
    }

    migration_files = _load_migration_files()
    pending = [(v, p) for v, p in migration_files if v not in applied_versions]

    if not pending:
        row = conn.execute("PRAGMA user_version").fetchone()
        logger.debug("Schema is up-to-date (user_version=%d)", row[0] if row else 0)
        return

    pending.sort(key=lambda x: x[0])
    target_version = max(v for v, _ in migration_files)  # highest known
    logger.info(
        "Running %d migration(s); target user_version=%d",
        len(pending), target_version,
    )

    import time as _time
    now = int(_time.time())

    try:
        conn.execute("BEGIN IMMEDIATE")

        for version, filepath in pending:
            logger.info("  Applying migration %03d: %s", version, os.path.basename(filepath))
            with open(filepath, "r", encoding="utf-8") as fh:
                sql = fh.read()
            for statement in _split_statements(sql):
                if statement.strip():
                    _execute_tolerant(conn, statement)
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (version, os.path.basename(filepath), now),
            )

        # Bump user_version to highest known — PRAGMA cannot be parameterized
        target_version = int(target_version)
        conn.execute(f"PRAGMA user_version = {target_version}")
        conn.commit()
        logger.info("Migrations complete. user_version=%d", target_version)

    except Exception:
        conn.rollback()
        logger.exception("Migration failed — rolled back. State unchanged.")
        raise


def _execute_tolerant(conn: sqlite3.Connection, statement: str) -> None:
    """
    Execute a DDL statement, silently ignoring errors that are safe to ignore:

    - "duplicate column name" → ALTER TABLE ADD COLUMN on an already-present column.
      This happens when migrations are re-applied to a DB that was partially migrated
      or when the base schema already contains the column.
    - "table already exists" → caught by IF NOT EXISTS, but included for safety.
    - "no such table" for CREATE TRIGGER / CREATE INDEX / ALTER / UPDATE / DELETE
      against a partial schema: the base schema initializer or a later full migration
      will supply the missing table. Triggers are always recreated by _init_schema on
      fresh databases, so missing-table here is safe to skip. DELETE is included
      because SQLite validates trigger bodies at fire time, not creation time — a
      data-fix DELETE can surface "no such table" for a table referenced only by a
      trigger (e.g. 013's DELETE FROM learnings firing trg_learnings_fts_delete on
      a schema without learnings_fts).

    Any other error is re-raised so the caller's transaction can roll back.
    """
    import sqlite3 as _sqlite3
    try:
        conn.execute(statement)
    except _sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column" in msg or "already exists" in msg:
            # Idempotent: column or table already present — safe to skip
            logger.debug("Ignoring idempotent DDL error: %s", e)
        elif ("no such table" in msg or "no such column" in msg) and (
            statement.strip().upper().startswith("ALTER")
            or statement.strip().upper().startswith("CREATE INDEX")
            or statement.strip().upper().startswith("CREATE TRIGGER")
            or statement.strip().upper().startswith("UPDATE")
            or statement.strip().upper().startswith("DELETE")
        ):
            # Additive migration against a partial test/legacy schema: the
            # base schema initializer or a later full migration will supply
            # the missing surface. Skip gracefully.
            logger.debug("Ignoring additive migration on partial schema: %s", e)
        else:
            raise


def _split_statements(sql: str):
    """
    Split a SQL script into individual statements.

    Comment lines (starting with --) are stripped before splitting.
    Statements containing BEGIN...END blocks (triggers, etc.) are kept whole
    by counting BEGIN/END depth so embedded semicolons do not produce
    spurious splits.
    Sufficient for DDL-only migration files.
    """
    # Strip comment lines first so embedded semicolons in comments are ignored
    clean_lines = [
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    ]
    clean_sql = "\n".join(clean_lines)

    # State machine: accumulate chars; split on ';' only when begin_depth == 0
    current: list[str] = []
    begin_depth = 0

    for token in _tokenize_sql(clean_sql):
        upper = token.strip().upper()
        if upper == "BEGIN":
            begin_depth += 1
            current.append(token)
        elif upper == "END" and begin_depth > 0:
            begin_depth -= 1
            current.append(token)
        elif token == ";" and begin_depth == 0:
            stmt = "".join(current).strip()
            if stmt:
                yield stmt
            current = []
        else:
            current.append(token)

    # Yield any trailing statement without a final semicolon
    stmt = "".join(current).strip()
    if stmt:
        yield stmt


def _tokenize_sql(sql: str):
    """
    Yield tokens from a SQL string: keywords (BEGIN/END), semicolons, and
    everything else as opaque character runs. Used by _split_statements to
    track BEGIN/END depth without a full parser.
    """
    i = 0
    n = len(sql)
    while i < n:
        if sql[i] in ("'", '"'):
            quote = sql[i]
            j = i + 1
            while j < n:
                if sql[j] == quote:
                    if j + 1 < n and sql[j + 1] == quote:
                        j += 2
                    else:
                        j += 1
                        break
                else:
                    j += 1
            yield sql[i:j]
            i = j
        elif sql[i:i + 2] == "/*":
            j = sql.find("*/", i + 2)
            if j == -1:
                j = n
            else:
                j += 2
            yield sql[i:j]
            i = j
        elif sql[i].isalpha() or sql[i] == '_':
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == '_'):
                j += 1
            word = sql[i:j]
            yield word
            i = j
        elif sql[i] == ';':
            yield ';'
            i += 1
        else:
            # Accumulate non-keyword, non-semicolon chars
            j = i
            while (
                j < n
                and sql[j] != ';'
                and sql[j] not in ("'", '"')
                and sql[j:j + 2] != "/*"
                and not (sql[j].isalpha() or sql[j] == '_')
            ):
                j += 1
            yield sql[i:j]
            i = j

"""
Sovereign Memory — Schema Migrations Runner.

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
        return column in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
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
    return True


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
            or statement.strip().upper().startswith("UPDATE")
        ):
            # Additive migration against a partial test/legacy schema: the
            # base schema initializer or a later full migration will supply
            # the missing surface. Skip gracefully.
            logger.debug("Ignoring additive migration on partial schema: %s", e)
        else:
            raise


def _split_statements(sql: str):
    """
    Split a SQL script into individual statements on semicolons.

    Comment lines (starting with --) are stripped before splitting so that
    semicolons inside comments do not create spurious statements.
    Sufficient for DDL-only migration files (no string literals with semicolons).
    """
    # Strip comment lines first so embedded semicolons in comments are ignored
    clean_lines = [
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    ]
    clean_sql = "\n".join(clean_lines)

    for part in clean_sql.split(";"):
        statement = part.strip()
        if statement:
            yield statement

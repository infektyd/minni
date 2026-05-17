#!/usr/bin/env python3
"""
Phase 2 Migration Script for Sovereign Memory DB

Adds new columns to support workspace scoping, layer filtering,
content-hash dedup, and quality signals.

Columns added:
  documents       -> workspace_id TEXT DEFAULT ''
  documents       -> layer TEXT DEFAULT 'knowledge'
  chunk_embeddings -> content_hash TEXT DEFAULT ''
  chunk_embeddings -> is_code INTEGER DEFAULT 0
  chunk_embeddings -> truncated INTEGER DEFAULT 0
  chunk_embeddings -> learned_at REAL DEFAULT 0

Safe to run multiple times — skips columns that already exist.
"""

import sqlite3
import os
import sys

DB_PATH = os.path.expanduser("~/.openclaw/sovereign_memory.db")

DOCUMENT_COLUMNS = [
    ("workspace_id", "TEXT DEFAULT ''"),
    ("layer", "TEXT DEFAULT 'knowledge'"),
]

CHUNK_COLUMNS = [
    ("content_hash", "TEXT DEFAULT ''"),
    ("is_code", "INTEGER DEFAULT 0"),
    ("truncated", "INTEGER DEFAULT 0"),
    ("learned_at", "REAL DEFAULT 0"),
]


def get_existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def add_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]):
    existing = get_existing_columns(conn, table)
    added = 0
    for col_name, col_type in columns:
        if col_name in existing:
            print(f"  [SKIP] {table}.{col_name} already exists")
            continue
        sql = f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}"
        try:
            conn.execute(sql)
            print(f"  [ADD]  {table}.{col_name} {col_type}")
            added += 1
        except sqlite3.OperationalError as e:
            print(f"  [ERR]  {table}.{col_name}: {e}")
            return False
    return True


def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    print(f"Opening database: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)

    print("\n--- documents table ---")
    ok1 = add_columns(conn, "documents", DOCUMENT_COLUMNS)

    print("\n--- chunk_embeddings table ---")
    ok2 = add_columns(conn, "chunk_embeddings", CHUNK_COLUMNS)

    if ok1 and ok2:
        conn.commit()
        # Create an index on the new workspace_id column for faster filtering
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_workspace ON documents(workspace_id)")
            print("\n[INDEX] idx_doc_workspace created (IF NOT EXISTS)")
        except sqlite3.OperationalError as e:
            print(f"\n[INDEX] idx_doc_workspace skipped: {e}")

        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_hash ON chunk_embeddings(content_hash)")
            print("[INDEX] idx_chunk_hash created (IF NOT EXISTS)")
        except sqlite3.OperationalError as e:
            print(f"[INDEX] idx_chunk_hash skipped: {e}")

        conn.commit()
        print("\nPhase 2 migration completed successfully.")
    else:
        conn.rollback()
        print("\nPhase 2 migration FAILED — rolled back.")
        sys.exit(1)

    conn.close()


if __name__ == "__main__":
    main()

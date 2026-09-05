"""
Tests for Minni Typed Memory Graph Substrate Schema Verifier and Migration 021.

Covers:
- TC-READY-01 through TC-READY-07 (acceptance test matrix).
- Migration 021 execution and idempotency.
- Runner integration (_migration_present_in_schema(conn, 21) and run_migrations(conn)).
- Tolerant DDL on partial schemas (skipping stamp until ready).
- Hard failure / rollback on drifted schema.
"""

import sqlite3
import pytest

from minni.graph_readiness import (
    check_graph_readiness,
    verify_graph_schema,
    SchemaVerificationReport,
    SchemaVerificationError,
)
from minni.migrations import (
    run_migrations,
    _migration_present_in_schema,
    _verify_migration_021_graph_schema,
)


def _create_baseline_schema(conn: sqlite3.Connection) -> None:
    """Create standard Minni baseline tables required prior to migration 021."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS documents (
            doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            agent TEXT DEFAULT 'unknown',
            sigil TEXT DEFAULT '❓',
            last_modified REAL,
            indexed_at REAL,
            access_count INTEGER DEFAULT 0,
            last_accessed REAL,
            decay_score REAL DEFAULT 1.0,
            whole_document INTEGER DEFAULT 0,
            page_status TEXT DEFAULT 'candidate',
            privacy_level TEXT DEFAULT 'safe',
            page_type TEXT,
            superseded_by INTEGER,
            expires_at REAL,
            evidence_refs TEXT,
            layer TEXT DEFAULT NULL
        );

        CREATE TABLE IF NOT EXISTS learnings (
            learning_id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            source_doc_ids TEXT,
            source_query TEXT,
            confidence REAL DEFAULT 1.0,
            embedding BLOB,
            created_at REAL NOT NULL,
            access_count INTEGER DEFAULT 0,
            last_accessed REAL,
            superseded_by INTEGER
        );

        CREATE TABLE IF NOT EXISTS candidate_packets (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            principal TEXT NOT NULL,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            layer TEXT,
            privacy_level TEXT,
            content TEXT NOT NULL,
            evidence_refs TEXT,
            derived_from TEXT,
            instruction_like INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'proposed',
            proposed_at REAL NOT NULL,
            resolved_at REAL,
            resolved_by TEXT,
            resolution_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS memory_links (
            source_doc_id INTEGER NOT NULL,
            target_doc_id INTEGER NOT NULL,
            link_type TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created_at REAL,
            PRIMARY KEY(source_doc_id, target_doc_id, link_type),
            FOREIGN KEY(source_doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE,
            FOREIGN KEY(target_doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS contradiction_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_a_id INTEGER,
            memory_b_id INTEGER,
            detected_at REAL NOT NULL,
            detection_method TEXT DEFAULT 'cosine',
            resolution_id INTEGER
        );
        """
    )
    conn.commit()


def _apply_migration_021_sql(conn: sqlite3.Connection) -> None:
    """Read and execute 021_typed_memory_graph.sql."""
    import os
    migrations_dir = os.path.join(os.path.dirname(__file__), "..", "src", "minni", "migrations")
    sql_path = os.path.join(migrations_dir, "021_typed_memory_graph.sql")
    with open(sql_path, "r", encoding="utf-8") as f:
        sql = f.read()
    conn.executescript(sql)
    conn.commit()


# =========================================================================
# Acceptance Matrix: TC-READY-01 through TC-READY-07
# =========================================================================


def test_composite_foreign_key_cannot_satisfy_single_column_contract():
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)
    conn.executescript("""
        DROP TABLE learning_documents;
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL,
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id),
            FOREIGN KEY (learning_id, created_at)
                REFERENCES learnings(learning_id, category) ON DELETE CASCADE
        );
    """)
    # Restore the actual required index from the migration, independent of its name.
    from minni.graph_readiness import REQUIRED_INDEXES
    for name, table, unique, columns, predicate in REQUIRED_INDEXES:
        if table == "learning_documents":
            conn.execute(f"CREATE {'UNIQUE ' if unique else ''}INDEX {name} ON {table}({', '.join(columns)})")
    report = verify_graph_schema(conn)
    assert not report.ready
    assert report.status == "schema_drifted"
    assert any("foreign key" in error for error in report.errors)
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.OperationalError, match="foreign key mismatch"):
        conn.execute("INSERT INTO learning_documents VALUES(1, 1, 0)")


def test_tc_ready_01_clean_db_ready():
    """TC-READY-01: Clean DB migrated via 021_typed_memory_graph.sql is ready."""
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    report = verify_graph_schema(conn)
    assert report.ready is True
    assert report.status == "ready"
    assert report.missing_items == []
    assert report.errors == []

    # Verify check_graph_readiness wrapper and tuple unpacking
    res = check_graph_readiness(conn)
    assert res.ready is True
    assert res.status == "ready"
    ready_bool, msg = res
    assert ready_bool is True
    assert msg == "ready"


def test_tc_ready_02_missing_learning_documents():
    """TC-READY-02: Fresh DB missing table learning_documents yields schema_missing."""
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    # documents, memory_links, contradiction_log exist, but learning_documents is absent

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_missing"
    assert "table:learning_documents" in report.missing_items

    ready_bool, msg = check_graph_readiness(conn)
    assert ready_bool is False
    assert "schema_missing" in msg
    assert "learning_documents" in msg


def test_tc_ready_03_drifted_edge_status_nullability_and_default():
    """TC-READY-03: memory_links.edge_status created without NOT NULL DEFAULT 'active' flags drift."""
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    # Recreate memory_links with nullable edge_status and wrong default
    conn.executescript(
        """
        DROP TABLE memory_links;
        CREATE TABLE memory_links (
            source_doc_id INTEGER NOT NULL,
            target_doc_id INTEGER NOT NULL,
            link_type TEXT NOT NULL,
            weight REAL DEFAULT 1.0,
            created_at REAL,
            confidence REAL,
            inference_method TEXT,
            model_id TEXT,
            prompt_version TEXT,
            inference_run_id TEXT,
            evidence_json TEXT,
            inferred_at REAL,
            edge_status TEXT DEFAULT 'pending',  -- nullable and wrong default
            PRIMARY KEY(source_doc_id, target_doc_id, link_type)
        );
        CREATE INDEX idx_memory_links_target_active
            ON memory_links(target_doc_id, edge_status, link_type, source_doc_id);
        CREATE INDEX idx_memory_links_source_active
            ON memory_links(source_doc_id, edge_status, link_type, target_doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    error_text = " ".join(report.errors)
    assert "nullability mismatch" in error_text or "default value mismatch" in error_text


def test_tc_ready_04_drifted_pk_shape_learning_documents():
    """TC-READY-04: learning_documents with 3-column PK or inverted sequence flags drift."""
    # Sub-case A: 3-column PK
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    conn.executescript(
        """
        DROP TABLE learning_documents;
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id, created_at)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("primary key shape mismatch" in err for err in report.errors)

    # Sub-case B: Swapped PK order (doc_id, learning_id)
    conn2 = sqlite3.connect(":memory:")
    _create_baseline_schema(conn2)
    _apply_migration_021_sql(conn2)

    conn2.executescript(
        """
        DROP TABLE learning_documents;
        CREATE TABLE learning_documents (
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            created_at REAL,
            PRIMARY KEY (doc_id, learning_id)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn2.commit()

    report2 = verify_graph_schema(conn2)
    assert report2.ready is False
    assert report2.status == "schema_drifted"
    assert any("primary key shape mismatch" in err for err in report2.errors)


def test_tc_ready_05_drifted_index_column_sequence():
    """TC-READY-05: idx_memory_links_target_active created with swapped column sequence flags drift."""
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    # Recreate index with swapped columns
    conn.executescript(
        """
        DROP INDEX idx_memory_links_target_active;
        CREATE INDEX idx_memory_links_target_active
            ON memory_links(source_doc_id, edge_status, link_type, target_doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("column sequence mismatch" in err for err in report.errors)


def test_tc_ready_06_drifted_missing_fk_cascade():
    """TC-READY-06: learning_documents created without ON DELETE CASCADE on doc_id flags drift."""
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    conn.executescript(
        """
        DROP TABLE learning_documents;
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id), -- missing ON DELETE CASCADE
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("ON DELETE mismatch" in err for err in report.errors)


def test_tc_ready_07_drifted_index_unique_and_partial_predicate():
    """TC-READY-07: idx_documents_memory_uri created non-unique or without WHERE clause flags drift."""
    # Sub-case A: Non-unique
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    conn.executescript(
        """
        DROP INDEX idx_documents_memory_uri;
        CREATE INDEX idx_documents_memory_uri
            ON documents(memory_uri) WHERE memory_uri IS NOT NULL;
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("uniqueness mismatch" in err for err in report.errors)

    # Sub-case B: Missing partial WHERE clause
    conn2 = sqlite3.connect(":memory:")
    _create_baseline_schema(conn2)
    _apply_migration_021_sql(conn2)

    conn2.executescript(
        """
        DROP INDEX idx_documents_memory_uri;
        CREATE UNIQUE INDEX idx_documents_memory_uri
            ON documents(memory_uri);
        """
    )
    conn2.commit()

    report2 = verify_graph_schema(conn2)
    assert report2.ready is False
    assert report2.status == "schema_drifted"
    assert any("partial predicate mismatch" in err or "not partial" in err for err in report2.errors)

    # Sub-case C: Predicate with arbitrary suffix / AND 0 to bypass uniqueness
    conn3 = sqlite3.connect(":memory:")
    _create_baseline_schema(conn3)
    _apply_migration_021_sql(conn3)

    conn3.executescript(
        """
        DROP INDEX idx_documents_memory_uri;
        CREATE UNIQUE INDEX idx_documents_memory_uri
            ON documents(memory_uri) WHERE memory_uri IS NOT NULL AND 0;
        """
    )
    conn3.commit()

    report3 = verify_graph_schema(conn3)
    assert report3.ready is False
    assert report3.status == "schema_drifted"
    assert any("partial predicate mismatch" in err for err in report3.errors)

    # Sub-case D: Normalized parenthesized predicate should be accepted
    conn4 = sqlite3.connect(":memory:")
    _create_baseline_schema(conn4)
    _apply_migration_021_sql(conn4)

    conn4.executescript(
        """
        DROP INDEX idx_documents_memory_uri;
        CREATE UNIQUE INDEX idx_documents_memory_uri
            ON documents(memory_uri) WHERE (memory_uri IS NOT NULL);
        """
    )
    conn4.commit()

    report4 = verify_graph_schema(conn4)
    assert report4.ready is True
    assert report4.status == "ready"


def test_tc_ready_drifted_fk_referencing_wrong_column():
    """Regression: FK on learning_documents referencing wrong column learnings(category) flags drift."""
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    conn.executescript(
        """
        DROP TABLE learning_documents;
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(category), -- wrong column!
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    error_text = " ".join(report.errors)
    assert "targets wrong column" in error_text or "lacks primary/unique key" in error_text


def test_tc_ready_dropping_learnings_parent_table_fails():
    """Regression: Dropping or missing learnings parent table fails readiness."""
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    # Verify initially ready
    assert verify_graph_schema(conn).ready is True

    # Drop parent table learnings
    conn.execute("DROP TABLE learnings")
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_missing"
    assert "table:learnings" in report.missing_items


def test_tc_ready_parent_column_lacking_key_semantics_fails():
    """Regression: Parent column without primary or unique key semantics flags drift."""
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    # Recreate learnings where learning_id is not a PK or UNIQUE
    conn.executescript(
        """
        DROP TABLE learning_documents;
        DROP TABLE learnings;
        CREATE TABLE learnings (
            learning_id INTEGER, -- not a PK!
            agent_id TEXT NOT NULL,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("lacks primary/unique key semantics" in err for err in report.errors)


def test_tc_ready_rejects_composite_pk_parent_key():
    """
    Regression: Parent column that is merely part of a composite primary key
    cannot be referenced by a single-column FK; flags schema_drifted and raises
    foreign key mismatch upon child insert under PRAGMA foreign_keys = ON.
    """
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    conn.executescript(
        """
        DROP TABLE learning_documents;
        DROP TABLE learnings;
        CREATE TABLE learnings (
            learning_id INTEGER NOT NULL,
            agent_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (learning_id, category)
        );
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("lacks primary/unique key semantics" in err for err in report.errors)

    # Runtime non-vacuity proof: SQLite rejects child insert under foreign_keys = ON
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO documents (doc_id, path) VALUES (1, '/path/doc')")
    conn.execute("INSERT INTO learnings (learning_id, agent_id, category, content, created_at) VALUES (1, 'a', 'c', 'txt', 1.0)")
    with pytest.raises(sqlite3.OperationalError, match='foreign key mismatch - "learning_documents" referencing "learnings"'):
        conn.execute("INSERT INTO learning_documents (learning_id, doc_id) VALUES (1, 1)")


def test_tc_ready_rejects_partial_unique_parent_key():
    """
    Regression: Parent column covered only by a partial UNIQUE index cannot satisfy
    an SQLite foreign key constraint; flags schema_drifted and raises foreign key mismatch.
    """
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    conn.executescript(
        """
        DROP TABLE learning_documents;
        DROP TABLE learnings;
        CREATE TABLE learnings (
            learning_id INTEGER,
            agent_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX idx_learnings_partial_pk
            ON learnings(learning_id) WHERE category IS NOT NULL;
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("lacks primary/unique key semantics" in err for err in report.errors)

    # Runtime non-vacuity proof: SQLite rejects child insert under foreign_keys = ON
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO documents (doc_id, path) VALUES (1, '/path/doc')")
    conn.execute("INSERT INTO learnings (learning_id, agent_id, category, content, created_at) VALUES (1, 'a', 'c', 'txt', 1.0)")
    with pytest.raises(sqlite3.OperationalError, match='foreign key mismatch - "learning_documents" referencing "learnings"'):
        conn.execute("INSERT INTO learning_documents (learning_id, doc_id) VALUES (1, 1)")


def test_tc_ready_rejects_collation_mismatched_parent_key():
    """
    Regression: Parent unique constraint with mismatched collation (e.g. NOCASE vs child BINARY)
    cannot satisfy SQLite foreign key constraint; flags schema_drifted and raises foreign key mismatch.
    """
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    conn.executescript(
        """
        DROP TABLE learning_documents;
        DROP TABLE learnings;
        CREATE TABLE learnings (
            learning_id INTEGER,
            agent_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(learning_id COLLATE NOCASE)
        );
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("lacks primary/unique key semantics" in err for err in report.errors)

    # Runtime non-vacuity proof: SQLite rejects child insert under foreign_keys = ON
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO documents (doc_id, path) VALUES (1, '/path/doc')")
    conn.execute("INSERT INTO learnings (learning_id, agent_id, category, content, created_at) VALUES (1, 'a', 'c', 'txt', 1.0)")
    with pytest.raises(sqlite3.OperationalError, match='foreign key mismatch - "learning_documents" referencing "learnings"'):
        conn.execute("INSERT INTO learning_documents (learning_id, doc_id) VALUES (1, 1)")


def test_tc_ready_child_foreign_key_insert_succeeds_on_valid_parent():
    """
    Confirm that on a clean, ready schema, child inserts succeed with PRAGMA foreign_keys = ON,
    proving that valid parent keys do not encounter foreign key mismatch.
    """
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    report = verify_graph_schema(conn)
    assert report.ready is True
    assert report.status == "ready"

    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO documents (doc_id, path) VALUES (42, '/path/doc42')")
    conn.execute(
        "INSERT INTO learnings (learning_id, agent_id, category, content, created_at) "
        "VALUES (101, 'main', 'architecture', 'graph memory pattern', 123456.0)"
    )
    conn.execute(
        "INSERT INTO learning_documents (learning_id, doc_id, created_at) VALUES (101, 42, 123456.0)"
    )
    conn.commit()

    # Query back the child row
    row = conn.execute(
        "SELECT learning_id, doc_id FROM learning_documents WHERE learning_id = 101"
    ).fetchone()
    assert row == (101, 42)


def test_tc_ready_rejects_declared_collation_mismatch_with_binary_unique_index():
    """
    Regression: Parent column declared with non-INTEGER / COLLATE NOCASE cannot be saved
    by an index declared COLLATE BINARY; the base schema contract requires exact INTEGER rowid PK,
    and SQLite rejects child insert with foreign key mismatch under PRAGMA foreign_keys = ON.
    """
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    conn.executescript(
        """
        DROP TABLE learning_documents;
        DROP TABLE learnings;
        CREATE TABLE learnings (
            learning_id TEXT COLLATE NOCASE,
            agent_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            content TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE UNIQUE INDEX lk ON learnings(learning_id COLLATE BINARY);
        CREATE TABLE learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id)
        );
        CREATE INDEX idx_learning_documents_doc_id ON learning_documents(doc_id);
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is False
    assert report.status == "schema_drifted"
    assert any("lacks primary/unique key semantics" in err for err in report.errors)

    # Runtime non-vacuity proof: SQLite rejects child insert under foreign_keys = ON
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("INSERT INTO documents (doc_id, path) VALUES (1, '/path/doc')")
    conn.execute(
        "INSERT INTO learnings (learning_id, agent_id, category, content, created_at) "
        "VALUES ('x', 'a', 'c', 'txt', 1.0)"
    )
    with pytest.raises(sqlite3.OperationalError, match='foreign key mismatch - "learning_documents" referencing "learnings"'):
        conn.execute("INSERT INTO learning_documents (learning_id, doc_id) VALUES (1, 1)")


def test_tc_ready_expression_unique_index_does_not_crash():
    """
    Regression: Expression unique indexes (where indexed column name in PRAGMA index_xinfo is None)
    must not cause an AttributeError / crash in the schema verifier.
    """
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    _apply_migration_021_sql(conn)

    # Add expression unique indexes to parent tables
    conn.executescript(
        """
        CREATE UNIQUE INDEX idx_learnings_content_expr ON learnings(lower(content));
        CREATE UNIQUE INDEX idx_documents_uri_prefix ON documents(substr(memory_uri, 1, 8));
        """
    )
    conn.commit()

    report = verify_graph_schema(conn)
    assert report.ready is True
    assert report.status == "ready"


# =========================================================================
# Migration Runner Integration & Edge Cases
# =========================================================================


def test_migration_runner_clean_full_migration(tmp_path):
    """Clean baseline database runs all migrations up to 021 and verifies ready."""
    db_path = str(tmp_path / "full.db")
    conn = sqlite3.connect(db_path)
    _create_baseline_schema(conn)

    run_migrations(conn)

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 21

    # schema_migrations must record version 21
    applied = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert 21 in applied

    # Verifier must pass
    report = verify_graph_schema(conn)
    assert report.ready is True
    assert report.status == "ready"

    # _migration_present_in_schema must be True
    assert _migration_present_in_schema(conn, 21) is True

    # Integrity check passes
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"
    conn.close()


def test_migration_runner_idempotency(tmp_path):
    """Running migrations twice consecutively is safe and leaves schema ready."""
    db_path = str(tmp_path / "idempotent.db")
    conn = sqlite3.connect(db_path)
    _create_baseline_schema(conn)

    run_migrations(conn)
    v1 = conn.execute("PRAGMA user_version").fetchone()[0]

    # Second call
    run_migrations(conn)
    v2 = conn.execute("PRAGMA user_version").fetchone()[0]
    assert v1 == v2
    assert v2 >= 21

    report = verify_graph_schema(conn)
    assert report.ready is True
    assert report.status == "ready"

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    assert integrity == "ok"
    conn.close()


def test_migration_runner_partial_schema_tolerance(tmp_path):
    """Applying migrations to a partial schema skips stamping 21 and does not falsely bump user_version to 21."""
    db_path = str(tmp_path / "partial.db")
    conn = sqlite3.connect(db_path)
    # Only create documents table (missing memory_links, contradiction_log, learnings)
    conn.execute("CREATE TABLE documents (doc_id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE)")
    conn.commit()

    run_migrations(conn)

    # 21 must NOT be stamped in schema_migrations because base tables were missing
    applied = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert 21 not in applied
    assert _migration_present_in_schema(conn, 21) is False

    # Honest user_version: user_version must match actually applied migrations (< 21)
    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    assert uv < 21
    max_applied = max(applied) if applied else 0
    assert uv == max_applied

    # Now supply missing base tables
    _create_baseline_schema(conn)

    # Run migrations again: 21 should now apply and stamp cleanly
    run_migrations(conn)

    applied_after = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert 21 in applied_after
    assert _migration_present_in_schema(conn, 21) is True
    assert verify_graph_schema(conn).ready is True
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 21
    conn.close()


def test_migration_runner_stamped_21_catches_applied_schema_drift(tmp_path):
    """Regression: If 21 is recorded in schema_migrations but schema drifts, runner raises SchemaVerificationError."""
    db_path = str(tmp_path / "stamped_drift.db")
    conn = sqlite3.connect(db_path)
    _create_baseline_schema(conn)

    run_migrations(conn)

    # Verify migration 21 was recorded
    applied = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert 21 in applied
    assert verify_graph_schema(conn).ready is True

    # Induce drift by dropping required index idx_documents_memory_uri
    conn.execute("DROP INDEX idx_documents_memory_uri")
    conn.commit()

    # run_migrations must NOT blindly trust the stamp or return 'up-to-date'; it must catch drift and raise
    with pytest.raises(SchemaVerificationError) as exc_info:
        run_migrations(conn)

    assert "Applied migration 021 schema has drifted" in str(exc_info.value)
    assert "idx_documents_memory_uri" in str(exc_info.value)
    conn.close()


def test_migration_runner_drifted_schema_raises_and_rolls_back(tmp_path):
    """If 021 results in a drifted schema during run, verification raises SchemaVerificationError."""
    db_path = str(tmp_path / "drifted.db")
    conn = sqlite3.connect(db_path)
    _create_baseline_schema(conn)

    # Pre-create learning_documents with a 3-column PK to induce drift
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS learning_documents (
            learning_id INTEGER NOT NULL REFERENCES learnings(learning_id),
            doc_id INTEGER NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
            created_at REAL,
            PRIMARY KEY (learning_id, doc_id, created_at)
        );
        """
    )
    conn.commit()

    with pytest.raises(SchemaVerificationError) as exc_info:
        run_migrations(conn)

    assert "Migration 021 verification failed" in str(exc_info.value)

    # 21 must not be in schema_migrations
    applied = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert 21 not in applied
    conn.close()


def test_migration_021_partial_schema_leaves_no_dangling_fk_and_retries(tmp_path):
    """Regression: 021 must not stamp a REFERENCES clause to a missing parent.

    Partial shape from the field: contradiction_log exists (created by
    migration 009) but documents was never created (no migration file creates
    it; db._init_schema does). The old runner executed 021's ALTERs
    tolerantly, so ADD COLUMN ... REFERENCES documents(doc_id) succeeded
    against the missing parent, and every later INSERT with foreign_keys=ON
    failed with "no such table: main.documents". The runner must skip 021's
    statements entirely (non-destructive), keep contradiction_log writable,
    leave 021 unstamped, and apply it on a later run once the base tables
    arrive — with pre-existing rows preserved.
    """
    db_path = str(tmp_path / "dangling_fk.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")

    run_migrations(conn)

    # documents is created by db._init_schema, not by any migration file.
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchone() is None
    # 021 skipped non-destructively: unstamped, user_version stays honest.
    applied = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert 21 not in applied

    # contradiction_log (from 009) must carry NO FK targeting documents ...
    fk_targets = {
        str(row[2]).lower()
        for row in conn.execute("PRAGMA foreign_key_list(contradiction_log)").fetchall()
    }
    assert "documents" not in fk_targets
    # ... so writes with FK enforcement ON succeed.
    conn.execute(
        "INSERT INTO contradiction_log (memory_a_id, memory_b_id, detected_at, detection_method) "
        "VALUES (1, 2, 0.0, 'test')"
    )
    conn.commit()

    # Base tables arrive later; retry applies 021 with data preserved.
    _create_baseline_schema(conn)
    run_migrations(conn)

    applied_after = {
        v for (v,) in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert 21 in applied_after
    assert verify_graph_schema(conn).ready is True
    assert conn.execute("SELECT COUNT(*) FROM contradiction_log").fetchone()[0] == 1
    conn.execute("INSERT INTO documents (path) VALUES ('retry-doc')")
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO contradiction_log "
        "(memory_a_id, memory_b_id, detected_at, detection_method, source_doc_id) "
        "VALUES (1, 2, 0.0, 'test', ?)",
        (doc_id,),
    )
    conn.commit()
    conn.close()


def test_migration_021_backfills_legacy_graph_rows():
    conn = sqlite3.connect(":memory:")
    _create_baseline_schema(conn)
    conn.execute("INSERT INTO documents (path) VALUES ('legacy-source')")
    source_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO documents (path) VALUES ('legacy-target')")
    target_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.executemany(
        "INSERT INTO memory_links (source_doc_id, target_doc_id, link_type) VALUES (?, ?, ?)",
        [
            (source_id, target_id, "wikilink"),
            (target_id, source_id, "derived_from"),
            (source_id, source_id, "other"),
        ],
    )
    conn.execute(
        "INSERT INTO contradiction_log "
        "(memory_a_id, memory_b_id, detected_at, detection_method, resolution_id) "
        "VALUES (1, 2, 0.0, 'legacy', NULL)"
    )
    _apply_migration_021_sql(conn)

    rows = conn.execute(
        "SELECT link_type, confidence, inference_method FROM memory_links ORDER BY link_type"
    ).fetchall()
    assert rows == [
        ("derived_from", 1.0, "writeback_evidence"),
        ("other", 1.0, "legacy"),
        ("wikilink", 1.0, "explicit_wikilink"),
    ]
    assert conn.execute(
        "SELECT resolution_status FROM contradiction_log"
    ).fetchone()[0] == "legacy_unclassified"
    conn.close()

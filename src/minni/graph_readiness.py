"""
Minni — Typed Memory Graph Substrate Schema Verifier & Runtime Readiness.

Provides a unified verifier (verify_graph_schema) and runtime readiness probe
(check_graph_readiness) to validate all 18 added/defined columns across 4 tables,
5 secondary indexes + composite PK, cascade FKs, exact names/types/nullability/defaults,
and strict composite PK shape.

Governs both:
1. Migration runner completion detection (_migration_present_in_schema(conn, 21)).
2. Runtime read/write readiness gating.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
import sqlite3
from typing import Any

logger = logging.getLogger("sovereign.graph_readiness")


class SchemaVerificationError(Exception):
    """Raised when the typed memory graph schema diverges from the normative contract."""


@dataclass
class SchemaVerificationReport:
    """Detailed verification report for typed memory graph schema."""

    ready: bool
    status: str  # 'ready', 'schema_missing', 'schema_drifted'
    errors: list[str] = field(default_factory=list)
    missing_items: list[str] = field(default_factory=list)

    def __iter__(self):
        """Enable tuple unpacking (ready, message) for backward-compatible callers."""
        yield self.ready
        if self.missing_items or self.errors:
            detail = "; ".join(self.errors) if self.errors else ", ".join(self.missing_items)
            yield f"{self.status}: {detail}"
        else:
            yield self.status

    def __getitem__(self, index: int) -> Any:
        return list(self)[index]

    def __len__(self) -> int:
        return 2


ReadinessResult = SchemaVerificationReport

# 5 Required tables (including referenced parent learnings)
REQUIRED_TABLES = [
    "documents",
    "learnings",
    "learning_documents",
    "memory_links",
    "contradiction_log",
]

# 18 Required columns across the 4 tables:
# (table, column_name, expected_type, notnull, expected_default, pk_index)
# pk_index: 0 for non-PK, 1 for 1st PK col, 2 for 2nd PK col, etc.
REQUIRED_COLUMNS: dict[str, list[tuple[str, str, int, str | None, int]]] = {
    "documents": [
        ("memory_kind", "TEXT", 0, None, 0),
        ("memory_uri", "TEXT", 0, None, 0),
    ],
    "learning_documents": [
        ("learning_id", "INTEGER", 1, None, 1),
        ("doc_id", "INTEGER", 1, None, 2),
        ("created_at", "REAL", 0, None, 0),
    ],
    "memory_links": [
        ("confidence", "REAL", 0, None, 0),
        ("inference_method", "TEXT", 0, None, 0),
        ("model_id", "TEXT", 0, None, 0),
        ("prompt_version", "TEXT", 0, None, 0),
        ("inference_run_id", "TEXT", 0, None, 0),
        ("evidence_json", "TEXT", 0, None, 0),
        ("inferred_at", "REAL", 0, None, 0),
        ("edge_status", "TEXT", 1, "active", 0),
    ],
    "contradiction_log": [
        ("source_doc_id", "INTEGER", 0, None, 0),
        ("target_doc_id", "INTEGER", 0, None, 0),
        ("edge_run_id", "TEXT", 0, None, 0),
        ("confidence", "REAL", 0, None, 0),
        ("resolution_status", "TEXT", 0, "unresolved", 0),
    ],
}

# Expected Foreign Key constraints:
# (from_col, target_table, target_col, allowed_on_delete_actions)
REQUIRED_FOREIGN_KEYS: dict[str, list[tuple[str, str, str, tuple[str, ...]]]] = {
    "learning_documents": [
        ("doc_id", "documents", "doc_id", ("CASCADE",)),
        ("learning_id", "learnings", "learning_id", ("NO ACTION", "RESTRICT")),
    ],
    "contradiction_log": [
        ("source_doc_id", "documents", "doc_id", ("SET NULL",)),
        ("target_doc_id", "documents", "doc_id", ("SET NULL",)),
    ],
}

# Pre-existing FKs the unexpected-foreign-key scan must tolerate. These are
# real schema history, not drift: migration 009 defines
# contradiction_log.resolution_id -> candidate_packets(candidate_id), so every
# database that ran migrations 001-020 carries it into 021 verification.
# Deliberately NOT in REQUIRED_FOREIGN_KEYS: presence is not required and no
# on-delete semantics are enforced — the scan only skips flagging them.
ALLOWED_EXTRA_FOREIGN_KEYS: dict[str, set[tuple[str, str, str]]] = {
    "contradiction_log": {("resolution_id", "candidate_packets", "candidate_id")},
}

# 5 Required Secondary Indexes:
# (index_name, table_name, is_unique, indexed_columns, partial_predicate)
REQUIRED_INDEXES: list[tuple[str, str, bool, list[str], str | None]] = [
    (
        "idx_documents_memory_uri",
        "documents",
        True,
        ["memory_uri"],
        "WHERE memory_uri IS NOT NULL",
    ),
    (
        "idx_learning_documents_doc_id",
        "learning_documents",
        False,
        ["doc_id"],
        None,
    ),
    (
        "idx_memory_links_target_active",
        "memory_links",
        False,
        ["target_doc_id", "edge_status", "link_type", "source_doc_id"],
        None,
    ),
    (
        "idx_memory_links_source_active",
        "memory_links",
        False,
        ["source_doc_id", "edge_status", "link_type", "target_doc_id"],
        None,
    ),
    (
        "idx_contradiction_graph_pair",
        "contradiction_log",
        False,
        ["source_doc_id", "target_doc_id", "resolution_status"],
        None,
    ),
]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _normalize_default(val: Any) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if s.upper() == "NULL":
        return None
    # Strip enclosing quotes if string literal default
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    return s


def _normalize_sql(sql: str) -> str:
    """Normalize whitespace and lower-case for predicate comparison."""
    return re.sub(r"\s+", " ", sql).strip().lower()


def _iter_check_bodies(ddl: str):
    """Yield inner text of top-level CHECK(...) constraints, nesting-aware.

    A flat ``[^()]*`` match misses the valid SQLite form
    ``CHECK(edge_status IN ('active'))`` whose IN-list carries its own
    parens, letting a restrictive lifecycle pass. Depth counting handles one
    or more nested levels; parens inside quoted literals are out of scope.
    """
    for match in re.finditer(r"\bCHECK\s*\(", ddl, re.IGNORECASE):
        depth = 0
        start = match.end()
        pos = start
        while pos < len(ddl):
            char = ddl[pos]
            if char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    yield ddl[start:pos]
                    break
                depth -= 1
            pos += 1


def _single_pk_column(conn: sqlite3.Connection, table: str) -> str | None:
    """Name the single-column PK of *table*, or None when not exactly one."""
    try:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return None
    pks = [row for row in info if row[5] > 0]
    if len(pks) != 1:
        return None
    return str(pks[0][1])


def memory_links_typed_columns_present(conn: Any) -> bool:
    """True when memory_links carries the 021 typed-edge columns writers set.

    Accepts a connection or cursor (both expose ``.execute``). Explicit-link
    writers (writeback, wiki_indexer, vault_ingest) consult this to fall back
    to the legacy 5-column insert when 021 is unavailable — db.py treats a
    failed migrations run as non-fatal, so the columns can genuinely be
    absent at write time.
    """
    try:
        rows = conn.execute("PRAGMA table_info(memory_links)").fetchall()
    except (sqlite3.Error, AttributeError):
        return False
    present = {row[1] for row in rows}
    return "confidence" in present and "inference_method" in present


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name=?", (table, column)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _is_primary_key_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Verify that target column is the standalone (single-column) primary key of table."""
    try:
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        pk_cols = [row for row in info if row[5] > 0]
        return len(pk_cols) == 1 and pk_cols[0][1].lower() == column.lower()
    except sqlite3.Error:
        return False


def _is_integer_rowid_primary_key(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """
    Verify that target column is an exact SQLite INTEGER rowid primary key:
    declared type is INTEGER, single-column PK, and not backed by an explicit
    secondary/auto PK index (which occurs in WITHOUT ROWID tables).
    """
    try:
        tinfo = conn.execute(f"PRAGMA table_info({table})").fetchall()
        col_row = None
        pk_cols = []
        for r in tinfo:
            if r[1].lower() == column.lower():
                col_row = r
            if r[5] > 0:
                pk_cols.append(r)

        if col_row is None or len(pk_cols) != 1 or pk_cols[0][1].lower() != column.lower():
            return False

        if str(col_row[2]).strip().upper() != "INTEGER":
            return False

        idx_list = conn.execute(f"PRAGMA index_list({table})").fetchall()
        for idx_row in idx_list:
            if str(idx_row[3]).lower() == "pk":
                return False

        return True
    except sqlite3.Error:
        return False


def _is_primary_or_unique_key(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check Minni's two supported parent keys, not arbitrary SQL equivalence."""
    if (table.lower(), column.lower()) not in {
        ("learnings", "learning_id"), ("documents", "doc_id")
    }:
        return False
    return _is_integer_rowid_primary_key(conn, table, column)


def _normalize_predicate(pred_str: str) -> str:
    s = _normalize_sql(pred_str)
    if s.startswith("where "):
        s = s[6:].strip()
    s = s.rstrip(";").strip()
    # Strip matching outer parens if the entire predicate is wrapped
    while s.startswith("(") and s.endswith(")"):
        inner = s[1:-1].strip()
        depth = 0
        balanced = True
        for ch in inner:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth < 0:
                balanced = False
                break
        if balanced and depth == 0:
            s = inner
        else:
            break
    return s


def check_graph_readiness(conn: sqlite3.Connection) -> SchemaVerificationReport:
    """Lightweight convenience wrapper returning SchemaVerificationReport."""
    return verify_graph_schema(conn)


def verify_graph_schema(conn: sqlite3.Connection) -> SchemaVerificationReport:
    """
    Validate the SQLite schema against the normative typed memory graph specification.

    Returns:
        SchemaVerificationReport with ready=True, status='ready' on success.
        If any required table is missing, returns status='schema_missing'.
        If tables exist but columns, types, defaults, PK shape, FK actions,
        or indexes mismatch, returns status='schema_drifted'.
    """
    missing_tables: list[str] = []
    for tbl in REQUIRED_TABLES:
        if not _table_exists(conn, tbl):
            missing_tables.append(tbl)

    if missing_tables:
        missing_items = [f"table:{tbl}" for tbl in missing_tables]
        errors = [f"table '{tbl}' missing" for tbl in missing_tables]
        return SchemaVerificationReport(
            ready=False,
            status="schema_missing",
            errors=errors,
            missing_items=missing_items,
        )

    errors: list[str] = []
    missing_items: list[str] = []

    # 1. Inspect columns across all required tables
    for tbl, cols in REQUIRED_COLUMNS.items():
        try:
            info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
        except sqlite3.Error as e:
            errors.append(f"Failed to query table_info for '{tbl}': {e}")
            continue

        # info tuple: (cid, name, type, notnull, dflt_value, pk)
        col_map = {row[1]: row for row in info}

        for col_name, exp_type, exp_notnull, exp_dflt, exp_pk in cols:
            if col_name not in col_map:
                missing_items.append(f"column:{tbl}.{col_name}")
                errors.append(f"table '{tbl}' missing column '{col_name}'")
                continue

            row = col_map[col_name]
            act_type = str(row[2]).upper()
            act_notnull = int(row[3])
            act_dflt = _normalize_default(row[4])
            act_pk = int(row[5])

            if act_type != exp_type.upper():
                errors.append(
                    f"column '{tbl}.{col_name}' declared type mismatch: expected {exp_type}, got {act_type}"
                )

            if act_pk != exp_pk:
                errors.append(
                    f"column '{tbl}.{col_name}' primary key position mismatch: expected {exp_pk}, got {act_pk}"
                )

            if act_notnull != exp_notnull:
                errors.append(
                    f"column '{tbl}.{col_name}' nullability mismatch: expected notnull={exp_notnull}, got {act_notnull}"
                )

            if act_dflt != exp_dflt:
                errors.append(
                    f"column '{tbl}.{col_name}' default value mismatch: expected {exp_dflt!r}, got {act_dflt!r}"
                )

    # edge_status must support the full lifecycle, not just its declared shape.
    try:
        ddl_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_links'"
        ).fetchone()
        ddl = str(ddl_row[0] or "") if ddl_row else ""
        for check_body in _iter_check_bodies(ddl):
            if "edge_status" not in check_body.lower():
                continue
            allowed_match = re.search(
                r"edge_status\s+IN\s*\(([^)]*)\)", check_body, re.IGNORECASE
            )
            if allowed_match:
                allowed = {
                    value.lower()
                    for value in re.findall(r"['\"]([^'\"]+)['\"]", allowed_match.group(1))
                }
                if "active" in allowed and "stale" not in allowed:
                    errors.append(
                        "table 'memory_links' CHECK constraint prevents edge_status='stale'"
                    )
            elif re.search(
                r"(?:edge_status\s*=\s*['\"]active['\"]|['\"]active['\"]\s*=\s*edge_status)",
                check_body,
                re.IGNORECASE,
            ) and not re.search(r"\bstale\b", check_body, re.IGNORECASE):
                errors.append(
                    "table 'memory_links' CHECK constraint prevents edge_status='stale'"
                )
    except sqlite3.Error as e:
        errors.append(f"Failed to inspect CHECK constraints for 'memory_links': {e}")

    # 2. Strict Composite Primary Key check for learning_documents
    try:
        ld_info = conn.execute("PRAGMA table_info(learning_documents)").fetchall()
        pk_cols = sorted([row for row in ld_info if row[5] > 0], key=lambda r: r[5])
        pk_names = [r[1] for r in pk_cols]
        expected_pk = ["learning_id", "doc_id"]
        if len(pk_names) != 2 or pk_names != expected_pk:
            errors.append(
                f"table 'learning_documents' primary key shape mismatch: expected {expected_pk}, got {pk_names}"
            )
    except sqlite3.Error as e:
        errors.append(f"Failed to inspect PK for 'learning_documents': {e}")

    # memory_links retains the baseline edge identity used by triple-key upserts.
    try:
        link_info = conn.execute("PRAGMA table_info(memory_links)").fetchall()
        link_pk_cols = sorted(
            [row for row in link_info if row[5] > 0], key=lambda r: r[5]
        )
        expected_link_pk = ["source_doc_id", "target_doc_id", "link_type"]
        link_pk_names = [row[1] for row in link_pk_cols]
        if link_pk_names != expected_link_pk:
            errors.append(
                f"table 'memory_links' primary key shape mismatch: expected "
                f"{expected_link_pk}, got {link_pk_names}"
            )
    except sqlite3.Error as e:
        errors.append(f"Failed to inspect PK for 'memory_links': {e}")

    # 3. Foreign Key Constraints & Referenced Parent Semantics check
    for tbl, fks in REQUIRED_FOREIGN_KEYS.items():
        try:
            fk_list = conn.execute(f"PRAGMA foreign_key_list({tbl})").fetchall()
        except sqlite3.Error as e:
            errors.append(f"Failed to inspect FK list for '{tbl}': {e}")
            continue

        # fk_list tuple: (id, seq, table, from, to, on_update, on_delete, match)
        expected_signatures = {
            (from_col.lower(), target_tbl.lower(), target_col.lower())
            for from_col, target_tbl, target_col, _ in fks
        } | ALLOWED_EXTRA_FOREIGN_KEYS.get(tbl, set())
        for row in fk_list:
            # An omitted FK target (REFERENCES parent with no column) is valid
            # SQLite meaning the parent's PK: resolve it to the actual single
            # PK column so semantic matching below judges it, instead of
            # rejecting a NULL target here. Unresolvable targets stay None and
            # are still flagged — strictness is preserved, not broadened.
            to_col = row[4]
            if to_col is None:
                to_col = _single_pk_column(conn, str(row[2]))
            signature = (
                str(row[3]).lower(),
                str(row[2]).lower(),
                str(to_col).lower() if to_col is not None else None,
            )
            if row[1] != 0 or signature not in expected_signatures:
                errors.append(
                    f"table '{tbl}' contains unexpected foreign key: "
                    f"{row[3]} -> {row[2]}({row[4]})"
                )

        for from_col, target_tbl, target_col, allowed_on_delete in fks:
            # First, validate referenced parent table exists
            if not _table_exists(conn, target_tbl):
                errors.append(
                    f"table '{tbl}' foreign key references missing parent table '{target_tbl}'"
                )
                missing_items.append(f"table:{target_tbl}")
                continue

            # Validate target column exists on parent table
            if not _column_exists(conn, target_tbl, target_col):
                errors.append(
                    f"table '{tbl}' foreign key references missing column '{target_tbl}.{target_col}'"
                )
                missing_items.append(f"column:{target_tbl}.{target_col}")
                continue

            # Validate target column has valid key semantics (PK or UNIQUE)
            if not _is_primary_or_unique_key(conn, target_tbl, target_col):
                errors.append(
                    f"referenced parent column '{target_tbl}.{target_col}' lacks primary/unique key semantics"
                )
                continue

            matching = [
                row for row in fk_list
                if row[1] == 0
                and sum(other[0] == row[0] for other in fk_list) == 1
                and str(row[3]).lower() == from_col.lower()
                and str(row[2]).lower() == target_tbl.lower()
                and (
                    (row[4] is not None and str(row[4]).lower() == target_col.lower())
                    or (row[4] is None and _is_primary_key_column(conn, target_tbl, target_col))
                )
            ]
            if not matching:
                # Find partial matches to provide detailed diagnostic
                partial_matches = [
                    row for row in fk_list
                    if str(row[3]).lower() == from_col.lower() and str(row[2]).lower() == target_tbl.lower()
                ]
                if partial_matches:
                    act_cols = [str(r[4]) for r in partial_matches]
                    errors.append(
                        f"table '{tbl}' foreign key '{from_col}' -> {target_tbl} targets wrong column: "
                        f"expected '{target_col}', got {act_cols}"
                    )
                else:
                    errors.append(
                        f"table '{tbl}' missing foreign key: {from_col} -> {target_tbl}({target_col})"
                    )
                continue

            fk_row = matching[0]
            act_on_delete = str(fk_row[6]).upper()
            if act_on_delete not in allowed_on_delete:
                errors.append(
                    f"table '{tbl}' foreign key {from_col} -> {target_tbl}({target_col}) ON DELETE mismatch: "
                    f"expected one of {allowed_on_delete}, got '{act_on_delete}'"
                )

    # 4. Secondary Indexes check
    for idx_name, tbl, exp_unique, exp_cols, exp_partial in REQUIRED_INDEXES:
        try:
            idx_list = conn.execute(f"PRAGMA index_list({tbl})").fetchall()
        except sqlite3.Error as e:
            errors.append(f"Failed to inspect index_list for '{tbl}': {e}")
            continue

        # idx_list tuple: (seq, name, unique, origin, partial)
        matching_idx = [row for row in idx_list if row[1] == idx_name]
        if not matching_idx:
            missing_items.append(f"index:{idx_name}")
            errors.append(f"table '{tbl}' missing index '{idx_name}'")
            continue

        idx_row = matching_idx[0]
        act_unique = bool(idx_row[2])
        act_partial = bool(idx_row[4])

        if act_unique != exp_unique:
            errors.append(
                f"index '{idx_name}' uniqueness mismatch: expected unique={exp_unique}, got {act_unique}"
            )

        # Inspect indexed columns sequence
        try:
            info_rows = conn.execute(f"PRAGMA index_info({idx_name})").fetchall()
            sorted_info = sorted(info_rows, key=lambda r: r[0])
            act_cols = [r[2] for r in sorted_info]
            if act_cols != exp_cols:
                errors.append(
                    f"index '{idx_name}' column sequence mismatch: expected {exp_cols}, got {act_cols}"
                )

            if idx_name == "idx_documents_memory_uri":
                xinfo_rows = conn.execute(f"PRAGMA index_xinfo({idx_name})").fetchall()
                indexed_xinfo = sorted(
                    (row for row in xinfo_rows if row[5]), key=lambda row: row[0]
                )
                act_collations = [str(row[4]).upper() for row in indexed_xinfo]
                if act_collations != ["BINARY"]:
                    errors.append(
                        f"index '{idx_name}' collation mismatch: expected ['BINARY'], "
                        f"got {act_collations}"
                    )
        except sqlite3.Error as e:
            errors.append(f"Failed to query index_info for '{idx_name}': {e}")

        # Check partial predicate if specified
        if exp_partial is not None:
            if not act_partial:
                errors.append(
                    f"index '{idx_name}' expected partial predicate ({exp_partial}), but index is not partial"
                )
            else:
                try:
                    sql_row = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                        (idx_name,),
                    ).fetchone()
                    raw_sql = sql_row[0] if sql_row and sql_row[0] else ""
                    match = re.search(r"\bwhere\s+(.*)$", raw_sql, re.IGNORECASE | re.DOTALL)
                    if not match:
                        errors.append(
                            f"index '{idx_name}' expected partial predicate '{exp_partial}', but no WHERE clause found in DDL '{raw_sql}'"
                        )
                    else:
                        act_pred = _normalize_predicate(match.group(1))
                        exp_pred = _normalize_predicate(exp_partial)
                        if act_pred != exp_pred:
                            errors.append(
                                f"index '{idx_name}' partial predicate mismatch: expected '{exp_partial}', got '{match.group(1).strip()}'"
                            )
                except sqlite3.Error as e:
                    errors.append(f"Failed to inspect index DDL for '{idx_name}': {e}")
        else:
            if act_partial:
                errors.append(
                    f"index '{idx_name}' is unexpectedly partial"
                )

    # learning_documents is an N:1 mapping: multiple learnings may share a doc.
    try:
        for idx_row in conn.execute("PRAGMA index_list(learning_documents)").fetchall():
            if not bool(idx_row[2]):
                continue
            index_name = idx_row[1]
            index_info = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
            index_columns = [row[2] for row in sorted(index_info, key=lambda r: r[0])]
            if index_columns == ["doc_id"]:
                errors.append(
                    "table 'learning_documents' has unexpected unique constraint on 'doc_id'"
                )
    except sqlite3.Error as e:
        errors.append("Failed to inspect unique constraints for 'learning_documents': "
                      f"{e}")

    # Any additional UNIQUE constraint touching memory_uri can reject distinct
    # URIs and make the graph unusable, even when the required index is valid.
    try:
        for idx_row in conn.execute("PRAGMA index_list(documents)").fetchall():
            index_name = str(idx_row[1])
            if not bool(idx_row[2]) or index_name == "idx_documents_memory_uri":
                continue
            index_info = conn.execute(f"PRAGMA index_info({index_name})").fetchall()
            index_columns = [row[2] for row in sorted(index_info, key=lambda r: r[0])]
            index_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            if "memory_uri" in index_columns or (
                index_sql and index_sql[0] and "memory_uri" in index_sql[0].lower()
            ):
                errors.append(
                    f"table 'documents' has unexpected unique constraint involving 'memory_uri': "
                    f"{index_name}"
                )
    except sqlite3.Error as e:
        errors.append("Failed to inspect unique constraints for 'documents': " f"{e}")

    if errors or missing_items:
        return SchemaVerificationReport(
            ready=False,
            status="schema_drifted",
            errors=errors,
            missing_items=missing_items,
        )

    return SchemaVerificationReport(
        ready=True,
        status="ready",
        errors=[],
        missing_items=[],
    )

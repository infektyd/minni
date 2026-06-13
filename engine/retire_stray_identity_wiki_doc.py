#!/usr/bin/env python3
"""
One-time cleanup: remove wiki-layer duplicates of whole-document identity envelopes.

The auto-index cron can write *_HOSTED_AGENT_ENVELOPE.md files into
~/wiki/auto-indexed/, which the wiki indexer then ingests as knowledge-layer
candidates. Those rows compete in recall against the authoritative identity
docs (whole_document=1, agent=identity:*). This script deletes the stray wiki
rows and removes matching files from auto-indexed/.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = os.path.expanduser("~/.minni/sovereign.db")
DEFAULT_AUTO_INDEXED = os.path.expanduser("~/wiki/auto-indexed")


def _basename_upper(path: str) -> str:
    return Path(path).stem.upper()


def find_stray_wiki_docs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT w.doc_id, w.path, w.agent
        FROM documents w
        WHERE w.agent LIKE 'wiki:%'
          AND EXISTS (
            SELECT 1 FROM documents i
            WHERE i.whole_document = 1
              AND i.agent LIKE 'identity:%'
              AND UPPER(i.path) LIKE '%' || UPPER(substr(w.path, instr(w.path, '/') + 1)) || '%'
          )
        """
    ).fetchall()

    # Fallback: basename match (handles differing directory prefixes).
    if rows:
        return rows

    wiki_rows = conn.execute(
        "SELECT doc_id, path, agent FROM documents WHERE agent LIKE 'wiki:%'"
    ).fetchall()
    stray: list[sqlite3.Row] = []
    for row in wiki_rows:
        base = _basename_upper(row["path"])
        hit = conn.execute(
            """
            SELECT 1 FROM documents
            WHERE whole_document = 1
              AND agent LIKE 'identity:%'
              AND UPPER(path) LIKE ?
            """,
            (f"%{base}%",),
        ).fetchone()
        if hit:
            stray.append(row)
    return stray


def delete_wiki_doc(conn: sqlite3.Connection, doc_id: int) -> None:
    conn.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
    conn.execute(
        "DELETE FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
        (doc_id, doc_id),
    )
    conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))


def remove_auto_indexed_file(path: str, auto_indexed_root: str) -> bool:
    if "auto-indexed" not in path:
        return False
    basename = os.path.basename(path)
    candidate = os.path.join(auto_indexed_root, basename)
    if os.path.isfile(candidate):
        os.remove(candidate)
        return True
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def retire_stray_identity_wiki_docs(
    db_path: str = DEFAULT_DB,
    auto_indexed_root: str = DEFAULT_AUTO_INDEXED,
    dry_run: bool = False,
) -> dict:
    if not os.path.isfile(db_path):
        return {"status": "error", "message": f"DB not found: {db_path}"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        stray = find_stray_wiki_docs(conn)
        deleted_ids: list[int] = []
        removed_files: list[str] = []

        for row in stray:
            doc_id = row["doc_id"]
            doc_path = row["path"]
            if dry_run:
                deleted_ids.append(doc_id)
                candidate = os.path.join(auto_indexed_root, os.path.basename(doc_path))
                if os.path.isfile(candidate):
                    removed_files.append(candidate)
                elif os.path.isfile(doc_path):
                    removed_files.append(doc_path)
                continue

            delete_wiki_doc(conn, doc_id)
            deleted_ids.append(doc_id)
            if remove_auto_indexed_file(doc_path, auto_indexed_root):
                removed_files.append(
                    os.path.join(auto_indexed_root, os.path.basename(doc_path))
                    if os.path.isfile(os.path.join(auto_indexed_root, os.path.basename(doc_path)))
                    else doc_path
                )

        if not dry_run:
            conn.commit()

        return {
            "status": "success",
            "dry_run": dry_run,
            "deleted_doc_ids": deleted_ids,
            "removed_files": removed_files,
            "count": len(deleted_ids),
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to sovereign.db")
    parser.add_argument(
        "--auto-indexed",
        default=DEFAULT_AUTO_INDEXED,
        help="auto-indexed wiki directory to clean files from",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not delete")
    args = parser.parse_args(argv)

    result = retire_stray_identity_wiki_docs(
        db_path=args.db,
        auto_indexed_root=args.auto_indexed,
        dry_run=args.dry_run,
    )
    print(result)
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
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
import json
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = os.path.expanduser("~/.minni/minni.db")
DEFAULT_AUTO_INDEXED = os.path.expanduser("~/wiki/auto-indexed")
DEFAULT_TARGET_DOC_IDS = frozenset({829, 836, 865})


def _basename_upper(path: str) -> str:
    return Path(path).stem.upper()


def _is_identity_envelope_path(path: str) -> bool:
    basename = _basename_upper(path)
    return "HOSTED_AGENT_ENVELOPE" in basename or "AGENT_ENVELOPE" in basename


def find_stray_wiki_docs(
    conn: sqlite3.Connection,
    target_doc_ids: set[int] | frozenset[int] | None = DEFAULT_TARGET_DOC_IDS,
) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    identity_rows = conn.execute(
        "SELECT path FROM documents WHERE whole_document = 1 AND agent LIKE 'identity:%'"
    ).fetchall()
    identity_stems = {
        _basename_upper(row["path"])
        for row in identity_rows
        if _is_identity_envelope_path(row["path"])
    }

    if target_doc_ids is None:
        wiki_rows = conn.execute(
            "SELECT doc_id, path, agent FROM documents WHERE agent LIKE 'wiki:%'"
        ).fetchall()
    else:
        ids = sorted(int(doc_id) for doc_id in target_doc_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        wiki_rows = conn.execute(
            f"""SELECT doc_id, path, agent FROM documents
                WHERE agent LIKE 'wiki:%' AND doc_id IN ({placeholders})""",
            ids,
        ).fetchall()

    stray: list[sqlite3.Row] = []
    for row in wiki_rows:
        if (
            _is_identity_envelope_path(row["path"])
            and _basename_upper(row["path"]) in identity_stems
        ):
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


def remove_auto_indexed_file(path: str, auto_indexed_root: str) -> str | None:
    if "auto-indexed" not in path:
        return None
    basename = os.path.basename(path)
    candidate = os.path.join(auto_indexed_root, basename)
    if os.path.isfile(candidate):
        os.remove(candidate)
        return candidate
    if os.path.isfile(path):
        os.remove(path)
        return path
    return None


def retire_stray_identity_wiki_docs(
    db_path: str = DEFAULT_DB,
    auto_indexed_root: str = DEFAULT_AUTO_INDEXED,
    dry_run: bool = False,
    target_doc_ids: set[int] | frozenset[int] | None = DEFAULT_TARGET_DOC_IDS,
) -> dict:
    if not os.path.isfile(db_path):
        return {"status": "error", "message": f"DB not found: {db_path}"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        stray = find_stray_wiki_docs(conn, target_doc_ids=target_doc_ids)
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
            removed_file = remove_auto_indexed_file(doc_path, auto_indexed_root)
            if removed_file:
                removed_files.append(removed_file)

        if not dry_run:
            conn.commit()

        return {
            "status": "success",
            "dry_run": dry_run,
            "target_doc_ids": sorted(target_doc_ids) if target_doc_ids is not None else None,
            "deleted_doc_ids": deleted_ids,
            "removed_files": removed_files,
            "count": len(deleted_ids),
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help="Path to minni.db")
    parser.add_argument(
        "--auto-indexed",
        default=DEFAULT_AUTO_INDEXED,
        help="auto-indexed wiki directory to clean files from",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not delete")
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--doc-id",
        action="append",
        type=int,
        dest="doc_ids",
        help="Exact wiki document id to retire. Repeat to override the default 829/836/865 target set.",
    )
    target_group.add_argument(
        "--all-matching",
        action="store_true",
        help="Retire every matching wiki identity-envelope duplicate instead of only doc_ids 829/836/865",
    )
    args = parser.parse_args(argv)

    result = retire_stray_identity_wiki_docs(
        db_path=args.db,
        auto_indexed_root=args.auto_indexed,
        dry_run=args.dry_run,
        target_doc_ids=None
        if args.all_matching
        else frozenset(args.doc_ids or DEFAULT_TARGET_DOC_IDS),
    )
    print(json.dumps(result, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())

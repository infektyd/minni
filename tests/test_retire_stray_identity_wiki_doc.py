import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _create_documents_table(conn):
    conn.execute(
        """CREATE TABLE documents (
           doc_id INTEGER PRIMARY KEY,
           path TEXT,
           agent TEXT,
           whole_document INTEGER DEFAULT 0
        )"""
    )


def _seed_identity_and_wiki_rows(conn):
    conn.executemany(
        """INSERT INTO documents(doc_id, path, agent, whole_document)
           VALUES (?, ?, ?, ?)""",
        [
            (101, "/identities/CODEX_HOSTED_AGENT_ENVELOPE.md", "identity:codex", 1),
            (102, "/identities/CLAUDE-CODE_HOSTED_AGENT_ENVELOPE.md", "identity:claude-code", 1),
            (103, "/identities/GEMINI_HOSTED_AGENT_ENVELOPE.md", "identity:gemini", 1),
            (829, "/wiki/auto-indexed/CLAUDE-CODE_HOSTED_AGENT_ENVELOPE.md", "wiki:unknown", 0),
            (836, "/wiki/auto-indexed/GEMINI_HOSTED_AGENT_ENVELOPE.md", "wiki:unknown", 0),
            (865, "/wiki/auto-indexed/CODEX_HOSTED_AGENT_ENVELOPE.md", "wiki:unknown", 0),
            (837, "/wiki/AGENTS.md", "wiki:unknown", 0),
            (900, "/wiki/auto-indexed/CODEX_HOSTED_AGENT_ENVELOPE.md", "wiki:codex", 0),
        ],
    )


def test_find_stray_wiki_docs_retires_only_exact_identity_doc_ids_by_default():
    from minni.retire_stray_identity_wiki_doc import find_stray_wiki_docs

    conn = sqlite3.connect(":memory:")
    _create_documents_table(conn)
    _seed_identity_and_wiki_rows(conn)

    rows = find_stray_wiki_docs(conn)

    assert [(row["doc_id"], row["path"]) for row in rows] == [
        (829, "/wiki/auto-indexed/CLAUDE-CODE_HOSTED_AGENT_ENVELOPE.md"),
        (836, "/wiki/auto-indexed/GEMINI_HOSTED_AGENT_ENVELOPE.md"),
        (865, "/wiki/auto-indexed/CODEX_HOSTED_AGENT_ENVELOPE.md"),
    ]


def test_find_stray_wiki_docs_all_matching_is_explicit_escape_hatch():
    from minni.retire_stray_identity_wiki_doc import find_stray_wiki_docs

    conn = sqlite3.connect(":memory:")
    _create_documents_table(conn)
    _seed_identity_and_wiki_rows(conn)

    rows = find_stray_wiki_docs(conn, target_doc_ids=None)

    assert [row["doc_id"] for row in rows] == [829, 836, 865, 900]


def test_retire_stray_identity_wiki_docs_deletes_rows_links_and_files(tmp_path):
    from minni.retire_stray_identity_wiki_doc import retire_stray_identity_wiki_docs

    db_path = tmp_path / "minni.db"
    auto_indexed = tmp_path / "auto-indexed"
    auto_indexed.mkdir()
    for name in (
        "CLAUDE-CODE_HOSTED_AGENT_ENVELOPE.md",
        "GEMINI_HOSTED_AGENT_ENVELOPE.md",
        "CODEX_HOSTED_AGENT_ENVELOPE.md",
    ):
        (auto_indexed / name).write_text("# duplicate\n", encoding="utf-8")

    conn = sqlite3.connect(db_path)
    _create_documents_table(conn)
    _seed_identity_and_wiki_rows(conn)
    conn.execute("CREATE TABLE chunk_embeddings (doc_id INTEGER)")
    conn.execute("CREATE TABLE vault_fts (doc_id INTEGER)")
    conn.execute("CREATE TABLE memory_links (source_doc_id INTEGER, target_doc_id INTEGER)")
    conn.executemany("INSERT INTO chunk_embeddings(doc_id) VALUES (?)", [(829,), (836,), (865,), (900,)])
    conn.executemany("INSERT INTO vault_fts(doc_id) VALUES (?)", [(829,), (836,), (865,), (900,)])
    conn.executemany(
        "INSERT INTO memory_links(source_doc_id, target_doc_id) VALUES (?, ?)",
        [(829, 900), (837, 865), (900, 101)],
    )
    conn.commit()
    conn.close()

    result = retire_stray_identity_wiki_docs(
        db_path=str(db_path),
        auto_indexed_root=str(auto_indexed),
    )

    assert result["deleted_doc_ids"] == [829, 836, 865]
    assert {Path(path).name for path in result["removed_files"]} == {
        "CLAUDE-CODE_HOSTED_AGENT_ENVELOPE.md",
        "GEMINI_HOSTED_AGENT_ENVELOPE.md",
        "CODEX_HOSTED_AGENT_ENVELOPE.md",
    }

    conn = sqlite3.connect(db_path)
    remaining_doc_ids = [
        row[0] for row in conn.execute("SELECT doc_id FROM documents ORDER BY doc_id")
    ]
    assert 837 in remaining_doc_ids
    assert 900 in remaining_doc_ids
    assert 829 not in remaining_doc_ids
    assert list(conn.execute("SELECT doc_id FROM chunk_embeddings WHERE doc_id IN (829,836,865)")) == []
    assert list(conn.execute("SELECT doc_id FROM vault_fts WHERE doc_id IN (829,836,865)")) == []
    assert list(conn.execute("SELECT source_doc_id, target_doc_id FROM memory_links WHERE source_doc_id IN (829,836,865) OR target_doc_id IN (829,836,865)")) == []
    conn.close()


def test_cli_rejects_doc_id_with_all_matching(capsys):
    from minni.retire_stray_identity_wiki_doc import main

    with pytest.raises(SystemExit) as exc:
        main(["--doc-id", "829", "--all-matching", "--dry-run"])

    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(__file__))


def test_find_stray_wiki_docs_requires_exact_filename_stem_match():
    from retire_stray_identity_wiki_doc import find_stray_wiki_docs

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE documents (
           doc_id INTEGER PRIMARY KEY,
           path TEXT,
           agent TEXT,
           whole_document INTEGER DEFAULT 0
        )"""
    )
    conn.execute(
        """INSERT INTO documents(path, agent, whole_document)
           VALUES ('/identities/CODEX_HOSTED_AGENT_ENVELOPE.md', 'identity:codex', 1)"""
    )
    conn.execute(
        """INSERT INTO documents(path, agent, whole_document)
           VALUES ('/wiki/envelope.md', 'wiki:codex', 0)"""
    )
    conn.execute(
        """INSERT INTO documents(path, agent, whole_document)
           VALUES ('/wiki/CODEX_HOSTED_AGENT_ENVELOPE.md', 'wiki:codex', 0)"""
    )

    rows = find_stray_wiki_docs(conn)

    assert [row["path"] for row in rows] == ["/wiki/CODEX_HOSTED_AGENT_ENVELOPE.md"]

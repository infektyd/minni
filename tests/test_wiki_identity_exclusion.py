"""Wiki indexer must not ingest whole-document identity envelope duplicates."""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    db_path = str(tmp_path / "test.db")
    cfg = SovereignConfig(db_path=db_path)

    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    return db_obj, cfg


def _seed_identity_doc(conn, identity_path: str, agent_id: str = "claude-code") -> int:
    import numpy as np

    now = time.time()
    conn.execute(
        """INSERT INTO documents
           (path, agent, sigil, last_modified, indexed_at, whole_document, layer)
           VALUES (?, ?, ?, ?, ?, 1, 'identity')""",
        (identity_path, f"identity:{agent_id}", "🤖", now, now),
    )
    doc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    emb_bytes = np.zeros(384, dtype="float32").tobytes()
    conn.execute(
        """INSERT INTO chunk_embeddings
           (doc_id, chunk_index, chunk_text, embedding, model_name, computed_at)
           VALUES (?, 0, ?, ?, 'test', ?)""",
        (doc_id, "whole identity envelope body", emb_bytes, now),
    )
    conn.execute(
        """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
           VALUES (?, ?, ?, ?, ?)""",
        (doc_id, identity_path, "whole identity envelope body", f"identity:{agent_id}", "🤖"),
    )
    conn.commit()
    return doc_id


class TestWikiIdentityExclusion:
    def test_skips_envelope_when_identity_whole_doc_exists(self, tmp_path):
        from minni.wiki_indexer import WikiIndexer

        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()

        identity_path = str(
            tmp_path / "identities" / "claude-code" / "FAKEAGENT_HOSTED_AGENT_ENVELOPE.md"
        )
        os.makedirs(os.path.dirname(identity_path), exist_ok=True)
        with open(identity_path, "w", encoding="utf-8") as f:
            f.write("# Identity envelope\nminni_layer_mode: hosted_agent_envelope\n")
        _seed_identity_doc(conn, identity_path)

        wiki_dir = tmp_path / "wiki" / "auto-indexed"
        wiki_dir.mkdir(parents=True)
        envelope_copy = wiki_dir / "FAKEAGENT_HOSTED_AGENT_ENVELOPE.md"
        envelope_copy.write_text(
            "---\ntitle: Fake Envelope\nstatus: candidate\nprivacy: safe\ntype: concept\n"
            "---\n\nminni_layer_mode: hosted_agent_envelope\nDuplicated identity body.\n",
            encoding="utf-8",
        )

        indexer = WikiIndexer(db=db_obj, config=cfg)
        stats = indexer.index_wiki(str(wiki_dir.parent))

        assert stats["indexed"] == 0
        assert stats["skipped"] >= 1

        wiki_rows = conn.execute(
            "SELECT doc_id FROM documents WHERE agent LIKE 'wiki:%' AND path = ?",
            (str(envelope_copy),),
        ).fetchall()
        assert wiki_rows == []

        identity_rows = conn.execute(
            "SELECT doc_id FROM documents WHERE agent = 'identity:claude-code'"
        ).fetchall()
        assert len(identity_rows) == 1

        db_obj.close()

    def test_skips_auto_indexed_envelope_filename_without_identity_seed(self, tmp_path):
        from minni.wiki_indexer import WikiIndexer

        db_obj, cfg = _make_db(tmp_path)

        wiki_dir = tmp_path / "wiki" / "auto-indexed"
        wiki_dir.mkdir(parents=True)
        envelope_copy = wiki_dir / "ORPHAN_HOSTED_AGENT_ENVELOPE.md"
        envelope_copy.write_text(
            "---\ntitle: Orphan Envelope\nstatus: candidate\nprivacy: safe\ntype: concept\n"
            "---\n\nminni_layer_mode: hosted_agent_envelope\n",
            encoding="utf-8",
        )

        indexer = WikiIndexer(db=db_obj, config=cfg)
        stats = indexer.index_wiki(str(wiki_dir.parent))

        assert stats["indexed"] == 0
        assert stats["skipped"] >= 1
        db_obj.close()

    def test_does_not_skip_substring_filename_collision(self, tmp_path):
        from minni.wiki_indexer import WikiIndexer

        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()

        identity_path = str(tmp_path / "identities" / "CODEX_HOSTED_AGENT_ENVELOPE.md")
        os.makedirs(os.path.dirname(identity_path), exist_ok=True)
        _seed_identity_doc(conn, identity_path)

        wiki_dir = tmp_path / "wiki"
        wiki_dir.mkdir(parents=True)
        note = wiki_dir / "agent.md"
        note.write_text(
            "---\ntitle: Agent Note\nstatus: accepted\nprivacy: safe\ntype: concept\n"
            "---\n\n"
            + " ".join(f"agent-note-{i}" for i in range(90)),
            encoding="utf-8",
        )

        indexer = WikiIndexer(db=db_obj, config=cfg)
        stats = indexer.index_wiki(str(wiki_dir))

        assert stats["indexed"] == 1
        row = conn.execute(
            "SELECT doc_id FROM documents WHERE agent LIKE 'wiki:%' AND path = ?",
            (str(note),),
        ).fetchone()
        assert row is not None
        db_obj.close()

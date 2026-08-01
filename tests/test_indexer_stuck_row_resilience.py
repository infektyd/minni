"""Audit R0 (grok-review on PR #242): a poisoned ``last_modified`` must not
permanently stick a row.

``indexer.VaultIndexer.index_vault`` and ``wiki_indexer.WikiIndexer.index_wiki``
both compare ``row["last_modified"] >= mtime`` to decide whether a file needs
reindexing. ``last_modified`` is the same REAL-affinity column class as
``indexed_at`` (audit R0's headline bug) — an out-of-tree writer could poison
it with TEXT. Before this fix, that comparison raised ``TypeError``, which the
per-file ``except Exception`` turned into a permanent ``stats["errors"]``: the
row was never rewritten, so it stayed stuck on every subsequent run.

The fix treats an unparseable ``last_modified`` as older than any real mtime
(default 0.0), so the row is reindexed — and thereby repaired — instead of
stuck.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

POISON_GARBAGE = "not a timestamp"


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


def _poison_last_modified(conn, path: str) -> None:
    """Insert a row the way a pre-fix (or out-of-tree) writer did, with the
    normalizing triggers out of the way — the read path must hold on its own."""
    conn.execute("DROP TRIGGER IF EXISTS trg_documents_normalize_ts_insert")
    conn.execute("DROP TRIGGER IF EXISTS trg_documents_normalize_ts_update")
    conn.execute(
        "INSERT INTO documents (path, last_modified, indexed_at) VALUES (?, ?, ?)",
        (path, POISON_GARBAGE, time.time() - 86400),
    )
    conn.commit()


def test_vault_indexer_reindexes_instead_of_sticking_on_poisoned_last_modified(tmp_path):
    from minni.config import SovereignConfig
    from minni.indexer import VaultIndexer

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    doc_path = vault_dir / "note.md"
    doc_path.write_text("---\nagent: claude-code\n---\n\nHello world.\n", encoding="utf-8")

    db_obj, cfg = _make_db(tmp_path)
    conn = db_obj._get_conn()
    _poison_last_modified(conn, str(doc_path.resolve()))

    cfg2 = SovereignConfig(db_path=cfg.db_path, vault_path=str(vault_dir))
    indexer = VaultIndexer(db=db_obj, config=cfg2)
    stats = indexer.index_vault()

    assert stats.get("errors", 0) == 0, "poisoned row must not raise, only reindex"
    assert stats["indexed"] >= 1, "the stuck row must have been reindexed, not skipped forever"

    row = conn.execute(
        "SELECT typeof(last_modified) t, last_modified FROM documents WHERE path = ?",
        (str(doc_path.resolve()),),
    ).fetchone()
    assert row["t"] in ("real", "integer"), "the repair must have overwritten the poison"


def test_wiki_indexer_reindexes_instead_of_sticking_on_poisoned_last_modified(tmp_path):
    from minni.wiki_indexer import WikiIndexer
    from minni.config import SovereignConfig

    wiki_dir = tmp_path / "wiki" / "auto-indexed"
    wiki_dir.mkdir(parents=True)
    doc_path = wiki_dir / "note.md"
    doc_path.write_text(
        "---\ntitle: Note\nstatus: candidate\nprivacy: safe\ntype: concept\n---\n\nHello wiki.\n",
        encoding="utf-8",
    )

    db_obj, cfg = _make_db(tmp_path)
    conn = db_obj._get_conn()
    _poison_last_modified(conn, str(doc_path.resolve()))

    indexer = WikiIndexer(db=db_obj, config=cfg)
    stats = indexer.index_wiki(str(wiki_dir.parent))

    assert stats.get("errors", 0) == 0, "poisoned row must not raise, only reindex"
    assert stats["indexed"] >= 1, "the stuck row must have been reindexed, not skipped forever"

    row = conn.execute(
        "SELECT typeof(last_modified) t, last_modified FROM documents WHERE path = ?",
        (str(doc_path.resolve()),),
    ).fetchone()
    assert row["t"] in ("real", "integer"), "the repair must have overwritten the poison"

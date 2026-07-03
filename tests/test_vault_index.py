"""Per-vault index store tests.

These tests pin the operator decision that agent vault markdown indexes into a
vault-local SQLite/FAISS store, not the shared Minni DB.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


def test_vault_index_config_points_inside_vault_dot_index(tmp_path):
    from minni.vault_index import build_vault_index_config, vault_index_paths

    vault = tmp_path / "codex-vault"
    paths = vault_index_paths(vault)
    cfg = build_vault_index_config(vault)

    assert paths.db_path == vault / ".index" / "vault.db"
    assert paths.faiss_index_path == vault / ".index" / "vault.faiss"
    assert paths.faiss_manifest_path == vault / ".index" / "vault.manifest.json"
    assert Path(cfg.vault_path) == vault
    assert Path(cfg.db_path) == paths.db_path
    assert Path(cfg.faiss_index_path) == paths.faiss_index_path
    assert Path(cfg.faiss_manifest_path) == paths.faiss_manifest_path


def test_open_vault_index_initializes_isolated_schema_without_touching_shared_db(tmp_path):
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.vault_index import open_vault_index, vault_index_paths

    shared_cfg = SovereignConfig(
        db_path=str(tmp_path / "shared" / "minni.db"),
        vault_path=str(tmp_path / "shared-vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        writeback_enabled=False,
    )
    shared_db = SovereignDB(shared_cfg)
    shared_db._get_conn()

    vault = tmp_path / "codex-vault"
    db_obj, indexer = open_vault_index(vault, base_config=shared_cfg)
    db_obj._get_conn()

    paths = vault_index_paths(vault)
    assert paths.db_path.exists()
    assert indexer.db is db_obj
    assert Path(indexer.config.db_path) == paths.db_path

    with sqlite3.connect(paths.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            )
        }
    assert "documents" in tables
    assert "chunk_embeddings" in tables
    assert "memory_links" in tables

    with shared_db.cursor() as c:
        c.execute("SELECT COUNT(*) FROM documents")
        assert c.fetchone()[0] == 0


def test_vault_faiss_persistence_uses_vault_faiss_path(tmp_path):
    from minni.faiss_index import FAISSIndex
    from minni.vault_index import build_vault_index_config, open_vault_index, vault_index_paths

    vault = tmp_path / "codex-vault"
    cfg = build_vault_index_config(vault)
    db_obj, _ = open_vault_index(vault)
    conn = db_obj._get_conn()

    idx = FAISSIndex(cfg)
    vecs = np.eye(cfg.embedding_dim, dtype=np.float32)[:3]
    idx.build_from_vectors([1, 2, 3], vecs)

    assert idx.save_to_disk(db_conn=conn)

    paths = vault_index_paths(vault)
    assert paths.faiss_manifest_path.exists()
    assert paths.faiss_index_path.exists() or Path(str(paths.faiss_index_path) + ".npz").exists()

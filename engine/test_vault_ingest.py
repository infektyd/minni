"""Tests for the vault_ingest AFM pass.

The pass must gather from vault wiki markdown into a per-vault index store only.
The source vault markdown is never rewritten.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


class _FakeEmbedder:
    def encode(self, text: str):
        vec = np.zeros(384, dtype=np.float32)
        vec[sum(text.encode("utf-8")) % 384] = 1.0
        return vec


def _install_fake_embedder(monkeypatch):
    import models

    monkeypatch.setattr(models, "get_embedder", lambda: _FakeEmbedder())


def _make_shared_db(tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "shared" / "minni.db"),
        vault_path=str(tmp_path / "shared-vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "shared.faiss"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _long_body(marker: str, *, link: str = "") -> str:
    words = " ".join(f"{marker}-{i}" for i in range(90))
    return f"{link}\n\n## Notes\n\n{words}\n"


def _write_page(path: Path, title: str, body: str, privacy: str = "safe") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {title}",
                "type: concept",
                "status: accepted",
                f"privacy: {privacy}",
                "---",
                "",
                body,
            ]
        ),
        encoding="utf-8",
    )


def _doc_rows(index_db: Path):
    with sqlite3.connect(index_db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT path, agent, page_type FROM documents ORDER BY path")]


def _count_shared_documents(db_obj) -> int:
    with db_obj.cursor() as c:
        c.execute("SELECT COUNT(*) FROM documents")
        return c.fetchone()[0]


def _wiki_snapshot(vault: Path) -> dict[str, bytes]:
    wiki = vault / "wiki"
    return {
        str(path.relative_to(wiki)): path.read_bytes()
        for path in sorted(wiki.rglob("*.md"))
    }


def test_vault_ingest_indexes_wiki_into_per_vault_store_only(tmp_path, monkeypatch):
    from afm_passes.vault_ingest import run
    from vault_index import vault_index_paths

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    _write_page(vault / "wiki" / "alpha.md", "Alpha", _long_body("alpha", link="[[beta]]"))
    _write_page(vault / "wiki" / "beta.md", "Beta", _long_body("beta"))

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False, trace_id="trace-test")

    paths = vault_index_paths(vault)
    assert result["status"] == "ok"
    assert result["drafts"] == []
    assert result["agent_id"] == "codex"
    assert result["files_seen"] == 2
    assert result["indexed"] == 2
    assert result["index_db_path"] == str(paths.db_path)
    assert paths.db_path.exists()
    assert paths.faiss_manifest_path.exists()
    assert paths.faiss_index_path.exists() or Path(str(paths.faiss_index_path) + ".npz").exists()

    rows = _doc_rows(paths.db_path)
    assert [row["agent"] for row in rows] == ["codex", "codex"]
    assert {row["page_type"] for row in rows} == {"concept"}
    assert _count_shared_documents(shared_db) == 0

    with sqlite3.connect(paths.db_path) as conn:
        link_count = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    assert link_count == 1
    assert chunk_count > 0


def test_vault_ingest_wet_run_never_modifies_wiki_markdown(tmp_path, monkeypatch):
    from afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    _write_page(vault / "wiki" / "alpha.md", "Alpha", _long_body("alpha", link="[[beta]]"))
    _write_page(vault / "wiki" / "beta.md", "Beta", _long_body("beta"))
    before = _wiki_snapshot(vault)

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert result["status"] == "ok"
    assert _wiki_snapshot(vault) == before


def test_vault_ingest_incremental_reindexes_and_prunes_index_rows_only(tmp_path, monkeypatch):
    from afm_passes.vault_ingest import run
    from vault_index import vault_index_paths

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    alpha = vault / "wiki" / "alpha.md"
    beta = vault / "wiki" / "beta.md"
    _write_page(alpha, "Alpha", _long_body("alpha"))
    _write_page(beta, "Beta", _long_body("beta"))

    first = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    second = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert first["indexed"] == 2
    assert second["indexed"] == 0
    assert second["skipped_unchanged"] == 2

    before_alpha = alpha.read_bytes()
    _write_page(alpha, "Alpha", _long_body("alpha-changed"))
    future = time.time() + 5
    os.utime(alpha, (future, future))
    changed = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert changed["indexed"] >= 1
    assert alpha.read_bytes() == before_alpha.replace(b"alpha-", b"alpha-changed-")

    beta.unlink()
    pruned = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert pruned["pruned"] == 1
    assert not beta.exists()

    paths = vault_index_paths(vault)
    rows = _doc_rows(paths.db_path)
    assert [Path(row["path"]).name for row in rows] == ["alpha.md"]


def test_vault_ingest_isolates_two_vault_stores_and_shared_db(tmp_path, monkeypatch):
    from afm_passes.vault_ingest import run
    from vault_index import vault_index_paths

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    codex_vault = tmp_path / "codex-vault"
    claude_vault = tmp_path / "claudecode-vault"
    _write_page(codex_vault / "wiki" / "codex.md", "Codex", _long_body("codex-only"))
    _write_page(claude_vault / "wiki" / "claude.md", "Claude", _long_body("claude-only"))

    codex = run(shared_db, cfg, vault_path=str(codex_vault), dry_run=False)
    claude = run(shared_db, cfg, vault_path=str(claude_vault), dry_run=False)

    codex_db = vault_index_paths(codex_vault).db_path
    claude_db = vault_index_paths(claude_vault).db_path
    assert codex["index_db_path"] == str(codex_db)
    assert claude["index_db_path"] == str(claude_db)
    assert codex_db != claude_db

    codex_rows = _doc_rows(codex_db)
    claude_rows = _doc_rows(claude_db)
    assert [Path(row["path"]).name for row in codex_rows] == ["codex.md"]
    assert [row["agent"] for row in codex_rows] == ["codex"]
    assert [Path(row["path"]).name for row in claude_rows] == ["claude.md"]
    assert [row["agent"] for row in claude_rows] == ["claude-code"]
    assert _count_shared_documents(shared_db) == 0


def test_vault_ingest_purges_index_rows_when_page_becomes_blocked(tmp_path, monkeypatch):
    from afm_passes.vault_ingest import run
    from vault_index import vault_index_paths

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    alpha = vault / "wiki" / "alpha.md"
    _write_page(alpha, "Alpha", _long_body("alpha"))

    first = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert first["indexed"] == 1

    _write_page(alpha, "Alpha", _long_body("alpha"), privacy="blocked")
    future = time.time() + 5
    os.utime(alpha, (future, future))
    second = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert second["indexed"] == 0
    assert second["pruned"] == 1
    db_path = vault_index_paths(vault).db_path
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM vault_fts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
    assert alpha.exists()

    third = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert third["indexed"] == 0
    assert third["pruned"] == 0


def test_vault_ingest_dry_run_writes_nothing(tmp_path, monkeypatch):
    from afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    page = vault / "wiki" / "alpha.md"
    _write_page(page, "Alpha", _long_body("alpha"))
    before = page.read_bytes()

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=True)

    assert result["dry_run"] is True
    assert result["would_index"] == 1
    assert not (vault / ".index").exists()
    assert page.read_bytes() == before
    assert _count_shared_documents(shared_db) == 0


def test_vault_ingest_unknown_vault_slug_skips_without_index_store(tmp_path, monkeypatch):
    from afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "mystery-vault"
    _write_page(vault / "wiki" / "alpha.md", "Alpha", _long_body("alpha"))

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert result["status"] == "skipped"
    assert result["reason"] == "unknown_vault_slug"
    assert result["agent_id"] is None
    assert not (vault / ".index").exists()


def test_daemon_compile_registers_vault_ingest_dry_run(tmp_path, monkeypatch):
    import minnid
    import principal as principal_mod
    from config import SovereignConfig

    _install_fake_embedder(monkeypatch)
    vault = tmp_path / "codex-vault"
    _write_page(vault / "wiki" / "alpha.md", "Alpha", _long_body("alpha"))

    principals = tmp_path / "principals"
    principals.mkdir()
    principal_file = principals / "main.json"
    principal_file.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(tmp_path)],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(principal_file, 0o600)
    monkeypatch.setattr(principal_mod, "PRINCIPALS_DIR", principals)

    cfg = SovereignConfig(
        db_path=str(tmp_path / "shared" / "minni.db"),
        vault_path=str(vault),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "shared.faiss"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)

    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "daemon.compile",
            "params": {
                "pass_name": "vault_ingest",
                "vault_path": str(vault),
                "dry_run": True,
            },
        }
    )

    assert "error" not in resp
    assert resp["result"]["status"] == "ok"
    assert resp["result"]["dry_run"] is True
    assert resp["result"]["would_index"] == 1

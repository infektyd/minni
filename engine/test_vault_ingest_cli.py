"""Tests for manual vault_ingest CLI helpers in index_all.py."""

from __future__ import annotations

import os
import sqlite3
import sys
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


def _write_page(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {marker}",
                "type: concept",
                "status: accepted",
                "privacy: safe",
                "---",
                "",
                " ".join(f"{marker}-manual-{i}" for i in range(90)),
            ]
        ),
        encoding="utf-8",
    )


def _make_cfg(tmp_path):
    from config import SovereignConfig

    return SovereignConfig(
        db_path=str(tmp_path / "shared" / "minni.db"),
        vault_path=str(tmp_path / "shared-vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "shared.faiss"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )


def test_discover_agent_vaults_excludes_legacy_bare_vault(tmp_path):
    from index_all import discover_agent_vaults

    minni_home = tmp_path / ".minni"
    (minni_home / "codex-vault").mkdir(parents=True)
    (minni_home / "claudecode-vault").mkdir()
    (minni_home / "vault").mkdir()

    found = discover_agent_vaults(minni_home)

    assert [path.name for path in found] == ["claudecode-vault", "codex-vault"]


def test_index_agent_vaults_runs_vault_ingest_per_temp_vault(tmp_path, monkeypatch):
    from index_all import index_agent_vaults

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    minni_home = tmp_path / ".minni"
    codex = minni_home / "codex-vault"
    claude = minni_home / "claudecode-vault"
    legacy = minni_home / "vault"
    _write_page(codex / "wiki" / "codex.md", "codex")
    _write_page(claude / "wiki" / "claude.md", "claude")
    _write_page(legacy / "wiki" / "legacy.md", "legacy")

    stats = index_agent_vaults(config=cfg, minni_home=minni_home, dry_run=False)

    assert sorted(stats) == [str(claude), str(codex)]
    assert stats[str(codex)]["indexed"] == 1
    assert stats[str(claude)]["indexed"] == 1
    assert not (legacy / ".index").exists()

    for vault, expected_agent in ((codex, "codex"), (claude, "claude-code")):
        db_path = vault / ".index" / "vault.db"
        assert db_path.exists()
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT agent, path FROM documents").fetchall()
        assert rows == [(expected_agent, str(vault / "wiki" / f"{expected_agent.split('-')[0]}.md"))]

"""Daemon document recall over per-vault indexes.

All state is tmp_path-backed. These tests prove scoped document recall uses
agent-local vault indexes without touching live ~/.minni.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))


class _FakeEmbedder:
    def encode(self, text: str):
        vec = np.zeros(384, dtype=np.float32)
        vec[sum(text.encode("utf-8")) % 384] = 1.0
        return vec


def _install_fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    import minni.models as models

    monkeypatch.setattr(models, "get_embedder", lambda: _FakeEmbedder())


def _long_body(marker: str) -> str:
    return " ".join(f"{marker}-beacon-{i}" for i in range(100))


def _write_page(path: Path, title: str, marker: str, privacy: str = "safe") -> None:
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
                _long_body(marker),
                "",
            ]
        ),
        encoding="utf-8",
    )


def _make_cfg(tmp_path: Path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "shared" / "minni.db"),
        vault_path=str(tmp_path / "shared-vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "shared.faiss"),
        writeback_enabled=False,
        reranker_enabled=False,
        hyde_enabled=False,
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


def _seed_shared_doc(
    db_obj,
    path: Path,
    content: str,
    *,
    agent: str = "wiki:concept",
    privacy: str = "safe",
) -> int:
    now = time.time()
    vec = _FakeEmbedder().encode(content).astype(np.float32).tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at,
                page_status, privacy_level, page_type, layer)
               VALUES (?, ?, 'wiki', ?, ?, 'accepted', ?, 'concept', 'knowledge')""",
            (str(path), agent, now, now, privacy),
        )
        doc_id = c.lastrowid
        c.execute(
            "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, ?, 'wiki')",
            (doc_id, str(path), content, agent),
        )
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, heading_context, model_name, computed_at, layer)
               VALUES (?, 0, ?, ?, 'shared', 'fake', ?, 'knowledge')""",
            (doc_id, content, vec, now),
        )
        return int(c.lastrowid)


def _install_principal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    agent_id: str = "codex",
    capabilities: list[str] | None = None,
) -> None:
    import minni.minnid as minnid
    import minni.principal as principal_mod

    principals = tmp_path / "principals"
    principals.mkdir(exist_ok=True)
    f = principals / f"{agent_id}.json"
    f.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "capabilities": capabilities or ["*"],
                "allowed_vault_roots": [str(tmp_path)],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(f, 0o600)
    monkeypatch.setattr(principal_mod, "PRINCIPALS_DIR", principals)

    original_resolve = principal_mod.resolve_effective_principal

    def _resolve(
        *,
        supplied_agent_id=None,
        transport="uds",
        principals_dir=None,
        operator_context=False,
    ):
        target = str(supplied_agent_id or agent_id).strip() or agent_id
        op_ctx = operator_context or target in principal_mod.OPERATOR_RESERVED_AGENT_IDS
        return original_resolve(
            supplied_agent_id=supplied_agent_id or agent_id,
            transport=transport,
            principals_dir=principals_dir or principals,
            operator_context=op_ctx,
        )

    monkeypatch.setattr(principal_mod, "resolve_effective_principal", _resolve)
    monkeypatch.setattr(minnid, "resolve_effective_principal", _resolve)


def _install_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cfg, vaults: dict[str, Path]) -> None:
    import minni.minnid as minnid

    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)
    monkeypatch.setattr(minnid, "_retrieval", None)
    monkeypatch.setattr(minnid, "_vault_retrieval_cache", {})
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps({k: str(v) for k, v in vaults.items()}))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


def _search(params: dict):
    from minni.minnid import _dispatch_sync

    return _dispatch_sync({"jsonrpc": "2.0", "id": 1, "method": "search", "params": params})


def _drill(params: dict):
    from minni.minnid import _dispatch_sync

    return _dispatch_sync({"jsonrpc": "2.0", "id": 1, "method": "sm_drill", "params": params})


def _sources(resp: dict) -> list[str]:
    assert "error" not in resp, resp
    return [Path(row["source"]).name for row in resp["result"]["results"]]


def _src_markers(resp: dict) -> list[str]:
    assert "error" not in resp, resp
    return [row.get("src") for row in resp["result"]["results"]]


def _rows(resp: dict) -> list[dict]:
    assert "error" not in resp, resp
    return resp["result"]["results"]


def _hit_reference(row: dict) -> dict:
    return {
        "doc_id": row.get("doc_id"),
        "chunk_id": row.get("chunk_id"),
        "src": row.get("src"),
        "source": row.get("source"),
        "wikilink": row.get("wikilink"),
        "score": row.get("score"),
        "provenance": row.get("provenance"),
    }


def _build_indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    db_obj, cfg = _make_cfg(tmp_path)
    codex_vault = tmp_path / "codex-vault"
    claude_vault = tmp_path / "claudecode-vault"
    _write_page(codex_vault / "wiki" / "codex.md", "Codex", "codex-personal")
    _write_page(claude_vault / "wiki" / "claude.md", "Claude", "claude-foreign")
    run(db_obj, cfg, vault_path=str(codex_vault), dry_run=False)
    run(db_obj, cfg, vault_path=str(claude_vault), dry_run=False)
    _seed_shared_doc(db_obj, tmp_path / "legacy" / "shared.md", _long_body("legacy-shared"))
    return db_obj, cfg, codex_vault, claude_vault


def test_scope_personal_searches_callers_per_vault_index(tmp_path, monkeypatch):
    _, cfg, codex_vault, claude_vault = _build_indexes(tmp_path, monkeypatch)
    _install_principal(monkeypatch, tmp_path, "codex")
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )

    resp = _search(
        {
            "query": "beacon",
            "agent_id": "codex",
            "scope": "personal",
            "expand": False,
            "limit": 5,
        }
    )

    assert _sources(resp) == ["codex.md"]
    assert _src_markers(resp) == ["p"]
    assert "source_agent" not in _rows(resp)[0]
    assert "source_index_db_path" not in _rows(resp)[0]


def test_default_scope_both_merges_personal_and_combined(tmp_path, monkeypatch):
    _, cfg, codex_vault, claude_vault = _build_indexes(tmp_path, monkeypatch)
    _install_principal(monkeypatch, tmp_path, "codex")
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )

    resp = _search({"query": "beacon", "agent_id": "codex", "expand": False, "limit": 10})

    assert set(_sources(resp)) == {"codex.md", "claude.md", "shared.md"}
    by_source = {Path(row["source"]).name: row["src"] for row in _rows(resp)}
    assert by_source["codex.md"] == "p"
    assert by_source["claude.md"] == "c"
    assert by_source["shared.md"] == "c"


def test_missing_personal_index_falls_back_to_shared_legacy(tmp_path, monkeypatch):
    _install_fake_embedder(monkeypatch)
    db_obj, cfg = _make_cfg(tmp_path)
    codex_vault = tmp_path / "codex-vault"
    codex_vault.mkdir()
    _seed_shared_doc(db_obj, tmp_path / "legacy" / "shared.md", _long_body("legacy-shared"))
    _install_principal(monkeypatch, tmp_path, "codex")
    _install_runtime(monkeypatch, tmp_path, cfg, {"codex": codex_vault})

    resp = _search(
        {
            "query": "beacon",
            "agent_id": "codex",
            "scope": "personal",
            "expand": False,
            "limit": 5,
        }
    )

    assert _sources(resp) == ["shared.md"]
    assert _src_markers(resp) == ["c"]


def test_scope_combined_merges_all_existing_vault_indexes_plus_shared(tmp_path, monkeypatch):
    _, cfg, codex_vault, claude_vault = _build_indexes(tmp_path, monkeypatch)
    _install_principal(monkeypatch, tmp_path, "codex")
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )

    resp = _search(
        {
            "query": "beacon",
            "agent_id": "codex",
            "scope": "combined",
            "expand": False,
            "limit": 10,
        }
    )

    assert set(_sources(resp)) == {"codex.md", "claude.md", "shared.md"}
    assert set(_src_markers(resp)) == {"c"}


def test_drill_reference_returns_personal_full_provenance(tmp_path, monkeypatch):
    from minni.vault_index import vault_index_paths

    _, cfg, codex_vault, claude_vault = _build_indexes(tmp_path, monkeypatch)
    _install_principal(monkeypatch, tmp_path, "codex")
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )
    search = _search(
        {
            "query": "beacon",
            "agent_id": "codex",
            "scope": "personal",
            "expand": False,
            "limit": 5,
        }
    )
    ref = _hit_reference(_rows(search)[0])

    drilled = _drill({"references": [ref], "depth": "chunk"})

    assert "error" not in drilled, drilled
    result = drilled["result"]["results"][0]
    assert Path(result["source"]).name == "codex.md"
    assert result["src"] == "p"
    full = result["full_provenance"]
    assert full["owning_agent_id"] == "codex"
    assert full["source_vault"] == str(codex_vault.resolve())
    assert full["index_db_path"] == str(vault_index_paths(codex_vault).db_path.resolve())
    assert isinstance(full["indexed_at"], float)
    assert full["score_components"]["score"] == ref["score"]


def test_drill_reference_resolves_combined_foreign_vault_provenance(tmp_path, monkeypatch):
    from minni.vault_index import vault_index_paths

    _, cfg, codex_vault, claude_vault = _build_indexes(tmp_path, monkeypatch)
    _install_principal(monkeypatch, tmp_path, "codex")
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )
    search = _search(
        {
            "query": "beacon",
            "agent_id": "codex",
            "scope": "combined",
            "expand": False,
            "limit": 10,
        }
    )
    claude_hit = next(row for row in _rows(search) if Path(row["source"]).name == "claude.md")

    drilled = _drill({"references": [_hit_reference(claude_hit)], "depth": "chunk"})

    assert "error" not in drilled, drilled
    result = drilled["result"]["results"][0]
    assert Path(result["source"]).name == "claude.md"
    assert result["src"] == "c"
    full = result["full_provenance"]
    assert full["owning_agent_id"] == "claude-code"
    assert full["source_vault"] == str(claude_vault.resolve())
    assert full["index_db_path"] == str(vault_index_paths(claude_vault).db_path.resolve())
    assert isinstance(full["indexed_at"], float)


@pytest.mark.parametrize(
    ("params", "expected_sources"),
    [
        ({"cross_agent": True}, {"codex.md", "claude.md", "shared.md"}),
        ({"cross_agent": False}, {"codex.md", "claude.md", "shared.md"}),
        ({}, {"codex.md", "claude.md", "shared.md"}),
    ],
)
def test_cross_agent_alias_mapping_is_back_compatible(
    tmp_path,
    monkeypatch,
    params,
    expected_sources,
):
    _, cfg, codex_vault, claude_vault = _build_indexes(tmp_path, monkeypatch)
    _install_principal(monkeypatch, tmp_path, "codex")
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )

    resp = _search(
        {
            "query": "beacon",
            "agent_id": "codex",
            "expand": False,
            "limit": 10,
            **params,
        }
    )

    assert set(_sources(resp)) == expected_sources


def test_no_principal_keeps_shared_legacy_only(tmp_path, monkeypatch):
    import minni.minnid as minnid

    _, cfg, codex_vault, claude_vault = _build_indexes(tmp_path, monkeypatch)
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )
    monkeypatch.setattr(
        minnid,
        "resolve_effective_principal",
        lambda *, supplied_agent_id=None, transport="uds": None,
    )

    resp = _search({"query": "beacon", "expand": False, "limit": 10})

    assert _sources(resp) == ["shared.md"]
    assert _src_markers(resp) == ["c"]


def _build_private_foreign_indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Codex owns a safe page; claude-code's vault holds a privacy: private page."""
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    db_obj, cfg = _make_cfg(tmp_path)
    codex_vault = tmp_path / "codex-vault"
    claude_vault = tmp_path / "claudecode-vault"
    _write_page(codex_vault / "wiki" / "codex.md", "Codex", "codex-personal")
    _write_page(
        claude_vault / "wiki" / "claude-private.md",
        "Claude Private",
        "claude-private",
        privacy="private",
    )
    run(db_obj, cfg, vault_path=str(codex_vault), dry_run=False)
    run(db_obj, cfg, vault_path=str(claude_vault), dry_run=False)
    _seed_shared_doc(db_obj, tmp_path / "legacy" / "shared.md", _long_body("legacy-shared"))
    return db_obj, cfg, codex_vault, claude_vault


@pytest.mark.parametrize("scope", ["combined", None])
def test_combined_scope_gates_foreign_private_pages_for_non_operator(
    tmp_path, monkeypatch, scope
):
    _, cfg, codex_vault, claude_vault = _build_private_foreign_indexes(tmp_path, monkeypatch)
    _install_principal(monkeypatch, tmp_path, "codex", capabilities=["search", "read"])
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )

    params = {"query": "beacon", "agent_id": "codex", "expand": False, "limit": 10}
    if scope is not None:
        params["scope"] = scope
    resp = _search(params)

    assert set(_sources(resp)) == {"codex.md", "shared.md"}


def test_shared_fallback_gates_foreign_private_docs_for_non_operator(tmp_path, monkeypatch):
    _install_fake_embedder(monkeypatch)
    db_obj, cfg = _make_cfg(tmp_path)
    codex_vault = tmp_path / "codex-vault"
    codex_vault.mkdir()
    _seed_shared_doc(db_obj, tmp_path / "legacy" / "shared.md", _long_body("legacy-shared"))
    _seed_shared_doc(
        db_obj,
        tmp_path / "legacy" / "claude-private.md",
        _long_body("legacy-private"),
        agent="claude-code",
        privacy="private",
    )
    _install_principal(monkeypatch, tmp_path, "codex", capabilities=["search", "read"])
    _install_runtime(monkeypatch, tmp_path, cfg, {"codex": codex_vault})

    resp = _search(
        {
            "query": "beacon",
            "agent_id": "codex",
            "scope": "personal",
            "expand": False,
            "limit": 5,
        }
    )

    assert _sources(resp) == ["shared.md"]
    assert _src_markers(resp) == ["c"]


def test_drill_reference_gates_foreign_private_page_for_non_operator(tmp_path, monkeypatch):
    _, cfg, codex_vault, claude_vault = _build_private_foreign_indexes(tmp_path, monkeypatch)
    _install_principal(monkeypatch, tmp_path, "codex", capabilities=["search", "read"])
    _install_runtime(
        monkeypatch,
        tmp_path,
        cfg,
        {"codex": codex_vault, "claude-code": claude_vault},
    )

    own_source = str(codex_vault / "wiki" / "codex.md")
    foreign_source = str(claude_vault / "wiki" / "claude-private.md")
    drilled = _drill(
        {
            "references": [
                {"source": own_source, "src": "c"},
                {"source": foreign_source, "src": "c"},
            ],
            "depth": "chunk",
        }
    )

    assert "error" not in drilled, drilled
    result_sources = [Path(row["source"]).name for row in drilled["result"]["results"]]
    assert result_sources == ["codex.md"]
    assert drilled["result"]["missing"] == [foreign_source]


def test_drill_numeric_ids_gate_foreign_private_shared_docs(tmp_path, monkeypatch):
    _install_fake_embedder(monkeypatch)
    db_obj, cfg = _make_cfg(tmp_path)
    codex_vault = tmp_path / "codex-vault"
    codex_vault.mkdir()
    shared_chunk = _seed_shared_doc(
        db_obj, tmp_path / "legacy" / "shared.md", _long_body("legacy-shared")
    )
    private_chunk = _seed_shared_doc(
        db_obj,
        tmp_path / "legacy" / "claude-private.md",
        _long_body("legacy-private"),
        agent="claude-code",
        privacy="private",
    )
    _install_principal(monkeypatch, tmp_path, "codex", capabilities=["search", "read"])
    _install_runtime(monkeypatch, tmp_path, cfg, {"codex": codex_vault})

    drilled = _drill({"chunk_ids": [shared_chunk, private_chunk], "depth": "chunk"})

    assert "error" not in drilled, drilled
    result_sources = [Path(row["source"]).name for row in drilled["result"]["results"]]
    assert result_sources == ["shared.md"]
    assert drilled["result"]["missing"] == [private_chunk]

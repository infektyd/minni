import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _seed_doc(db_obj, path="/wiki/source.md", agent="wiki:concept"):
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, sigil, last_modified, indexed_at, access_count, last_accessed,
             decay_score, whole_document, page_status, privacy_level, page_type)
            VALUES (?, ?, '?', ?, ?, 0, NULL, 1.0, 0, 'accepted', 'safe', 'concept')
            """,
            (path, agent, now, now),
        )
        return c.lastrowid


def test_writeback_learning_with_evidence_adds_derived_from_edges(tmp_path, monkeypatch):
    from writeback import WriteBackMemory

    db_obj, cfg = _make_db(tmp_path)
    evidence_id = _seed_doc(db_obj)
    wb = WriteBackMemory(db_obj, cfg)

    import writeback as wb_mod
    original_prop = wb_mod.WriteBackMemory.model.fget
    wb_mod.WriteBackMemory.model = property(lambda self: None)
    try:
        learning_id = wb.store_learning(
            agent_id="agent-a",
            content="Evidence-backed learning",
            category="fact",
            evidence_doc_ids=[evidence_id],
        )
    finally:
        wb_mod.WriteBackMemory.model = property(original_prop)

    with db_obj.cursor() as c:
        learning_doc = c.execute(
            "SELECT doc_id, path FROM documents WHERE path = ?",
            (f"learning://{learning_id}",),
        ).fetchone()
        assert learning_doc is not None
        edge = c.execute(
            """
            SELECT source_doc_id, target_doc_id, link_type
            FROM memory_links
            WHERE source_doc_id = ? AND target_doc_id = ? AND link_type = 'derived_from'
            """,
            (learning_doc["doc_id"], evidence_id),
        ).fetchone()
    assert edge is not None


def test_sovrd_learn_with_evidence_adds_derived_from_edges(tmp_path, monkeypatch):
    import sovrd
    import writeback as wb_mod
    from writeback import WriteBackMemory

    db_obj, cfg = _make_db(tmp_path)
    evidence_id = _seed_doc(db_obj)
    monkeypatch.setattr(sovrd, "_writeback", WriteBackMemory(db_obj, cfg))
    original_prop = wb_mod.WriteBackMemory.model.fget
    wb_mod.WriteBackMemory.model = property(lambda self: None)
    try:
        resp = sovrd._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "learn",
                "params": {
                    "content": "Daemon evidence learning",
                    "agent_id": "agent-b",
                    "evidence_doc_ids": [evidence_id],
                },
            }
        )
    finally:
        wb_mod.WriteBackMemory.model = property(original_prop)

    learning_id = resp["result"]["learning_id"]
    with db_obj.cursor() as c:
        learning_doc_id = c.execute(
            "SELECT doc_id FROM documents WHERE path = ?",
            (f"learning://{learning_id}",),
        ).fetchone()["doc_id"]
        edge_count = c.execute(
            """
            SELECT COUNT(*) AS n FROM memory_links
            WHERE source_doc_id = ? AND target_doc_id = ? AND link_type = 'derived_from'
            """,
            (learning_doc_id, evidence_id),
        ).fetchone()["n"]
    assert edge_count == 1


def test_status_includes_latency_histograms(monkeypatch):
    import sovrd

    monkeypatch.setattr(sovrd, "_request_count", 0)
    monkeypatch.setattr(sovrd, "_latencies", {})
    for value in (0.01, 0.02, 0.03, 0.04):
        sovrd._record_latency("search", value)

    result = sovrd._handle_status({}, 1)["result"]
    latencies = result["daemon"]["latencies"]
    assert set(["search", "learn", "read", "embedding", "cross_encoder"]).issubset(latencies)
    assert latencies["search"]["count"] == 4
    assert latencies["search"]["p50_ms"] > 0
    assert latencies["search"]["p95_ms"] >= latencies["search"]["p50_ms"]


def test_sovrd_read_includes_layer_1_identity_before_context(tmp_path, monkeypatch):
    import sovrd

    db_obj, _cfg = _make_db(tmp_path)
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, sigil, last_modified, indexed_at, access_count, last_accessed,
             decay_score, whole_document, page_status, privacy_level, page_type, layer)
            VALUES (?, 'identity:codex', 'C', ?, ?, 0, NULL, 1.0, 1, 'accepted', 'safe', 'schema', 'identity')
            """,
            ("/identity/codex/CODEX_HOSTED_AGENT_ENVELOPE.md", now, now),
        )
        identity_doc_id = c.lastrowid
        c.execute(
            """
            INSERT INTO chunk_embeddings
            (doc_id, chunk_index, chunk_text, embedding, model_name, computed_at, layer)
            VALUES (?, 0, ?, ?, 'test', ?, 'identity')
            """,
            (
                identity_doc_id,
                "Sovereign Memory gives owned agents a soul. It gives hosted agents a map.",
                b"0" * 1536,
                now,
            ),
        )
        _seed_doc(db_obj, path="/wiki/context.md", agent="codex")

    monkeypatch.setattr(sovrd, "SovereignDB", lambda: db_obj)

    resp = sovrd._handle_read({"agent_id": "codex", "limit": 5}, 1)
    context = resp["result"]["context"]

    assert "## Agent Identity: Codex" in context
    assert "### CODEX_HOSTED_AGENT_ENVELOPE" in context
    assert "owned agents a soul" in context
    assert context.index("## Agent Identity: Codex") < context.index("## Prior Context (codex)")


def test_status_accepts_persisted_faiss_npz_cache(tmp_path, monkeypatch):
    import sovrd
    from config import SovereignConfig

    db_path = tmp_path / "sovereign_memory.db"
    faiss_dir = tmp_path / "faiss"
    faiss_dir.mkdir()
    (faiss_dir / "index.manifest.json").write_text(
        '{"chunk_count": 1, "db_checksum": "abc"}',
        encoding="utf-8",
    )
    (faiss_dir / "index.faiss.npz").write_bytes(b"npz-cache")

    cfg = SovereignConfig(
        db_path=str(db_path),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "legacy.index"),
    )
    monkeypatch.setattr(sovrd, "DEFAULT_CONFIG", cfg)

    result = sovrd._handle_status({}, 1)["result"]

    assert result["engine"]["faiss_ok"] is True
    assert result["engine"]["faiss_path"] == str(faiss_dir / "index.faiss.npz")


def test_python_format_recall_includes_backend_badge():
    import sovrd

    formatted = sovrd.formatRecall(
        "backend provenance",
        {"backend": "faiss-disk+qdrant", "results": "### result.md"},
    )
    assert "Query: backend provenance [faiss-disk+qdrant]" in formatted


def test_health_report_returns_required_fields(tmp_path, monkeypatch):
    import sovrd
    from writeback import WriteBackMemory

    db_obj, cfg = _make_db(tmp_path)
    _seed_doc(db_obj, path="/wiki/old.md")
    monkeypatch.setattr(sovrd, "_writeback", WriteBackMemory(db_obj, cfg))
    monkeypatch.setattr(sovrd, "DEFAULT_CONFIG", cfg)

    resp = sovrd._dispatch({"jsonrpc": "2.0", "id": 1, "method": "health_report", "params": {}})
    assert "error" not in resp
    assert {
        "stale_docs",
        "never_recalled",
        "contradicting_learnings",
        "vector_backend_lag",
        "faiss_cache_age_seconds",
    }.issubset(resp["result"])


def test_hygiene_report_clean_vault_has_zero_blocks(tmp_path):
    from hygiene import run_hygiene_report

    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "logs").mkdir()
    (vault / "wiki" / "alpha.md").write_text(
        "---\n"
        "title: Alpha\n"
        "status: accepted\n"
        "privacy: safe\n"
        "type: concept\n"
        "sources:\n"
        "  - logs/source.md\n"
        "---\n"
        "# Alpha\n\n"
        "Clean page.\n",
        encoding="utf-8",
    )
    (vault / "logs" / "source.md").write_text("source", encoding="utf-8")
    (vault / "index.md").write_text("- [[wiki/alpha]]\n", encoding="utf-8")
    (vault / "log.md").write_text("source\n", encoding="utf-8")

    summary = run_hygiene_report(vault)
    assert summary["counts"]["block"] == 0
    assert any((vault / "logs").glob("hygiene-*.md"))


def test_sovrd_hygiene_report_returns_json_summary(tmp_path):
    import sovrd

    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "logs").mkdir()
    (vault / "index.md").write_text("", encoding="utf-8")
    (vault / "log.md").write_text("", encoding="utf-8")

    resp = sovrd._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "hygiene_report",
            "params": {"vault": str(vault)},
        }
    )
    assert "error" not in resp
    assert resp["result"]["status"] == "ok"
    assert "counts" in resp["result"]
    assert os.path.exists(resp["result"]["report_path"])

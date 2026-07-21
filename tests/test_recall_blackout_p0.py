"""Recall blackout P0 cluster (2026-07-19 forensics punch list) — TDD.

The 2026-07-18 Codex session ran 14 recalls over 14.8h and got zero hits while
every queried document existed on disk, in vault_fts, and with embeddings.
Three stacked engine/wiring causes plus one write-path cause:

P0-A  can_read_document scoped out 100% of candidates: legacy docs store
      RELATIVE paths (resolve against daemon cwd -> fail allows_vault_root),
      legacy docs have EMPTY workspace_id ('' -> 'default') vs a named call
      workspace, and the path gate ran before the same-agent ownership check.
      Contract: an agent must be able to read documents stamped with its own
      agent_id; unstamped (empty) doc workspace is wildcard for the owner;
      auth filtering to zero must yield a diagnostic, never a bare [].
P0-B  _semantic_search returned [] silently when the embedder never loaded —
      FTS-only recall all day with every health light green.
P0-C  _sanitize_fts_query space-joins tokens -> FTS5 implicit AND of every
      token; a dated/specific query over-constrains to zero. learnings_fts
      already degrades to OR; the document path must too.
P0-D  plugin sent the identity as `agent` but provenance_claim reads only
      `agent_id` -> claim-less -> default principal 'main' -> ownership
      mismatch -> every vault_write checkpoint indexed 'degraded'.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.principal import EffectivePrincipal, can_read_document
from minni.minnid_runtime.provenance import provenance_claim


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
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


def _p(agent="codex", ws="default", roots=None, caps=None):
    return EffectivePrincipal(
        agent_id=agent,
        workspace_id=ws,
        capabilities=caps or ["search", "recall"],
        allowed_vault_roots=roots or ["/tmp/test-vault"],
    )


# ── P0-A: authorization scope-out ────────────────────────────────────────────


def test_same_agent_relative_path_allowed():
    """789/789 real docs store relative paths; the owner must still read them."""
    p = _p("codex")
    meta = {
        "agent": "codex",
        "privacy_level": "safe",
        "path": "wiki/sessions/2026-07-18-checkpoint.md",
        "page_status": "accepted",
        "workspace_id": "",
    }
    assert can_read_document(p, "default", meta) is True


def test_same_agent_empty_doc_workspace_is_owner_wildcard():
    """777/789 legacy docs have workspace_id='' while calls stamp a named ws."""
    p = _p("codex", ws="workspace-observatory")
    meta = {
        "agent": "codex",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/note.md",
        "page_status": "accepted",
        "workspace_id": "",
    }
    assert can_read_document(p, "workspace-observatory", meta) is True


def test_same_agent_explicit_ws_mismatch_still_denied():
    """Only an EMPTY doc workspace is owner-wildcard; explicit mismatch stays denied."""
    p = _p("codex", ws="ws-b")
    meta = {
        "agent": "codex",
        "privacy_level": "safe",
        "path": "/tmp/test-vault/wiki/note.md",
        "workspace_id": "ws-a",
    }
    assert can_read_document(p, "ws-b", meta) is False


def test_same_agent_blocked_still_denied():
    """The ownership fast-path must not bypass privacy=blocked."""
    p = _p("codex")
    meta = {
        "agent": "codex",
        "privacy_level": "blocked",
        "path": "wiki/sessions/x.md",
        "workspace_id": "",
    }
    assert can_read_document(p, "default", meta) is False


def test_foreign_private_relative_path_still_denied():
    """Relative-path handling must not open foreign private docs."""
    p = _p("codex")
    meta = {
        "agent": "other-agent",
        "privacy_level": "private",
        "path": "wiki/sessions/secret.md",
        "workspace_id": "",
    }
    assert can_read_document(p, "default", meta) is False


def test_read_gate_reports_suppression_diagnostic():
    """H2 silent-empty contract: filtering a non-empty candidate set to zero
    must produce a machine-readable diagnostic, never a bare empty list."""
    from minni.retrieval import RetrievalEngine

    p = _p("codex")
    merged = [
        {
            "doc_id": 1,
            "agent": "other-agent",
            "privacy_level": "private",
            "path": "/elsewhere/secret.md",
            "workspace_id": "default",
        },
        {
            "doc_id": 2,
            "agent": "third-agent",
            "privacy_level": "private",
            "path": "/elsewhere/too.md",
            "workspace_id": "default",
        },
    ]
    filtered, diag = RetrievalEngine.apply_read_gate(p, "default", merged)
    assert filtered == []
    assert diag is not None
    assert diag["pre_gate"] == 2
    assert diag["suppressed"] == 2
    assert "scope" in str(diag).lower() or "suppressed" in str(diag)


# ── P0-B: dead semantic leg must fail loud ───────────────────────────────────


def test_semantic_search_fail_loud_when_encoder_down(tmp_path, monkeypatch, caplog):
    import logging
    import minni.models as models_mod
    from minni.retrieval import RetrievalEngine

    monkeypatch.setattr(models_mod, "get_embedder", lambda: None)
    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    with caplog.at_level(logging.WARNING):
        out = engine._semantic_search("any query", 5)

    assert out == []
    assert engine.vector_model_down is True
    assert any(
        "vector" in rec.message.lower() or "encoder" in rec.message.lower()
        or "semantic" in rec.message.lower()
        for rec in caplog.records
    ), "encoder-down must log a WARNING, not silently return []"


def test_vector_model_down_defaults_false(tmp_path):
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
    assert engine.vector_model_down is False


# ── P0-C: document FTS must degrade to OR semantics ─────────────────────────


def test_fts_search_degrades_to_or_semantics(tmp_path, monkeypatch):
    """A dated/specific query must still find the doc matching its rare terms."""
    import minni.models as models_mod
    from minni.retrieval import RetrievalEngine

    monkeypatch.setattr(models_mod, "get_embedder", lambda: None)
    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    res = engine.index_durable_document(
        content=(
            "# Session checkpoint\n\n"
            "Session checkpoint for plan-5912f669c1b58c4e recorded with the "
            "observatory forensics deliverables and the punch list."
        ),
        path="wiki/sessions/checkpoint.md",
        agent="codex",
    )
    assert res.get("doc_id") is not None

    # Strict AND over these tokens includes 2026/07/18 which are absent from
    # the content -> zero rows before the fix.
    hits = engine._fts_search("session checkpoint 2026-07-18 plan-5912f669c1b58c4e", 5)
    assert len(hits) >= 1
    assert hits[0]["path"] == "wiki/sessions/checkpoint.md"


def test_fts_search_strict_pass_unchanged(tmp_path, monkeypatch):
    """Queries the strict AND pass already answers must not be diluted."""
    import minni.models as models_mod
    from minni.retrieval import RetrievalEngine

    monkeypatch.setattr(models_mod, "get_embedder", lambda: None)
    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    engine.index_durable_document(
        content="# Alpha\n\nThe alpha subsystem exports the tidal calibration table.",
        path="wiki/alpha.md",
        agent="codex",
    )
    engine.index_durable_document(
        content="# Beta\n\nThe beta subsystem has nothing to do with calibration.",
        path="wiki/beta.md",
        agent="codex",
    )
    hits = engine._fts_search("alpha tidal calibration", 5)
    assert len(hits) >= 1
    assert hits[0]["path"] == "wiki/alpha.md"


# ── P0-D: vault_write identity must reach provenance ─────────────────────────


def test_provenance_claim_vault_index_doc_accepts_agent_fallback():
    """Legacy plugins send `agent`; the claim must not silently vanish.
    resolve_effective_principal still verifies the claim against the stamped
    identity, so this is a claim-source widening, not an auth bypass."""
    assert provenance_claim("vault_index_doc", {"agent": "codex"}) == "codex"


def test_provenance_claim_agent_id_still_wins():
    assert provenance_claim("vault_index_doc", {"agent_id": "a", "agent": "b"}) == "a"


def test_provenance_claim_other_methods_unchanged():
    assert provenance_claim("search", {"agent": "codex"}) is None


def test_server_ts_vault_index_doc_sends_agent_id():
    """Regression pin on the plugin call site: the vault_index_doc RPC params
    must carry agent_id (the field provenance_claim reads), not just agent."""
    server_ts = (
        Path(__file__).resolve().parents[1] / "plugins" / "minni" / "src" / "server.ts"
    )
    src = server_ts.read_text()
    idx = src.find('"vault_index_doc"')
    assert idx != -1, "vault_index_doc call site missing from server.ts"
    window = src[idx : idx + 600]
    assert "agent_id" in window, (
        "server.ts must send agent_id in vault_index_doc params — `agent` alone "
        "is not read by provenance_claim and degrades indexing to principal 'main'"
    )

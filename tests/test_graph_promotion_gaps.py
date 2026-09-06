"""Atomic canonical identity for auto-accept and AFM promotion.

Handler-level tests on disposable SQLite with a stub embedder (no real
model): auto-accept and AFM promote commit learning + canonical node + join
in one transaction, preserve the original owner, keep duplicates terminal,
fall back to staged proposals when preparation fails, deliver post-commit
indexing without ever failing committed memory, and roll back cleanly on
canonical failure.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.minnid_runtime import governance
from minni.minnid_runtime.afm import AFMContext, promote_candidate_durable
from minni.principal import EffectivePrincipal


CONTENT = "Pin the CLI version so a release becomes a reviewable file change."


class _FakeModel:
    def encode(self, text, **kwargs):
        vec = np.ones(4, dtype=np.float32)
        return vec / np.linalg.norm(vec)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import minni.config as cfg
    import minni.writeback as wb_mod
    from minni.db import SovereignDB
    from minni.migrations import run_migrations

    path = str(tmp_path / "gaps.db")
    monkeypatch.setattr(cfg.DEFAULT_CONFIG, "db_path", path, raising=False)
    db = SovereignDB()
    run_migrations(db._get_conn())
    monkeypatch.setattr(
        wb_mod.WriteBackMemory, "model", property(lambda self: _FakeModel()))
    return db, path


def _principal(**over):
    args = {"agent_id": "codex", "capabilities": ["learn"]}
    args.update(over)
    return EffectivePrincipal(**args)


def _gov_context(principal, db, indexed=None, archived=None):
    import minni.config as cfg
    from minni.db import SovereignDB
    from minni.writeback import WriteBackMemory

    calls = indexed if indexed is not None else []

    def index_agent(agent_id, content, key=None, db=None, **kwargs):
        calls.append((agent_id, content, key,
                      os.path.abspath(db.config.db_path) if db else None))
        return True

    class _Logger:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    return governance.GovernanceContext(
        handler_principal=lambda params, rid, **k: (principal, None),
        lazy_writeback=lambda: WriteBackMemory(SovereignDB(), cfg.DEFAULT_CONFIG),
        sovereign_db=SovereignDB,
        make_response=lambda result, rid: {"result": result},
        make_error=lambda code, message, rid: {
            "error": {"code": code, "message": message}},
        logger=_Logger(),
        index_durable_learning=index_agent,
        maybe_archive_inbox_source=lambda *a, **k: (
            archived.append(a) if archived is not None else None),
        lazy_episodic=lambda: None,
        record_latency=lambda *a, **k: None,
        increment_request_count=None,
    ), calls


def _afm_context(db, config, indexed=None):
    import minni.writeback as wb_mod
    from minni.writeback import WriteBackMemory

    calls = indexed if indexed is not None else []

    def index_agent(agent_id, content, key=None, db=None, **kwargs):
        calls.append((agent_id, content, key,
                      os.path.abspath(db.config.db_path) if db else None))
        return True

    class _Logger:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    return AFMContext(
        make_error=lambda code, message, rid: {
            "error": {"code": code, "message": message}},
        make_response=lambda result, rid: {"result": result},
        guard_vault_root=lambda *a, **k: None,
        lazy_writeback=lambda: WriteBackMemory(db, config),
        trace_ring=lambda: None,
        record_latency=lambda *a, **k: None,
        maybe_archive_inbox_source=lambda *a, **k: None,
        logger=_Logger(),
        index_durable_learning=index_agent,
    ), calls


def _rows(db, sql, args=()):
    with db.cursor() as c:
        return [tuple(r) for r in c.execute(sql, args).fetchall()]


def _stage(db, principal, content=CONTENT, **params):
    ctx, indexed = _gov_context(principal, db)
    full = {"content": content}
    full.update(params)
    return governance.stage_candidate(full, 1, ctx), indexed


def _propose_afm(db, agent="codex", content=CONTENT):
    with db.cursor() as c:
        c.execute(
            """INSERT INTO candidate_packets
               (principal, workspace_id, privacy_level, content,
                instruction_like, status, proposed_at)
               VALUES (?, 'default', 'safe', ?, 0, 'proposed', 0.0)""",
            (agent, content),
        )
        return c.lastrowid


def _stub_model(monkeypatch):
    import minni.writeback as wb_mod

    monkeypatch.setattr(
        wb_mod.WriteBackMemory, "model", property(lambda self: _FakeModel()))


def _afm_env(tmp_path):
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.migrations import run_migrations
    from minni.minnid_runtime.governance import ensure_content_hash_column

    config = SovereignConfig(
        db_path=str(tmp_path / "afm.db"),
        vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "notes"),
        faiss_index_path=str(tmp_path / "index.faiss"),
        writeback_enabled=False,
        reranker_enabled=False, attribution_enabled=False,
    )
    db = SovereignDB(config)
    run_migrations(db._get_conn())
    # Production contract (not a migration): the learn path ensures
    # learnings.content_hash, which promote_candidate_durable's INSERT names.
    # The fixture prepares it exactly the way production does.
    assert ensure_content_hash_column(db) is True
    assert "content_hash" in [
        r[1] for r in _rows(db, "PRAGMA table_info(learnings)")]
    return db, config


def test_auto_accept_commits_canonical_atomically(env):
    """Auto-accept writes learning + accepted mark + canonical node + join
    in one transaction, owned by the proposer."""
    db, _ = env
    res, indexed = _stage(db, _principal(auto_accept_own=True))
    assert res["result"]["status"] == "accepted"
    lid = res["result"]["learning_id"]
    learnings = _rows(db, "SELECT learning_id, agent_id FROM learnings")
    assert learnings == [(lid, "codex")]
    joins = _rows(db, "SELECT learning_id, doc_id FROM learning_documents")
    assert len(joins) == 1 and joins[0][0] == lid
    docs = _rows(db, "SELECT doc_id, agent, memory_kind FROM documents")
    assert len(docs) == 1
    assert docs[0][0] == joins[0][1] and docs[0][1] == "codex"
    assert docs[0][2] == "learning"
    assert res["result"].get("indexed", True) is not False
    assert indexed and indexed[0][0] == "codex"


def test_auto_accept_canonical_failure_stages_proposal(env, monkeypatch):
    """Canonical failure rolls back everything; the proposal still stages."""
    import minni.minnid_runtime.governance as gov_mod

    db, _ = env

    def boom(*args, **kwargs):
        raise RuntimeError("canonical store offline")
    monkeypatch.setattr(gov_mod, "ensure_canonical_learning_node", boom)
    res, _ = _stage(db, _principal(auto_accept_own=True))
    assert res["result"]["status"] == "proposed"
    assert "learning_id" not in res["result"]
    assert _rows(db, "SELECT status FROM candidate_packets") == [
        ("proposed",)]
    assert _rows(db, "SELECT * FROM learnings") == []
    assert _rows(db, "SELECT * FROM learning_documents") == []
    assert _rows(db, "SELECT * FROM documents") == []


def test_auto_accept_duplicate_stays_single(env):
    """A concurrent duplicate accept does not double-write the learning."""
    db, _ = env
    first, _ = _stage(db, _principal(auto_accept_own=True))
    assert first["result"]["status"] == "accepted"
    second, _ = _stage(db, _principal(auto_accept_own=True))
    assert second["result"]["status"] == "proposed"
    assert _rows(db, "SELECT * FROM learnings") \
        == _rows(db, "SELECT * FROM learnings WHERE learning_id=?",
                 (first["result"]["learning_id"],))
    assert len(_rows(db, "SELECT * FROM learning_documents")) == 1
    statuses = _rows(db, "SELECT status FROM candidate_packets ORDER BY 1")
    assert sorted(s[0] for s in statuses) == ["accepted", "proposed"]


def test_auto_accept_offline_model_stages_proposed(env, monkeypatch):
    """No embedder: proposal stages, nothing durable is written."""
    import minni.writeback as wb_mod

    db, _ = env
    monkeypatch.setattr(
        wb_mod.WriteBackMemory, "model", property(lambda self: None))
    res, _ = _stage(db, _principal(auto_accept_own=True))
    assert res["result"]["status"] == "proposed"
    assert _rows(db, "SELECT * FROM learnings") == []
    assert _rows(db, "SELECT * FROM learning_documents") == []


def test_auto_accept_index_failure_stays_accepted_once(env):
    """A failing post-commit index reports indexed=False but never fails
    or duplicates the committed learning."""
    db, _ = env
    ctx, _ = _gov_context(_principal(auto_accept_own=True), db)

    def failing(*args, **kwargs):
        raise RuntimeError("index offline")
    ctx = ctx.__class__(**{**ctx.__dict__, "index_durable_learning": failing})
    res = governance.stage_candidate({"content": CONTENT}, 1, ctx)
    assert res["result"]["status"] == "accepted"
    assert res["result"]["indexed"] is False
    assert len(_rows(db, "SELECT * FROM learnings")) == 1
    assert len(_rows(db, "SELECT * FROM learning_documents")) == 1


def test_afm_promote_commits_canonical_and_delivers_index(tmp_path, monkeypatch):
    """AFM promote writes learning + join owned by the candidate owner and
    delivers post-commit indexing against the same DB."""
    _stub_model(monkeypatch)
    db, config = _afm_env(tmp_path)
    cid = _propose_afm(db, agent="codex")
    ctx, indexed = _afm_context(db, config)
    lid = promote_candidate_durable(cid, "test-promote", ctx)
    assert isinstance(lid, int)
    assert _rows(db, "SELECT learning_id, agent_id FROM learnings") == [
        (lid, "codex")]
    joins = _rows(db, "SELECT learning_id, doc_id FROM learning_documents")
    assert len(joins) == 1 and joins[0][0] == lid
    docs = _rows(db, "SELECT doc_id, agent FROM documents")
    assert [(joins[0][1], "codex")] == [(docs[0][0], docs[0][1])]
    assert _rows(db, "SELECT status FROM candidate_packets") == [
        ("accepted",)]
    assert len(indexed) == 1
    assert indexed[0][0] == "codex" and indexed[0][1] == CONTENT
    assert indexed[0][3] == os.path.abspath(config.db_path)


def test_afm_promote_index_failure_keeps_promotion(tmp_path, monkeypatch):
    """Index delivery raising/False never fails committed AFM memory."""
    _stub_model(monkeypatch)
    db, config = _afm_env(tmp_path)

    for mode in ("raise", "false"):
        cid = _propose_afm(db, content=f"{CONTENT} {mode}")
        ctx, _ = _afm_context(db, config)

        def deliver(*args, **kwargs):
            if mode == "raise":
                raise RuntimeError("index offline")
            return False
        ctx = ctx.__class__(**{**ctx.__dict__,
                               "index_durable_learning": deliver})
        lid = promote_candidate_durable(cid, "test-promote", ctx)
        assert isinstance(lid, int), mode
        assert _rows(db, "SELECT status FROM candidate_packets"
                         " WHERE candidate_id=?", (cid,)) == [("accepted",)]
    assert len(_rows(db, "SELECT * FROM learnings")) == 2


@pytest.mark.parametrize("field,value", [
    ("privacy_level", "blocked"),
    ("content", "Replacement content swapped mid-prepare."),
    ("principal", "mallory"),
    ("instruction_like", 1),
    ("evidence_refs", '["https://example.invalid/x"]'),
])
def test_afm_promote_rejects_mid_prepare_drift(tmp_path, monkeypatch, field, value):
    """A safe proposed candidate changed during embedding must not promote,
    stamp, or fence: drift returns None with zero effects and the review
    fence stays pending."""
    _stub_model(monkeypatch)
    db, config = _afm_env(tmp_path)
    cid = _propose_afm(db)

    class _DriftingModel(_FakeModel):
        def encode(self, text, **kwargs):
            with db.cursor() as c:
                c.execute(
                    f"UPDATE candidate_packets SET {field}=?"
                    " WHERE status='proposed'",
                    (value,),
                )
            return super().encode(text, **kwargs)

    import minni.writeback as wb_mod

    monkeypatch.setattr(
        wb_mod.WriteBackMemory, "model", property(lambda self: _DriftingModel()))
    archived = []
    ctx, indexed = _afm_context(db, config)
    ctx = ctx.__class__(**{**ctx.__dict__,
                           "maybe_archive_inbox_source": lambda *a: archived.append(a)})
    assert promote_candidate_durable(cid, "test-promote", ctx) is None
    assert _rows(db, "SELECT status FROM candidate_packets") == [
        ("proposed",)]
    assert _rows(db, "SELECT * FROM learnings") == []
    assert _rows(db, "SELECT * FROM consolidation_actions") == []
    assert _rows(db, "SELECT * FROM learning_documents") == []
    assert _rows(db, "SELECT * FROM documents") == []
    assert indexed == []
    assert archived == []


def test_afm_promote_canonical_failure_stays_proposed(tmp_path, monkeypatch):
    """Canonical failure rolls the AFM promotion back; proposal survives."""
    import minni.minnid_runtime.afm as afm_mod

    _stub_model(monkeypatch)
    db, config = _afm_env(tmp_path)
    cid = _propose_afm(db)
    hook_calls = []

    def failing_canonical(*args, **kwargs):
        hook_calls.append((args, kwargs))
        raise RuntimeError("canonical down")
    monkeypatch.setattr(
        afm_mod, "ensure_canonical_learning_node", failing_canonical)
    ctx, indexed = _afm_context(db, config)
    assert promote_candidate_durable(cid, "test-promote", ctx) is None
    # The rollback path is real: the canonical hook actually ran.
    assert len(hook_calls) == 1
    assert hook_calls[0][1].get("learning_id") == 1
    assert hook_calls[0][1].get("agent_id") == "codex"
    assert _rows(db, "SELECT status FROM candidate_packets") == [
        ("proposed",)]
    assert _rows(db, "SELECT * FROM learnings") == []
    assert _rows(db, "SELECT * FROM learning_documents") == []
    assert _rows(db, "SELECT * FROM documents") == []
    assert _rows(db, "SELECT * FROM consolidation_actions") == []
    assert indexed == []


def test_afm_uses_committed_store_config_when_writeback_has_none(tmp_path):
    import types

    db, config = _afm_env(tmp_path)
    cid = _propose_afm(db)
    context, indexed = _afm_context(db, config)
    wb = types.SimpleNamespace(db=db, model=_FakeModel())
    context = context.__class__(**{
        **context.__dict__, "lazy_writeback": lambda: wb,
    })
    lid = promote_candidate_durable(cid, "store-bound config", context)
    assert isinstance(lid, int)
    assert len(_rows(db, "SELECT * FROM learning_documents WHERE learning_id=?", (lid,))) == 1
    assert indexed[0][3] == os.path.abspath(config.db_path)

"""Regressions across AFM/governance and production index-result adapters.

All storage is SQLite in memory. Model and index dependencies are fakes;
decision, lifecycle application, RPC handlers and index adapter are real.
"""

from contextlib import contextmanager
from dataclasses import replace
import logging
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

import minni.minnid as daemon
from minni.afm_passes import consolidation
from minni.minnid_runtime import afm, governance
from minni.principal import EffectivePrincipal


CONTENT = "Deployment health checks require the configured local socket."


class MemoryDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.config = SimpleNamespace(
            db_path=":memory:", vault_path="/unused-test-vault",
            writeback_enabled=False, afm_loop_schedule={},
        )
        self.conn.executescript("""
            CREATE TABLE learnings (
                learning_id INTEGER PRIMARY KEY, agent_id TEXT, category TEXT,
                content TEXT, created_at REAL, content_hash TEXT,
                status TEXT DEFAULT 'active', embedding BLOB, source_doc_ids TEXT,
                source_query TEXT, confidence REAL, assertion TEXT,
                applies_when TEXT, evidence_doc_ids TEXT, contradicts_id INTEGER,
                superseded_by INTEGER
            );
            CREATE TABLE candidate_packets (
                candidate_id INTEGER PRIMARY KEY, principal TEXT,
                workspace_id TEXT, layer TEXT, privacy_level TEXT, content TEXT,
                evidence_refs TEXT, derived_from TEXT, instruction_like INTEGER,
                status TEXT, proposed_at REAL, resolved_at REAL, resolved_by TEXT,
                resolution_reason TEXT
            );
            CREATE TABLE consolidation_actions (
                action_id INTEGER PRIMARY KEY, action_type TEXT, claim TEXT,
                category TEXT, status TEXT, detail TEXT, created_at REAL,
                target_learning_id INTEGER, confidence REAL
            );
        """)

    @contextmanager
    def cursor(self):
        with self.conn:
            yield self.conn.cursor()

    @contextmanager
    def transaction(self):
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn.cursor()


@pytest.fixture
def db(monkeypatch):
    database = MemoryDB()
    monkeypatch.setattr(consolidation, "_triage_advisory", lambda *a: None)
    yield database
    database.conn.close()


def candidate(db, principal="codex", privacy="safe"):
    with db.transaction() as cursor:
        cursor.execute("""
            INSERT INTO candidate_packets
            (principal, workspace_id, privacy_level, content, instruction_like,
             status, proposed_at)
            VALUES (?, 'default', ?, ?, 0, 'proposed', 1)
        """, (principal, privacy, CONTENT))
        return cursor.lastrowid


def existing_learning(db, principal, status):
    with db.transaction() as cursor:
        cursor.execute("""
            INSERT INTO learnings (agent_id, content, content_hash, status)
            VALUES (?, ?, ?, ?)
        """, (principal, CONTENT, consolidation.content_hash(CONTENT), status))


def apply_afm(db):
    result = consolidation.run(db, db.config)
    wb = SimpleNamespace(db=db, config=db.config, model=None)
    archived = []
    afm.apply_consolidation_result(result, SimpleNamespace(
        lazy_writeback=lambda: wb,
        maybe_archive_inbox_source=lambda _db, cid: archived.append(cid),
        logger=logging.getLogger(__name__),
    ))
    assert result["applied"]["errors"] == 0
    assert result["applied"]["remaining_proposed"] == []
    return result, archived


@pytest.mark.parametrize("principal,status,is_duplicate", [
    ("codex", "active", True),
    ("codex", "rejected", False),
    ("codex", "superseded", False),
    ("codex", "expired", False),
    ("cursor", "active", False),
])
def test_afm_only_discards_an_active_duplicate_owned_by_the_candidate(
    db, principal, status, is_duplicate,
):
    existing_learning(db, principal, status)
    cid = candidate(db)
    result, archived = apply_afm(db)
    assert result["dedup_candidate_ids"] == ([cid] if is_duplicate else [])
    state = db.conn.execute(
        "SELECT status FROM candidate_packets WHERE candidate_id=?", (cid,)
    ).fetchone()[0]
    assert state == ("rejected" if is_duplicate else "accepted")
    assert archived == [cid]
    active = db.conn.execute(
        "SELECT COUNT(*) FROM learnings WHERE agent_id='codex' AND status='active'"
    ).fetchone()[0]
    assert active == 1


def test_same_batch_dedup_keeps_one_learning_per_owner(db):
    first = candidate(db, "codex")
    other_owner = candidate(db, "cursor")
    duplicate = candidate(db, "codex")
    result, _ = apply_afm(db)
    assert result["promote_candidate_ids"] == [first, other_owner]
    assert result["dedup_candidate_ids"] == [duplicate]
    assert [r[0] for r in db.conn.execute(
        "SELECT agent_id FROM learnings ORDER BY agent_id"
    )] == ["codex", "cursor"]


def test_other_owners_hash_does_not_bypass_review_privacy(db):
    existing_learning(db, "cursor", "active")
    cid = candidate(db, privacy="review")
    result, archived = apply_afm(db)
    assert result["review_candidate_ids"] == [cid]
    assert result["promote_candidate_ids"] == result["dedup_candidate_ids"] == []
    assert archived == []
    assert db.conn.execute(
        "SELECT status FROM candidate_packets WHERE candidate_id=?", (cid,)
    ).fetchone()[0] == "proposed"


@pytest.mark.parametrize("route", ["afm", "automatic"])
def test_superseded_reference_does_not_block_relearning(db, route):
    # force learn's supersedes field sets the reference without changing the
    # prior row's status. Recall excludes that row, so dedup must exclude it too.
    existing_learning(db, "codex", "active")
    with db.transaction() as cursor:
        cursor.execute("""
            INSERT INTO learnings (agent_id, content, content_hash, status)
            VALUES ('codex', 'Updated deployment process', ?, 'active')
        """, (consolidation.content_hash("Updated deployment process"),))
        cursor.execute(
            "UPDATE learnings SET superseded_by=? WHERE learning_id=1",
            (cursor.lastrowid,),
        )
    if route == "afm":
        cid = candidate(db)
        result, _ = apply_afm(db)
        assert result["promote_candidate_ids"] == [cid]
        assert result["dedup_candidate_ids"] == []
    else:
        ctx = replace(governed_context(db), index_durable_learning=lambda *a, **k: True)
        response = governance.handle_learn({"content": CONTENT}, 1, ctx)
        assert response["result"]["status"] == "accepted"


def governed_context(db, *, automatic=True):
    principal = EffectivePrincipal(
        agent_id="codex", capabilities=["govern"], auto_accept_own=automatic,
    )
    model = SimpleNamespace(encode=lambda *a, **k: np.ones(4, dtype=np.float32))
    wb = SimpleNamespace(
        db=db, config=db.config, model=model,
        detect_contradictions=lambda **k: [],
        add_derived_from_edges=lambda **k: None,
    )
    return governance.GovernanceContext(
        handler_principal=lambda *a: (principal, None),
        lazy_writeback=lambda: wb, sovereign_db=lambda: db,
        lazy_episodic=lambda: None, record_latency=lambda *a: None,
        make_response=lambda result, rid: {"result": result},
        make_error=lambda code, message, rid: {"error": {"code": code, "message": message}},
        index_durable_learning=daemon._index_durable_learning,
        maybe_archive_inbox_source=lambda *a: None,
    )


@pytest.mark.parametrize("route", ["automatic", "manual", "force"])
@pytest.mark.parametrize("index_result", [
    "initialization_failed",
    {"status": "degraded", "reason": "index unavailable"},
    {"status": "ok", "doc_id": 1, "chunks": 0},
    {"status": "ok", "doc_id": 1, "chunks": 1},
])
def test_real_index_adapter_reports_degradation_after_durable_commit(
    db, monkeypatch, route, index_result,
):
    # Production adapter is NOT replaced by a raising fake: its own failure
    # handling must reach the RPC response, including a returned engine status.
    monkeypatch.setattr(daemon.DEFAULT_CONFIG, "db_path", db.config.db_path)

    def retrieval():
        if index_result == "initialization_failed":
            raise RuntimeError("index initialization failed")
        return SimpleNamespace(
            config=db.config, index_durable_document=lambda **k: index_result,
        )

    monkeypatch.setattr(daemon, "_lazy_retrieval", retrieval)
    ctx = governed_context(db, automatic=route == "automatic")
    response = governance.handle_learn(
        {"content": CONTENT, "force": route == "force"}, 1, ctx,
    )
    assert "error" not in response
    if route == "manual":
        response = governance.resolve_candidate({
            "candidate_id": response["result"]["candidate_id"], "decision": "accept",
        }, 2, ctx)
    assert "error" not in response
    result = response["result"]
    assert result["learning_id"] is not None
    assert db.conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0] == 1
    if index_result == {"status": "ok", "doc_id": 1, "chunks": 1}:
        assert "indexed" not in result
    else:
        assert result["indexed"] is False


def test_missing_store_binding_reports_index_failure(monkeypatch):
    def never_open_live_engine():
        pytest.fail("must not open an engine without an explicit store DB")

    monkeypatch.setattr(daemon, "_lazy_retrieval", never_open_live_engine)
    assert daemon._index_durable_learning("codex", CONTENT, "learning:1") is False


def test_legacy_none_returning_index_adapter_remains_compatible(db):
    context = SimpleNamespace(
        index_durable_learning=lambda *a, **k: None,
        maybe_archive_inbox_source=lambda *a: None,
        logger=logging.getLogger(__name__),
    )
    assert governance.settle_accepted_candidate(context, db, 1, "codex", CONTENT, 1)


@pytest.mark.parametrize("transition", ["rejected", "superseded"])
def test_dedup_rechecks_replacement_after_selection(db, transition):
    existing_learning(db, "codex", "active")
    cid = candidate(db)
    result = consolidation.run(db, db.config)
    assert result["dedup_candidate_ids"] == [cid]
    with db.transaction() as c:
        if transition == "rejected":
            c.execute("UPDATE learnings SET status='rejected'")
        else:
            c.execute("UPDATE learnings SET superseded_by=42")
    archived = []
    context = SimpleNamespace(
        lazy_writeback=lambda: SimpleNamespace(db=db, config=db.config, model=None),
        maybe_archive_inbox_source=lambda _db, item: archived.append(item),
        logger=logging.getLogger(__name__),
    )
    afm.apply_consolidation_result(result, context)
    assert result["applied"]["deduped"] == 0
    assert result["applied"]["remaining_proposed"] == [cid]
    assert archived == []


def test_failed_batch_promotion_cannot_discard_its_duplicate(db, monkeypatch):
    first, second = candidate(db), candidate(db)
    result = consolidation.run(db, db.config)
    assert result["dedup_candidate_ids"] == [second]
    monkeypatch.setattr(afm, "promote_candidate_durable", lambda *args: None)
    archived = []
    afm.apply_consolidation_result(result, SimpleNamespace(
        lazy_writeback=lambda: SimpleNamespace(db=db, config=db.config, model=None),
        maybe_archive_inbox_source=lambda _db, item: archived.append(item),
        logger=logging.getLogger(__name__),
    ))
    assert set(result["applied"]["remaining_proposed"]) == {first, second}
    assert result["applied"]["deduped"] == 0
    assert archived == []


def test_alias_owners_share_the_promoted_principal_for_dedup(db):
    first = candidate(db, "xai")
    second = candidate(db, "grok-build")
    result, archived = apply_afm(db)
    assert result["promote_candidate_ids"] == [first]
    assert result["dedup_candidate_ids"] == [second]
    assert len(archived) == 2
    assert [r[0] for r in db.conn.execute("SELECT agent_id FROM learnings")] == ["grok-build"]

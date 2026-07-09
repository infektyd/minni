"""Slice B security regressions: retrieval read-policy (R4, R8, M3).

R5 is covered in test_pr6_contradictions.py
(test_learn_does_not_leak_cross_agent_contradictions); R6 is covered in
test_new01_health_report_redaction.py
(test_handle_health_report_redacts_identified_non_operator).
"""

from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from minni.principal import EffectivePrincipal


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig
    from minni.db import SovereignDB

    cfg = SovereignConfig(
        db_path=str(tmp_path / "sliceb.db"),
        reranker_enabled=False,
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _make_engine(tmp_path):
    from minni.faiss_index import FAISSIndex
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    return RetrievalEngine(db_obj, cfg, FAISSIndex(cfg)), db_obj, cfg


def _seed_doc(db_obj, path, agent, text, *, privacy="safe", status="accepted", page_type=None):
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at, page_status,
                privacy_level, page_type)
               VALUES (?, ?, '?', ?, ?, ?, ?, ?)""",
            (path, agent, now, now, status, privacy, page_type),
        )
        doc_id = c.lastrowid
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, embedding, heading_context, computed_at)
               VALUES (?, 0, ?, ?, '', ?)""",
            (doc_id, text, np.zeros(384, dtype=np.float32).tobytes(), now),
        )
        chunk_id = c.lastrowid
    return doc_id, chunk_id


# ── R4: wikilink neighborhood must be gated + LIKE-metachar safe ────────────


def test_fetch_linked_context_denies_foreign_private_doc(tmp_path):
    """R4: a wikilink to another agent's PRIVATE doc must not leak via the
    neighborhood summary path — can_read_document gates it."""
    engine, db_obj, cfg = _make_engine(tmp_path)
    _seed_doc(
        db_obj,
        "wiki/secret-plan.md",
        "victim",
        "the private launch date is classified",
        privacy="private",
    )

    principal = EffectivePrincipal(agent_id="attacker", capabilities=["read", "search"])
    contexts = engine._fetch_linked_context(
        ["wiki/secret-plan"], principal=principal, workspace="default"
    )
    assert contexts == [], contexts


def test_fetch_linked_context_allows_own_safe_doc(tmp_path):
    """R4 regression guard: an agent's OWN safe doc still resolves via wikilink."""
    engine, db_obj, cfg = _make_engine(tmp_path)
    _seed_doc(db_obj, "wiki/my-note.md", "codex", "my own safe note body", privacy="safe")

    principal = EffectivePrincipal(agent_id="codex", capabilities=["read", "search"])
    contexts = engine._fetch_linked_context(
        ["wiki/my-note"], principal=principal, workspace="default"
    )
    assert len(contexts) == 1
    assert contexts[0]["path"] == "wiki/my-note.md"


def test_fetch_linked_context_rejects_like_wildcard(tmp_path):
    """R4: a '[[%]]' wikilink must NOT fan out across every document via LIKE."""
    engine, db_obj, cfg = _make_engine(tmp_path)
    _seed_doc(db_obj, "wiki/a.md", "codex", "doc a", privacy="safe")
    _seed_doc(db_obj, "wiki/b.md", "codex", "doc b", privacy="safe")

    principal = EffectivePrincipal(agent_id="codex", capabilities=["read", "search"])
    # A raw '%' would match '%%' → every path; '_' matches any single char.
    contexts = engine._fetch_linked_context(
        ["%", "_"], principal=principal, workspace="default"
    )
    assert contexts == [], contexts


def test_fetch_linked_context_fails_closed_without_principal(tmp_path):
    """R4 residual: principal=None must not return linked chunk text."""
    engine, db_obj, cfg = _make_engine(tmp_path)
    _seed_doc(db_obj, "wiki/my-note.md", "codex", "should not leak without principal", privacy="safe")

    contexts = engine._fetch_linked_context(
        ["wiki/my-note"], principal=None, workspace="default"
    )
    assert contexts == [], contexts


# ── M3: expand_result must surface real privacy/status ──────────────────────


def test_expand_result_denies_foreign_private_via_direct_id(tmp_path):
    """M3: expand_result previously omitted privacy_level from its SELECT, so
    can_read_document saw privacy='safe' and returned foreign private docs on the
    direct-id path. With the columns populated, the gate denies it."""
    engine, db_obj, cfg = _make_engine(tmp_path)
    doc_id, chunk_id = _seed_doc(
        db_obj,
        "wiki/other-private.md",
        "victim",
        "another agent's private content",
        privacy="private",
    )

    principal = EffectivePrincipal(agent_id="attacker", capabilities=["read"])
    out = engine.expand_result(chunk_id, principal=principal, workspace="default")
    assert out is None, out


def test_expand_result_allows_own_safe_via_direct_id(tmp_path):
    """M3 regression guard: an agent's own safe doc still expands."""
    engine, db_obj, cfg = _make_engine(tmp_path)
    doc_id, chunk_id = _seed_doc(
        db_obj, "wiki/mine.md", "codex", "my safe content", privacy="safe"
    )

    principal = EffectivePrincipal(agent_id="codex", capabilities=["read"])
    out = engine.expand_result(chunk_id, principal=principal, workspace="default")
    assert out is not None
    assert out.get("privacy_level") == "safe"


# ── R8: trace ring is owner-bound ───────────────────────────────────────────


def test_trace_ring_denies_non_owner():
    """R8: a trace stored with an owner is not readable by a different requester
    that merely knows the trace_id."""
    from minni.trace import TraceRing

    ring = TraceRing()
    tid = ring.add({"query": "secret query", "final_ordering": [1, 2]}, owner="codex")

    # Owner reads it (and never sees the private _owner key).
    got = ring.get(tid, requester="codex")
    assert got is not None
    assert got["query"] == "secret query"
    assert "_owner" not in got

    # A different principal is denied.
    assert ring.get(tid, requester="attacker") is None


def test_trace_ring_legacy_unowned_entry_still_readable():
    """R8 back-compat: an entry stored with no owner is not broken."""
    from minni.trace import TraceRing

    ring = TraceRing()
    tid = ring.add({"query": "legacy"})
    assert ring.get(tid, requester="anyone") is not None
    assert ring.get(tid) is not None

"""Red-then-Green verification suite for Minni Architectural Inquest Blueprint (Phases 1-3)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from minni.config import SovereignConfig
from minni.db import SovereignDB
from minni.request_deadline import current_deadline, request_deadline
from minni.migrations import run_migrations
from minni.minnid_runtime import recall as recall_mod
from minni.retrieval import RetrievalEngine


# ---------------------------------------------------------------------------
# Phase 1: Parallel Legs, Foreign Vault Pre-Filter, RRF Fix & <EVIDENCE> Tag
# ---------------------------------------------------------------------------


def test_phase1_gather_leg_results_engages_pool_with_deadline():
    """_gather_leg_results must run in parallel on minni-leg worker threads even
    when a request deadline is active, propagating the deadline via contextvars."""
    seen_threads: list[str] = []
    seen_deadlines: list[float | None] = []
    barrier = threading.Barrier(2, timeout=2.0)

    def _leg_one():
        barrier.wait()
        seen_threads.append(threading.current_thread().name)
        seen_deadlines.append(current_deadline())
        return "leg-1"

    def _leg_two():
        barrier.wait()
        seen_threads.append(threading.current_thread().name)
        seen_deadlines.append(current_deadline())
        return "leg-2"

    target_deadline = time.monotonic() + 15.0
    with request_deadline(target_deadline):
        out = recall_mod._gather_leg_results([_leg_one, _leg_two], target_deadline)

    assert out == ["leg-1", "leg-2"]
    assert any("minni-leg" in name for name in seen_threads), (
        f"Expected minni-leg worker threads when deadline is set, got {seen_threads}"
    )
    assert seen_deadlines == [target_deadline, target_deadline]


def test_phase1_rrf_weights_and_fts_slot_reservation_prevent_lexical_eviction(tmp_path):
    """Config defaults must be fts_weight=0.60, semantic_weight=0.40, and _rrf_merge
    must reserve slots for top FTS5 hits so an unembedded status='candidate' Rank-1
    lexical hit is never evicted by 20 status='accepted' semantic-only hits."""
    cfg = SovereignConfig(
        db_path=str(tmp_path / "rrf.db"),
        faiss_index_path=str(tmp_path / "rrf.faiss"),
        vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "learnings"),
        graph_export_dir=str(tmp_path / "graphs"),
        reranker_enabled=False,
        hyde_enabled=False,
    )
    assert cfg.fts_weight == pytest.approx(0.60)
    assert cfg.semantic_weight == pytest.approx(0.40)

    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    try:
        fts_results = [
            {
                "doc_id": 999,
                "path": "wiki/sessions/exact-lexical-match.md",
                "agent": "claude-code",
                "sigil": "x",
                "decay_score": 1.0,
                "chunk_text": "Exact lexical Rank 1 candidate note body",
                "heading_context": "Exact Match",
                "page_status": "candidate",
                "privacy_level": "safe",
                "page_type": "session",
                "layer": "knowledge",
            }
        ]
        semantic_results = [
            {
                "doc_id": i,
                "path": f"wiki/concepts/sem-noise-{i}.md",
                "agent": "claude-code",
                "sigil": "x",
                "decay_score": 1.0,
                "chunk_text": f"Semantic neighbor {i}",
                "heading_context": f"Sem {i}",
                "page_status": "accepted",
                "privacy_level": "safe",
                "page_type": "concept",
                "layer": "knowledge",
            }
            for i in range(1, 25)
        ]

        merged = engine._rrf_merge(fts_results, semantic_results, limit=20)
        merged_ids = [r["doc_id"] for r in merged]
        assert 999 in merged_ids, (
            f"FTS Rank 1 candidate (doc_id=999) was evicted from top-20 RRF pool: {merged_ids}"
        )
    finally:
        db.close()


def test_phase1_snippet_depth_never_truncates_inside_evidence_tag_and_stamps_real_score(tmp_path):
    """At depth='snippet', _SNIPPET_MAX_CHARS truncation must slice the note prose
    BEFORE wrapping in <EVIDENCE ...>...</EVIDENCE>, and score attribute must match
    the result's actual score rather than '0.000'."""
    cfg = SovereignConfig(
        db_path=str(tmp_path / "snip.db"),
        faiss_index_path=str(tmp_path / "snip.faiss"),
        vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "learnings"),
        graph_export_dir=str(tmp_path / "graphs"),
        reranker_enabled=False,
        hyde_enabled=False,
        feedback_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    try:
        long_prose = (
            "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
            * 12
        )
        long_rel_path = (
            "wiki/sessions/2026-10-13-very-long-nested-session-note-filename-"
            "that-pushes-evidence-opening-tag-past-two-hundred-chars.md"
        )
        engine.index_durable_document(
            content=f"---\ntitle: Long Snippet Test\nstatus: accepted\nprivacy: safe\n---\n# Long Snippet Test\n\n{long_prose}",
            path=long_rel_path,
            agent="claude-code",
            sigil="x",
            privacy_level="safe",
            page_status="accepted",
            layer="knowledge",
        )

        results = engine.retrieve(
            query="Alpha beta gamma delta",
            limit=5,
            depth="snippet",
            expand=False,
            budget_tokens=False,
            update_access=False,
        )
        assert results, "Expected snippet result"
        hit = results[0]
        text = hit["text"]
        assert text.startswith("<EVIDENCE "), f"Expected <EVIDENCE opening tag, got: {text!r}"
        assert text.endswith("</EVIDENCE>"), (
            f"Snippet depth truncated closing </EVIDENCE> tag: {text!r}"
        )
        assert 'score="0.000"' not in text, (
            f"Expected non-zero score attribute in <EVIDENCE> tag, got: {text!r}"
        )
        assert "Alpha beta gamma" in text
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Phase 2: In-Memory Prefix/Token Trie & Fast Recall Mode (< 10 ms)
# ---------------------------------------------------------------------------


def test_phase2_in_memory_trie_and_fast_recall_under_10ms(tmp_path):
    """RetrievalEngine must maintain an in-memory inverted prefix/token Trie
    updated on index_durable_document and support mode='fast' (Tier 1 Trie +
    Tier 2 FTS5 BM25) in under 10 ms without touching FAISS or Cross-Encoder."""
    cfg = SovereignConfig(
        db_path=str(tmp_path / "fast.db"),
        faiss_index_path=str(tmp_path / "fast.faiss"),
        vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "learnings"),
        graph_export_dir=str(tmp_path / "graphs"),
        reranker_enabled=True,
        hyde_enabled=False,
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    try:
        engine.index_durable_document(
            content="---\ntitle: Quartz Cadence Router\nstatus: accepted\nprivacy: safe\n---\n# Quartz Cadence Router\n\nRoutes high-priority telemetry frames over Unix domain sockets.",
            path="wiki/concepts/quartz-cadence-router.md",
            agent="claude-code",
            sigil="x",
            privacy_level="safe",
            page_status="accepted",
            layer="knowledge",
        )
        # Warm trie once
        trie_hits = engine.trie_search("quartz cadence", limit=5)
        assert trie_hits, "Expected in-memory Trie to match title/wikilink prefix"
        assert any("quartz-cadence-router" in h.get("path", "") for h in trie_hits)

        t0 = time.perf_counter()
        results = engine.retrieve(
            query="quartz cadence router",
            limit=5,
            depth="snippet",
            expand=False,
            mode="fast",
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert results, "Expected fast-mode recall results"
        assert elapsed_ms < 10.0, f"Expected mode='fast' recall < 10 ms, took {elapsed_ms:.2f} ms"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Phase 3: Migration 023, Unblock Candidates & Preserve Author/Category
# ---------------------------------------------------------------------------


def test_phase3_migration_023_unblocks_legacy_proposed_candidates(tmp_path):
    """Migration 023 must add candidate_packets.category, backfill NULL
    requested_privacy on proposed rows to 'safe', and supersede stale afm_review
    rows in consolidation_actions so _proposed_candidates returns them."""
    from minni.afm_passes.consolidation import _proposed_candidates

    cfg = SovereignConfig(
        db_path=str(tmp_path / "mig023.db"),
        faiss_index_path=str(tmp_path / "mig023.faiss"),
        vault_path=str(tmp_path / "vault"),
        writeback_path=str(tmp_path / "learnings"),
        graph_export_dir=str(tmp_path / "graphs"),
    )
    db = SovereignDB(cfg)
    try:
        with db.transaction() as c:
            c.execute(
                """
                INSERT INTO candidate_packets
                (principal, workspace_id, layer, privacy_level, requested_privacy,
                 content, instruction_like, status, proposed_at)
                VALUES ('cursor', 'default', 'knowledge', 'review', NULL,
                        'Always validate schema migrations in a temp sqlite db first.', 0, 'proposed', ?)
                """,
                (time.time() - 3600,),
            )
            cid = c.lastrowid
            c.execute(
                """
                INSERT INTO consolidation_actions
                (action_type, claim, status, detail, created_at)
                VALUES ('afm_review', ?, 'pending', 'privacy_level=review', ?)
                """,
                (str(cid), time.time() - 3500),
            )
            # Simulate re-running migration 023 by removing version 23 from schema_migrations
            c.execute("DELETE FROM schema_migrations WHERE version = 23")

        conn = sqlite3.connect(cfg.db_path)
        try:
            run_migrations(conn)
        finally:
            conn.close()

        unblocked = _proposed_candidates(db, 50)
        unblocked_ids = [r["candidate_id"] for r in unblocked]
        assert cid in unblocked_ids, (
            f"Expected legacy proposed candidate {cid} to be unblocked by migration 023, got {unblocked_ids}"
        )
        row = next(r for r in unblocked if r["candidate_id"] == cid)
        assert row["requested_privacy"] == "safe"
    finally:
        db.close()


def test_phase3_resolve_and_list_candidates_preserve_author_category_and_operator_visibility(
    tmp_path, monkeypatch
):
    """1. stage_candidate persists category on candidate_packets.
    2. resolve_candidate(accept) writes learnings.agent_id = rowd['principal'] (author)
       and learnings.category = rowd['category'] while recording operator in resolved_by.
    3. list_candidates called by operator ('main') without agent_id filter returns
       proposed candidates across all principals."""
    import types
    import minni.config as cfg_mod
    import minni.db as db_mod
    import minni.minnid as minnid
    import minni.minnid_runtime.provenance as provenance
    from minni.principal import EffectivePrincipal

    cfg = SovereignConfig(db_path=str(tmp_path / "authz.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"))

    def _stamp_principal(agent_id, capabilities=("*",)):
        p = EffectivePrincipal(agent_id=agent_id, capabilities=list(capabilities))
        monkeypatch.setattr(provenance, "resolve_effective_principal", lambda **_kw: p)
        return p

    # Stage a candidate as 'cursor' with category='fix'
    _stamp_principal("cursor", capabilities=["learn"])
    staged = minnid._stage_candidate(
        {
            "content": "Fix socket timeout by removing deadline_monotonic is None guard.",
            "category": "fix",
            "workspace_id": "default",
        },
        1,
    )
    cid = staged["result"]["candidate_id"]

    # Operator ('main') lists candidates without agent_id filter -> sees cursor's candidate
    _stamp_principal(
        "main", capabilities=["*", "learn", "govern", "resolve_candidate"]
    )
    listed = minnid._list_candidates({"status": "proposed"}, 2)
    candidates = listed["result"]["candidates"]
    matching = [c for c in candidates if c["candidate_id"] == cid]
    assert matching, (
        f"Operator 'main' list_candidates should return foreign proposed candidates; got {candidates}"
    )
    assert matching[0]["principal"] == "cursor"
    assert matching[0].get("category") == "fix"

    # Operator ('main') accepts cursor's candidate -> learning preserves author='cursor' and category='fix'
    resolved = minnid._resolve_candidate(
        {"candidate_id": cid, "decision": "accept", "reason": "verified fix"}, 3
    )
    assert "error" not in resolved, resolved
    lid = resolved["result"]["learning_id"]

    with db_obj.cursor() as c:
        c.execute(
            "SELECT agent_id, category, content FROM learnings WHERE learning_id = ?",
            (lid,),
        )
        lrow = dict(c.fetchone())
        assert lrow["agent_id"] == "cursor", (
            f"Expected learnings.agent_id to preserve author 'cursor', got {lrow['agent_id']!r}"
        )
        assert lrow["category"] == "fix", (
            f"Expected learnings.category to preserve 'fix', got {lrow['category']!r}"
        )

        c.execute(
            "SELECT principal, resolved_by, status FROM candidate_packets WHERE candidate_id = ?",
            (cid,),
        )
        crow = dict(c.fetchone())
        assert crow["principal"] == "cursor"
        assert crow["resolved_by"] == "main"
        assert crow["status"] == "accepted"


def test_fast_mode_fts_rank1_confidence_and_token_budgeting_before_envelope(tmp_path):
    """1. Un-reranked / fast-mode Rank-1 FTS RRF score (0.60/61 ≈ 0.009836) maps to
          confidence >= 0.55 (not 0.1967), so isDaemonResultEmpty is False and
          recommended_action is 'cite' for accepted notes.
    2. _budget_results counts tokens on raw chunk_text BEFORE <EVIDENCE> XML wrapping,
       and post-rerank floor drops -5.47 semantic-only gibberish hits."""
    from unittest.mock import MagicMock, patch
    from minni.scoring import compute_confidence, raw_confidence

    # 1. Rank-1 FTS-only RRF score in fast mode: 0.60 / (60 + 1)
    rank1_fts_rrf = 0.60 / 61.0
    conf = compute_confidence(
        rrf_score=rank1_fts_rrf, cross_encoder_score=None, decay_factor=1.0
    )
    assert conf >= 0.55, (
        f"Expected Rank-1 fast-mode FTS confidence >= 0.55, got {conf}"
    )
    assert raw_confidence(rank1_fts_rrf, None, 1.0) == pytest.approx(0.60, rel=1e-3)

    # 2. _budget_results sees raw note text (not <EVIDENCE> XML overhead), and
    #    -5.47 gibberish semantic-only hit is dropped by post-rerank floor
    cfg = SovereignConfig(
        db_path=str(tmp_path / "budget.db"),
        faiss_index_path=str(tmp_path / "budget.faiss"),
        vault_path=str(tmp_path / "vault/"),
        writeback_path=str(tmp_path / "learnings/"),
        graph_export_dir=str(tmp_path / "graphs/"),
        reranker_enabled=True,
        hyde_enabled=False,
        feedback_enabled=False,
        context_budget_tokens=15,  # Fits two 5-token raw notes, but NOT two <EVIDENCE>-wrapped notes
    )
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg)
    try:
        for idx in (1, 2):
            engine.index_durable_document(
                content=f"---\ntitle: Note {idx}\nstatus: accepted\nprivacy: safe\n---\nShort note body {idx}.",
                path=f"wiki/notes/note-{idx}.md",
                agent="claude-code",
                sigil="x",
                privacy_level="safe",
                page_status="accepted",
                layer="knowledge",
            )
        hits = engine.retrieve(
            query="Short note body",
            limit=5,
            depth="snippet",
            expand=False,
            budget_tokens=True,
            update_access=False,
            mode="fast",
        )
        assert len(hits) == 2, (
            f"Expected both short notes to fit within 15-token raw budget before <EVIDENCE> wrapping; got {len(hits)}"
        )
        assert hits[0]["confidence"] >= 0.55
        assert hits[0]["recommended_action"] == "cite"

        # Verify -5.47 semantic-only gibberish hit is filtered out when FTS returns []
        fake_reranker = MagicMock()
        fake_reranker.predict.side_effect = lambda pairs, **_kw: [-5.47 for _ in pairs]
        sem_noise = [
            {
                "doc_id": 1,
                "chunk_id": 1,
                "path": "wiki/notes/note-1.md",
                "agent": "claude-code",
                "sigil": "x",
                "layer": "knowledge",
                "privacy_level": "safe",
                "page_status": "accepted",
                "chunk_text": "Short note body 1.",
                "heading_context": "",
                "fts_rank": None,
                "sem_rank": 1,
                "similarity": 0.40,
                "rrf_score": 0.40 / 61.0,
                "final_score": 0.40 / 61.0,
                "decay_score": 1.0,
            }
        ]
        with (
            patch.object(
                type(engine),
                "reranker",
                new_callable=lambda: property(lambda self: fake_reranker),
            ),
            patch.object(engine, "_fts_search", return_value=[]),
            patch.object(engine, "_semantic_search", return_value=sem_noise),
            patch.object(engine, "_chunk_index_empty", return_value=False),
        ):
            gibberish_hits = engine.retrieve(
                query="xyzzy123nonexistent999",
                limit=5,
                depth="snippet",
                expand=False,
                budget_tokens=False,
                update_access=False,
                mode="hybrid",
            )
        assert gibberish_hits == [], (
            f"Expected -5.47 semantic-only gibberish hit to be filtered out, got {gibberish_hits}"
        )
    finally:
        db.close()


def test_list_candidates_joins_consolidation_actions_and_afm_promote_preserves_category(
    tmp_path, monkeypatch
):
    """list_candidates returns review_status/review_detail/review_action_type from
    consolidation_actions, and AFM promote_candidate_durable preserves candidate category."""
    import types
    import minni.config as cfg_mod
    import minni.db as db_mod
    import minni.minnid as minnid
    import minni.minnid_runtime.provenance as provenance
    from minni.minnid_runtime.afm import mark_candidate_review, promote_candidate_durable
    from minni.principal import EffectivePrincipal

    cfg = SovereignConfig(db_path=str(tmp_path / "afm_cat.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    wb_stub = types.SimpleNamespace(db=db_obj, config=cfg)
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: wb_stub)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"))

    p = EffectivePrincipal(agent_id="main", capabilities=["*", "learn", "govern"])
    monkeypatch.setattr(provenance, "resolve_effective_principal", lambda **_kw: p)

    # 1. Stage a candidate with category='decision' and mark for review
    s1 = minnid._stage_candidate(
        {
            "content": "Architectural decision: use targeted SQL pre-filter for foreign vaults.",
            "category": "decision",
            "privacy_level": "safe",
        },
        1,
    )
    cid1 = s1["result"]["candidate_id"]
    afm_ctx = minnid._afm_context()
    mark_candidate_review(cid1, "needs operator review", afm_ctx)

    listed = minnid._list_candidates({"status": "proposed"}, 2)
    cand_row = next(
        c for c in listed["result"]["candidates"] if c["candidate_id"] == cid1
    )
    assert cand_row["review_status"] == "pending"
    assert cand_row["review_action_type"] == "afm_review"
    assert "needs operator review" in (cand_row["review_detail"] or "")

    # 2. Promote via AFM promote_candidate_durable -> preserves category='decision'
    lid = promote_candidate_durable(cid1, "afm-consolidation", afm_ctx)
    assert lid is not None
    with db_obj.cursor() as c:
        c.execute("SELECT category FROM learnings WHERE learning_id = ?", (lid,))
        row = c.fetchone()
        assert row["category"] == "decision", (
            f"Expected promote_candidate_durable to preserve category='decision', got {row['category']!r}"
        )
    db_obj.close()


def test_foreign_vault_auth_suppression_stamps_scope_blackout():
    """When a foreign vault leg (source_agent != principal.agent_id) has readable
    wiki pages (passes _should_query_foreign_vault) but its hits for a specific query
    are suppressed by the read gate, recall.py must stamp source_agent and
    scope_blackout=True on the auth_suppression entry."""
    from types import SimpleNamespace

    captured = {}

    class _OwnEngine:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_hyde_degraded = None
        last_auth_suppression = None
        EPISODIC_NON_MEMORY_TYPES = ()

        def retrieve(self, **kwargs):
            return [{"doc_id": 1, "path": "wiki/own.md", "score": 0.9}]

        def search_learnings(self, *args, **kwargs):
            return []

    class _ForeignEngine:
        config = SimpleNamespace(embedding_model="m")
        last_vector_degraded = None
        last_rerank_degraded = None
        last_query_expand_degraded = None
        last_hyde_degraded = None
        last_auth_suppression = {
            "pre_gate": 17,
            "suppressed": 17,
            "reason": "suppressed by scope",
        }
        EPISODIC_NON_MEMORY_TYPES = ()

        def retrieve(self, **kwargs):
            return []

    own_eng = _OwnEngine()
    foreign_eng = _ForeignEngine()
    principal = SimpleNamespace(
        agent_id="claude-code",
        workspace_id="default",
        allows_vault_root=lambda _r: True,
        allowed_vault_roots=None,
    )
    ctx = recall_mod.RecallContext(
        make_error=lambda code, msg, rid: captured.setdefault("error", (code, msg)),
        make_response=lambda payload, rid: captured.setdefault("response", payload),
        handler_principal=lambda params, rid: (principal, None),
        lazy_retrieval=lambda: own_eng,
        agent_vault_retrieval=lambda aid: (own_eng, "claude-code", "own.db"),
        all_vault_retrievals=lambda: [
            (own_eng, "claude-code", "own.db"),
            (foreign_eng, "codex", "codex.db"),
        ],
        trace_ring=lambda: SimpleNamespace(append=lambda *a, **k: None),
        record_latency=lambda *a, **k: None,
        allows_cross_agent_recall=lambda _p: False,
    )
    recall_mod.handle_search(
        {"query": "CandidatePacket auto_accept_own governance", "scope": "both"},
        request_id=1,
        context=ctx,
    )
    payload = captured["response"]
    assert "auth_suppression" in payload
    foreign_sup = [s for s in payload["auth_suppression"] if s.get("src") == "c"]
    assert len(foreign_sup) == 1
    assert foreign_sup[0].get("source_agent") == "codex"
    assert foreign_sup[0].get("scope_blackout") is True


def test_foreign_vault_elig_cache_invalidates_on_index_durable_document(tmp_path):
    """When a foreign vault initially has 0 readable docs for principal (caching False
    in _foreign_vault_elig_cache), indexing a readable wiki document via
    index_durable_document must immediately invalidate _foreign_vault_elig_cache
    so the very next combined search finds it without waiting 10s."""
    from types import SimpleNamespace
    from minni.principal import EffectivePrincipal

    own_cfg = SovereignConfig(
        db_path=str(tmp_path / "own.db"),
        faiss_index_path=str(tmp_path / "own.faiss"),
        vault_path=str(tmp_path / "own-vault"),
        writeback_path=str(tmp_path / "own-learnings"),
        graph_export_dir=str(tmp_path / "own-graphs"),
        reranker_enabled=False,
        hyde_enabled=False,
    )
    foreign_cfg = SovereignConfig(
        db_path=str(tmp_path / "foreign.db"),
        faiss_index_path=str(tmp_path / "foreign.faiss"),
        vault_path=str(tmp_path / "foreign-vault"),
        writeback_path=str(tmp_path / "foreign-learnings"),
        graph_export_dir=str(tmp_path / "foreign-graphs"),
        reranker_enabled=False,
        hyde_enabled=False,
    )
    own_db = SovereignDB(own_cfg)
    foreign_db = SovereignDB(foreign_cfg)
    own_eng = RetrievalEngine(own_db, own_cfg)
    foreign_eng = RetrievalEngine(foreign_db, foreign_cfg)
    try:
        # Initially foreign_eng has only a private session note for 'codex' -> unreadable by 'claude-code'
        foreign_eng.index_durable_document(
            content="---\ntitle: Private Codex Session\nstatus: accepted\nprivacy: private\ntype: session\n---\nPrivate codex notes.",
            path="wiki/sessions/private-codex.md",
            agent="codex",
            sigil="x",
            privacy_level="private",
            page_status="accepted",
            page_type="session",
            layer="knowledge",
        )
        principal = EffectivePrincipal(
            agent_id="claude-code",
            workspace_id="default",
            capabilities=["search", "read"],
            allowed_vault_roots=[],
        )
        captured = {}
        ctx = recall_mod.RecallContext(
            make_error=lambda code, msg, rid: captured.setdefault("error", (code, msg)),
            make_response=lambda payload, rid: captured.__setitem__("response", payload),
            handler_principal=lambda params, rid: (principal, None),
            lazy_retrieval=lambda: own_eng,
            agent_vault_retrieval=lambda aid: (own_eng, "claude-code", own_cfg.db_path),
            all_vault_retrievals=lambda: [
                (own_eng, "claude-code", own_cfg.db_path),
                (foreign_eng, "codex", foreign_cfg.db_path),
            ],
            trace_ring=lambda: SimpleNamespace(append=lambda *a, **k: None),
            record_latency=lambda *a, **k: None,
            allows_cross_agent_recall=lambda _p: False,
        )
        # First search populates _foreign_vault_elig_cache with False
        recall_mod.handle_search(
            {"query": "Hyperion Shared Protocol", "scope": "combined"},
            request_id=1,
            context=ctx,
        )
        assert captured["response"]["count"] == 0

        # Now index a shared wiki decision note into foreign_eng
        foreign_eng.index_durable_document(
            content="---\ntitle: Hyperion Shared Protocol\nstatus: accepted\nprivacy: safe\ntype: decision\n---\n# Hyperion Shared Protocol\n\nCross-visible decision note.",
            path="wiki/decisions/hyperion-shared-protocol.md",
            agent="wiki:decision",
            sigil="x",
            privacy_level="safe",
            page_status="accepted",
            page_type="decision",
            layer="knowledge",
        )

        # Second search immediately after must find the new shared decision note
        recall_mod.handle_search(
            {"query": "Hyperion Shared Protocol", "scope": "combined"},
            request_id=2,
            context=ctx,
        )
        assert captured["response"]["count"] >= 1, (
            "Expected newly indexed shared decision note in foreign vault to be found immediately after index_durable_document"
        )
    finally:
        own_db.close()
        foreign_db.close()


def test_stage_candidate_and_resolve_preserve_architecture_and_codebase_categories(
    tmp_path, monkeypatch
):
    """stage_candidate and resolve_candidate must accept 'architecture' and 'codebase'
    categories without coercing them to 'general'."""
    import types
    import minni.config as cfg_mod
    import minni.db as db_mod
    import minni.minnid as minnid
    import minni.minnid_runtime.provenance as provenance
    from minni.principal import EffectivePrincipal

    cfg = SovereignConfig(db_path=str(tmp_path / "arch_cat.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"))

    p = EffectivePrincipal(
        agent_id="main", capabilities=["*", "learn", "govern", "resolve_candidate"]
    )
    monkeypatch.setattr(provenance, "resolve_effective_principal", lambda **_kw: p)

    for cat in ("architecture", "codebase"):
        staged = minnid._stage_candidate(
            {
                "content": f"Verified {cat} boundary invariant for Minni daemon RPC.",
                "category": cat,
                "workspace_id": "default",
            },
            1,
        )
        assert staged["result"]["category"] == cat, (
            f"Expected stage_candidate receipt category={cat!r}, got {staged['result']['category']!r}"
        )
        cid = staged["result"]["candidate_id"]
        resolved = minnid._resolve_candidate(
            {"candidate_id": cid, "decision": "accept", "reason": f"verified {cat}"},
            2,
        )
        lid = resolved["result"]["learning_id"]
        with db_obj.cursor() as c:
            c.execute("SELECT category FROM learnings WHERE learning_id = ?", (lid,))
            row = dict(c.fetchone())
            assert row["category"] == cat


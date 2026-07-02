"""
Correction re-injection tests — audit cluster C1.

Encodes the production failure: an operator looped 3 hours on a belief that
had already been corrected in the vault, because corrections never re-surfaced.

Covers:
  recall-F3   Bounded salience channel for correction-class notes in RRF scoring
  recall-F4   Decay grace window + floor for correction-class notes
  hooks-PL-1  subscribe_contradictions checked/matched discriminator
  hooks-PL-2  (a) search-path learning surfacing + learning_reads tracking
                  (the search RPC matches the learnings table directly; doc
                  retrieval can never carry learnings)
              (b) stale_beliefs no longer requires a <24h read of a SUPERSEDED
                  learning (which could never get a fresh read row)
  regression  A superseded belief with a stored correction MUST surface the
              correction via stale_beliefs.

All state is tmp_path-backed; no live ~/.minni access.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# Hermetic principal setup (same pattern as test_pr6_contradictions.py).
@pytest.fixture(autouse=True)
def setup_hermetic_principals(tmp_path, monkeypatch):
    import principal
    import minnid

    pdir = tmp_path / "principals"
    pdir.mkdir(exist_ok=True)

    original_resolve = principal.resolve_effective_principal

    def _patched_resolve(*, supplied_agent_id=None, transport="uds", principals_dir=None, operator_context=False):
        target_dir = principals_dir or pdir
        target_agent = str(supplied_agent_id or "").strip()
        # The principal file must be named for the agent it grants, else resolve
        # never matches it and the "*" grant silently degrades to caps [] (the
        # bug that let pre-cap-gate resolve_contradiction pass on operator
        # owner-check alone). {agent}.json for a named agent; local.json for the
        # default. Mirrors test_pr6_contradictions' hermetic helper.
        if target_agent:
            fname, file_agent = f"{target_agent}.json", target_agent
        else:
            fname, file_agent = "local.json", "main"
        f = target_dir / fname
        f.write_text(json.dumps({
            "agent_id": file_agent,
            "workspace_id": "default",
            "capabilities": ["*"]
        }), encoding="utf-8")
        os.chmod(f, 0o600)

        op_ctx = operator_context or (target_agent in principal.OPERATOR_RESERVED_AGENT_IDS)
        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=target_dir,
            operator_context=op_ctx,
        )

    monkeypatch.setattr(principal, "resolve_effective_principal", _patched_resolve)
    monkeypatch.setattr(minnid, "resolve_effective_principal", _patched_resolve)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path, **cfg_overrides):
    """Return (SovereignDB, SovereignConfig) backed by a temporary SQLite file."""
    import db as db_mod
    from config import SovereignConfig

    db_path = str(tmp_path / "test.db")
    cfg = SovereignConfig(db_path=db_path, **cfg_overrides)

    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    return db_obj, cfg


def _make_engine(tmp_path):
    """RetrievalEngine against a fresh test DB, no FAISS/model loading."""
    from retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
    return engine, db_obj, cfg


def _patch_writeback(tmp_path, monkeypatch):
    """Point the minnid singleton at a fresh test DB (no embedding model)."""
    import minnid
    import writeback as wb_mod
    from writeback import WriteBackMemory

    db_obj, cfg = _make_db(tmp_path)
    wb = WriteBackMemory(db_obj, cfg)
    monkeypatch.setattr(minnid, "_writeback", wb)
    monkeypatch.setattr(
        wb_mod.WriteBackMemory, "model", property(lambda self: None)
    )
    return wb, db_obj, cfg


def _patch_engine_and_writeback(tmp_path, monkeypatch):
    """Wire ONE test DB into both minnid singletons: a real RetrievalEngine
    (embedding model disabled, reranker/HyDE off — pure local FTS + learnings
    matching) and WriteBackMemory. Dispatch tests can then exercise the
    production search RPC end to end with no FakeEngine."""
    import minnid
    import writeback as wb_mod
    from retrieval import RetrievalEngine
    from writeback import WriteBackMemory

    db_obj, cfg = _make_db(tmp_path, reranker_enabled=False, hyde_enabled=False)
    wb = WriteBackMemory(db_obj, cfg)
    monkeypatch.setattr(minnid, "_writeback", wb)
    monkeypatch.setattr(
        wb_mod.WriteBackMemory, "model", property(lambda self: None)
    )
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
    monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: None))
    monkeypatch.setattr(minnid, "_retrieval", engine)
    return engine, db_obj, cfg


def _insert_learning(db_obj, content, agent="codex", created_at=None):
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO learnings
               (agent_id, category, content, confidence, created_at, status)
               VALUES (?, 'fact', ?, 1.0, ?, 'active')""",
            (agent, content, created_at or time.time()),
        )
        return c.lastrowid


def _dispatch(method, params):
    from minnid import _dispatch_sync
    return _dispatch_sync({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    })


def _rrf_doc(doc_id, page_type, decay_score):
    return {
        "doc_id": doc_id,
        "path": f"/vault/{doc_id}.md",
        "agent": "test",
        "sigil": "T",
        "decay_score": decay_score,
        "page_type": page_type,
        "chunk_text": f"doc {doc_id}",
        "heading_context": "",
        "page_status": "accepted",
        "privacy_level": "safe",
        "evidence_refs": None,
        "indexed_at": time.time(),
        "layer": "knowledge",
    }


def _insert_document(db_obj, path, page_type, indexed_at, last_accessed,
                     access_count, decay_score=1.0):
    with db_obj.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at, last_accessed,
                access_count, decay_score, whole_document, page_status,
                privacy_level, page_type)
               VALUES (?, 'test', 'T', ?, ?, ?, ?, ?, 0, 'accepted', 'safe', ?)""",
            (path, indexed_at, indexed_at, last_accessed, access_count,
             decay_score, page_type),
        )
        return c.lastrowid


# ---------------------------------------------------------------------------
# recall-F3 — salience channel in RRF scoring
# ---------------------------------------------------------------------------

class TestCorrectionSalience:

    def test_fresh_correction_outranks_saturated_habitual_hit(self, tmp_path):
        """The audited inversion: identity/habitual doc saturated at decay=1.0
        vs a 1-day-old unaccessed correction at decay=0.906. With equal RRF
        contribution the correction must now rank first."""
        engine, db_obj, cfg = _make_engine(tmp_path)

        habitual = _rrf_doc(1, None, 1.0)        # reread stale belief, decay saturated
        correction = _rrf_doc(2, "correction", 0.906)  # 1 day old, never accessed

        # Same rank in both streams → identical rrf_score.
        merged = engine._rrf_merge([habitual, correction], [habitual, correction], 10)
        by_id = {d["doc_id"]: d for d in merged}

        assert by_id[2]["salience_boost"] == pytest.approx(0.25)
        assert by_id[1]["salience_boost"] == 0.0
        # decay floor does not matter here (0.906 > 0.5); boost alone must win:
        # rrf * 0.906 * 1.25 = rrf * 1.1325 > rrf * 1.0
        assert merged[0]["doc_id"] == 2, "fresh correction must outrank the stale habitual hit"

    def test_non_correction_scoring_unchanged(self, tmp_path):
        """Default scoring stays exactly rrf * decay for ordinary notes."""
        engine, db_obj, cfg = _make_engine(tmp_path)
        doc = _rrf_doc(7, "concept", 0.8)
        merged = engine._rrf_merge([doc], [doc], 10)
        d = merged[0]
        assert d["salience_boost"] == 0.0
        assert d["final_score"] == pytest.approx(d["rrf_score"] * 0.8)

    def test_correction_decay_floor_applies_in_scoring(self, tmp_path):
        """A heavily decayed correction is scored with the configured floor so
        it cannot fade below the belief it superseded."""
        engine, db_obj, cfg = _make_engine(tmp_path)
        faded = _rrf_doc(3, "decision", 0.05)
        merged = engine._rrf_merge([faded], [faded], 10)
        d = merged[0]
        assert d["final_score"] == pytest.approx(
            d["rrf_score"] * cfg.correction_decay_floor * (1 + cfg.correction_salience_boost)
        )

    def test_multi_backend_merge_applies_same_salience(self, tmp_path):
        """_rrf_merge_multi mirrors _rrf_merge (one-sided fixes are the
        codebase's #1 bug class)."""
        engine, db_obj, cfg = _make_engine(tmp_path)
        habitual = _rrf_doc(1, None, 1.0)
        correction = _rrf_doc(2, "correction", 0.906)
        merged = engine._rrf_merge_multi(
            [habitual, correction], [habitual, correction], [], 10
        )
        assert merged[0]["doc_id"] == 2
        assert {d["doc_id"]: d for d in merged}[2]["salience_boost"] == pytest.approx(0.25)

    def test_score_merged_doc_tolerates_explicit_none_decay(self, tmp_path):
        """A merged doc carrying decay_score=None (e.g. from a downstream
        caller) must score with the 1.0 default, not TypeError in max()."""
        engine, db_obj, cfg = _make_engine(tmp_path)
        d = _rrf_doc(4, "correction", None)
        d["rrf_score"] = 0.5
        engine._score_merged_doc(d)
        assert d["final_score"] == pytest.approx(
            0.5 * 1.0 * (1 + cfg.correction_salience_boost)
        )
        plain = _rrf_doc(5, "concept", None)
        plain["rrf_score"] = 0.5
        engine._score_merged_doc(plain)
        assert plain["final_score"] == pytest.approx(0.5)

    def test_reranker_propagates_correction_boost(self, tmp_path):
        """recall-F3 reranker leg: with the cross-encoder active the final
        ordering is logit-driven — the salience boost must reach rerank_score
        or the whole fix is bypassed on the default reranker_enabled=True
        path."""
        engine, db_obj, cfg = _make_engine(tmp_path)

        class FakeReranker:
            model_name = "fake-ce"

            def predict(self, pairs):
                # habitual hit scores slightly above the correction raw.
                return [1.0, 0.9]

        engine._reranker = FakeReranker()
        habitual = _rrf_doc(1, "concept", 1.0)
        correction = _rrf_doc(2, "correction", 1.0)
        ranked = engine._rerank("service x port", [habitual, correction])
        assert ranked[0]["doc_id"] == 2, \
            "boosted correction logit must outrank the slightly-higher habitual logit"
        assert ranked[0]["rerank_score"] == pytest.approx(
            0.9 * (1 + cfg.correction_salience_boost)
        )
        assert ranked[0]["salience_boost"] == pytest.approx(cfg.correction_salience_boost)
        # the non-correction logit is untouched
        assert {d["doc_id"]: d for d in ranked}[1]["rerank_score"] == pytest.approx(1.0)

    def test_reranker_boost_is_sign_safe_for_negative_logits(self, tmp_path):
        """Cross-encoder logits can be negative; a multiplicative boost on a
        negative logit would DEMOTE the correction. The boost must move a
        correction up regardless of logit sign."""
        engine, db_obj, cfg = _make_engine(tmp_path)

        class FakeReranker:
            model_name = "fake-ce"

            def predict(self, pairs):
                return [-0.9, -1.0]

        engine._reranker = FakeReranker()
        habitual = _rrf_doc(1, "concept", 1.0)
        correction = _rrf_doc(2, "correction", 1.0)
        ranked = engine._rerank("service x port", [habitual, correction])
        assert ranked[0]["doc_id"] == 2
        assert ranked[0]["rerank_score"] == pytest.approx(
            -1.0 / (1 + cfg.correction_salience_boost)
        )

    def test_reranker_boost_lifts_zero_logit_corrections(self, tmp_path):
        """A correction whose raw logit is exactly 0.0 previously got
        0.0 * (1 + boost) = 0.0 — no lift at all. It must land at +boost so it
        outranks zero-logit habitual hits."""
        engine, db_obj, cfg = _make_engine(tmp_path)

        class FakeReranker:
            model_name = "fake-ce"

            def predict(self, pairs):
                return [0.0, 0.0]

        engine._reranker = FakeReranker()
        habitual = _rrf_doc(1, "concept", 1.0)
        correction = _rrf_doc(2, "correction", 1.0)
        ranked = engine._rerank("service x port", [habitual, correction])
        assert ranked[0]["doc_id"] == 2, \
            "zero-logit correction must outrank the zero-logit habitual hit"
        assert ranked[0]["rerank_score"] == pytest.approx(
            cfg.correction_salience_boost
        )
        assert {d["doc_id"]: d for d in ranked}[1]["rerank_score"] == 0.0

    def test_reranker_boost_applies_on_cache_hit_branch(self, tmp_path):
        """The `not missing` early-return branch (every score served from the
        rerank cache, model never called) must apply the correction boost too:
        the cache stores raw model scores, so the boost is re-derived on every
        call — including fully-cached ones."""
        from rerank_cache import GLOBAL_RERANK_CACHE

        engine, db_obj, cfg = _make_engine(tmp_path)

        class ExplodingReranker:
            model_name = "fake-ce"

            def predict(self, pairs):
                raise AssertionError("cache-hit branch must not call the model")

        engine._reranker = ExplodingReranker()
        query = "cache-hit correction boost branch query"
        habitual = _rrf_doc(1, "concept", 1.0)
        habitual["chunk_id"] = 9101
        correction = _rrf_doc(2, "correction", 1.0)
        correction["chunk_id"] = 9102
        model_name, model_version = engine._reranker_identity(engine._reranker)
        GLOBAL_RERANK_CACHE.set(model_name, model_version, query, 9101, 1.0)
        GLOBAL_RERANK_CACHE.set(model_name, model_version, query, 9102, 0.9)
        try:
            ranked = engine._rerank(query, [habitual, correction])
        finally:
            GLOBAL_RERANK_CACHE.invalidate_chunks([9101, 9102])
        assert ranked[0]["doc_id"] == 2, \
            "boost must apply on the all-cached branch, not just after predict()"
        assert ranked[0]["rerank_score"] == pytest.approx(
            0.9 * (1 + cfg.correction_salience_boost)
        )
        assert {d["doc_id"]: d for d in ranked}[1]["rerank_score"] == pytest.approx(1.0)

    def test_score_merged_doc_zero_decay_is_not_coerced_to_one(self, tmp_path):
        """decay_score=0.0 is a real value (fully decayed), not a missing one:
        the old falsy-or coerced it to 1.0, silently un-decaying the doc."""
        engine, db_obj, cfg = _make_engine(tmp_path)
        plain = _rrf_doc(6, "concept", 0.0)
        plain["rrf_score"] = 0.5
        engine._score_merged_doc(plain)
        assert plain["final_score"] == 0.0
        # Corrections lift fully-decayed scores to the decay floor instead.
        corr = _rrf_doc(7, "correction", 0.0)
        corr["rrf_score"] = 0.5
        engine._score_merged_doc(corr)
        assert corr["final_score"] == pytest.approx(
            0.5 * cfg.correction_decay_floor * (1 + cfg.correction_salience_boost)
        )


# ---------------------------------------------------------------------------
# recall-F4 — decay grace window + floor
# ---------------------------------------------------------------------------

class TestCorrectionDecay:

    # All four correction-class types from config.correction_page_types — a
    # typo or omission in the config set must fail these, not pass silently.
    @pytest.mark.parametrize(
        "page_type", ["correction", "contradiction", "decision", "fix"]
    )
    def test_fresh_correction_holds_full_strength(self, tmp_path, page_type):
        """A 1-day-old unaccessed correction must not decay below a stale
        belief that is reread every boot (the audited 0.906-vs-1.0 inversion)."""
        from decay import MemoryDecay

        db_obj, cfg = _make_db(tmp_path)
        assert page_type in cfg.correction_page_types
        now = time.time()
        belief_id = _insert_document(
            db_obj, "/vault/belief.md", None,
            indexed_at=now - 30 * 86400, last_accessed=now, access_count=20,
        )
        correction_id = _insert_document(
            db_obj, "/vault/correction.md", page_type,
            indexed_at=now - 1 * 86400, last_accessed=None, access_count=0,
        )

        MemoryDecay(db_obj, cfg).run_decay()

        with db_obj.cursor() as c:
            scores = {
                row["doc_id"]: row["decay_score"]
                for row in c.execute("SELECT doc_id, decay_score FROM documents")
            }
        assert scores[correction_id] == pytest.approx(1.0), \
            "correction inside grace window must hold decay=1.0"
        assert scores[correction_id] >= scores[belief_id], \
            "correction must not decay below the belief it supersedes"

    @pytest.mark.parametrize(
        "page_type", ["correction", "contradiction", "decision", "fix"]
    )
    def test_old_correction_floors_instead_of_fading(self, tmp_path, page_type):
        """Past the grace window a correction decays normally but never below
        correction_decay_floor."""
        from decay import MemoryDecay

        db_obj, cfg = _make_db(tmp_path)
        assert page_type in cfg.correction_page_types
        now = time.time()
        old_correction = _insert_document(
            db_obj, "/vault/old-correction.md", page_type,
            indexed_at=now - 90 * 86400, last_accessed=None, access_count=0,
        )
        old_note = _insert_document(
            db_obj, "/vault/old-note.md", None,
            indexed_at=now - 90 * 86400, last_accessed=None, access_count=0,
        )

        MemoryDecay(db_obj, cfg).run_decay()

        with db_obj.cursor() as c:
            scores = {
                row["doc_id"]: row["decay_score"]
                for row in c.execute("SELECT doc_id, decay_score FROM documents")
            }
        assert scores[old_correction] == pytest.approx(cfg.correction_decay_floor)
        assert scores[old_note] == pytest.approx(cfg.decay_min_score)


# ---------------------------------------------------------------------------
# hooks-PL-2 leg (a) — search-path learning surfacing + learning_reads tracking
# ---------------------------------------------------------------------------

class TestSearchLearningReads:

    def test_search_dispatch_surfaces_and_tracks_learnings(self, tmp_path, monkeypatch):
        """The production wiring with the REAL RetrievalEngine (no FakeEngine):
        the 'search' RPC matches the learnings table for the query, surfaces
        the hits in the response, and writes a learning_reads row for each.
        The previous learning:// scan over retrieve() results was dead code —
        learning:// rows are never indexed in vault_fts/FAISS, so retrieve()
        can never return them."""
        engine, db_obj, cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        # An ORDINARY learning: no evidence docs, no learning:// document row.
        lid = _insert_learning(db_obj, "service X listens on port 8080")

        resp = _dispatch("search", {
            "query": "service X port", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        result = resp["result"]
        assert [l["learning_id"] for l in result["learnings"]] == [lid]
        assert "port 8080" in result["learnings"][0]["content"]

        with db_obj.cursor() as c:
            rows = c.execute(
                "SELECT learning_id, agent_id, source FROM learning_reads"
            ).fetchall()
        assert [(r["learning_id"], r["agent_id"], r["source"]) for r in rows] == [
            (lid, "codex", "minnid.search"),
        ]

    def test_search_dispatch_does_not_resurface_superseded_learnings(self, tmp_path, monkeypatch):
        """Superseded beliefs must not be re-surfaced (or re-read-tracked) by
        the search RPC — only active learnings qualify."""
        engine, db_obj, cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        stale = _insert_learning(db_obj, "service X listens on port 8080")
        current = _insert_learning(db_obj, "service X moved to port 9090")
        with db_obj.cursor() as c:
            c.execute(
                "UPDATE learnings SET superseded_by = ? WHERE learning_id = ?",
                (current, stale),
            )

        resp = _dispatch("search", {
            "query": "service X port", "agent_id": "codex", "expand": False,
        })
        assert "error" not in resp
        surfaced = [l["learning_id"] for l in resp["result"]["learnings"]]
        assert surfaced == [current]
        with db_obj.cursor() as c:
            rows = c.execute(
                "SELECT learning_id FROM learning_reads ORDER BY learning_id"
            ).fetchall()
        assert [r["learning_id"] for r in rows] == [current]

    def test_search_learnings_records_reads(self, tmp_path):
        """retrieval.search_learnings is a read — it must write learning_reads
        (the tracking block previously lived in search_episodic, where it
        crashed on a missing learning_id key)."""
        engine, db_obj, cfg = _make_engine(tmp_path)
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
                   VALUES ('codex', 'fix', 'websocket reconnect needs 500ms backoff', 1.0, ?)""",
                (now,),
            )
            lid = c.lastrowid

        results = engine.search_learnings("websocket backoff", agent_id="codex")
        assert [r["learning_id"] for r in results] == [lid]

        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT agent_id, source FROM learning_reads WHERE learning_id = ?",
                (lid,),
            ).fetchone()
            access = c.execute(
                "SELECT access_count FROM learnings WHERE learning_id = ?", (lid,)
            ).fetchone()
        assert row is not None, "search_learnings must record a learning_reads row"
        assert row["agent_id"] == "codex"
        assert row["source"] == "retrieval.search_learnings"
        assert access["access_count"] == 1

    def test_search_learnings_same_tick_rereads_do_not_drop_tracking(self, tmp_path, monkeypatch):
        """The learning_reads PK is (learning_id, agent_id, read_at): two
        searches in the same clock tick collide, and the plain INSERT raised
        an IntegrityError that the except swallowed — dropped tracking.
        INSERT OR IGNORE keeps the existing row, the results, and the access
        bump."""
        engine, db_obj, cfg = _make_engine(tmp_path)
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at)
                   VALUES ('codex', 'fix', 'websocket reconnect needs 500ms backoff', 1.0, ?)""",
                (now,),
            )
            lid = c.lastrowid

        frozen = time.time()
        monkeypatch.setattr(time, "time", lambda: frozen)
        first = engine.search_learnings("websocket backoff", agent_id="codex")
        second = engine.search_learnings("websocket backoff", agent_id="codex")
        assert [r["learning_id"] for r in first] == [lid]
        assert [r["learning_id"] for r in second] == [lid]

        with db_obj.cursor() as c:
            count = c.execute(
                "SELECT COUNT(*) AS n FROM learning_reads WHERE learning_id = ?",
                (lid,),
            ).fetchone()["n"]
            access = c.execute(
                "SELECT access_count FROM learnings WHERE learning_id = ?", (lid,)
            ).fetchone()["access_count"]
        assert count == 1, "same-instant duplicate read collapses to one row"
        assert access == 2, "the access bump must survive the PK collision"

    def test_search_episodic_no_longer_crashes_on_hits(self, tmp_path):
        """search_episodic previously raised KeyError('learning_id') on any hit."""
        engine, db_obj, cfg = _make_engine(tmp_path)
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO episodic_events (agent_id, event_type, content, created_at)
                   VALUES ('codex', 'observation', 'deployment rollback completed', ?)""",
                (now,),
            )

        results = engine.search_episodic("deployment rollback", agent_id="codex")
        assert len(results) == 1
        assert results[0]["event_type"] == "observation"


# ---------------------------------------------------------------------------
# hooks-PL-1 / hooks-PL-2 leg (b) — stale_beliefs discriminator + matching
# ---------------------------------------------------------------------------

class TestStaleBeliefs:

    def test_regression_correction_surfaces_for_old_read(self, tmp_path, monkeypatch):
        """THE seed failure: belief read >24h ago, then corrected. The old
        JOIN (lr.read_at >= now-24h) returned events:[] forever; any
        historical read must now match."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                   VALUES ('a', 'fact', 'auth uses JWT tokens', 1.0, ?, 'active')""",
                (now - 5 * 86400,),
            )
            belief_id = c.lastrowid
            # Read 3 days ago — outside the old 24h window.
            c.execute(
                """INSERT INTO learning_reads (learning_id, agent_id, read_at, source)
                   VALUES (?, 'codex', ?, 'minnid.search')""",
                (belief_id, now - 3 * 86400),
            )

        resolved = _dispatch("resolve_contradiction", {
            "new_content": "Corrected: auth uses session cookies since April 2026",
            "supersede_ids": [belief_id],
            "agent_id": "operator",
        })
        assert resolved["result"]["status"] == "ok"

        subscribed = _dispatch("minni_subscribe_contradictions", {"agent_id": "codex"})
        assert "error" not in subscribed
        result = subscribed["result"]
        assert result["status"] == "matched"
        assert len(result["events"]) == 1
        assert result["events"][0]["superseded_learning_id"] == belief_id
        assert result["events"][0]["new_learning_id"] == resolved["result"]["new_learning_id"]

    def test_empty_result_is_discriminated_not_silent(self, tmp_path, monkeypatch):
        """hooks-PL-1: events:[] must carry checked/no-match diagnostics so
        silence is honest."""
        _patch_writeback(tmp_path, monkeypatch)

        subscribed = _dispatch("minni_subscribe_contradictions", {"agent_id": "codex"})
        assert "error" not in subscribed
        result = subscribed["result"]
        assert result["events"] == []
        assert result["status"] == "checked_no_match"
        checked = result["checked"]
        # R10: the unscoped global "events in window" count is no longer returned.
        assert "contradiction_events_in_window" not in checked
        assert checked["contradiction_events_for_agent_reads"] == 0
        assert checked["learning_reads_for_agent"] == 0
        assert checked["read_window_hours"] is None
        assert checked["event_window_days"] == 30

    def test_explicit_read_window_still_filters(self, tmp_path, monkeypatch):
        """Callers can opt back into read-recency filtering."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                   VALUES ('a', 'fact', 'old belief', 1.0, ?, 'active')""",
                (now - 5 * 86400,),
            )
            belief_id = c.lastrowid
            c.execute(
                """INSERT INTO learning_reads (learning_id, agent_id, read_at, source)
                   VALUES (?, 'codex', ?, 'minnid.search')""",
                (belief_id, now - 3 * 86400),
            )

        resolved = _dispatch("resolve_contradiction", {
            "new_content": "corrected belief",
            "supersede_ids": [belief_id],
            "agent_id": "operator",
        })
        assert resolved["result"]["status"] == "ok"

        subscribed = _dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "read_window_hours": 24,
        })
        result = subscribed["result"]
        assert result["events"] == []
        assert result["status"] == "checked_no_match"
        assert result["checked"]["read_window_hours"] == 24
        # R10: global count dropped; the agent-scoped read count remains the
        # meaningful, non-leaking signal. codex DID read the superseded belief,
        # so the agent-scoped count is 1 even though the read_window filtered the
        # event out of the returned list.
        assert "contradiction_events_in_window" not in result["checked"]
        assert result["checked"]["contradiction_events_for_agent_reads"] == 1

    def test_event_window_bounds_old_events(self, tmp_path, monkeypatch):
        """Events older than event_window_days are not re-surfaced at boot."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                   VALUES ('a', 'fact', 'ancient belief', 1.0, ?, 'superseded')""",
                (now - 100 * 86400,),
            )
            belief_id = c.lastrowid
            c.execute(
                """INSERT INTO learning_reads (learning_id, agent_id, read_at, source)
                   VALUES (?, 'codex', ?, 'minnid.read')""",
                (belief_id, now - 95 * 86400),
            )
            c.execute(
                """INSERT INTO contradiction_events
                   (superseded_learning_id, new_learning_id, originating_agent, created_at)
                   VALUES (?, 999, 'operator', ?)""",
                (belief_id, now - 60 * 86400),
            )

        subscribed = _dispatch("minni_subscribe_contradictions", {"agent_id": "codex"})
        result = subscribed["result"]
        assert result["events"] == []
        assert result["status"] == "checked_no_match"
        # ...unless the caller widens the window explicitly.
        subscribed = _dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "event_window_days": 90,
        })
        assert subscribed["result"]["status"] == "matched"
        assert len(subscribed["result"]["events"]) == 1


# ---------------------------------------------------------------------------
# End-to-end regression: search → correct → stale_beliefs
# ---------------------------------------------------------------------------

class TestEndToEndReinjection:

    def test_search_read_then_correction_fires_stale_beliefs(self, tmp_path, monkeypatch):
        """Full REAL chain for the hook recall path (no FakeEngine): the
        belief is surfaced by the production search RPC — the real
        RetrievalEngine matching the learnings table — which writes the
        learning_reads row; the belief is then superseded; stale_beliefs MUST
        fire — encoding the operator's 3-hour-loop failure."""
        engine, db_obj, cfg = _patch_engine_and_writeback(tmp_path, monkeypatch)
        belief_id = _insert_learning(
            db_obj, "service X listens on port 8080",
            created_at=time.time() - 2 * 86400,
        )

        # 1. The hook recall path surfaces the belief via the search RPC; the
        #    real engine matches the learnings table and records the read.
        searched = _dispatch("search", {
            "query": "service X port", "agent_id": "codex", "expand": False,
        })
        assert "error" not in searched
        assert [l["learning_id"] for l in searched["result"]["learnings"]] == [belief_id]
        with db_obj.cursor() as c:
            read = c.execute(
                "SELECT agent_id, source FROM learning_reads WHERE learning_id = ?",
                (belief_id,),
            ).fetchone()
        assert read is not None, "the search RPC must record the learning read"
        assert read["agent_id"] == "codex"
        assert read["source"] == "minnid.search"

        # 2. The belief is corrected (operator stores a correction).
        resolved = _dispatch("resolve_contradiction", {
            "new_content": "Correction: service X moved to port 9090 on 2026-06-01",
            "supersede_ids": [belief_id],
            "agent_id": "operator",
        })
        assert resolved["result"]["status"] == "ok"

        # 3. Next boot: stale_beliefs must surface the correction.
        subscribed = _dispatch("minni_subscribe_contradictions", {"agent_id": "codex"})
        result = subscribed["result"]
        assert result["status"] == "matched"
        assert [e["superseded_learning_id"] for e in result["events"]] == [belief_id]

        # 3b. A new search must never re-surface the superseded belief (and
        #     therefore never write a fresh read row for it) — the correction
        #     itself reaches codex via stale_beliefs above.
        searched_again = _dispatch("search", {
            "query": "service X port", "agent_id": "codex", "expand": False,
        })
        surfaced = [l["learning_id"] for l in searched_again["result"]["learnings"]]
        assert belief_id not in surfaced

        # 4. The CORRECTED TEXT must be reachable via new_learning_id — the
        # production failure was the operator never seeing the new content,
        # so ID linkage alone does not prove the data path.
        new_lid = result["events"][0]["new_learning_id"]
        assert new_lid == resolved["result"]["new_learning_id"]
        with db_obj.cursor() as c:
            row = c.execute(
                "SELECT content, superseded_by FROM learnings WHERE learning_id = ?",
                (new_lid,),
            ).fetchone()
            old = c.execute(
                "SELECT superseded_by FROM learnings WHERE learning_id = ?",
                (belief_id,),
            ).fetchone()
        assert row is not None
        assert "port 9090" in row["content"]
        assert row["superseded_by"] is None, "the correction itself must be active"
        assert old["superseded_by"] == new_lid


# ---------------------------------------------------------------------------
# subscribe_contradictions parameter hygiene (review round 2)
# ---------------------------------------------------------------------------

class TestSubscribeContradictionsParams:

    def _seed_corrected_belief(self, db_obj, agent="codex"):
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                   VALUES ('a', 'fact', 'belief', 1.0, ?, 'active')""",
                (now - 5 * 86400,),
            )
            belief_id = c.lastrowid
            c.execute(
                """INSERT INTO learning_reads (learning_id, agent_id, read_at, source)
                   VALUES (?, ?, ?, 'minnid.search')""",
                (belief_id, agent, now - 3 * 86400),
            )
        resolved = _dispatch("resolve_contradiction", {
            "new_content": "corrected belief",
            "supersede_ids": [belief_id],
            "agent_id": "operator",
        })
        assert resolved["result"]["status"] == "ok"
        return belief_id

    def test_read_window_zero_is_honored_not_coerced(self, tmp_path, monkeypatch):
        """read_window_hours=0 means 'no reads qualify' — the old falsy-or
        guard silently substituted 24h."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        self._seed_corrected_belief(db_obj)

        subscribed = _dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "read_window_hours": 0,
        })
        result = subscribed["result"]
        assert result["events"] == []
        assert result["status"] == "checked_no_match"
        assert result["checked"]["read_window_hours"] == 0

    def test_checked_reports_agent_scoped_event_count(self, tmp_path, monkeypatch):
        """Another agent's stale belief must show up in the global window
        count but NOT in the agent-scoped count, so events:[] is explainable."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        self._seed_corrected_belief(db_obj, agent="other-agent")

        subscribed = _dispatch("minni_subscribe_contradictions", {"agent_id": "codex"})
        result = subscribed["result"]
        assert result["events"] == []
        checked = result["checked"]
        # R10: another agent's contradiction event must NOT be visible to codex.
        # The global count that leaked it is dropped; the agent-scoped count is 0.
        assert "contradiction_events_in_window" not in checked
        assert checked["contradiction_events_for_agent_reads"] == 0

    def test_window_params_are_clamped(self, tmp_path, monkeypatch):
        """Local-DoS guard: huge windows / future since_ts are clamped, and
        the clamped event_since is echoed so callers can detect it."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        self._seed_corrected_belief(db_obj)
        now = time.time()

        subscribed = _dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "event_window_days": 1e18,
            "read_window_hours": 1e18,
            "since_ts": 9999999999999.0,
        })
        result = subscribed["result"]
        checked = result["checked"]
        assert checked["event_window_days"] == 365.0
        assert checked["read_window_hours"] == 8760.0
        assert checked["since_ts"] <= now + 120
        # event_since stays within the clamped window (never full-history 0).
        assert checked["event_since"] >= now - 365 * 86400 - 120

    def test_event_window_zero_is_honored_not_coerced(self, tmp_path, monkeypatch):
        """event_window_days=0 means 'no historic events' — the old falsy-or
        guard silently substituted 30 (mirror of the read_window_hours=0
        fix; the docstring's [0, 365] clamp must actually reach 0)."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        self._seed_corrected_belief(db_obj)

        subscribed = _dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "event_window_days": 0,
        })
        result = subscribed["result"]
        assert result["events"] == []
        assert result["status"] == "checked_no_match"
        assert result["checked"]["event_window_days"] == 0

    def test_nan_since_ts_does_not_suppress_matches(self, tmp_path, monkeypatch):
        """NaN since_ts is truthy (survives the falsy-or) and makes every SQL
        comparison false — a poisoned query returned the same
        checked_no_match shape as a genuinely empty table, defeating the
        hooks-PL-1 discriminator. Non-finite falls back to the default 0."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        belief_id = self._seed_corrected_belief(db_obj)

        subscribed = _dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "since_ts": float("nan"),
        })
        result = subscribed["result"]
        assert result["status"] == "matched"
        assert [e["superseded_learning_id"] for e in result["events"]] == [belief_id]

    def test_nan_event_window_does_not_bypass_dos_cap(self, tmp_path, monkeypatch):
        """min/max do not clamp NaN: a NaN event_window_days collapsed
        event_since to 0.0 (max(0.0, nan) is 0.0 in CPython) — the exact
        full-history scan the cap was introduced to block. Non-finite falls
        back to the default 30-day window."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        self._seed_corrected_belief(db_obj)
        now = time.time()

        for poisoned in (float("nan"), float("inf"), "nan"):
            subscribed = _dispatch("minni_subscribe_contradictions", {
                "agent_id": "codex",
                "event_window_days": poisoned,
            })
            checked = subscribed["result"]["checked"]
            assert checked["event_window_days"] == 30.0
            assert checked["event_since"] == pytest.approx(now - 30 * 86400, abs=120)

    def test_nan_read_window_falls_back_to_default(self, tmp_path, monkeypatch):
        """A non-finite read_window_hours must behave like the default (no
        read-recency filter), not poison read_since."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        belief_id = self._seed_corrected_belief(db_obj)

        subscribed = _dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "read_window_hours": float("nan"),
        })
        result = subscribed["result"]
        assert result["status"] == "matched"
        assert [e["superseded_learning_id"] for e in result["events"]] == [belief_id]
        assert result["checked"]["read_window_hours"] is None

    def test_since_ts_zero_is_capped_to_event_window(self, tmp_path, monkeypatch):
        """since_ts=0 does NOT mean all-history: the 30-day default window is
        a hard cap, surfaced via checked.event_since."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        now = time.time()
        subscribed = _dispatch("minni_subscribe_contradictions", {
            "agent_id": "codex",
            "since_ts": 0,
        })
        checked = subscribed["result"]["checked"]
        assert checked["since_ts"] == 0
        assert checked["event_since"] == pytest.approx(now - 30 * 86400, abs=120)

    def test_non_numeric_params_return_invalid_params_error(self, tmp_path, monkeypatch):
        """Non-numeric since_ts/read_window_hours/event_window_days must be a
        JSON-RPC -32602, never an unhandled ValueError/TypeError — the float()
        conversions used to sit outside any try, so {"since_ts": "foo"} killed
        the whole daemon connection with no error response."""
        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        for params in (
            {"since_ts": "foo"},
            {"read_window_hours": "foo"},
            {"event_window_days": "foo"},
            {"since_ts": [1]},
            {"read_window_hours": {}},
            {"event_window_days": [1]},
        ):
            resp = _dispatch(
                "minni_subscribe_contradictions", {"agent_id": "codex", **params}
            )
            assert "result" not in resp, f"{params} must not succeed"
            assert resp["error"]["code"] == -32602, \
                f"{params} must produce JSON-RPC invalid-params, got {resp['error']}"


# ---------------------------------------------------------------------------
# Real-pipeline integration: engine.retrieve() over a corrections DB
# ---------------------------------------------------------------------------

class TestRetrieveIntegration:

    def _insert_indexed_doc(self, db_obj, path, page_type, decay_score, content):
        """A document that is actually retrievable: documents row + vault_fts
        row (the same dual write the indexer performs)."""
        now = time.time()
        doc_id = _insert_document(
            db_obj, path, page_type,
            indexed_at=now - 86400, last_accessed=now, access_count=0,
            decay_score=decay_score,
        )
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
                   VALUES (?, ?, ?, 'test', 'T')""",
                (doc_id, path, content),
            )
        return doc_id

    def test_real_retrieve_correction_outranks_saturated_habitual_hit(self, tmp_path, monkeypatch):
        """No FakeEngine: the full retrieve() pipeline (FTS → RRF → salience
        scoring → status filter → formatting) over a DB holding a saturated
        habitual hit (decay=1.0) and a fresher correction (decay=0.906) must
        rank the correction first — the audited inversion, end to end. Both
        docs carry identical FTS content so only decay/salience can decide."""
        from retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path, reranker_enabled=False, hyde_enabled=False)
        monkeypatch.setattr(RetrievalEngine, "model", property(lambda self: None))
        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

        text = "service X listens on port 8080 per the runbook"
        habitual_id = self._insert_indexed_doc(
            db_obj, "/vault/wiki/concepts/habitual.md", None, 1.0, text)
        correction_id = self._insert_indexed_doc(
            db_obj, "/vault/wiki/decisions/correction.md", "correction", 0.906, text)

        results = engine.retrieve(
            "service X port", limit=5, expand=False, use_hyde=False)
        assert sorted(r["doc_id"] for r in results) == sorted(
            [habitual_id, correction_id])
        assert results[0]["doc_id"] == correction_id, \
            "fresh correction must outrank the saturated habitual hit"
        assert results[0]["provenance"]["salience_boost"] == pytest.approx(
            cfg.correction_salience_boost)

        # Control: with the salience boost zeroed the saturated habitual hit
        # wins — proving the boost (not FTS tie-break order) flips the result.
        from config import SovereignConfig
        cfg_off = SovereignConfig(
            db_path=cfg.db_path, reranker_enabled=False, hyde_enabled=False,
            correction_salience_boost=0.0,
        )
        engine_off = RetrievalEngine(db_obj, cfg_off, faiss_index=object())
        results_off = engine_off.retrieve(
            "service X port", limit=5, expand=False, use_hyde=False)
        assert results_off[0]["doc_id"] == habitual_id, \
            "without the boost the decay-saturated habitual hit must win"

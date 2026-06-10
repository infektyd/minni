"""
Correction re-injection tests — audit cluster C1.

Encodes the production failure: an operator looped 3 hours on a belief that
had already been corrected in the vault, because corrections never re-surfaced.

Covers:
  recall-F3   Bounded salience channel for correction-class notes in RRF scoring
  recall-F4   Decay grace window + floor for correction-class notes
  hooks-PL-1  subscribe_contradictions checked/matched discriminator
  hooks-PL-2  (a) search-path learning_reads tracking
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

    def _patched_resolve(*, supplied_agent_id=None, transport="uds", principals_dir=None):
        target_dir = principals_dir or pdir
        target_agent = str(supplied_agent_id or "main").strip() or "main"
        f = target_dir / "local.json"
        f.write_text(json.dumps({
            "agent_id": target_agent,
            "workspace_id": "default",
            "capabilities": ["*"]
        }), encoding="utf-8")
        os.chmod(f, 0o600)

        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=target_dir
        )

    monkeypatch.setattr(principal, "resolve_effective_principal", _patched_resolve)
    monkeypatch.setattr(minnid, "resolve_effective_principal", _patched_resolve)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    """Return (SovereignDB, SovereignConfig) backed by a temporary SQLite file."""
    import db as db_mod
    from config import SovereignConfig

    db_path = str(tmp_path / "test.db")
    cfg = SovereignConfig(db_path=db_path)

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
# hooks-PL-2 leg (a) — search-path learning_reads tracking
# ---------------------------------------------------------------------------

class TestSearchLearningReads:

    def test_search_records_reads_for_learning_backed_docs(self, tmp_path, monkeypatch):
        from minnid import _record_learning_reads_for_search

        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        # Real learnings rows for ids 7 and 9 so the test stays valid if a
        # FK constraint is ever added to learning_reads.learning_id.
        now = time.time()
        with db_obj.cursor() as c:
            for lid in (7, 9):
                c.execute(
                    """INSERT INTO learnings
                       (learning_id, agent_id, category, content, confidence, created_at)
                       VALUES (?, 'codex', 'fact', ?, 1.0, ?)""",
                    (lid, f"learning {lid}", now),
                )
        results = [
            {"source": "learning://7", "score": 1.0},
            {"path": "learning://9"},
            {"source": "/vault/wiki/regular-note.md"},
            {"source": "learning://not-a-number"},
        ]
        recorded = _record_learning_reads_for_search("codex", results)
        assert recorded == 2

        with db_obj.cursor() as c:
            rows = c.execute(
                "SELECT learning_id, agent_id, source FROM learning_reads ORDER BY learning_id"
            ).fetchall()
        assert [(r["learning_id"], r["agent_id"], r["source"]) for r in rows] == [
            (7, "codex", "minnid.search"),
            (9, "codex", "minnid.search"),
        ]

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
        assert checked["contradiction_events_in_window"] == 0
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
        assert result["checked"]["contradiction_events_in_window"] == 1

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
        """Full chain for the hook recall path: a learning surfaced via the
        search RPC (learning:// doc), later superseded, MUST fire
        stale_beliefs — encoding the operator's 3-hour-loop failure."""
        from minnid import _record_learning_reads_for_search

        wb, db_obj, cfg = _patch_writeback(tmp_path, monkeypatch)
        now = time.time()
        with db_obj.cursor() as c:
            c.execute(
                """INSERT INTO learnings (agent_id, category, content, confidence, created_at, status)
                   VALUES ('codex', 'fact', 'service X listens on port 8080', 1.0, ?, 'active')""",
                (now - 2 * 86400,),
            )
            belief_id = c.lastrowid

        # 1. The hook recall path surfaces the belief via search (learning:// doc).
        _record_learning_reads_for_search(
            "codex", [{"source": f"learning://{belief_id}"}]
        )

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
        assert checked["contradiction_events_in_window"] == 1
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

"""Tests for learnings_fts UPDATE/DELETE sync triggers."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


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


def test_learnings_fts_update_syncs(tmp_path):
    db_obj, _cfg = _make_db(tmp_path)

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, created_at) VALUES (?, ?, ?, ?)",
            ("agent1", "general", "some content", 1000.0),
        )
        lid = c.lastrowid

    with db_obj.cursor() as c:
        c.execute(
            "UPDATE learnings SET agent_id=? WHERE learning_id=?",
            ("agent2", lid),
        )

    with db_obj.cursor() as c:
        c.execute("SELECT * FROM learnings_fts WHERE learning_id=?", (lid,))
        row = c.fetchone()

    assert row is not None
    assert dict(row)["agent_id"] == "agent2"


def test_learnings_fts_delete_syncs(tmp_path):
    db_obj, _cfg = _make_db(tmp_path)

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, created_at) VALUES (?, ?, ?, ?)",
            ("agent1", "general", "some content", 1000.0),
        )
        lid = c.lastrowid

    with db_obj.cursor() as c:
        c.execute("DELETE FROM learnings WHERE learning_id=?", (lid,))

    with db_obj.cursor() as c:
        c.execute("SELECT * FROM learnings_fts WHERE learning_id=?", (lid,))
        row = c.fetchone()

    assert row is None

def test_search_learnings_natural_language_question_matches(tmp_path):
    """A question-shaped query must still recall the learning that answers it.

    FTS5 MATCH treats space-joined terms as implicit AND, so the raw question
    "What is the hard timeout of the Aurora Protocol seal phase?" required the
    stored content to contain EVERY token — including "what" — and matched
    nothing. search_learnings must degrade to OR semantics (bm25-ranked) when
    the strict AND query yields no rows.
    """
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                "main",
                "general",
                "The Aurora Protocol seal phase has a hard timeout of thirty seconds.",
                1.0,
                1000.0,
            ),
        )
        lid = c.lastrowid
        # A decoy the OR terms also brush against ("protocol") — the answering
        # learning must still rank first under bm25.
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("main", "general", "The transfer protocol docs moved to the wiki.", 1.0, 1000.0),
        )

    results = engine.search_learnings(
        "What is the hard timeout of the Aurora Protocol seal phase?",
        agent_id="main",
    )
    assert results, "natural-language question matched no learnings (AND-only FTS)"
    assert results[0]["learning_id"] == lid, (
        "the learning answering the question must rank first"
    )


def test_search_learnings_strict_and_still_preferred(tmp_path):
    """When the AND query DOES match, results are unchanged (no OR dilution)."""
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("main", "general", "websocket reconnect needs 500ms backoff", 1.0, 1000.0),
        )
        lid = c.lastrowid
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("main", "general", "the websocket dashboard is deprecated", 1.0, 1000.0),
        )

    results = engine.search_learnings("websocket backoff", agent_id="main")
    assert [r["learning_id"] for r in results] == [lid], (
        "strict AND match must return only the learning containing all terms"
    )


def test_search_learnings_or_fallback_survives_operator_tokens(tmp_path):
    """A query containing a literal uppercase OR/AND must not corrupt the
    OR-joined fallback expression (operands are lowercased; FTS5 matching is
    case-insensitive, operators are only recognized uppercase)."""
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("main", "general", "deploys go through the staging gate first", 1.0, 1000.0),
        )
        lid = c.lastrowid

    results = engine.search_learnings(
        "production OR staging deploy gate ordering", agent_id="main"
    )
    assert [r["learning_id"] for r in results] == [lid]


def test_search_learnings_or_fallback_ranks_answer_above_shared_term_decoys(tmp_path):
    """bm25 precision under the OR fallback on a corpus with heavy term overlap.

    Many learnings share common terms ("protocol", "seal", "phase") with the
    question; only one answers it. The OR fallback must still rank the
    answering learning first — bm25 weights the hit that matches more (and
    rarer) query terms above decoys brushing a single common term.
    """
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    decoys = [
        "The transfer protocol docs moved to the wiki last sprint.",
        "Seal the deployment ticket before the retro meeting.",
        "Phase two of the migration starts after the freeze window.",
        "The hard drive on the build box was replaced on Tuesday.",
        "Timeout budgets for the ingest workers are tracked in the runbook.",
        "The protocol review board meets on the first Monday of the month.",
    ]
    with db_obj.cursor() as c:
        for content in decoys:
            c.execute(
                "INSERT INTO learnings (agent_id, category, content, confidence, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                ("main", "general", content, 1.0, 1000.0),
            )
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                "main",
                "general",
                "The Aurora Protocol seal phase has a hard timeout of thirty seconds.",
                1.0,
                1000.0,
            ),
        )
        answer_lid = c.lastrowid

    results = engine.search_learnings(
        "What is the hard timeout of the Aurora Protocol seal phase?",
        agent_id="main",
    )
    assert results, "OR fallback returned nothing on a shared-term corpus"
    assert results[0]["learning_id"] == answer_lid, (
        f"bm25 must rank the answering learning first, got: {results[0]['content']!r}"
    )


def test_search_learnings_fallback_survives_fts_syntax_characters(tmp_path):
    """FTS5 syntax in the raw query (*, :, quotes, -, ^, parens) must not make
    either the strict pass or the OR fallback raise — _sanitize_fts_query
    strips all non-word characters before terms are joined."""
    from minni.retrieval import RetrievalEngine

    db_obj, cfg = _make_db(tmp_path)
    engine = RetrievalEngine(db_obj, cfg, faiss_index=object())

    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO learnings (agent_id, category, content, confidence, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("main", "general", "the caret module exports a quoted prefix helper", 1.0, 1000.0),
        )
        lid = c.lastrowid

    results = engine.search_learnings(
        'caret:* "quoted phrase" -prefix ^helper NEAR(module export)',
        agent_id="main",
    )
    assert results, "syntax-heavy query matched nothing through the fallback"
    assert results[0]["learning_id"] == lid

"""#287 — episodic_fts must stay in step with episodic_events.

Two asymmetries, both found during adversarial review of PR #279 and both
latent-but-silent at the time they were filed:

  1. episodic_events carried an AFTER INSERT FTS trigger only, while learnings
     carries insert/update/delete. An UPDATE left the index holding the OLD
     text — the current content unfindable, the stale content still matching —
     and episodic_index_coverage could not see it, because that metric checks
     an event_id is PRESENT, not that the indexed text is FAITHFUL.

  2. reconcile_episodic_fts defined its repair set on event_id alone while
     episodic_index_coverage defined "indexed" on event_id AND agent_id. An
     index row filed under the wrong agent was therefore a state the metric
     reported (ratio below 1.0) that the repair could never close.

Every test drives the real schema (db._init_schema) or the real reconcile —
none reimplement the predicates they are checking.
"""

import time

import pytest


def _make_db(tmp_path, **cfg_overrides):
    """(SovereignDB, SovereignConfig) over a temporary SQLite file."""
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "test.db"), **cfg_overrides)
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _add_event(db_obj, agent_id="forge", content="original alpha text"):
    with db_obj.cursor() as c:
        c.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES (?, 'message', ?, ?)",
            (agent_id, content, time.time()),
        )
        return c.lastrowid


def _match(db_obj, term):
    with db_obj.cursor() as c:
        return c.execute(
            "SELECT COUNT(*) AS n FROM episodic_fts WHERE episodic_fts MATCH ?",
            (term,),
        ).fetchone()["n"]


class TestUpdateKeepsTheIndexFaithful:
    """An UPDATE must move the index with the row."""

    def test_updated_content_is_findable_and_stale_text_is_not(self, tmp_path):
        db_obj, _ = _make_db(tmp_path)
        event_id = _add_event(db_obj)
        assert _match(db_obj, "alpha") == 1

        with db_obj.cursor() as c:
            c.execute(
                "UPDATE episodic_events SET content = 'revised beta text'"
                " WHERE event_id = ?",
                (event_id,),
            )

        assert _match(db_obj, "beta") == 1, (
            "the current text must be findable — without an UPDATE trigger the "
            "index still holds the old content and this returns 0"
        )
        assert _match(db_obj, "alpha") == 0, (
            "the superseded text must stop matching; a stale hit is a lie about "
            "what the agent remembers"
        )
        db_obj.close()

    def test_an_agent_id_change_moves_the_index_row(self, tmp_path):
        """agent_id is in the trigger's UPDATE OF list because search_episodic
        filters on the FTS copy: a row left under the old agent is unreachable
        by the new owner and still reachable by the old one."""
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path)
        event_id = _add_event(db_obj, agent_id="forge", content="gamma text")

        with db_obj.cursor() as c:
            c.execute(
                "UPDATE episodic_events SET agent_id = 'codex' WHERE event_id = ?",
                (event_id,),
            )

        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
        assert len(engine.search_episodic("gamma", agent_id="codex")) == 1
        assert len(engine.search_episodic("gamma", agent_id="forge")) == 0
        db_obj.close()

    def test_clearing_content_removes_the_index_row(self, tmp_path):
        """The insert trigger is guarded on `WHEN NEW.content IS NOT NULL`, so
        an UPDATE to NULL must REMOVE the row rather than write NULL content
        into fts5 — this is why the trigger is delete-then-insert and not
        learnings' update-in-place."""
        db_obj, _ = _make_db(tmp_path)
        event_id = _add_event(db_obj)

        with db_obj.cursor() as c:
            c.execute(
                "UPDATE episodic_events SET content = NULL WHERE event_id = ?",
                (event_id,),
            )
            remaining = c.execute(
                "SELECT COUNT(*) AS n FROM episodic_fts"
                " WHERE CAST(event_id AS INTEGER) = ?",
                (event_id,),
            ).fetchone()["n"]

        assert remaining == 0, "a NULL-content event must not keep an index row"
        assert _match(db_obj, "alpha") == 0
        db_obj.close()

    def test_update_does_not_duplicate_the_index_row(self, tmp_path):
        db_obj, _ = _make_db(tmp_path)
        event_id = _add_event(db_obj)

        with db_obj.cursor() as c:
            for i in range(3):
                c.execute(
                    "UPDATE episodic_events SET content = ? WHERE event_id = ?",
                    (f"revision {i}", event_id),
                )
            rows = c.execute(
                "SELECT COUNT(*) AS n FROM episodic_fts"
                " WHERE CAST(event_id AS INTEGER) = ?",
                (event_id,),
            ).fetchone()["n"]

        assert rows == 1, f"repeated updates must not accumulate rows (got {rows})"
        assert _match(db_obj, "revision") == 1
        db_obj.close()


class TestDeleteRemovesTheIndexRow:
    def test_deleting_an_event_removes_its_index_row(self, tmp_path):
        """episodic.py's two prune paths hand-delete FTS rows first, so this
        trigger is redundant for them by design — it is what protects a third
        prune path written later, and the orphan count in health from drifting
        upward for no reason anyone can find."""
        db_obj, _ = _make_db(tmp_path)
        event_id = _add_event(db_obj)

        with db_obj.cursor() as c:
            c.execute("DELETE FROM episodic_events WHERE event_id = ?", (event_id,))

        assert _match(db_obj, "alpha") == 0, "the index row outlived its event"
        db_obj.close()

    def test_existing_prune_paths_still_work(self, tmp_path):
        """cleanup_expired deletes FTS rows explicitly and THEN the events, so
        the new delete trigger fires against rows already gone. That must be a
        no-op, not an error or a double-count."""
        from minni.episodic import EpisodicMemory

        db_obj, cfg = _make_db(tmp_path)
        old = time.time() - 999_999
        with db_obj.cursor() as c:
            for i in range(3):
                c.execute(
                    "INSERT INTO episodic_events"
                    " (agent_id, event_type, content, created_at)"
                    " VALUES ('forge', 'message', ?, ?)",
                    (f"expired row {i}", old),
                )

        removed = EpisodicMemory(db_obj, cfg).cleanup_expired(max_age_seconds=60)

        assert removed == 3
        with db_obj.cursor() as c:
            assert c.execute(
                "SELECT COUNT(*) AS n FROM episodic_fts"
            ).fetchone()["n"] == 0
            assert c.execute(
                "SELECT COUNT(*) AS n FROM episodic_events"
            ).fetchone()["n"] == 0
        db_obj.close()


class TestRepairSetMatchesTheCoveragePredicate:
    """The metric and the repair must define coverage the same way, or the
    metric reports a gap the repair reports as already fixed."""

    def _wrong_agent_row(self, tmp_path):
        db_obj, cfg = _make_db(tmp_path)
        with db_obj.cursor() as c:
            # Drop the triggers so the bad row can be planted the way a
            # pre-trigger write would have left it.
            c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_insert")
            c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_update")
            c.execute(
                "INSERT INTO episodic_events"
                " (agent_id, event_type, content, created_at)"
                " VALUES ('forge', 'message', 'gamma scoped text', ?)",
                (time.time(),),
            )
            event_id = c.lastrowid
            c.execute(
                "INSERT INTO episodic_fts(event_id, agent_id, content)"
                " VALUES (?, 'someone-else', 'gamma scoped text')",
                (event_id,),
            )
        return db_obj, cfg, event_id

    def test_one_sweep_repairs_a_wrong_agent_row(self, tmp_path):
        from minni.backfill import episodic_index_coverage
        from minni.episodic import reconcile_episodic_fts

        db_obj, _, _ = self._wrong_agent_row(tmp_path)
        assert episodic_index_coverage(db_obj)["episodic_index_ratio"] == 0.0

        result = reconcile_episodic_fts(db_obj._get_conn())

        assert result["missing_before"] == 1, (
            "the repair set must see what the metric sees — keyed on event_id "
            "alone this reported 0 while the ratio sat at 0.0 forever"
        )
        assert result["inserted"] == 1
        assert episodic_index_coverage(db_obj)["episodic_index_ratio"] == 1.0
        db_obj.close()

    def test_the_repaired_row_reaches_its_owner_and_only_its_owner(self, tmp_path):
        from minni.episodic import reconcile_episodic_fts
        from minni.retrieval import RetrievalEngine

        db_obj, cfg, _ = self._wrong_agent_row(tmp_path)
        reconcile_episodic_fts(db_obj._get_conn())

        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
        assert len(engine.search_episodic("gamma", agent_id="forge")) == 1, (
            "the owner still cannot see its own memory"
        )
        assert len(engine.search_episodic("gamma", agent_id="someone-else")) == 0, (
            "the stale row must be gone, not merely joined by a correct one — "
            "otherwise the event leaks to an agent that never recorded it"
        )
        db_obj.close()

    def test_the_repair_leaves_exactly_one_row(self, tmp_path):
        from minni.episodic import reconcile_episodic_fts

        db_obj, _, event_id = self._wrong_agent_row(tmp_path)
        conn = db_obj._get_conn()
        reconcile_episodic_fts(conn)

        rows = conn.execute(
            "SELECT COUNT(*) FROM episodic_fts WHERE CAST(event_id AS INTEGER) = ?",
            (event_id,),
        ).fetchone()[0]
        assert rows == 1, f"delete-then-insert must not duplicate (got {rows})"
        db_obj.close()

    def test_the_repair_is_still_idempotent(self, tmp_path):
        from minni.episodic import reconcile_episodic_fts

        db_obj, _, _ = self._wrong_agent_row(tmp_path)
        conn = db_obj._get_conn()
        reconcile_episodic_fts(conn)

        assert reconcile_episodic_fts(conn) == {"missing_before": 0, "inserted": 0}
        db_obj.close()

    def test_correctly_indexed_rows_are_never_rewritten(self, tmp_path):
        """Events already matching on both columns must not be in the repair
        set — a repair that churns healthy rows would mask its own no-op."""
        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = _make_db(tmp_path)
        _add_event(db_obj, content="healthy row")
        conn = db_obj._get_conn()

        assert reconcile_episodic_fts(conn) == {"missing_before": 0, "inserted": 0}
        assert _match(db_obj, "healthy") == 1
        db_obj.close()


class TestFreshSchemaCarriesTheTriggers:
    """Pins db._init_schema independently of migration 019.

    Both install these triggers, by the same belt-and-braces pattern learnings
    uses (base schema + migration 011). That redundancy means a test driving a
    normal SovereignDB cannot tell which source installed them: deleting them
    from _init_schema leaves the suite green because the migration re-adds them.
    Verified — both such mutants survived the full suite. So the fresh-schema
    path is asserted here directly, against a connection that has had
    _init_schema run on it and nothing else.
    """

    def _bare_schema_conn(self, tmp_path, monkeypatch):
        """A database built by _init_schema with run_migrations neutralised, so
        nothing but the base schema can supply the triggers under test."""
        import minni.db as db_mod
        import minni.migrations as migrations_mod
        from minni.config import SovereignConfig

        monkeypatch.setattr(migrations_mod, "run_migrations", lambda conn: None)

        cfg = SovereignConfig(db_path=str(tmp_path / "bare.db"))
        old_flag = db_mod._migrations_run
        db_mod._migrations_run = False
        try:
            db_obj = db_mod.SovereignDB(cfg)
            conn = db_obj._get_conn()
        finally:
            db_mod._migrations_run = old_flag

        # Guard the guard: if migrations somehow still ran, this test would be
        # asserting the very redundancy it exists to see past.
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master"
            " WHERE type='table' AND name='schema_migrations'"
        ).fetchone()[0] == 0, "run_migrations was not neutralised"
        return db_obj, conn

    def test_init_schema_installs_all_three_episodic_fts_triggers(
        self, tmp_path, monkeypatch
    ):
        db_obj, conn = self._bare_schema_conn(tmp_path, monkeypatch)
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
                " AND name LIKE 'trg_episodic_fts%'"
            ).fetchall()
        }
        assert names == {
            "trg_episodic_fts_insert",
            "trg_episodic_fts_update",
            "trg_episodic_fts_delete",
        }, (
            "a fresh database must carry insert/update/delete on episodic_fts, "
            f"matching learnings_fts (got {names})"
        )
        db_obj.close()

    def test_the_fresh_schema_triggers_actually_fire(self, tmp_path, monkeypatch):
        """Existence is not behaviour — drive each one."""
        db_obj, conn = self._bare_schema_conn(tmp_path, monkeypatch)
        conn.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES ('forge', 'message', 'alpha text', 1.0)"
        )
        event_id = conn.execute("SELECT MAX(event_id) FROM episodic_events").fetchone()[0]

        def match(term):
            return conn.execute(
                "SELECT COUNT(*) FROM episodic_fts WHERE episodic_fts MATCH ?",
                (term,),
            ).fetchone()[0]

        assert match("alpha") == 1, "insert trigger"

        conn.execute(
            "UPDATE episodic_events SET content = 'beta text' WHERE event_id = ?",
            (event_id,),
        )
        assert match("beta") == 1 and match("alpha") == 0, "update trigger"

        conn.execute("DELETE FROM episodic_events WHERE event_id = ?", (event_id,))
        assert match("beta") == 0, "delete trigger"
        db_obj.close()


class TestMigration019:
    def test_migration_adds_the_triggers_to_an_existing_database(self, tmp_path):
        """An installed database predates these triggers; _init_schema only
        helps fresh ones."""
        from minni.migrations import run_migrations

        db_obj, _ = _make_db(tmp_path)
        conn = db_obj._get_conn()
        conn.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_update")
        conn.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_delete")
        conn.execute("DELETE FROM schema_migrations WHERE version = 19")
        conn.commit()

        run_migrations(conn)
        conn.commit()

        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
                " AND name LIKE 'trg_episodic_fts%'"
            ).fetchall()
        }
        assert names == {
            "trg_episodic_fts_insert",
            "trg_episodic_fts_update",
            "trg_episodic_fts_delete",
        }, f"migration 019 did not install both triggers (got {names})"

        # And they work, not merely exist.
        event_id = _add_event(db_obj, content="post migration text")
        with db_obj.cursor() as c:
            c.execute(
                "UPDATE episodic_events SET content = 'updated after migration'"
                " WHERE event_id = ?",
                (event_id,),
            )
        assert _match(db_obj, "updated") == 1
        db_obj.close()

    def test_migration_is_idempotent(self, tmp_path):
        from minni.migrations import run_migrations

        db_obj, _ = _make_db(tmp_path)
        conn = db_obj._get_conn()
        conn.execute("DELETE FROM schema_migrations WHERE version = 19")
        conn.commit()
        run_migrations(conn)
        conn.commit()
        run_migrations(conn)
        conn.commit()

        event_id = _add_event(db_obj, content="idempotent check")
        rows = conn.execute(
            "SELECT COUNT(*) FROM episodic_fts WHERE CAST(event_id AS INTEGER) = ?",
            (event_id,),
        ).fetchone()[0]
        assert rows == 1, "a doubled trigger would insert twice"
        db_obj.close()

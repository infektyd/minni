"""#287 / #311 — episodic_fts must stay in step with episodic_events.

#311 rebuilds episodic_fts so event_id is stored as the FTS5 table's built-in
INTEGER PRIMARY KEY `rowid` (`USING fts5(agent_id UNINDEXED, content, ...)`):
  1. Keyed deletes and updates use `WHERE rowid = OLD.event_id` (O(log n) B-tree
     lookup instead of an O(n) full scan of an UNINDEXED event_id column).
  2. `trg_episodic_fts_delete` is restored for full insert/update/delete trigger
     symmetry with `learnings_fts`, while `trim_recall_traces` stays flat at the
     5,000 expiring / 50,000 retained shape.
  3. All `CAST(...event_id AS INTEGER)` and `event_id IS NULL` workarounds are
     retired from `episodic.py` and `backfill.py`.

Every test drives the real schema (db._init_schema) or the real reconcile —
none reimplement the predicates they are checking.
"""

from pathlib import Path
import time

import pytest


def _make_db(tmp_path, **cfg_overrides):
    """(MinniDB, MinniConfig) over a temporary SQLite file."""
    import minni.db as db_mod
    from minni.config import MinniConfig

    cfg = MinniConfig(db_path=str(tmp_path / "test.db"), **cfg_overrides)
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.MinniDB(cfg)
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


class TestRowidSchemaAndNoCastWorkarounds:
    """#311: episodic_fts is keyed by rowid = event_id with no event_id payload
    column and no CAST/NULL guards in episodic.py or backfill.py."""

    def test_episodic_fts_is_keyed_by_rowid_with_no_event_id_column(self, tmp_path):
        db_obj, _ = _make_db(tmp_path)
        event_id = _add_event(db_obj, agent_id="forge", content="rowid check alpha")
        with db_obj.cursor() as c:
            cols = [
                r["name"]
                for r in c.execute("PRAGMA table_info(episodic_fts)").fetchall()
            ]
            assert cols == ["agent_id", "content"], (
                f"episodic_fts must not carry an event_id payload column (got {cols})"
            )
            row = c.execute(
                "SELECT rowid, agent_id, content FROM episodic_fts WHERE rowid = ?",
                (event_id,),
            ).fetchone()
            assert row is not None
            assert row["rowid"] == event_id
            assert row["agent_id"] == "forge"
        db_obj.close()

    def test_no_cast_or_null_event_id_guards_in_episodic_or_backfill(self):
        repo_root = Path(__file__).resolve().parent.parent
        for rel_path in ("src/minni/episodic.py", "src/minni/backfill.py"):
            source = (repo_root / rel_path).read_text(encoding="utf-8")
            assert "CAST(" not in source, f"{rel_path} still contains CAST("
            assert "event_id IS NULL" not in source, (
                f"{rel_path} still contains event_id IS NULL"
            )


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

    def test_an_event_id_change_moves_the_index_rowid(self, tmp_path):
        db_obj, _ = _make_db(tmp_path)
        event_id = _add_event(db_obj, agent_id="forge", content="movable delta")
        new_id = event_id + 500
        with db_obj.cursor() as c:
            c.execute(
                "UPDATE episodic_events SET event_id = ? WHERE event_id = ?",
                (new_id, event_id),
            )
            old_count = c.execute(
                "SELECT COUNT(*) AS n FROM episodic_fts WHERE rowid = ?",
                (event_id,),
            ).fetchone()["n"]
            new_count = c.execute(
                "SELECT COUNT(*) AS n FROM episodic_fts WHERE rowid = ?",
                (new_id,),
            ).fetchone()["n"]
        assert old_count == 0
        assert new_count == 1
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
                "SELECT COUNT(*) AS n FROM episodic_fts WHERE rowid = ?",
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
                "SELECT COUNT(*) AS n FROM episodic_fts WHERE rowid = ?",
                (event_id,),
            ).fetchone()["n"]

        assert rows == 1, f"repeated updates must not accumulate rows (got {rows})"
        assert _match(db_obj, "revision") == 1
        db_obj.close()


class TestUpdateDoesNotTouchBystanders:
    """The UPDATE trigger's WHERE clause is what stops one event's edit from
    rewriting the whole index. A single-event fixture cannot see the
    difference: `DELETE FROM episodic_fts` and
    `DELETE FROM episodic_fts WHERE rowid = OLD.event_id` behave identically
    when there is only one row. Verified — that mutant survived the full suite
    before this test existed."""

    def test_updating_one_event_leaves_every_other_row_intact(self, tmp_path):
        db_obj, _ = _make_db(tmp_path)
        _add_event(db_obj, agent_id="forge", content="bystander alpha")
        _add_event(db_obj, agent_id="codex", content="bystander beta")
        target = _add_event(db_obj, agent_id="forge", content="target gamma")

        with db_obj.cursor() as c:
            c.execute(
                "UPDATE episodic_events SET content = 'target delta'"
                " WHERE event_id = ?",
                (target,),
            )

        assert _match(db_obj, "alpha") == 1, "a bystander row was destroyed"
        assert _match(db_obj, "beta") == 1, "a bystander row was destroyed"
        assert _match(db_obj, "delta") == 1
        assert _match(db_obj, "gamma") == 0
        with db_obj.cursor() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM episodic_fts").fetchone()["n"]
        assert total == 3, f"index row count must be preserved (got {total})"
        db_obj.close()

    def test_a_multi_row_update_reindexes_every_matched_row(self, tmp_path):
        db_obj, _ = _make_db(tmp_path)
        for i in range(4):
            _add_event(db_obj, agent_id="forge", content=f"alpha row {i}")
        _add_event(db_obj, agent_id="codex", content="untouched beta")

        with db_obj.cursor() as c:
            c.execute(
                "UPDATE episodic_events SET content = 'bulk gamma'"
                " WHERE agent_id = 'forge'"
            )

        assert _match(db_obj, "gamma") == 4
        assert _match(db_obj, "alpha") == 0
        assert _match(db_obj, "beta") == 1
        db_obj.close()


class TestMisfiledIndexRowsAreCollected:
    """Out-of-band intruder or misfiled index rows in episodic_fts must be
    collected by reconcile_episodic_fts without disturbing valid rows."""

    def _correct_plus_intruder(self, tmp_path):
        db_obj, cfg = _make_db(tmp_path)
        event_id = _add_event(db_obj, agent_id="forge", content="gamma scoped text")
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO episodic_fts(rowid, agent_id, content)"
                " VALUES (?, 'intruder', 'gamma scoped text')",
                (event_id + 1000,),
            )
        return db_obj, cfg, event_id

    def test_the_intruder_row_is_removed(self, tmp_path):
        from minni.episodic import reconcile_episodic_fts

        db_obj, _, event_id = self._correct_plus_intruder(tmp_path)
        conn = db_obj._get_conn()

        assert reconcile_episodic_fts(conn)["removed"] == 1

        rows = conn.execute(
            "SELECT rowid, agent_id FROM episodic_fts",
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(event_id, "forge")], (
            f"exactly the owner's row must remain (got {rows})"
        )
        db_obj.close()

    def test_the_event_stops_leaking_to_the_wrong_agent(self, tmp_path):
        from minni.episodic import reconcile_episodic_fts
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path)
        event_id = _add_event(db_obj, agent_id="forge", content="gamma scoped text")
        with db_obj.cursor() as c:
            c.execute("DELETE FROM episodic_fts WHERE rowid = ?", (event_id,))
            c.execute(
                "INSERT INTO episodic_fts(rowid, agent_id, content)"
                " VALUES (?, 'intruder', 'gamma scoped text')",
                (event_id,),
            )
        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
        assert len(engine.search_episodic("gamma", agent_id="intruder")) == 1

        reconcile_episodic_fts(db_obj._get_conn())

        assert len(engine.search_episodic("gamma", agent_id="intruder")) == 0, (
            "an agent that never recorded this event can still read it"
        )
        assert len(engine.search_episodic("gamma", agent_id="forge")) == 1, (
            "the owner lost its own memory"
        )
        db_obj.close()


class TestDeleteTriggerAndOrphanSweep:
    """#311: With episodic_fts keyed by rowid = event_id, trg_episodic_fts_delete
    deletes FTS rows in O(log n) on the live path while preserving bystander rows,
    and reconcile_episodic_fts continues to collect any out-of-band orphans."""

    def test_delete_trigger_removes_index_row_immediately(self, tmp_path):
        db_obj, _ = _make_db(tmp_path)
        event_id = _add_event(db_obj)
        assert _match(db_obj, "alpha") == 1

        with db_obj.cursor() as c:
            c.execute("DELETE FROM episodic_events WHERE event_id = ?", (event_id,))

        assert _match(db_obj, "alpha") == 0, (
            "trg_episodic_fts_delete must remove the FTS row immediately on DELETE"
        )
        db_obj.close()

    def test_deleting_one_event_leaves_bystander_rows_intact(self, tmp_path):
        """Bystander coverage for DELETE: a trigger missing `WHERE rowid = OLD.event_id`
        would wipe the index of every other agent."""
        db_obj, _ = _make_db(tmp_path)
        _add_event(db_obj, agent_id="forge", content="bystander alpha")
        _add_event(db_obj, agent_id="codex", content="bystander beta")
        doomed = _add_event(db_obj, agent_id="forge", content="doomed gamma")

        with db_obj.cursor() as c:
            c.execute("DELETE FROM episodic_events WHERE event_id = ?", (doomed,))

        assert _match(db_obj, "alpha") == 1, "another event's row was collateral"
        assert _match(db_obj, "beta") == 1, "another agent's row was collateral"
        assert _match(db_obj, "gamma") == 0
        with db_obj.cursor() as c:
            total = c.execute("SELECT COUNT(*) AS n FROM episodic_fts").fetchone()["n"]
        assert total == 2
        db_obj.close()

    def test_out_of_band_orphan_is_collected_by_sweep_without_touching_live_rows(
        self, tmp_path
    ):
        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = _make_db(tmp_path)
        _add_event(db_obj, agent_id="forge", content="bystander alpha")
        _add_event(db_obj, agent_id="codex", content="bystander beta")
        with db_obj.cursor() as c:
            c.execute(
                "INSERT INTO episodic_fts(rowid, agent_id, content)"
                " VALUES (999999, 'forge', 'doomed gamma')"
            )

        assert _match(db_obj, "gamma") == 1
        result = reconcile_episodic_fts(db_obj._get_conn())

        assert result["removed"] == 1, (
            f"exactly the orphan must go, not the whole index (got {result})"
        )
        assert _match(db_obj, "alpha") == 1, "another agent's row was collateral"
        assert _match(db_obj, "beta") == 1, "another agent's row was collateral"
        assert _match(db_obj, "gamma") == 0
        db_obj.close()

    def test_trim_recall_traces_5000_of_50000_stays_flat_with_delete_trigger(
        self, tmp_path
    ):
        """#311 benchmark: with rowid-keyed episodic_fts and trg_episodic_fts_delete
        active, pruning 5,000 expiring traces against 50,000 retained events stays
        flat (< 2.0s) instead of regressing to the 16.674s UNINDEXED scan."""
        import time as _time

        from minni.episodic import EpisodicMemory

        db_obj, cfg = _make_db(tmp_path)
        now = time.time()
        with db_obj.cursor() as c:
            c.executemany(
                "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
                " VALUES (?, ?, ?, ?)",
                [("forge", "recall", f"trace {i}", now - 999_999) for i in range(5000)]
                + [("forge", "message", f"kept {i}", now) for i in range(50_000)],
            )

        started = _time.perf_counter()
        removed = EpisodicMemory(db_obj, cfg).trim_recall_traces(max_age_seconds=60)
        elapsed = _time.perf_counter() - started

        assert removed == 5000
        assert elapsed < 2.0, (
            f"pruning 5000 traces against 50000 retained took {elapsed:.2f}s — "
            "this is the quadratic delete path returning"
        )
        with db_obj.cursor() as c:
            fts_remaining = c.execute(
                "SELECT COUNT(*) AS n FROM episodic_fts"
            ).fetchone()["n"]
        assert fts_remaining == 50_000
        db_obj.close()

    def test_existing_prune_paths_still_work(self, tmp_path):
        """cleanup_expired deletes events and trg_episodic_fts_delete removes
        their FTS rows cleanly."""
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
            c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_delete")
            c.execute(
                "INSERT INTO episodic_events"
                " (agent_id, event_type, content, created_at)"
                " VALUES ('forge', 'message', 'gamma scoped text', ?)",
                (time.time(),),
            )
            event_id = c.lastrowid
            c.execute(
                "INSERT INTO episodic_fts(rowid, agent_id, content)"
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
            "SELECT COUNT(*) FROM episodic_fts WHERE rowid = ?",
            (event_id,),
        ).fetchone()[0]
        assert rows == 1, f"delete-then-insert must not duplicate (got {rows})"
        db_obj.close()

    def test_the_repair_is_still_idempotent(self, tmp_path):
        from minni.episodic import reconcile_episodic_fts

        db_obj, _, _ = self._wrong_agent_row(tmp_path)
        conn = db_obj._get_conn()
        reconcile_episodic_fts(conn)

        assert reconcile_episodic_fts(conn) == {
            "missing_before": 0, "inserted": 0, "removed": 0,
        }
        db_obj.close()

    def test_correctly_indexed_rows_are_never_rewritten(self, tmp_path):
        """Events already matching on both columns must not be in the repair
        set — a repair that churns healthy rows would mask its own no-op."""
        from minni.episodic import reconcile_episodic_fts

        db_obj, _ = _make_db(tmp_path)
        _add_event(db_obj, content="healthy row")
        conn = db_obj._get_conn()

        assert reconcile_episodic_fts(conn) == {
            "missing_before": 0, "inserted": 0, "removed": 0,
        }
        assert _match(db_obj, "healthy") == 1
        db_obj.close()


class TestFreshSchemaCarriesTheTriggers:
    """Pins db._init_schema independently of migrations."""

    def _bare_schema_conn(self, tmp_path, monkeypatch):
        """A database built by _init_schema with run_migrations neutralised, so
        nothing but the base schema can supply the triggers under test."""
        import minni.db as db_mod
        import minni.migrations as migrations_mod
        from minni.config import MinniConfig

        monkeypatch.setattr(migrations_mod, "run_migrations", lambda conn: None)

        cfg = MinniConfig(db_path=str(tmp_path / "bare.db"))
        old_flag = db_mod._migrations_run
        db_mod._migrations_run = False
        try:
            db_obj = db_mod.MinniDB(cfg)
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

    def test_init_schema_installs_insert_update_and_delete_triggers(
        self, tmp_path, monkeypatch
    ):
        """#311: With episodic_fts keyed by rowid = event_id, _init_schema
        installs all three triggers (insert, update, delete), restoring full
        symmetry with learnings_fts."""
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
        }, f"unexpected episodic FTS trigger set (got {names})"
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
        assert match("beta") == 0, "delete trigger must remove the FTS row immediately"
        db_obj.close()


class TestMigration019:
    def test_migration_adds_the_triggers_to_an_existing_database(self, tmp_path):
        """An installed database predates these triggers; _init_schema only
        helps fresh ones."""
        from minni.migrations import run_migrations

        db_obj, _ = _make_db(tmp_path)
        conn = db_obj._get_conn()
        conn.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_update")
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
        assert "trg_episodic_fts_update" in names, (
            f"migration 019 did not install the update trigger (got {names})"
        )

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

    def test_migration_skips_a_schema_without_episodic_fts(self):
        """SQLite resolves trigger-body tables at FIRE time, so CREATE TRIGGER
        succeeds on a partial schema and _execute_tolerant sees no error — then
        every later UPDATE dies with "no such table: main.episodic_fts". The
        migration must decline to install the trigger it cannot support."""
        import sqlite3

        from minni.migrations import run_migrations

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE episodic_events ("
            " event_id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,"
            " event_type TEXT NOT NULL, content TEXT, created_at REAL)"
        )

        run_migrations(conn)
        conn.commit()

        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
            " AND name = 'trg_episodic_fts_update'"
        ).fetchone()[0] == 0, "the trigger was installed with no table to back it"

        conn.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES ('a', 'message', 'x', 1.0)"
        )
        # The point of the guard: this must not raise.
        conn.execute("UPDATE episodic_events SET content = 'y' WHERE event_id = 1")
        conn.execute("DELETE FROM episodic_events WHERE event_id = 1")
        conn.close()

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
            "SELECT COUNT(*) FROM episodic_fts WHERE rowid = ?",
            (event_id,),
        ).fetchone()[0]
        assert rows == 1, "a doubled trigger would insert twice"
        db_obj.close()


class TestMigration024:
    """#311: Migration 024 rebuilds legacy episodic_fts(event_id UNINDEXED, ...)
    into rowid-keyed episodic_fts(agent_id UNINDEXED, content), installs all
    three rowid-keyed triggers, and reconciles coverage."""

    def test_rebuilds_legacy_event_id_column_table_and_preserves_coverage(
        self, tmp_path
    ):
        from minni.backfill import episodic_index_coverage
        from minni.migrations import run_migrations
        from minni.retrieval import RetrievalEngine

        db_obj, cfg = _make_db(tmp_path)
        conn = db_obj._get_conn()

        # Simulate a pre-024 database with the legacy 3-column episodic_fts
        conn.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_insert")
        conn.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_update")
        conn.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_delete")
        conn.execute("DROP TABLE episodic_fts")
        conn.execute(
            """
            CREATE VIRTUAL TABLE episodic_fts USING fts5(
                event_id UNINDEXED,
                agent_id UNINDEXED,
                content,
                tokenize='porter unicode61'
            )
            """
        )
        conn.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES ('forge', 'message', 'legacy alpha event', 1.0)"
        )
        ev1 = conn.execute("SELECT MAX(event_id) FROM episodic_events").fetchone()[0]
        conn.execute(
            "INSERT INTO episodic_events (agent_id, event_type, content, created_at)"
            " VALUES ('codex', 'message', 'legacy beta event', 2.0)"
        )
        ev2 = conn.execute("SELECT MAX(event_id) FROM episodic_events").fetchone()[0]
        # Populate legacy table with one valid row, one wrong-agent row, and one orphan
        conn.execute(
            "INSERT INTO episodic_fts(event_id, agent_id, content)"
            " VALUES (?, 'forge', 'legacy alpha event')",
            (ev1,),
        )
        conn.execute(
            "INSERT INTO episodic_fts(event_id, agent_id, content)"
            " VALUES (?, 'intruder', 'legacy beta event')",
            (ev2,),
        )
        conn.execute(
            "INSERT INTO episodic_fts(event_id, agent_id, content)"
            " VALUES (999999, 'forge', 'legacy orphan')"
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = 24")
        conn.commit()

        run_migrations(conn)
        conn.commit()

        cols = [
            r[1] for r in conn.execute("PRAGMA table_info(episodic_fts)").fetchall()
        ]
        assert cols == ["agent_id", "content"], (
            f"migration 024 did not rebuild episodic_fts without event_id (got {cols})"
        )
        triggers = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
                " AND name LIKE 'trg_episodic_fts%'"
            ).fetchall()
        }
        assert triggers == {
            "trg_episodic_fts_insert",
            "trg_episodic_fts_update",
            "trg_episodic_fts_delete",
        }

        cov = episodic_index_coverage(db_obj)
        assert cov["episodic_index_ratio"] == 1.0
        assert cov["episodic_fts_orphans"] == 0

        engine = RetrievalEngine(db_obj, cfg, faiss_index=object())
        assert len(engine.search_episodic("alpha", agent_id="forge")) == 1
        assert len(engine.search_episodic("beta", agent_id="codex")) == 1
        assert len(engine.search_episodic("beta", agent_id="intruder")) == 0
        db_obj.close()

    def test_migration_024_is_idempotent(self, tmp_path):
        from minni.migrations import run_migrations

        db_obj, _ = _make_db(tmp_path)
        conn = db_obj._get_conn()
        event_id = _add_event(db_obj, content="idempotent 024 check")

        conn.execute("DELETE FROM schema_migrations WHERE version = 24")
        conn.commit()
        run_migrations(conn)
        conn.commit()
        run_migrations(conn)
        conn.commit()

        rows = conn.execute(
            "SELECT COUNT(*) FROM episodic_fts WHERE rowid = ?",
            (event_id,),
        ).fetchone()[0]
        assert rows == 1
        db_obj.close()

    def test_migration_024_skips_partial_schemas(self):
        import sqlite3

        from minni.migrations import _apply_migration_024_rebuild_episodic_fts_rowid

        # Missing both tables
        conn_empty = sqlite3.connect(":memory:")
        _apply_migration_024_rebuild_episodic_fts_rowid(conn_empty)
        assert conn_empty.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
        ).fetchone()[0] == 0
        conn_empty.close()

        # Missing episodic_fts
        conn_events_only = sqlite3.connect(":memory:")
        conn_events_only.execute(
            "CREATE TABLE episodic_events ("
            " event_id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,"
            " event_type TEXT NOT NULL, content TEXT, created_at REAL)"
        )
        _apply_migration_024_rebuild_episodic_fts_rowid(conn_events_only)
        assert conn_events_only.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
            " AND name LIKE 'trg_episodic_fts%'"
        ).fetchone()[0] == 0
        conn_events_only.close()


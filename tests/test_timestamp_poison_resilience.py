"""Audit R0 regression: a non-numeric timestamp must not take down a whole pass.

``documents.indexed_at`` / ``last_modified`` / ``last_accessed`` are REAL
*affinity* columns, which SQLite fills with a non-numeric TEXT value without
complaint. One live row held the TEXT ``'2026-06-19T22:55:32.509Z'``. Before
this fix that single row was enough to:

  - raise ValueError in ``retrieval._filter_candidates``, propagate out of
    ``handle_search`` and abort the ENTIRE date-filtered recall with -32000;
  - raise TypeError in ``decay.run_decay`` inside its transaction and abort the
    ENTIRE decay pass.

Three properties are pinned here:
  (a) write path: a non-numeric value cannot be stored;
  (b) read path: one poisoned row is parsed if it can be, skipped if it cannot,
      never fatal — and either way it stays REPORTED, not swallowed;
  (c) migration 016 repairs rows already stored.
"""

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# The exact value found in the live shared DB (doc_id 590).
POISON_ISO = "2026-06-19T22:55:32.509Z"
POISON_EPOCH = 1781909732.509          # Python parse, sub-second preserved
POISON_EPOCH_SECONDS = 1781909732.0    # SQL repair, strftime('%s') truncates
# Not recoverable by any parser — this is what "skip and report" is for.
POISON_GARBAGE = "not a timestamp"
# grok-review (PR #242): a numeric-TEXT epoch (e.g. a caller that stored
# str(time.time())) is recoverable by parse_epoch's first branch (float(text))
# but, before the fix, fell through the SQL repair's strftime path — which
# returns NULL for a bare numeric string on this SQLite build (it is not read
# as a Julian day) — into the 0.0 "needs attention" sentinel. That destroyed a
# perfectly good value.
POISON_NUMERIC_TEXT = "1700000000.5"
POISON_NUMERIC_TEXT_EPOCH = 1700000000.5
# Offset-qualified ISO-8601 (as opposed to the naive/'Z' form already covered
# above). Confirmed to already round-trip correctly through both
# minni.timestamps.parse_epoch and migration 016's strftime path on this
# SQLite build (3.53+); pinned here as a regression lock, not a fix.
POISON_ISO_OFFSET = "2026-08-01T12:00:00.123456+00:00"
POISON_ISO_OFFSET_EPOCH = 1785585600.123456
POISON_ISO_OFFSET_EPOCH_SECONDS = 1785585600.0


def _make_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "ts.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _poison_bypassing_triggers(cfg, path, **columns):
    """Insert a row the way a pre-016 (or out-of-tree) writer did, with the
    normalizing triggers out of the way: the READ path must hold on its own,
    not only behind the write guard."""
    raw = sqlite3.connect(cfg.db_path)
    raw.execute("DROP TRIGGER IF EXISTS trg_documents_normalize_ts_insert")
    raw.execute("DROP TRIGGER IF EXISTS trg_documents_normalize_ts_update")
    cols = ", ".join(["path", *columns])
    marks = ", ".join("?" * (len(columns) + 1))
    raw.execute(
        f"INSERT INTO documents ({cols}) VALUES ({marks})",
        (path, *columns.values()),
    )
    raw.commit()
    raw.close()


# --- (a) write path ---------------------------------------------------------

def test_coerce_epoch_never_returns_a_non_numeric_value():
    from minni.timestamps import coerce_epoch

    assert coerce_epoch(1781909732.0, field="indexed_at") == 1781909732.0
    assert coerce_epoch(None, field="indexed_at") is None
    # Recoverable: ISO-8601 and numeric strings are converted, not stored raw.
    assert coerce_epoch(POISON_ISO, field="indexed_at") == POISON_EPOCH
    assert coerce_epoch("1781909732.0", field="indexed_at") == 1781909732.0
    # Unrecoverable: falls back to the caller's default rather than writing through.
    assert coerce_epoch(POISON_GARBAGE, field="indexed_at", default=7.0) == 7.0
    # Non-finite floats are not storable epochs either.
    assert coerce_epoch(float("nan"), field="indexed_at", default=7.0) == 7.0


def test_migration_016_triggers_normalize_a_poisoned_write(tmp_path):
    """The universal guard. Every in-tree writer passes time.time(), so the
    realistic poison source is an out-of-tree script; a Python-side check alone
    cannot cover those, the DB triggers can."""
    db_obj, _ = _make_db(tmp_path)
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at, last_modified) VALUES (?, ?, ?)",
            ("poison://insert", POISON_ISO, POISON_ISO),
        )
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT typeof(indexed_at) ti, indexed_at, typeof(last_modified) tm "
            "FROM documents WHERE path = 'poison://insert'"
        ).fetchone()
    assert row["ti"] == "real" and row["tm"] == "real"
    assert row["indexed_at"] == POISON_EPOCH_SECONDS

    with db_obj.transaction() as c:
        c.execute(
            "UPDATE documents SET indexed_at = ? WHERE path = 'poison://insert'",
            (POISON_ISO,),
        )
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT typeof(indexed_at) ti, indexed_at FROM documents "
            "WHERE path = 'poison://insert'"
        ).fetchone()
    assert row["ti"] == "real" and row["indexed_at"] == POISON_EPOCH_SECONDS


def test_migration_016_triggers_recover_a_numeric_text_epoch(tmp_path):
    """grok-review, High: numeric-TEXT epochs must be recovered, not swept to
    the 0.0 sentinel alongside genuine garbage — parse_epoch already recovers
    them, the SQL side must match."""
    db_obj, _ = _make_db(tmp_path)
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at, last_modified) VALUES (?, ?, ?)",
            ("poison://numeric-text", POISON_NUMERIC_TEXT, POISON_NUMERIC_TEXT),
        )
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT typeof(indexed_at) ti, indexed_at FROM documents "
            "WHERE path = 'poison://numeric-text'"
        ).fetchone()
    assert row["ti"] == "real"
    assert row["indexed_at"] == POISON_NUMERIC_TEXT_EPOCH

    with db_obj.transaction() as c:
        c.execute(
            "UPDATE documents SET last_modified = ? WHERE path = 'poison://numeric-text'",
            (POISON_NUMERIC_TEXT,),
        )
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT typeof(last_modified) tm, last_modified FROM documents "
            "WHERE path = 'poison://numeric-text'"
        ).fetchone()
    assert row["tm"] == "real"
    assert row["last_modified"] == POISON_NUMERIC_TEXT_EPOCH


def test_migration_016_triggers_recover_an_offset_iso_epoch(tmp_path):
    """grok-review, Medium (refuted for this SQLite build, locked in as a
    regression test): offset-qualified ISO-8601 must match parse_epoch's
    reading, not silently fall to the 0.0 sentinel."""
    db_obj, _ = _make_db(tmp_path)
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
            ("poison://iso-offset", POISON_ISO_OFFSET),
        )
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT typeof(indexed_at) ti, indexed_at FROM documents "
            "WHERE path = 'poison://iso-offset'"
        ).fetchone()
    assert row["ti"] == "real"
    assert row["indexed_at"] == POISON_ISO_OFFSET_EPOCH_SECONDS


def test_migration_016_rejects_multi_dot_numeric_garbage(tmp_path):
    """grok-review round 2, Medium: a loose numeric-TEXT guard let
    '1700000000.5.9' / '12..3' through — CAST(... AS REAL) takes the longest
    leading numeric prefix and stores a plausible-looking but WRONG epoch,
    exactly the "quietly inventing a plausible date" failure the 0.0 sentinel
    exists to prevent. parse_epoch('1700000000.5.9') is None (float() raises),
    so the migration must land on 0.0 too, not a prefix-parsed REAL."""
    from minni.migrations import run_migrations
    from minni.timestamps import parse_epoch

    multi_dot = "1700000000.5.9"
    assert parse_epoch(multi_dot) is None

    db_path = tmp_path / "legacy_multidot.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE documents ("
        " doc_id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,"
        " last_modified REAL, indexed_at REAL, last_accessed REAL)"
    )
    conn.execute(
        "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
        ("legacy://multidot", multi_dot),
    )
    conn.commit()

    run_migrations(conn)

    stored = conn.execute(
        "SELECT indexed_at FROM documents WHERE path = 'legacy://multidot'"
    ).fetchone()[0]
    assert stored == 0.0, (
        "multi-dot garbage must fall to the visible sentinel, not a plausible "
        "invented epoch derived from a numeric prefix"
    )
    conn.close()


def test_migration_016_triggers_recover_whitespace_padded_numeric_text(tmp_path):
    """grok-review round 2, Low: a leading/trailing-space numeric epoch
    (e.g. ' 1700000000.5') is recoverable by parse_epoch (strip() then
    float()) but failed the un-trimmed SQL GLOB guard and fell to the 0.0
    sentinel instead."""
    from minni.timestamps import parse_epoch

    padded = " 1700000000.5"
    assert parse_epoch(padded) == 1700000000.5

    db_obj, _ = _make_db(tmp_path)
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
            ("poison://padded-numeric", padded),
        )
    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT typeof(indexed_at) t, indexed_at FROM documents "
            "WHERE path = 'poison://padded-numeric'"
        ).fetchone()
    assert row["t"] == "real"
    assert row["indexed_at"] == 1700000000.5


def test_migration_016_numeric_text_matches_python_parse_epoch(tmp_path):
    from minni.migrations import run_migrations
    from minni.timestamps import parse_epoch

    db_path = tmp_path / "legacy_numeric.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE documents ("
        " doc_id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,"
        " last_modified REAL, indexed_at REAL, last_accessed REAL)"
    )
    conn.execute(
        "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
        ("legacy://numeric", POISON_NUMERIC_TEXT),
    )
    conn.commit()

    run_migrations(conn)

    stored = conn.execute(
        "SELECT indexed_at FROM documents WHERE path = 'legacy://numeric'"
    ).fetchone()[0]
    assert stored == parse_epoch(POISON_NUMERIC_TEXT) == POISON_NUMERIC_TEXT_EPOCH
    conn.close()


# --- (b) read path ----------------------------------------------------------

def test_filter_candidates_does_not_abort_on_a_text_indexed_at():
    """The headline bug: float() on a TEXT indexed_at raised ValueError, which
    handle_search turned into -32000 for the WHOLE recall."""
    from minni.retrieval import RetrievalEngine

    engine = RetrievalEngine.__new__(RetrievalEngine)  # no DB needed
    candidates = [
        {"doc_id": 1, "layer": "knowledge", "created_at": time.time()},
        {"doc_id": 590, "layer": "knowledge", "created_at": POISON_ISO},
    ]

    out = engine._filter_candidates(candidates, None, "2020-01-01", None)

    # Both survive: the poisoned value is recoverable, so it is parsed and the
    # row keeps its place in the window rather than being thrown away.
    assert sorted(r["doc_id"] for r in out) == [1, 590]


def test_filter_candidates_skips_and_reports_an_unparseable_row():
    from minni.retrieval import RetrievalEngine
    from minni.timestamps import (
        malformed_timestamp_report,
        reset_malformed_timestamps,
    )

    reset_malformed_timestamps()
    engine = RetrievalEngine.__new__(RetrievalEngine)
    candidates = [
        {"doc_id": 1, "layer": "knowledge", "created_at": time.time()},
        {"doc_id": 590, "layer": "knowledge", "created_at": POISON_GARBAGE},
    ]

    out = engine._filter_candidates(candidates, None, "2020-01-01", None)

    assert [r["doc_id"] for r in out] == [1], "unusable row skipped, not fatal"

    report = malformed_timestamp_report()
    assert report["total"] == 1, "a skipped row must be COUNTED, not swallowed"
    key = "retrieval._filter_candidates.created_at"
    assert report["by_field"].get(key) == 1
    assert report["examples"][key][0]["doc_id"] == 590, (
        "the bad row id must be identifiable from the report"
    )
    reset_malformed_timestamps()


def test_filter_candidates_keeps_bad_row_when_no_date_filter_applies():
    """Only the date filter needs the timestamp; a layer-only filter must not
    drop a row just because its timestamp is unusable."""
    from minni.retrieval import RetrievalEngine

    engine = RetrievalEngine.__new__(RetrievalEngine)
    out = engine._filter_candidates(
        [{"doc_id": 590, "layer": "knowledge", "created_at": POISON_GARBAGE}],
        ["knowledge"], None, None,
    )
    assert [r["doc_id"] for r in out] == [590]


def test_filter_candidates_falls_back_to_indexed_at_when_created_at_is_bad():
    """grok-review round 3: `r.get("created_at") or r.get("indexed_at") or 0`
    picked created_at whenever truthy, including non-numeric TEXT — shadowing
    a usable indexed_at on the same candidate and dropping the row under a
    date filter it would otherwise have passed."""
    from minni.retrieval import RetrievalEngine

    engine = RetrievalEngine.__new__(RetrievalEngine)
    good_indexed_at = time.time() - 86400 * 5
    out = engine._filter_candidates(
        [{
            "doc_id": 590, "layer": "knowledge",
            "created_at": POISON_GARBAGE, "indexed_at": good_indexed_at,
        }],
        None, "2020-01-01", None,
    )
    assert [r["doc_id"] for r in out] == [590], (
        "must fall back to the valid indexed_at instead of dropping the row "
        "because created_at was garbage"
    )


def test_decay_pass_survives_a_text_indexed_at(tmp_path):
    """Slice R7 schedules decay; it must not abort its transaction on contact
    with the row already sitting in the live DB."""
    from minni.decay import MemoryDecay

    db_obj, cfg = _make_db(tmp_path)
    now = time.time()
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at, page_type, access_count, decay_score)"
            " VALUES (?, ?, ?, ?, ?)",
            ("ok://doc", now - 86400 * 30, None, 0, 1.0),
        )
    _poison_bypassing_triggers(
        cfg, "poison://iso",
        indexed_at=POISON_ISO, page_type="correction", access_count=0, decay_score=1.0,
    )

    stats = MemoryDecay(db_obj, cfg).run_decay()

    assert stats["updated"] >= 1


def test_decay_pass_degrades_instead_of_skipping_a_bad_indexed_at(tmp_path):
    """grok-review round 3: a bad (non-NULL, unparseable) indexed_at used to
    `continue` past the whole document — a true skip — even when nothing else
    made that necessary: with no last_accessed at all, the NULL-indexed_at
    path already falls back to `now`, so the bad-indexed_at path should too,
    rather than leaving the row out of the pass entirely. The row is now
    processed (using the `now` fallback, since no last_accessed exists
    either) and counted into `bad_indexed_at`.
    """
    from minni.decay import MemoryDecay
    from minni.timestamps import (
        malformed_timestamp_report,
        reset_malformed_timestamps,
    )

    db_obj, cfg = _make_db(tmp_path)
    now = time.time()
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at, access_count, decay_score)"
            " VALUES (?, ?, ?, ?)",
            ("ok://doc", now - 86400 * 30, 0, 1.0),
        )
    _poison_bypassing_triggers(
        cfg, "poison://garbage",
        indexed_at=POISON_GARBAGE, access_count=0, decay_score=1.0,
    )

    reset_malformed_timestamps()
    stats = MemoryDecay(db_obj, cfg).run_decay()

    assert stats["bad_indexed_at"] == 1, (
        "the bad-indexed_at row must be counted, but as degraded, not skipped"
    )
    assert stats["updated"] >= 1, "the healthy row must still have been decayed"
    assert malformed_timestamp_report()["total"] >= 1

    with db_obj.cursor() as c:
        rows = {
            r["path"]: r["decay_score"]
            for r in c.execute("SELECT path, decay_score FROM documents")
        }
    assert rows["ok://doc"] < 1.0, "healthy doc decayed"
    # Falls back to `now` (no last_accessed to anchor on either), so its
    # apparent age is ~0 and its score stays at the ceiling — computed via
    # the fallback, not "left alone because it was skipped".
    assert rows["poison://garbage"] == 1.0
    reset_malformed_timestamps()


def test_decay_uses_valid_last_accessed_when_indexed_at_is_bad(tmp_path):
    """grok-review round 3's actually-reachable scenario: indexed_at is
    unparseable TEXT but last_accessed is a valid, old REAL — the document
    should decay using last_accessed as its anchor rather than being skipped
    outright (the old `continue` would have thrown this real, usable signal
    away)."""
    from minni.decay import MemoryDecay
    from minni.timestamps import reset_malformed_timestamps

    db_obj, cfg = _make_db(tmp_path)
    now = time.time()
    old_last_accessed = now - 86400 * 60
    _poison_bypassing_triggers(
        cfg, "poison://bad-indexed-at",
        indexed_at=POISON_GARBAGE, last_accessed=old_last_accessed,
        access_count=0, decay_score=1.0,
    )

    reset_malformed_timestamps()
    stats = MemoryDecay(db_obj, cfg).run_decay()

    assert stats["bad_indexed_at"] == 1
    assert stats["updated"] == 1, (
        "must have decayed using last_accessed as the anchor, not skipped"
    )

    with db_obj.cursor() as c:
        score = c.execute(
            "SELECT decay_score FROM documents WHERE path = 'poison://bad-indexed-at'"
        ).fetchone()["decay_score"]
    assert score < 1.0, "a 60-day-old last_accessed anchor must show real decay"
    reset_malformed_timestamps()


def test_decay_falls_back_to_indexed_at_on_bad_last_accessed(tmp_path):
    """grok-review, Low (round 1): a poisoned last_accessed alone used to
    `continue` past the whole document, even though the ordinary
    NULL-last_accessed path already falls back to indexed_at. A bad
    last_accessed must degrade to that same fallback, not skip the document
    outright.

    grok-review, Low (round 2): the fallback used to land in a shared
    "skipped" counter alongside a true skip, so the stats/log claimed
    "skipped" for a document that was in fact still decayed. It now has its
    own counter (`bad_last_accessed`), never conflated with a real skip
    (round 4: the shared counter was later removed entirely, since no row is
    ever left truly undecayed by this loop anymore).
    """
    from minni.decay import MemoryDecay
    from minni.timestamps import (
        malformed_timestamp_report,
        reset_malformed_timestamps,
    )

    db_obj, cfg = _make_db(tmp_path)
    now = time.time()
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at, access_count, decay_score)"
            " VALUES (?, ?, ?, ?)",
            ("ok://doc", now - 86400 * 30, 0, 1.0),
        )
    _poison_bypassing_triggers(
        cfg, "poison://bad-last-accessed",
        indexed_at=now - 86400 * 5, last_accessed=POISON_GARBAGE,
        access_count=0, decay_score=1.0,
    )

    reset_malformed_timestamps()
    stats = MemoryDecay(db_obj, cfg).run_decay()

    assert stats["bad_last_accessed"] == 1, "bad last_accessed counted separately"
    assert malformed_timestamp_report()["total"] >= 1

    with db_obj.cursor() as c:
        rows = {
            r["path"]: r["decay_score"]
            for r in c.execute("SELECT path, decay_score FROM documents")
        }
    assert rows["poison://bad-last-accessed"] != 1.0, (
        "must decay from indexed_at instead of being skipped entirely"
    )
    reset_malformed_timestamps()


def test_health_stale_docs_falls_back_to_last_modified_when_indexed_at_is_bad(tmp_path):
    """grok-review round 2, Low: `row["indexed_at"] or row["last_modified"] or 0`
    picked indexed_at whenever it was truthy — including a non-numeric TEXT
    value, which is truthy too — so a poisoned indexed_at would shadow a
    perfectly good last_modified and age_days would go to None even though a
    real age was computable. Must parse indexed_at first and only fall back
    to last_modified if that parse fails.

    Reachability note: SQLite orders TEXT values after INTEGER/REAL in a
    COALESCE comparison regardless of magnitude, so `WHERE COALESCE(indexed_at,
    last_modified, 0) < ?` can never actually select a row whose indexed_at is
    still poisoned TEXT — confirmed directly against this SQLite build. The
    stale_docs query's own WHERE clause is therefore a (separate, out-of-scope)
    blind spot: such rows never reach the loop this fix lives in. The
    preference-order bug in the loop body is still real defensive-correctness
    (and now matches decay.py's indexed_at/last_accessed handling), so it is
    fixed and exercised here directly against handle_health_report's loop via
    a stubbed cursor, bypassing the WHERE clause that would otherwise hide it.
    """
    from minni.minnid_runtime.health import HealthContext, handle_health_report
    from minni.principal import EffectivePrincipal

    now = time.time()
    good_last_modified = now - 86400 * 40

    class _StaleDocsCursor:
        """Returns the poisoned row only for the stale_docs SELECT; every
        other query in handle_health_report gets empty/zeroed results so the
        rest of the report assembles without touching a real DB."""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            self._sql = sql
            return self

        def fetchall(self):
            if "FROM documents" in self._sql and "COALESCE(indexed_at" in self._sql:
                return [{
                    "doc_id": 1,
                    "path": "poison://stale-fallback",
                    "indexed_at": POISON_GARBAGE,
                    "last_modified": good_last_modified,
                }]
            return []

        def fetchone(self):
            return {"n": 0, "max_rowid": 0}

    class _StaleDocsDB:
        def __init__(self, *a, **k):
            pass

        def cursor(self):
            return _StaleDocsCursor()

        def close(self):
            pass

    from minni.config import SovereignConfig
    from minni.timestamps import reset_malformed_timestamps

    reset_malformed_timestamps()
    # Hermetic: point vault_path at an empty tmp dir so the inbox_quarantine
    # scan (which reads context.default_config.vault_path) never touches a
    # real machine vault.
    hermetic_cfg = SovereignConfig(
        db_path=str(tmp_path / "unused.db"), vault_path=str(tmp_path),
    )
    context = HealthContext(
        make_error=lambda code, msg, rid: {"error": {"code": code, "message": msg}},
        make_response=lambda result, rid: {"result": result},
        guard_vault_root=lambda *a, **k: None,
        latency_snapshot=lambda: {},
        metrics_snapshot=lambda: {},
        afm_loop_enabled=lambda cfg: False,
        sovereign_db=lambda cfg: _StaleDocsDB(),
        default_config=hermetic_cfg,
    )
    op = EffectivePrincipal(agent_id="main", capabilities=["*"])
    resp = handle_health_report({"_recovery": False, "_principal": op}, 1, context)
    rep = resp["result"]

    entry = next(
        d for d in rep["stale_docs"] if d["path"] == "poison://stale-fallback"
    )
    assert entry["age_days"] is not None, (
        "must fall back to the valid last_modified instead of giving up "
        "because indexed_at was garbage"
    )
    assert entry["age_days"] == pytest.approx(40.0, abs=0.1)
    reset_malformed_timestamps()


def test_health_counts_stored_malformed_rows_even_if_nothing_read_them(tmp_path):
    """Read-path skips alone under-report: a poisoned row nothing has queried
    yet is still poison. This is the count the daemon health report publishes."""
    from minni.timestamps import (
        reset_malformed_timestamps,
        stored_malformed_timestamp_count,
    )

    db_obj, cfg = _make_db(tmp_path)
    _poison_bypassing_triggers(cfg, "poison://health", indexed_at=POISON_ISO)
    _poison_bypassing_triggers(cfg, "poison://health2", last_modified=POISON_GARBAGE)
    with db_obj.transaction() as c:
        c.execute(
            "INSERT INTO documents (path, indexed_at) VALUES (?, ?)",
            ("ok://health", time.time()),
        )

    reset_malformed_timestamps()
    with db_obj.cursor() as c:
        assert stored_malformed_timestamp_count(c) == 2


# --- (c) migration ----------------------------------------------------------

def test_migration_016_normalizes_rows_stored_before_the_fix(tmp_path):
    from minni.migrations import run_migrations

    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE documents ("
        " doc_id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,"
        " last_modified REAL, indexed_at REAL, last_accessed REAL)"
    )
    conn.executemany(
        "INSERT INTO documents (path, indexed_at, last_modified, last_accessed)"
        " VALUES (?, ?, ?, ?)",
        [
            ("legacy://iso", POISON_ISO, POISON_ISO, None),
            ("legacy://garbage", POISON_GARBAGE, None, None),
            ("legacy://ok", 1700000000.0, 1700000000.0, 1700000001.0),
        ],
    )
    conn.commit()

    run_migrations(conn)

    rows = {
        r[0]: tuple(r[1:])
        for r in conn.execute(
            "SELECT path, typeof(indexed_at), indexed_at, typeof(last_modified) "
            "FROM documents"
        )
    }
    assert rows["legacy://iso"] == ("real", POISON_EPOCH_SECONDS, "real")
    # Unparseable falls back to the 0.0 sentinel: visible as "needs attention"
    # rather than a plausible invented date.
    assert rows["legacy://garbage"] == ("real", 0.0, "null")
    assert rows["legacy://ok"] == ("real", 1700000000.0, "real")

    # Idempotent, and the table is clean afterwards.
    run_migrations(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM documents WHERE indexed_at IS NOT NULL "
        "AND typeof(indexed_at) NOT IN ('integer', 'real')"
    ).fetchone()[0] == 0
    conn.close()

"""P1.4 expand_typed_graph: privacy, caps, cycles, direction, deadlines."""
from __future__ import annotations

import dataclasses
import os
import sqlite3
import time

import pytest
import minni.graph_expansion as graph_expansion

from minni.config import DEFAULT_CONFIG
from minni.db import SovereignDB
from minni.graph_expansion import (
    MAX_GRAPH_CANDIDATES,
    MAX_NEIGHBORS_PER_SEED,
    MAX_SEED_PREFIX,
    MAX_SEEDS,
    expand_typed_graph,
)
from minni.principal import EffectivePrincipal, can_read_document
from minni.request_deadline import request_deadline

EMBED = b"\x00" * 1536


@pytest.fixture
def db(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = dataclasses.replace(
        DEFAULT_CONFIG,
        db_path=str(tmp_path / "graph.db"),
        vault_path=str(vault),
    )
    database = SovereignDB(cfg)
    database._get_conn()
    yield database
    database.close()


def _store(db):
    conn = db._get_conn()
    for row in conn.execute("PRAGMA database_list"):
        name = row["name"] if hasattr(row, "keys") else row[1]
        file = row["file"] if hasattr(row, "keys") else row[2]
        if str(name) == "main" and file:
            return os.path.realpath(str(file))
    raise AssertionError("expected a real on-disk main database")


def _principal(tmp_path, agent="codex"):
    return EffectivePrincipal(
        agent_id=agent,
        workspace_id="default",
        capabilities=["search", "read"],
        allowed_vault_roots=[str(tmp_path / "vault")],
    )


def _insert_doc(
    db,
    tmp_path,
    *,
    name,
    agent="codex",
    privacy="safe",
    status="accepted",
    page_type="learning",
    memory_kind="learning",
    body="body",
):
    path = str(tmp_path / "vault" / f"{name}.md")
    now = time.time()
    with db.cursor() as c:
        c.execute(
            """INSERT INTO documents
               (path, agent, sigil, last_modified, indexed_at, page_status,
                privacy_level, page_type, memory_kind, memory_uri)
               VALUES (?, ?, 'T', ?, ?, ?, ?, ?, ?, ?)""",
            (
                path,
                agent,
                now,
                now,
                status,
                privacy,
                page_type,
                memory_kind,
                f"learning://{name}",
            ),
        )
        doc_id = int(c.lastrowid)
        c.execute(
            """INSERT INTO chunk_embeddings
               (doc_id, chunk_index, chunk_text, heading_context, embedding, computed_at)
               VALUES (?, 0, ?, '', ?, ?)""",
            (doc_id, body, EMBED, now),
        )
    return doc_id


def _link(db, source, target, link_type, *, weight=1.0, confidence=1.0, status="active"):
    with db.cursor() as c:
        c.execute(
            """INSERT INTO memory_links
               (source_doc_id, target_doc_id, link_type, weight, created_at,
                confidence, edge_status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (source, target, link_type, weight, time.time(), confidence, status),
        )


def _seed(db, doc_id, score=1.0, store_id=None):
    tagged = {"doc_id": doc_id, "score": score, "store_id": _store(db) if store_id is None else store_id}
    return tagged


def _expand(db, tmp_path, seeds, *, agent="codex", deadline=None, principal=None, store_id=None):
    if deadline is None:
        deadline = time.monotonic() + 30
    if principal is None:
        principal = _principal(tmp_path, agent)
    if store_id is None:
        store_id = _store(db)
    with request_deadline(deadline):
        return expand_typed_graph(
            db=db,
            store_id=store_id,
            seeds=seeds,
            principal=principal,
            workspace="default",
            deadline_monotonic=deadline,
        )


def test_authorization_before_text_hydration(db, tmp_path, monkeypatch):
    seed = _insert_doc(db, tmp_path, name="seed", body="seed-body")
    secret = _insert_doc(
        db, tmp_path, name="secret", agent="foreign", privacy="private",
        body="SECRET-NEIGHBOR-TEXT",
    )
    public = _insert_doc(db, tmp_path, name="public", body="PUBLIC-NEIGHBOR-TEXT")
    _link(db, seed, secret, "relates")
    _link(db, seed, public, "extends")

    real_gate = can_read_document

    def spy(principal, workspace, metadata):
        assert "chunk_text" not in metadata
        assert "SECRET-NEIGHBOR-TEXT" not in str(metadata)
        return real_gate(principal, workspace, metadata)

    monkeypatch.setattr("minni.graph_expansion.can_read_document", spy)
    result = _expand(db, tmp_path, [_seed(db, seed)])
    assert result.graph_status == "ok"
    texts = [row["chunk_text"] for row in result.neighbors]
    assert "PUBLIC-NEIGHBOR-TEXT" in texts
    assert "SECRET-NEIGHBOR-TEXT" not in texts
    assert all("withheld" not in row for row in result.neighbors)
    ids = {row["doc_id"] for row in result.neighbors}
    assert public in ids
    assert secret not in ids


def test_denied_rows_do_not_hide_later_eligible_neighbor(db, tmp_path):
    """Counterexample: LIMIT 48 before auth hid neighbor 49 behind 48 denied."""
    seed = _insert_doc(db, tmp_path, name="seed")
    for i in range(48):
        denied = _insert_doc(
            db, tmp_path, name=f"denied-{i:02d}", agent="foreign", privacy="private",
            body=f"denied-{i}",
        )
        _link(db, seed, denied, "relates")
    visible = _insert_doc(db, tmp_path, name="visible-49", body="visible-49")
    _link(db, seed, visible, "relates")
    result = _expand(db, tmp_path, [_seed(db, seed)])
    ids = [row["doc_id"] for row in result.neighbors]
    assert visible in ids
    assert len(ids) == 1


def test_filter_before_per_seed_cap(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    for i in range(MAX_NEIGHBORS_PER_SEED + 2):
        denied = _insert_doc(
            db, tmp_path, name=f"denied-{i}", agent="foreign", privacy="private",
            body=f"denied-{i}",
        )
        _link(db, seed, denied, "relates", confidence=0.99)
    visible = _insert_doc(db, tmp_path, name="visible", body="visible-body")
    _link(db, seed, visible, "relates", confidence=0.50)
    result = _expand(db, tmp_path, [_seed(db, seed)])
    ids = [row["doc_id"] for row in result.neighbors]
    assert visible in ids
    assert len(ids) <= MAX_NEIGHBORS_PER_SEED


def test_mirrored_relates_dedup_before_cap(db, tmp_path):
    """Two directions to the same 6 neighbors must not consume the cap twice."""
    seed = _insert_doc(db, tmp_path, name="seed")
    neighbors = []
    for i in range(MAX_NEIGHBORS_PER_SEED):
        nb = _insert_doc(db, tmp_path, name=f"nb-{i}", body=f"nb-{i}")
        neighbors.append(nb)
        _link(db, seed, nb, "relates")
        _link(db, nb, seed, "relates")
    result = _expand(db, tmp_path, [_seed(db, seed)])
    ids = [row["doc_id"] for row in result.neighbors]
    assert sorted(ids) == sorted(neighbors)
    assert len(ids) == MAX_NEIGHBORS_PER_SEED


def test_cycles_do_not_emit_seed_or_self(db, tmp_path):
    a = _insert_doc(db, tmp_path, name="a", body="a-body")
    b = _insert_doc(db, tmp_path, name="b", body="b-body")
    _link(db, a, b, "relates")
    _link(db, b, a, "relates")
    _link(db, a, a, "relates")
    result = _expand(db, tmp_path, [_seed(db, a)])
    ids = [row["doc_id"] for row in result.neighbors]
    assert ids == [b]


def test_dedup_keeps_max_scoring_path(db, tmp_path):
    a = _insert_doc(db, tmp_path, name="a")
    b = _insert_doc(db, tmp_path, name="b")
    c = _insert_doc(db, tmp_path, name="c", body="shared")
    _link(db, a, c, "updates", confidence=1.0)
    _link(db, b, c, "relates", confidence=1.0)
    result = _expand(db, tmp_path, [_seed(db, a, 1.0), _seed(db, b, 1.0)])
    matches = [row for row in result.neighbors if row["doc_id"] == c]
    assert len(matches) == 1
    assert matches[0]["link_type"] == "updates"


def test_updates_incoming_finds_successor(db, tmp_path):
    old = _insert_doc(db, tmp_path, name="old", status="superseded", body="old-body")
    successor = _insert_doc(db, tmp_path, name="new", body="successor-body")
    _link(db, successor, old, "updates")
    result = _expand(db, tmp_path, [_seed(db, old, 0.8)])
    ids = [row["doc_id"] for row in result.neighbors]
    assert successor in ids
    assert old not in ids
    assert result.neighbors[0]["graph_paths"][0]["direction"] == "incoming"


def test_stale_and_wiki_and_closed_neighbors_are_absent(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    stale = _insert_doc(db, tmp_path, name="stale", body="stale-body")
    wiki = _insert_doc(
        db, tmp_path, name="wiki", page_type="wiki", memory_kind="wiki", body="wiki-body",
    )
    expired = _insert_doc(db, tmp_path, name="expired", status="expired", body="expired-body")
    _link(db, seed, stale, "relates", status="stale")
    _link(db, seed, wiki, "relates")
    _link(db, seed, expired, "relates")
    result = _expand(db, tmp_path, [_seed(db, seed)])
    assert {row["doc_id"] for row in result.neighbors} == set()


def test_seed_prefix_does_not_scan_unbounded_custom_sequence(db, tmp_path):
    first = _insert_doc(db, tmp_path, name="first")
    shown = _insert_doc(db, tmp_path, name="shown", body="from-prefix")
    _link(db, first, shown, "relates")
    later = _insert_doc(db, tmp_path, name="later")
    hidden = _insert_doc(db, tmp_path, name="hidden", body="from-tail")
    _link(db, later, hidden, "relates")
    store = _store(db)

    class CountingSeq:
        def __init__(self, items):
            self._items = items
            self.reads = 0

        def __len__(self):
            return len(self._items)

        def __getitem__(self, index):
            self.reads += 1
            return self._items[index]

    tail = [{"doc_id": later, "score": 99.0, "store_id": store}] * 5000
    padding = [{"skipped": True}] * (MAX_SEED_PREFIX - 1)
    seq = CountingSeq([_seed(db, first, 0.1)] + padding + tail)
    result = _expand(db, tmp_path, seq)
    assert seq.reads <= MAX_SEED_PREFIX
    texts = [row["chunk_text"] for row in result.neighbors]
    assert "from-prefix" in texts
    assert "from-tail" not in texts


def test_untagged_seeds_are_not_expanded(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    nb = _insert_doc(db, tmp_path, name="nb", body="leaked")
    _link(db, seed, nb, "relates")
    result = _expand(db, tmp_path, [{"doc_id": seed, "score": 1.0}])
    assert result.neighbors == ()


def test_caller_store_id_cannot_relabel_db(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    with pytest.raises(ValueError, match="cross-corpus"):
        _expand(db, tmp_path, [_seed(db, seed, store_id="alias")], store_id="alias")


def test_foreign_private_seed_does_not_leak_id_or_path(db, tmp_path):
    private_seed = _insert_doc(
        db, tmp_path, name="private-seed", agent="foreign", privacy="private",
        body="PRIVATE-SEED",
    )
    public = _insert_doc(db, tmp_path, name="public-from-private", body="should-not-appear")
    _link(db, private_seed, public, "relates")
    result = _expand(db, tmp_path, [_seed(db, private_seed)])
    blob = repr(result)
    assert result.neighbors == ()
    assert "PRIVATE-SEED" not in blob
    assert f"seed_doc_id" not in blob or private_seed not in [
        row.get("seed_doc_id") for row in result.neighbors
    ]
    assert str(private_seed) not in "".join(str(row.get("path") or "") for row in result.neighbors)


def test_null_confidence_is_legacy_explicit_one(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    nb = _insert_doc(db, tmp_path, name="nb", body="legacy")
    _link(db, seed, nb, "relates", confidence=None)
    result = _expand(db, tmp_path, [_seed(db, seed)])
    assert [row["doc_id"] for row in result.neighbors] == [nb]
    assert result.neighbors[0]["graph_paths"][0]["confidence"] == 1.0


def test_nonfinite_confidence_is_dropped_not_promoted(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    inf = _insert_doc(db, tmp_path, name="inf", body="inf-body")
    nan = _insert_doc(db, tmp_path, name="nan", body="nan-body")
    neg = _insert_doc(db, tmp_path, name="neg", body="neg-body")
    ok = _insert_doc(db, tmp_path, name="ok", body="ok-body")
    _link(db, seed, inf, "relates", confidence=float("inf"))
    _link(db, seed, nan, "relates", confidence=float("nan"))
    _link(db, seed, neg, "relates", confidence=-0.2)
    _link(db, seed, ok, "relates", confidence=0.9)
    result = _expand(db, tmp_path, [_seed(db, seed)])
    ids = [row["doc_id"] for row in result.neighbors]
    assert inf not in ids
    assert neg not in ids
    assert ok in ids
    # SQLite REAL may persist NaN as NULL; NULL is the legacy-explicit 1.0
    # policy. Inf/negative stay numeric and must not be promoted to 1.0.


def test_nan_deadline_does_not_expand(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    nb = _insert_doc(db, tmp_path, name="nb", body="nope")
    _link(db, seed, nb, "relates")
    result = expand_typed_graph(
        db=db,
        store_id=_store(db),
        seeds=[_seed(db, seed)],
        principal=_principal(tmp_path),
        deadline_monotonic=float("nan"),
    )
    assert result.graph_status == "disabled"
    assert result.neighbors == ()


def test_seed_cap_and_total_cap(db, tmp_path):
    seeds = []
    for i in range(MAX_SEEDS + 2):
        seed = _insert_doc(db, tmp_path, name=f"seed-{i}")
        seeds.append(_seed(db, seed, float(MAX_SEEDS + 2 - i)))
        for j in range(2):
            nb = _insert_doc(db, tmp_path, name=f"n-{i}-{j}", body=f"n-{i}-{j}")
            _link(db, seed, nb, "relates")
    result = _expand(db, tmp_path, seeds)
    assert len(result.neighbors) <= MAX_GRAPH_CANDIDATES


def test_principal_none_and_missing_deadline_disable(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    nb = _insert_doc(db, tmp_path, name="nb")
    _link(db, seed, nb, "relates")
    none = expand_typed_graph(
        db=db,
        store_id=_store(db),
        seeds=[_seed(db, seed)],
        principal=None,
        deadline_monotonic=time.monotonic() + 30,
    )
    assert none.graph_status == "disabled"
    assert none.neighbors == ()
    no_deadline = expand_typed_graph(
        db=db,
        store_id=_store(db),
        seeds=[_seed(db, seed)],
        principal=_principal(tmp_path),
        deadline_monotonic=None,
    )
    assert no_deadline.graph_status == "disabled"
    assert no_deadline.neighbors == ()


def test_expired_deadline_does_not_run_graph_leg(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    nb = _insert_doc(db, tmp_path, name="nb", body="should-not-hydrate")
    _link(db, seed, nb, "relates")
    result = _expand(db, tmp_path, [_seed(db, seed)], deadline=time.monotonic() - 1)
    assert result.graph_status == "degraded"
    assert result.neighbors == ()


def test_cross_store_seed_identity_is_rejected(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    with pytest.raises(ValueError, match="cross-corpus"):
        _expand(db, tmp_path, [_seed(db, seed, store_id="other-store")])


def _second_conn_privatize(db, doc_id, *, body="NEW-PRIVATE-CONTENT"):
    other = sqlite3.connect(_store(db))
    try:
        other.execute(
            "UPDATE documents SET agent='foreign', privacy_level='private' WHERE doc_id=?",
            (doc_id,),
        )
        other.execute(
            "UPDATE chunk_embeddings SET chunk_text=? WHERE doc_id=?",
            (body, doc_id),
        )
        other.commit()
    finally:
        other.close()


def test_second_connection_cannot_mix_private_content_into_hydration(db, tmp_path, monkeypatch):
    seed = _insert_doc(db, tmp_path, name="seed", body="seed-body")
    public = _insert_doc(db, tmp_path, name="public", body="PUBLIC-BODY")
    _link(db, seed, public, "relates")
    original = graph_expansion._hydrate_text

    def hijack(cursor, doc_id):
        assert cursor.connection.in_transaction, "hydration must run inside the read snapshot"
        _second_conn_privatize(db, doc_id)
        return original(cursor, doc_id)

    monkeypatch.setattr(graph_expansion, "_hydrate_text", hijack)
    result = _expand(db, tmp_path, [_seed(db, seed)])
    assert result.graph_status == "ok"
    texts = [row["chunk_text"] for row in result.neighbors]
    assert "PUBLIC-BODY" in texts
    assert "NEW-PRIVATE-CONTENT" not in texts
    assert all(row.get("privacy_level") != "private" for row in result.neighbors)


def test_second_connection_source_privacy_drift_uses_snapshot(db, tmp_path, monkeypatch):
    seed = _insert_doc(db, tmp_path, name="seed", body="SEED-PUBLIC")
    public = _insert_doc(db, tmp_path, name="nb", body="NB-PUBLIC")
    _link(db, seed, public, "relates")
    original = graph_expansion._authorize_seeds

    def hijack(cursor, ranked_seeds, **kwargs):
        assert cursor.connection.in_transaction, "seed auth must run inside the read snapshot"
        allowed = original(cursor, ranked_seeds, **kwargs)
        _second_conn_privatize(db, seed, body="SEED-NOW-PRIVATE")
        return allowed

    monkeypatch.setattr(graph_expansion, "_authorize_seeds", hijack)
    result = _expand(db, tmp_path, [_seed(db, seed)])
    assert result.graph_status == "ok"
    assert [row["doc_id"] for row in result.neighbors] == [public]
    blob = repr(result) + "".join(row.get("chunk_text") or "" for row in result.neighbors)
    assert "SEED-NOW-PRIVATE" not in blob
    assert "NEW-PRIVATE-CONTENT" not in blob


def test_sqlite_write_lock_is_deadline_bounded(db, tmp_path):
    seed = _insert_doc(db, tmp_path, name="seed")
    nb = _insert_doc(db, tmp_path, name="nb", body="locked")
    _link(db, seed, nb, "relates")
    path = _store(db)
    db._get_conn().execute("PRAGMA journal_mode=DELETE")
    holder = sqlite3.connect(path, timeout=30)
    holder.isolation_level = None
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    try:
        result = _expand(
            db, tmp_path, [_seed(db, seed)], deadline=started + 0.02,
        )
        assert result.graph_status == "degraded"
        assert result.neighbors == ()
        assert time.monotonic() - started < 0.5
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_denied_and_absent_neighbors_are_indistinguishable(db, tmp_path):
    seed_a = _insert_doc(db, tmp_path, name="seed-a")
    seed_b = _insert_doc(db, tmp_path, name="seed-b")
    private = _insert_doc(
        db, tmp_path, name="private", agent="foreign", privacy="private", body="hidden",
    )
    _link(db, seed_a, private, "relates")
    left = _expand(db, tmp_path, [_seed(db, seed_a)])
    right = _expand(db, tmp_path, [_seed(db, seed_b)])
    assert left.graph_status == right.graph_status == "ok"
    assert [(r["doc_id"], r["chunk_text"]) for r in left.neighbors] == [
        (r["doc_id"], r["chunk_text"]) for r in right.neighbors
    ]


def test_nested_expiry_releases_owned_savepoint_and_preserves_caller_write(db, tmp_path, monkeypatch):
    seed = _insert_doc(db, tmp_path, name="nested-seed", body="seed")
    neighbor = _insert_doc(db, tmp_path, name="nested-neighbor", body="neighbor")
    _link(db, seed, neighbor, "relates")
    seeds = [_seed(db, seed)]
    conn = db._get_conn()
    conn.execute("CREATE TEMP TABLE caller_pending(value INTEGER)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_pending VALUES (7)")
    conn.execute("SAVEPOINT minni_graph_expand")  # caller's legacy-looking name
    statements = []
    conn.set_trace_callback(statements.append)
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    original = graph_expansion._hydrate_text
    def expire(cursor, doc_id):
        now[0] += 1
        return original(cursor, doc_id)
    monkeypatch.setattr(graph_expansion, "_hydrate_text", expire)
    result = _expand(db, tmp_path, seeds, deadline=now[0] + 0.1)
    assert result.graph_status == "degraded"
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM caller_pending").fetchone()[0] == 7
    created = [sql.split()[-1] for sql in statements if sql.startswith("SAVEPOINT ")]
    assert len(created) == 1 and created[0] != "minni_graph_expand"
    with pytest.raises(sqlite3.OperationalError, match="no such savepoint"):
        conn.execute(f"RELEASE SAVEPOINT {created[0]}")
    conn.execute("RELEASE SAVEPOINT minni_graph_expand")
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM caller_pending").fetchone()[0] == 0


def test_failed_savepoint_creation_never_cleans_up_caller_scope(db):
    conn = db._get_conn()
    conn.execute("CREATE TEMP TABLE caller_pending(value INTEGER)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO caller_pending VALUES (9)")
    conn.execute("SAVEPOINT minni_graph_expand")
    actions = []
    def authorize(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_SAVEPOINT:
            actions.append((arg1, arg2))
            if arg1 == "BEGIN":
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    conn.set_authorizer(authorize)
    try:
        with request_deadline(time.monotonic() + 10):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                with graph_expansion._read_snapshot(conn):
                    pytest.fail("creation failure must not yield")
    finally:
        conn.set_authorizer(None)
    assert len(actions) == 1 and actions[0][0] == "BEGIN"
    assert actions[0][1] != "minni_graph_expand"
    assert conn.execute("SELECT value FROM caller_pending").fetchone()[0] == 9
    conn.execute("RELEASE SAVEPOINT minni_graph_expand")
    conn.rollback()


def test_cleanup_does_not_allow_general_sql_after_expiry(db, monkeypatch):
    from minni.request_deadline import RequestDeadlineExceeded

    conn = db._get_conn()
    conn.execute("BEGIN")
    now = [time.monotonic()]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    with request_deadline(now[0] + 0.1):
        with graph_expansion._read_snapshot(conn):
            now[0] += 1
        with pytest.raises(RequestDeadlineExceeded):
            conn.execute("SELECT 1")
    assert conn.in_transaction
    conn.rollback()

"""Store-time semantic indexing of durable memory (engine gap fix).

VERIFIED GAP: durable-store socket paths wrote learnings rows only; the FAISS
semantic index (chunk_embeddings) was populated solely by the out-of-band
VaultIndexer. So an agent recalling what it just stored got lexical-only hits
(the `learnings` stream); the semantic `results` stream was empty until an
index_all run.

FIX UNDER TEST: _resolve_candidate(accept) and _handle_learn(force=true) now
also chunk+embed the stored content into documents+chunk_embeddings+vault_fts
(via RetrievalEngine.index_durable_document) and refresh the live in-memory
FAISS, so a subsequent search in the SAME process returns the stored doc in the
semantic `results` stream — without an out-of-band indexer or restart.

These tests use TEMP DBs only (never the live ~/.minni). They reset the daemon
singletons so the shared RetrievalEngine reconnects to the temp DB.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minnid
import config as cfg_mod
from migrations import run_migrations
from principal import EffectivePrincipal


# A document long enough to survive the markdown chunker's min_tokens (64) floor
# — i.e. it actually produces semantic chunks, the same way a real corpus doc
# would. The distinctive query term ("photosynthesis") only appears here.
_DOC_PHOTO = (
    "# Plant Energy Conversion\n\n"
    "Photosynthesis is the biochemical process by which green plants, algae, and "
    "some bacteria convert light energy from the sun into chemical energy stored "
    "in glucose molecules. The reaction takes carbon dioxide drawn from the air "
    "and water absorbed through the roots and, driven by chlorophyll in the "
    "chloroplasts, produces sugar and releases oxygen as a byproduct. This "
    "oxygen-evolving light reaction is the foundation of nearly every food chain "
    "on the planet and is the reason the atmosphere is breathable. Without this "
    "energy conversion pathway the biosphere as we know it could not exist, and "
    "carbon fixation would stall entirely across every terrestrial ecosystem.\n"
)

_DOC_TIDES = (
    "# Ocean Tides\n\n"
    "Tides are the regular rise and fall of sea levels caused chiefly by the "
    "gravitational pull of the moon and, to a lesser degree, the sun acting on "
    "the rotating earth. The moon's gravity raises a bulge of water on the side "
    "of the planet nearest to it and a second bulge on the far side, so most "
    "coastlines experience two high tides and two low tides each lunar day. The "
    "timing and height of these tides shift predictably with the lunar cycle and "
    "the alignment of the sun, producing the stronger spring tides and the weaker "
    "neap tides that mariners have charted for centuries.\n"
)


@pytest.fixture
def temp_engine(tmp_path, monkeypatch):
    """Point the whole engine at a fresh temp DB + temp vault, reset singletons."""
    db_path = str(tmp_path / "durable_sem.db")
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", db_path, raising=False)
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"), raising=False
    )
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG,
        "writeback_path",
        str(tmp_path / "learnings"),
        raising=False,
    )
    # Reset the daemon singletons so the shared RetrievalEngine / WriteBackMemory
    # reconnect to THIS temp DB (their thread-local conns are lazy).
    monkeypatch.setattr(minnid, "_retrieval", None, raising=False)
    monkeypatch.setattr(minnid, "_writeback", None, raising=False)
    monkeypatch.setattr(minnid, "_episodic", None, raising=False)

    # Base schema first (_init_schema), THEN migrations ALTER it (the PR-6
    # learnings columns like `assertion`/`status` are ADDed by migration 005 on
    # top of the baseline table — same setup order the force-learn tests use).
    from db import SovereignDB
    _seed_db = SovereignDB()
    run_migrations(_seed_db._get_conn())

    yield db_path, tmp_path
    # Belt-and-suspenders: drop the engine handles so the next test rebuilds.
    minnid._retrieval = None
    minnid._writeback = None
    minnid._episodic = None


def _operator():
    return EffectivePrincipal(agent_id="main", capabilities=["*"])


def _semantic_doc_ids(resp):
    """The numeric doc_ids surfaced in the SEMANTIC `results` stream."""
    return [r.get("doc_id") for r in resp["result"]["results"]]


def _search(query, principal, depth="chunk"):
    return minnid._handle_search(
        {"query": query, "limit": 5, "depth": depth, "_principal": principal},
        request_id=99,
    )


def _result_text(item):
    """Best-effort raw text from a search result across depth tiers."""
    return (
        item.get("chunk_text")
        or item.get("full_document_text")
        or item.get("text")
        or item.get("snippet")
        or ""
    )


def _result_source(item):
    return str(item.get("source") or item.get("path") or "")


def test_resolve_accept_makes_doc_semantically_recallable(temp_engine):
    """Promote a candidate (accept) → it appears in the SEMANTIC results stream."""
    db_path, _ = temp_engine
    op = _operator()

    # Stage (proposal-first) then promote via the governed accept path.
    staged = minnid._stage_candidate(
        {"content": _DOC_PHOTO, "_principal": op, "workspace_id": "default"}, 1
    )
    cid = staged["result"]["candidate_id"]
    resolved = minnid._resolve_candidate(
        {"candidate_id": cid, "decision": "accept", "_principal": op}, 2
    )
    assert resolved["result"]["new_status"] == "accepted"
    lid = resolved["result"]["learning_id"]
    assert lid

    # chunk_embeddings must now hold this document's chunks (semantic index).
    conn = sqlite3.connect(db_path)
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    n_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    conn.close()
    assert n_chunks >= 1, "accept did not populate chunk_embeddings"
    assert n_docs >= 1

    # The decisive assertion: the SEMANTIC results stream (FAISS over
    # chunk_embeddings) returns the stored doc in-process, no index_all run.
    resp = _search("how do plants make energy from sunlight", op)
    results = resp["result"]["results"]
    assert results, "semantic results stream is empty — store-time index not visible"
    top = results[0]
    # The recalled doc is the store-time-indexed durable doc (synthetic _durable
    # path), and it surfaced with a real semantic rank — not a lexical-only hit.
    assert "_durable" in _result_source(top), (
        f"top semantic result is not the durable-stored doc: {top}"
    )
    assert "photosynthesis" in _result_text(top).lower()
    assert top.get("doc_id") is not None


def test_learn_force_makes_doc_semantically_recallable(temp_engine):
    """force=true durable learn → appears in the SEMANTIC results stream."""
    op = _operator()
    resp = minnid._handle_learn(
        {"content": _DOC_TIDES, "force": True, "_principal": op}, 1
    )
    assert resp["result"]["status"] == "ok"

    search = _search("what causes the rise and fall of the sea", op)
    results = search["result"]["results"]
    assert results, "force-learn content not in semantic results stream"
    top = results[0]
    assert "_durable" in _result_source(top)
    assert "tide" in _result_text(top).lower()


def test_learn_force_ignores_model_supplied_wiki_page_type(temp_engine):
    """M2: page_type was previously copied verbatim from the model's own
    frontmatter into the durable-learn synthetic doc. can_read_document treats
    page_type in {wiki,handoff,synthesis,decision,session} as cross-agent
    visible, so a force=true learn whose content frontmatter declared
    `type: wiki` would make an otherwise owner-scoped durable learning
    readable by every other agent. The synthetic doc's page_type must be
    pinned to a fixed non-cross-visible value ("learning") regardless of any
    model-supplied frontmatter type.
    """
    op = _operator()
    doc_with_wiki_frontmatter = (
        "---\n"
        "type: wiki\n"
        "status: accepted\n"
        "---\n\n"
        + _DOC_TIDES
    )
    resp = minnid._handle_learn(
        {"content": doc_with_wiki_frontmatter, "force": True, "_principal": op}, 1
    )
    assert resp["result"]["status"] == "ok"

    from db import SovereignDB
    db_obj = SovereignDB()
    with db_obj.cursor() as c:
        c.execute(
            "SELECT page_type FROM documents WHERE path LIKE '%_durable%' "
            "ORDER BY doc_id DESC LIMIT 1"
        )
        row = c.fetchone()
    assert row is not None, "durable-learn synthetic doc was not indexed"
    assert row["page_type"] == "learning", (
        f"model-supplied frontmatter 'type: wiki' leaked into the synthetic "
        f"doc's page_type (got {row['page_type']!r}); a foreign agent could "
        "read this owner-scoped learning via the shared-wiki cross-visibility "
        "grant in can_read_document"
    )
    assert row["page_type"] not in {
        "wiki", "handoff", "synthesis", "decision", "session",
    }


def test_multiple_relevant_docs_ranked(temp_engine):
    """Two stored docs → the relevant one ranks, both are semantically present."""
    op = _operator()
    for content in (_DOC_PHOTO, _DOC_TIDES):
        staged = minnid._stage_candidate(
            {"content": content, "_principal": op, "workspace_id": "default"}, 1
        )
        cid = staged["result"]["candidate_id"]
        minnid._resolve_candidate(
            {"candidate_id": cid, "decision": "accept", "_principal": op}, 2
        )

    resp = _search("oxygen and glucose from light in green leaves", op)
    results = resp["result"]["results"]
    assert results, "no semantic results for a stored-doc query"
    # The photosynthesis doc must be the top semantic hit for this query.
    assert "photosynthesis" in _result_text(results[0]).lower(), (
        f"expected photosynthesis doc on top, got: {_result_source(results[0])}"
    )

    # BOTH stored docs must be semantically indexed — not just the first one.
    # A regression where the SECOND store fails to index would leave the tides
    # doc invisible to a tides-specific query while the photosynthesis assertion
    # above still passes. Searching the other doc's topic catches that.
    tides = _search("what causes the rise and fall of the sea", op)
    tides_results = tides["result"]["results"]
    assert tides_results, "second stored doc (tides) is not semantically indexed"
    assert "tide" in _result_text(tides_results[0]).lower(), (
        f"expected tides doc on top for a tides query, got: "
        f"{_result_source(tides_results[0])}"
    )


def test_reaccept_does_not_duplicate_chunk_embeddings(temp_engine):
    """Idempotency via the REAL ingest path: stage+resolve the same content twice.

    This exercises the production duplicate case the prior version of this test
    missed: _resolve_candidate enforces terminal once-only resolution, so a
    re-store of identical content always goes through a NEW candidate -> NEW
    learning_id. If the synthetic ``documents.path`` were keyed on learning_id
    (the original bug) each store would mint a fresh path and accumulate
    duplicate documents + chunk_embeddings rows. Keying the path on the CONTENT
    hash collapses repeated stores of identical content onto one upsert target,
    so the row counts stay constant across the second ingest.
    """
    db_path, _ = temp_engine
    op = _operator()

    def _store():
        staged = minnid._stage_candidate(
            {"content": _DOC_PHOTO, "_principal": op, "workspace_id": "default"}, 1
        )
        cid = staged["result"]["candidate_id"]
        return minnid._resolve_candidate(
            {"candidate_id": cid, "decision": "accept", "_principal": op}, 2
        )

    def _counts():
        conn = sqlite3.connect(db_path)
        chunks = conn.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE doc_id IN "
            "(SELECT doc_id FROM documents WHERE path LIKE '%_durable%')"
        ).fetchone()[0]
        docs = conn.execute(
            "SELECT COUNT(*) FROM documents WHERE path LIKE '%_durable%'"
        ).fetchone()[0]
        conn.close()
        return docs, chunks

    r1 = _store()
    assert r1["result"]["new_status"] == "accepted"
    docs_first, chunks_first = _counts()
    assert chunks_first >= 1
    assert docs_first == 1

    # Re-store the SAME content through the FULL governed ingest path again —
    # this generates a NEW candidate and a NEW learning_id, exactly as a real
    # re-store would. The content hash keeps the documents.path stable, so the
    # second ingest UPSERTS rather than duplicating.
    r2 = _store()
    assert r2["result"]["new_status"] == "accepted"
    assert r2["result"]["learning_id"] != r1["result"]["learning_id"], (
        "test precondition: re-store must produce a new learning_id"
    )

    docs_second, chunks_second = _counts()
    assert docs_second == docs_first, (
        f"re-store created a duplicate documents row ({docs_first} -> {docs_second})"
    )
    assert chunks_second == chunks_first, (
        f"re-store duplicated chunk_embeddings rows "
        f"({chunks_first} -> {chunks_second})"
    )


def test_index_durable_learning_degrades_without_explicit_db(tmp_path, monkeypatch):
    """Data-safety + fail-open: omitting db= must NOT touch the live DB and must
    NOT raise.

    The data-safety guarantee is that a missing db= never defaults to the live
    DEFAULT_CONFIG database. The fail-open guarantee is that this helper NEVER
    raises (its caller's durable store has already committed; a raise here would
    fail the RPC for an already-persisted memory). So omitting db= must degrade
    gracefully: log + return, writing nothing.

    We point DEFAULT_CONFIG at a tmp path and assert no DB file is created there —
    making the 'never touches the live DB' guarantee machine-checkable rather than
    relying on the ValueError firing before any DB access (execution order).
    """
    live_db = tmp_path / "would_be_live.db"
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "db_path", str(live_db), raising=False
    )
    monkeypatch.setattr(minnid, "_retrieval", None, raising=False)

    # Must NOT raise — fail-open contract.
    minnid._index_durable_learning("main", _DOC_PHOTO, key="learning:1")

    # Must NOT have created/written the DEFAULT_CONFIG (would-be live) DB.
    assert not live_db.exists(), (
        "missing db= defaulted to the live DEFAULT_CONFIG database — data-safety "
        "guard breached"
    )


def test_divergent_db_writes_to_store_db_not_default(tmp_path, monkeypatch):
    """A db= pointing at DB-B must write there, leaving DEFAULT_CONFIG (DB-A) clean.

    Covers the transient-engine branch of _index_durable_learning: when the
    supplied store db differs from DEFAULT_CONFIG.db_path, the semantic index
    MUST land in the store's DB and the live DEFAULT_CONFIG DB must be untouched.
    """
    from db import SovereignDB
    from config import SovereignConfig

    db_a = str(tmp_path / "live_default.db")   # DEFAULT_CONFIG points here
    db_b = str(tmp_path / "store_target.db")   # explicit db= points here

    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", db_a, raising=False)
    monkeypatch.setattr(
        cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"), raising=False
    )
    monkeypatch.setattr(minnid, "_retrieval", None, raising=False)

    # Seed BOTH databases with the full schema.
    for path in (db_a, db_b):
        seed = SovereignDB(SovereignConfig(db_path=path))
        run_migrations(seed._get_conn())
        seed.close()

    store_db = SovereignDB(SovereignConfig(db_path=db_b))
    try:
        minnid._index_durable_learning(
            "main", _DOC_PHOTO, key="learning:42", db=store_db
        )
    finally:
        store_db.close()

    conn_b = sqlite3.connect(db_b)
    n_b = conn_b.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    conn_b.close()
    conn_a = sqlite3.connect(db_a)
    n_a = conn_a.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    conn_a.close()

    assert n_b >= 1, "transient-engine write did not land in the store DB (DB-B)"
    assert n_a == 0, "store-time index leaked into the DEFAULT_CONFIG live DB (DB-A)"


def test_warm_faiss_add_makes_new_doc_immediately_searchable(temp_engine):
    """Warm-index add() path: store doc1, search (warm FAISS), store doc2, recall doc2.

    The first search warms the live FAISS (count > 0). The SECOND store must add
    its vectors to the warm index in place so doc2 is searchable IMMEDIATELY in
    the same process — without a cold rebuild. A regression that drops the warm
    add() (making new chunks invisible until the next cold reload) fails here.
    """
    op = _operator()

    def _store(content):
        staged = minnid._stage_candidate(
            {"content": content, "_principal": op, "workspace_id": "default"}, 1
        )
        cid = staged["result"]["candidate_id"]
        return minnid._resolve_candidate(
            {"candidate_id": cid, "decision": "accept", "_principal": op}, 2
        )

    # 1) Store doc1, then search — this warms the live FAISS index (count > 0).
    _store(_DOC_PHOTO)
    warm = _search("how do plants make energy from sunlight", op)
    assert warm["result"]["results"], "doc1 not searchable after first store"
    engine = minnid._lazy_retrieval()
    count_after_doc1 = engine.faiss_index.count
    assert count_after_doc1 > 0, "FAISS did not warm after first search"

    # 2) With FAISS warm, store doc2 — its chunks must be add()ed in place.
    _store(_DOC_TIDES)

    # The warm add() path must have GROWN the live index in place. If it had
    # silently no-op'd or hit the failure path (which clears the index to cold),
    # count would NOT be strictly greater here — the next search would then cold-
    # rebuild from the DB and still surface doc2, masking the warm-add failure.
    # Asserting growth distinguishes the warm-add path from the cold-rebuild
    # fallback.
    assert engine.faiss_index.count > count_after_doc1, (
        "warm-index add() did not grow the live FAISS in place "
        f"({count_after_doc1} -> {engine.faiss_index.count}) — new chunks are "
        "relying on a cold rebuild instead of the warm-add path"
    )

    # 3) doc2 must be recallable IMMEDIATELY (no restart, no cold rebuild).
    resp = _search("what causes the rise and fall of the sea", op)
    results = resp["result"]["results"]
    assert results, "doc2 not in semantic results after warm-index add"
    assert "tide" in _result_text(results[0]).lower(), (
        f"warm-index add path did not surface doc2: {_result_source(results[0])}"
    )


def test_warm_faiss_add_via_learn_force_path(temp_engine):
    """Warm-index add() via the learn force=True path (not resolve accept).

    The existing warm-add test exercises only the resolve_candidate(accept)
    promote path. The immediate-durable _handle_learn(force=True) path also
    flows through index_durable_document -> _refresh_live_faiss, and must add
    its vectors to a WARM index in place so the doc is searchable IMMEDIATELY in
    the same process. A regression that drops the warm add() on the force path
    (new chunks invisible until a cold reload) fails here.
    """
    op = _operator()

    # 1) Force-learn doc1, then search — warms the live FAISS index (count > 0).
    r1 = minnid._handle_learn(
        {"content": _DOC_PHOTO, "force": True, "_principal": op}, 1
    )
    assert r1["result"]["status"] == "ok"
    warm = _search("how do plants make energy from sunlight", op)
    assert warm["result"]["results"], "doc1 not searchable after first force-learn"
    engine = minnid._lazy_retrieval()
    count_after_doc1 = engine.faiss_index.count
    assert count_after_doc1 > 0, "FAISS did not warm after first search"

    # 2) With FAISS warm, force-learn doc2 — its chunks must be add()ed in place.
    r2 = minnid._handle_learn(
        {"content": _DOC_TIDES, "force": True, "_principal": op}, 2
    )
    assert r2["result"]["status"] == "ok"

    # The warm add() path on the force-learn route must have grown the live index
    # in place. A silent no-op or the failure path (which clears to cold) would
    # leave count unchanged and rely on a cold rebuild — asserting growth proves
    # the warm-add path ran for force=True too.
    assert engine.faiss_index.count > count_after_doc1, (
        "warm-index add() did not grow the live FAISS in place on the learn "
        f"force=True path ({count_after_doc1} -> {engine.faiss_index.count})"
    )

    # 3) doc2 must be recallable IMMEDIATELY (no restart, no cold rebuild).
    resp = _search("what causes the rise and fall of the sea", op)
    results = resp["result"]["results"]
    assert results, "doc2 not in semantic results after warm-index add (force path)"
    assert "tide" in _result_text(results[0]).lower(), (
        f"warm-index add (force path) did not surface doc2: "
        f"{_result_source(results[0])}"
    )


def test_restore_does_not_inflate_live_faiss(temp_engine):
    """Finding #1: a RE-STORE must drop old chunk_ids from the live FAISS index.

    Store a doc into a warm index, then re-store the SAME path with CHANGED
    content. The old chunks are DELETEd from chunk_embeddings and new ones
    inserted + add()ed to the live index. Before the fix the old chunk_ids were
    never removed from the in-memory index, so its active (searchable) set grew
    on every re-store even though only the current chunks are valid. After the
    fix the live index's active set reflects ONLY the current chunks.

    We drive the re-store directly through index_durable_document on a stable
    path so we control the content change (the governed ingest path keys its
    synthetic path on the content hash, so changed content would mint a new
    path rather than re-storing the same one).
    """
    op = _operator()
    engine = minnid._lazy_retrieval()
    path = "agent://main/_durable/inflate-test.md"

    # First store + a search to WARM the live index (count > 0).
    r1 = engine.index_durable_document(
        content=_DOC_PHOTO, path=path, agent="main"
    )
    assert r1["status"] == "ok" and r1["chunks"] >= 1
    _search("how do plants make energy from sunlight", op)
    assert engine.faiss_index.count > 0, "FAISS did not warm"

    # The live index's ACTIVE (searchable) set is the chunk_ids still present in
    # _reverse_map; remove() tombstones drop ids from there. count (len of the
    # raw _chunk_ids list) intentionally does not shrink (tombstone-on-rebuild),
    # so the searchable map is the honest bound on the live index.
    active_after_first = len(engine.faiss_index._reverse_map)
    assert active_after_first == r1["chunks"], (
        f"active set after first store ({active_after_first}) != chunks "
        f"stored ({r1['chunks']})"
    )

    # Re-store the SAME path with CHANGED content several times. Without the fix,
    # each re-store leaves the prior chunks' ids in the active map, so the
    # searchable set grows unboundedly. With the fix it stays bounded to the
    # current doc's chunk count.
    last_chunks = r1["chunks"]
    for i in range(3):
        changed = _DOC_TIDES + f"\n\nRevision {i} of the tides document.\n"
        ri = engine.index_durable_document(
            content=changed, path=path, agent="main"
        )
        assert ri["status"] == "ok" and ri["chunks"] >= 1
        last_chunks = ri["chunks"]

    active_after_restores = len(engine.faiss_index._reverse_map)
    # The active (searchable) set must reflect ONLY the current chunks, not the
    # accumulated chunks of every revision.
    assert active_after_restores == last_chunks, (
        "live FAISS active set inflated on re-store: searchable set is "
        f"{active_after_restores} but the current doc only has {last_chunks} "
        "chunks — old chunk_ids were not removed from the in-memory index"
    )


def test_durable_store_succeeds_when_embedding_fails(temp_engine, monkeypatch):
    """FAIL-OPEN: an embedding failure must NOT fail the durable store."""
    db_path, _ = temp_engine
    op = _operator()

    # Make the engine's embedder raise on encode, simulating a model outage.
    engine = minnid._lazy_retrieval()

    class _BoomModel:
        def encode(self, *_a, **_k):
            raise RuntimeError("simulated embedder outage")

    monkeypatch.setattr(type(engine), "model", property(lambda self: _BoomModel()))

    staged = minnid._stage_candidate(
        {"content": _DOC_PHOTO, "_principal": op, "workspace_id": "default"}, 1
    )
    cid = staged["result"]["candidate_id"]
    resolved = minnid._resolve_candidate(
        {"candidate_id": cid, "decision": "accept", "_principal": op}, 2
    )
    # The durable store STILL succeeds despite the embedding outage.
    assert resolved["result"]["new_status"] == "accepted"
    assert resolved["result"]["learning_id"]

    conn = sqlite3.connect(db_path)
    learn = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    conn.close()
    assert learn >= 1, "durable learning was lost when embedding failed (not fail-open)"
    # Degraded state: every chunk encode raised, so NO chunk_embeddings rows must
    # have survived. A partial chunk row committed before the encode exception
    # would mean the fail-open path leaked a half-indexed document.
    assert n_chunks == 0, "partial chunk write survived encode failure"


def test_learn_force_succeeds_when_embedding_fails(temp_engine, monkeypatch):
    """FAIL-OPEN on the immediate-durable learn path too."""
    db_path, _ = temp_engine
    op = _operator()
    engine = minnid._lazy_retrieval()

    class _BoomModel:
        def encode(self, *_a, **_k):
            raise RuntimeError("simulated embedder outage")

    monkeypatch.setattr(type(engine), "model", property(lambda self: _BoomModel()))

    resp = minnid._handle_learn(
        {"content": _DOC_TIDES, "force": True, "_principal": op}, 1
    )
    assert resp["result"]["status"] == "ok", "force-learn store failed when embedding failed"
    conn = sqlite3.connect(db_path)
    learn = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    conn.close()
    assert learn >= 1
    assert n_chunks == 0, "partial chunk write survived encode failure"


# A multi-chunk document: enough headings/length that the markdown chunker emits
# several chunks, so a mid-stream (partial) encode failure is observable.
_DOC_MULTICHUNK = (
    _DOC_PHOTO
    + "\n\n## Tides\n\n" + _DOC_TIDES.split("\n\n", 1)[1]
    + "\n\n## More Tides\n\n" + _DOC_TIDES.split("\n\n", 1)[1]
)


def test_partial_encode_failure_writes_no_chunks_and_store_succeeds(
    temp_engine, monkeypatch
):
    """All-or-nothing semantic index: if encode succeeds on early chunks but
    FAILS on a later chunk, NO chunk_embeddings rows are committed for the doc
    (the partial batch is dropped) — yet the durable store still succeeds.

    This exercises the partial-encode path that the all-fail tests do not: the
    first encode returns a real vector, the second raises. The fix computes all
    embeddings BEFORE opening the write transaction, so a mid-stream failure
    aborts the whole semantic batch — no half-indexed document leaks.
    """
    db_path, _ = temp_engine
    op = _operator()
    engine = minnid._lazy_retrieval()

    # Sanity: the doc really does chunk into more than one piece, otherwise the
    # 'first succeeds, second fails' scenario is vacuous.
    assert len(engine.chunker.chunk_document(_DOC_MULTICHUNK)) >= 2, (
        "test precondition: _DOC_MULTICHUNK must produce multiple chunks"
    )

    import numpy as np

    class _PartialBoomModel:
        """Succeeds on the first encode call, raises on every later call."""

        def __init__(self):
            self.calls = 0

        def encode(self, *_a, **_k):
            self.calls += 1
            if self.calls == 1:
                return np.zeros(384, dtype=np.float32)
            raise RuntimeError("simulated mid-batch embedder outage")

    boom = _PartialBoomModel()
    monkeypatch.setattr(type(engine), "model", property(lambda self: boom))

    staged = minnid._stage_candidate(
        {"content": _DOC_MULTICHUNK, "_principal": op, "workspace_id": "default"}, 1
    )
    cid = staged["result"]["candidate_id"]
    resolved = minnid._resolve_candidate(
        {"candidate_id": cid, "decision": "accept", "_principal": op}, 2
    )
    assert resolved["result"]["new_status"] == "accepted", (
        "durable store failed on a partial encode failure (not fail-open)"
    )

    assert boom.calls >= 2, "test did not exercise a second (failing) encode"

    conn = sqlite3.connect(db_path)
    learn = conn.execute("SELECT COUNT(*) FROM learnings").fetchone()[0]
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    conn.close()
    assert learn >= 1, "durable learning lost on partial encode failure"
    assert n_chunks == 0, (
        "partial encode failure committed a half-indexed document "
        f"({n_chunks} chunk rows) — all-or-nothing invariant breached"
    )


# A SHORT durable memory — the natural shape of a real learning ("the lock code
# is X") — well below the markdown chunker's 64-token min floor. Before the
# short-content fix, chunk_document() dropped it entirely, so the doc landed
# with ZERO chunk_embeddings rows and was invisible to the semantic stream: the
# exact membench Layer-2 failure where every gold session (~40 tokens) was
# unretrievable while long distractors filled the top-k.
_DOC_SHORT = (
    "Planning note (winter cycle). The Sentinel invoice lock code is violet "
    "prism. Confirm with operations before the quarterly close."
)


def test_short_durable_learning_is_semantically_recallable(temp_engine):
    """A sub-min-token durable learning must still be embedded (single chunk)."""
    db_path, _ = temp_engine
    op = _operator()

    staged = minnid._stage_candidate(
        {"content": _DOC_SHORT, "_principal": op, "workspace_id": "default"}, 1
    )
    cid = staged["result"]["candidate_id"]
    resolved = minnid._resolve_candidate(
        {"candidate_id": cid, "decision": "accept", "_principal": op}, 2
    )
    assert resolved["result"]["new_status"] == "accepted"

    # The short content must produce at least one embedded chunk — the min-token
    # floor applies to intra-document fragments, not to a whole short memory.
    conn = sqlite3.connect(db_path)
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    conn.close()
    assert n_chunks >= 1, (
        "short durable content produced no embedded chunks — sub-64-token "
        "memories are semantically invisible"
    )

    # And it must actually surface in the SEMANTIC results stream.
    resp = _search("what is the Sentinel invoice lock code", op)
    results = resp["result"]["results"]
    assert results, "semantic results stream empty for a short stored memory"
    assert "violet prism" in _result_text(results[0]).lower(), (
        f"expected the short durable doc on top, got: {_result_source(results[0])}"
    )

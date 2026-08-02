"""R3: the AFM loop's own vault must be indexed, and its drafts must expire.

Two independent dark-memory defects, both measured against the live install
before the fix:

  * ~/.minni/vault held 1,213 draft pages under wiki/ and the shared DB held a
    row for none of them. The daemon's only scheduled indexer is the vault-watch
    sweep, and that sweep covered per-agent ``*-vault`` dirs only.
  * Of those 1,213 drafts, 0 had ever transitioned to ``status: expired`` --
    the oldest dated 2026-06-08, months past a 14-day TTL -- because
    _expire_stale_drafts' regex only matched an UNQUOTED expires_at and the
    writer emits the value through yaml.safe_dump, which quotes it.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))


class _FakeEmbedder:
    def encode(self, text: str):
        vec = np.zeros(384, dtype=np.float32)
        vec[sum(text.encode("utf-8")) % 384] = 1.0
        return vec


def _install_fake_embedder(monkeypatch):
    import minni.models as models

    monkeypatch.setattr(models, "get_embedder", lambda: _FakeEmbedder())


def _make_cfg(tmp_path):
    from minni.config import SovereignConfig

    return SovereignConfig(
        db_path=str(tmp_path / "shared" / "minni.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "shared.faiss"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )


def _draft(page_id: str = "page-abc123", title: str = "R3 coverage probe") -> dict:
    return {
        "title": title,
        "body": "A body long enough to chunk. " * 20,
        "page_id": page_id,
        "trace_id": "trace-r3",
        "kind": "concept",
        "sources": ["`probe`"],
    }


# ── Coverage: the shared vault is indexed at all ───────────────────────────


def _indexed_paths(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [row[0] for row in conn.execute("SELECT path FROM documents")]
    finally:
        conn.close()


def test_shared_vault_pages_land_in_the_shared_db(tmp_path, monkeypatch):
    """Files on disk in the configured vault == rows indexed for that vault."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)

    written = [
        _write_one(vault, _draft(page_id=f"page-{i:06d}", title=f"R3 probe {i}"))
        for i in range(3)
    ]
    on_disk = {str((vault / w["path"]).resolve()) for w in written}
    assert len(on_disk) == 3

    stats = index_shared_vault(cfg)

    assert stats[str(cfg.vault_path)]["status"] == "success"
    assert on_disk.issubset(set(_indexed_paths(cfg.db_path)))


def test_vault_watch_sweep_covers_the_shared_vault(tmp_path, monkeypatch):
    """The daemon's scheduled sweep -- not just the manual CLI -- must reach it.

    This is the defect proper: index_shared_vault existing is no use if the only
    thing that runs on a schedule still sweeps per-agent vaults alone.
    """
    import minni.minnid as minnid
    from minni.afm_writer import _write_one

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)

    vault = Path(cfg.vault_path)
    written = _write_one(vault, _draft())
    expected = str((vault / written["path"]).resolve())

    stats = minnid._vault_watch_sweep_once()

    assert str(cfg.vault_path) in stats, (
        "vault-watch sweep reported no result for the shared vault: "
        f"{sorted(stats)}"
    )
    assert expected in _indexed_paths(cfg.db_path)


def test_discover_agent_vaults_still_excludes_the_bare_vault(tmp_path):
    """The exclusion is deliberate and must survive the coverage fix.

    vault_ingest keys a per-agent sidecar store off the ``<slug>-vault`` name;
    the bare vault has no slug and is not per-agent. Coverage for it comes from
    the shared indexer, not from widening this glob.
    """
    from minni.index_all import discover_agent_vaults

    minni_home = tmp_path / ".minni"
    (minni_home / "codex-vault").mkdir(parents=True)
    (minni_home / "vault").mkdir()

    assert [p.name for p in discover_agent_vaults(minni_home)] == ["codex-vault"]


def test_vault_ingest_refuses_the_bare_vault_by_slug(tmp_path):
    """Second, independent reason the exclusion cannot simply be deleted."""
    from minni.afm_passes.vault_ingest import run as run_vault_ingest

    bare = tmp_path / ".minni" / "vault"
    (bare / "wiki").mkdir(parents=True)

    result = run_vault_ingest(None, _make_cfg(tmp_path), vault_path=str(bare), dry_run=True)

    assert result["status"] == "skipped"
    assert result["reason"] == "unknown_vault_slug"


# ── Prune scoping: making the sweep scheduled must not delete other sources ─


def _seed_row(db_path: str, path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO documents (path, agent, last_modified, indexed_at) VALUES (?,?,?,?)",
            (path, "test", time.time(), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def test_prune_spares_rows_outside_the_vault_and_virtual_durable_rows(tmp_path, monkeypatch):
    """VaultIndexer's prune reads the WHOLE documents table but only ever walks
    one vault, so an unscoped prune deletes rows it does not own. Harmless while
    index_vault only ran by hand inside index_all (the wiki indexer re-added its
    rows immediately after); permanent once the daemon runs it on an interval.

    ``vault/_durable/*.md`` rows are exempt for a second reason: the daemon
    indexes promoted learnings at synthetic paths and never writes the file, so
    absence from disk is their steady state, not staleness.
    """
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)

    _write_one(vault, _draft())
    index_shared_vault(cfg)  # first sweep also creates the schema

    # Rows this indexer does not own: another source's page, and a durable
    # learning the daemon indexed at a synthetic path with no file behind it.
    foreign = str(tmp_path / "wiki" / "some-other-source.md")
    durable = str(vault / "_durable" / "agent__deadbeef.md")
    _seed_row(cfg.db_path, foreign)
    _seed_row(cfg.db_path, durable)

    index_shared_vault(cfg)

    survivors = _indexed_paths(cfg.db_path)
    assert foreign in survivors, "prune deleted a row belonging to another source"
    assert durable in survivors, "prune deleted a virtual durable-learning row"


def test_prune_still_removes_stale_rows_it_does_own(tmp_path, monkeypatch):
    """The scoping must not turn the prune into a no-op."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)

    written = _write_one(vault, _draft())
    index_shared_vault(cfg)
    page = vault / written["path"]
    assert str(page.resolve()) in _indexed_paths(cfg.db_path)

    page.unlink()
    index_shared_vault(cfg)

    assert str(page.resolve()) not in _indexed_paths(cfg.db_path)


# ── Expiry: drafts past TTL actually transition ────────────────────────────


def test_writer_reader_round_trip_expires_a_draft_past_ttl(tmp_path):
    """The regression: a page THIS writer produced must be readable by the
    expiry reader. The frontmatter goes through yaml.safe_dump, which quotes
    timestamps, and the old pattern required them unquoted -- so the writer and
    the reader disagreed about their own format and nothing ever expired."""
    from minni.afm_writer import DRAFT_TTL_SECONDS, _expire_stale_drafts, _write_one

    vault = tmp_path / "vault"
    now = time.time()
    written = _write_one(vault, _draft(), now=now - DRAFT_TTL_SECONDS - 86400)
    page = vault / written["path"]

    assert "status: draft" in page.read_text(encoding="utf-8")
    assert _expire_stale_drafts(vault, now=now) == 1
    assert "status: expired" in page.read_text(encoding="utf-8")


def test_fresh_draft_within_ttl_is_left_alone(tmp_path):
    from minni.afm_writer import _expire_stale_drafts, _write_one

    vault = tmp_path / "vault"
    now = time.time()
    written = _write_one(vault, _draft(), now=now)

    assert _expire_stale_drafts(vault, now=now) == 0
    assert "status: draft" in (vault / written["path"]).read_text(encoding="utf-8")


def test_expires_at_is_compared_as_utc_not_local_time(tmp_path, monkeypatch):
    """time.mktime reads a struct_time as LOCAL time. The stamped value is UTC
    (trailing Z), so on any machine east of Greenwich mktime resolved it to an
    instant that many hours EARLIER, expiring drafts before their TTL."""
    from minni.afm_writer import DRAFT_TTL_SECONDS, _expire_stale_drafts, _write_one

    monkeypatch.setenv("TZ", "Europe/Stockholm")  # UTC+1/+2, so mktime skews early
    time.tzset()
    try:
        vault = tmp_path / "vault"
        now = time.time()
        # Expires one hour from now. Under mktime the +1/+2h offset drags that
        # inside the past and the draft is wrongly expired.
        written = _write_one(vault, _draft(), now=now - DRAFT_TTL_SECONDS + 3600)

        assert _expire_stale_drafts(vault, now=now) == 0
        assert "status: draft" in (vault / written["path"]).read_text(encoding="utf-8")
    finally:
        monkeypatch.undo()
        time.tzset()


def test_expiry_ignores_expires_at_written_in_the_body(tmp_path):
    """Only the page's own frontmatter decides its fate; prose quoting the field
    name must not be able to expire a draft that is not due."""
    from minni.afm_writer import _expire_stale_drafts, _write_one

    vault = tmp_path / "vault"
    now = time.time()
    draft = _draft()
    draft["body"] = "Quoting an old page here:\nexpires_at: '2020-01-01T00:00:00Z'\n"
    written = _write_one(vault, draft, now=now)

    assert _expire_stale_drafts(vault, now=now) == 0
    assert "status: draft" in (vault / written["path"]).read_text(encoding="utf-8")


# ── Round 2: findings from the Grok review of #256 ─────────────────────────


def test_unendorsed_pages_are_indexed_but_not_embedded(tmp_path, monkeypatch):
    """Grok #2. retrieve() drops draft/expired only AFTER FAISS has filled a
    fixed limit*5 candidate window, so embedding a vault that is ~95% drafts
    lets them evict accepted pages and shrink recall. Drafts must stay indexed
    and lexically findable, but must not consume vector slots."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)

    draft_page = vault / _write_one(vault, _draft(page_id="page-draft0"))["path"]
    accepted = vault / "wiki" / "concepts" / "accepted.md"
    accepted.parent.mkdir(parents=True, exist_ok=True)
    accepted.write_text(
        "---\ntitle: Accepted\nstatus: accepted\nagent: afm-loop\nprivacy: safe\n---\n\n"
        + "accepted body text " * 40,
        encoding="utf-8",
    )

    index_shared_vault(cfg)

    conn = sqlite3.connect(cfg.db_path)
    try:
        def chunks_for(p):
            return conn.execute(
                "SELECT COUNT(*) FROM chunk_embeddings ce JOIN documents d"
                " ON d.doc_id = ce.doc_id WHERE d.path = ?",
                (str(Path(p).resolve()),),
            ).fetchone()[0]

        indexed = _indexed_paths(cfg.db_path)
        # Indexed (coverage gate still met) ...
        assert str(draft_page.resolve()) in indexed
        assert str(accepted.resolve()) in indexed
        # ... but only the accepted page consumes vector slots.
        assert chunks_for(draft_page) == 0
        assert chunks_for(accepted) > 0
    finally:
        conn.close()


def test_endorsing_a_draft_makes_it_embeddable_on_the_next_sweep(tmp_path, monkeypatch):
    """The skip must be self-healing, not a permanent exclusion."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    page = vault / _write_one(vault, _draft())["path"]
    index_shared_vault(cfg)

    conn = sqlite3.connect(cfg.db_path)
    try:
        def chunks():
            return conn.execute(
                "SELECT COUNT(*) FROM chunk_embeddings ce JOIN documents d"
                " ON d.doc_id = ce.doc_id WHERE d.path = ?",
                (str(page.resolve()),),
            ).fetchone()[0]

        assert chunks() == 0
        page.write_text(
            page.read_text(encoding="utf-8").replace("status: draft", "status: accepted", 1),
            encoding="utf-8",
        )
        os.utime(page, (time.time() + 10, time.time() + 10))
        index_shared_vault(cfg)
        assert chunks() > 0
    finally:
        conn.close()


def test_sweep_invalidates_the_live_engine_faiss(tmp_path, monkeypatch):
    """Grok #1. index_shared_vault writes through its own throwaway DB+indexer,
    so its FAISS rebuild lands on a throwaway instance. A warm daemon engine
    short-circuits _ensure_faiss_loaded while count > 0 and would keep serving
    a stale index until restart."""
    import minni.minnid as minnid
    from minni.afm_writer import _write_one

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)

    class _WarmEngine:
        def __init__(self):
            self.faiss_index = self

        count = 5
        invalidated = False

        def invalidate(self):
            type(self).invalidated = True

    engine = _WarmEngine()
    monkeypatch.setattr(minnid, "_retrieval", engine)

    _write_one(Path(cfg.vault_path), _draft())
    minnid._vault_watch_sweep_once()

    assert _WarmEngine.invalidated, "live engine FAISS was never invalidated"


def test_sweep_does_not_construct_a_retrieval_engine_when_cold(tmp_path, monkeypatch):
    """Invalidation must not drag model loading onto the sweep thread."""
    import minni.minnid as minnid
    from minni.afm_writer import _write_one

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)
    monkeypatch.setattr(minnid, "_retrieval", None)

    def _boom():
        raise AssertionError("_lazy_retrieval() must not be called by the sweep")

    monkeypatch.setattr(minnid, "_lazy_retrieval", _boom)

    _write_one(Path(cfg.vault_path), _draft())
    minnid._vault_watch_sweep_once()  # must not raise


def test_sweep_expires_drafts_on_a_schedule(tmp_path, monkeypatch):
    """Grok #3. Expiry otherwise runs only inside _write_batch, which the AFM
    loop invokes only when a pass produced drafts — so a healthy but quiet loop
    never expires anything. The clock, not new work, must drive it."""
    import minni.minnid as minnid
    from minni.afm_writer import DRAFT_TTL_SECONDS, _write_one

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)

    vault = Path(cfg.vault_path)
    page = vault / _write_one(
        vault, _draft(), now=time.time() - DRAFT_TTL_SECONDS - 86400
    )["path"]
    assert "status: draft" in page.read_text(encoding="utf-8")

    minnid._vault_watch_sweep_once()

    assert "status: expired" in page.read_text(encoding="utf-8")


def test_body_prose_cannot_drag_a_page_into_the_expiry_path(tmp_path):
    """Grok #4. The expires_at read was frontmatter-scoped but the status/agent
    entry gate was not, so a non-AFM page whose BODY quoted both lines entered
    the expiry path — and the rewrite then edited that body text."""
    from minni.afm_writer import _expire_stale_drafts

    vault = tmp_path / "vault"
    page = vault / "wiki" / "notes" / "quoting.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    # An ACCEPTED, human-authored page. Its own expires_at is long past, so the
    # only thing standing between it and a rewrite is the status/agent gate --
    # which the body satisfies by quoting the AFM format. Under the whole-file
    # gate this page entered the expiry path and the substitution landed on the
    # quoted body line, silently corrupting the documentation it contains.
    original = (
        "---\n"
        "title: Notes on the AFM frontmatter format\n"
        "status: accepted\n"
        "agent: human\n"
        "expires_at: '2020-01-01T00:00:00Z'\n"
        "---\n\n"
        "AFM drafts look like this:\n\n"
        "status: draft\n"
        "agent: afm-loop\n"
    )
    page.write_text(original, encoding="utf-8")

    assert _expire_stale_drafts(vault, now=time.time()) == 0
    assert page.read_text(encoding="utf-8") == original


def test_expiry_rewrite_lands_in_frontmatter_not_body(tmp_path):
    """A real expiring draft whose body also contains the status line: only the
    frontmatter occurrence may be rewritten."""
    from minni.afm_writer import DRAFT_TTL_SECONDS, _expire_stale_drafts, _write_one

    vault = tmp_path / "vault"
    now = time.time()
    draft = _draft()
    draft["body"] = "Some prose that mentions status: draft in passing.\n"
    page = vault / _write_one(vault, draft, now=now - DRAFT_TTL_SECONDS - 86400)["path"]

    assert _expire_stale_drafts(vault, now=now) == 1
    text = page.read_text(encoding="utf-8")
    from minni.afm_writer import _extract_frontmatter

    assert "status: expired" in _extract_frontmatter(text)
    assert "mentions status: draft in passing" in text


# ── Round 3: findings from the second Grok review of #256 ──────────────────


def _write_page(path: Path, status: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {path.stem}\nstatus: {status}\nagent: afm-loop\nprivacy: safe\n---\n\n{body}\n",
        encoding="utf-8",
    )


def _engine(cfg):
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    return RetrievalEngine(SovereignDB(cfg), cfg)


def test_drafts_cannot_crowd_the_fts_window(tmp_path, monkeypatch):
    """Grok round 2 #1 (High). Un-darkening the vault put ~1,214 drafts into
    vault_fts. _fts_search takes a fixed LIMIT and retrieve() dropped
    draft/expired only afterwards, so a query the drafts match fills the window
    with pages nobody asked for and the accepted answer never enters the merge.
    A post-filter cannot recover rows that were never fetched."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)

    # Many drafts that all match the query term, and one accepted page that
    # also matches. Without SQL-side exclusion the drafts own the window.
    for i in range(60):
        _write_page(vault / "wiki" / "concepts" / f"draft{i}.md", "draft",
                    "quantum widget calibration " * 12)
    _write_page(vault / "wiki" / "concepts" / "the-answer.md", "accepted",
                "quantum widget calibration procedure of record " * 12)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        rows = eng._fts_search("quantum widget calibration", 20,
                               exclude_statuses=["draft", "expired"])
        statuses = {(r.get("page_status") or "candidate") for r in rows}
        assert "draft" not in statuses and "expired" not in statuses
        assert any(r["path"].endswith("the-answer.md") for r in rows), (
            "the accepted page was crowded out of the FTS window"
        )
    finally:
        eng.db.close()


def test_accepted_page_survives_a_draft_heavy_corpus_with_semantic_down(tmp_path, monkeypatch):
    """The failure the reviewer called out explicitly: with the embedder
    unavailable the semantic leg returns nothing, so if FTS is all drafts the
    post-filter empties the result and recall reads as broken."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    for i in range(60):
        _write_page(vault / "wiki" / "concepts" / f"d{i}.md", "draft",
                    "sovereign ledger reconciliation " * 12)
    _write_page(vault / "wiki" / "concepts" / "keeper.md", "accepted",
                "sovereign ledger reconciliation runbook " * 12)
    index_shared_vault(cfg)

    import minni.models as models

    monkeypatch.setattr(models, "get_embedder", lambda: None)  # semantic leg down
    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        results = eng.retrieve("sovereign ledger reconciliation", limit=5)
        names = [r.get("filename", "") for r in results]
        assert results, "recall was empty: drafts filled the window and were then dropped"
        assert any(n.endswith("keeper.md") for n in names), names
        assert not any(r.get("review_state") in {"draft", "expired"} for r in results)
    finally:
        eng.db.close()


def test_excluded_statuses_do_not_consume_the_final_limit(tmp_path, monkeypatch):
    """Grok round 2 #2. The filter ran after merge+rerank+truncate, so excluded
    pages could take final slots and then vanish, returning fewer than `limit`."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    for i in range(40):
        _write_page(vault / "wiki" / "concepts" / f"x{i}.md", "draft",
                    "harmonic drift compensation " * 12)
    for i in range(4):
        _write_page(vault / "wiki" / "concepts" / f"ok{i}.md", "accepted",
                    "harmonic drift compensation " * 12)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        results = eng.retrieve("harmonic drift compensation", limit=4)
        assert len(results) == 4, f"limit not filled with usable rows: {len(results)}"
        assert all(r.get("review_state") not in {"draft", "expired"} for r in results)
    finally:
        eng.db.close()


def test_include_drafts_still_reaches_drafts(tmp_path, monkeypatch):
    """SQL-side exclusion must be opt-out, not a permanent ban."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    _write_page(vault / "wiki" / "concepts" / "only-draft.md", "draft",
                "singular findable phrase herein " * 12)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        assert eng.retrieve("singular findable phrase herein", limit=5) == []
        opened = eng.retrieve("singular findable phrase herein", limit=5, include_drafts=True)
        assert any(r.get("filename", "").endswith("only-draft.md") for r in opened), opened
    finally:
        eng.db.close()


def test_prune_keeps_a_row_stored_under_a_non_canonical_path(tmp_path, monkeypatch):
    """Grok round 2 #3. disk_files is keyed on resolved paths; the prune tested
    the raw string only, so a row written under a symlinked or unnormalized
    spelling of a file that EXISTS would be deleted once the sweep runs on a
    timer."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    written = _write_one(vault, _draft())
    index_shared_vault(cfg)

    page = (vault / written["path"]).resolve()
    # Same file, non-canonical spelling. pathlib collapses a "." segment on its
    # own, so use ".." — which it preserves and only resolve() normalizes.
    noncanonical = str(page.parent / ".." / page.parent.name / page.name)
    assert noncanonical != str(page) and Path(noncanonical).resolve() == page
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("UPDATE documents SET path = ? WHERE path = ?", (noncanonical, str(page)))
        conn.commit()
    finally:
        conn.close()

    index_shared_vault(cfg)

    # The row must SURVIVE -- the file it names exists. It is also canonicalized
    # on the way through (round 4 #3), so assert the file is still represented
    # exactly once rather than asserting the non-canonical spelling persists.
    survivors = [p for p in _indexed_paths(cfg.db_path) if p.endswith(page.name)]
    assert survivors == [str(page)], (
        "prune deleted a row whose file exists, because the stored path was "
        f"not in canonical form: {survivors}"
    )


# ── Round 4: findings from the third Grok review of #256 ───────────────────


def test_indexing_does_not_hold_the_write_lock_across_the_walk(tmp_path, monkeypatch):
    """Grok round 3 #1 (High). Phase 1 used to run inside a single
    BEGIN IMMEDIATE, so the reserved lock was held for the whole sweep --
    ~36s measured on this vault against db.py's 30s busy timeout. A concurrent
    writer would get "database is locked". The lock must be released between
    files, and model.encode must not run under it."""
    import sqlite3 as sq

    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    for i in range(8):
        _write_page(vault / "wiki" / "concepts" / f"p{i}.md", "accepted", f"body {i} " * 40)

    index_shared_vault(cfg)  # create schema

    # A second connection tries to write DURING indexing, with a short timeout
    # so it fails fast if the indexer is holding the lock across the walk.
    other = sq.connect(cfg.db_path, timeout=0.5)
    blocked = []

    from minni.chunker import MarkdownChunker

    real_chunk = MarkdownChunker.chunk_document
    state = {"tried": False}

    def chunk_and_probe(self, content):
        # Runs mid-walk. If phase 1 still wrapped everything in one
        # transaction, this write would block and raise.
        if not state["tried"]:
            state["tried"] = True
            try:
                other.execute("CREATE TABLE IF NOT EXISTS r3_lock_probe (x INTEGER)")
                other.execute("INSERT INTO r3_lock_probe (x) VALUES (1)")
                other.commit()
            except Exception as exc:  # pragma: no cover - failure path
                blocked.append(exc)
        return real_chunk(self, content)

    monkeypatch.setattr(MarkdownChunker, "chunk_document", chunk_and_probe)
    for p in vault.rglob("*.md"):
        os.utime(p, (time.time() + 5, time.time() + 5))  # force reindex

    index_shared_vault(cfg)
    other.close()

    assert state["tried"], "probe never ran; test did not exercise the walk"
    assert not blocked, f"concurrent writer was locked out during indexing: {blocked}"


def test_expiry_does_not_clobber_a_concurrent_endorsement(tmp_path, monkeypatch):
    """Grok round 3 #2. Expiry read the page, decided, then wrote -- with no
    lock and no re-read. Now that it runs on the vault-watch thread alongside
    RPC endorsement, an accept landing in that window was overwritten with
    `status: expired` and lost."""
    from minni.afm_writer import (
        DRAFT_TTL_SECONDS,
        _expire_stale_drafts,
        _extract_frontmatter,
        _write_one,
        endorse_draft,
    )

    vault = tmp_path / "vault"
    now = time.time()
    draft = _draft(page_id="page-racey")
    page = vault / _write_one(vault, draft, now=now - DRAFT_TTL_SECONDS - 86400)["path"]

    # Endorse in the window between expiry's read and its write.
    import minni.afm_writer as writer

    real_extract = writer._extract_frontmatter
    fired = {"done": False}

    def extract_then_endorse(text):
        out = real_extract(text)
        if not fired["done"] and "status: draft" in out:
            fired["done"] = True
            endorse_draft(str(vault), "page-racey", "accept")
        return out

    monkeypatch.setattr(writer, "_extract_frontmatter", extract_then_endorse)

    _expire_stale_drafts(vault, now=now)

    fm = _extract_frontmatter(page.read_text(encoding="utf-8"))
    assert "status: accepted" in fm, f"endorsement was clobbered by expiry:\n{fm}"
    assert "status: expired" not in fm


def test_reindex_adopts_a_non_canonical_row_instead_of_duplicating(tmp_path, monkeypatch):
    """Grok round 3 #3. Keeping non-canonical rows in the prune was only half
    the fix: phase 1 looked up by resolved path only, so it INSERTed a second
    row for the same file and the prune then kept both."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    written = _write_one(vault, _draft())
    index_shared_vault(cfg)

    page = (vault / written["path"]).resolve()
    noncanonical = str(page.parent / ".." / page.parent.name / page.name)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("UPDATE documents SET path = ? WHERE path = ?", (noncanonical, str(page)))
        conn.commit()
    finally:
        conn.close()

    os.utime(page, (time.time() + 10, time.time() + 10))  # force reindex
    index_shared_vault(cfg)

    rows = [p for p in _indexed_paths(cfg.db_path) if p.endswith(page.name)]
    assert len(rows) == 1, f"duplicate rows for one file: {rows}"
    assert rows[0] == str(page), "row was not normalized to the canonical path"


# ── Round 5: findings from the fourth Grok review of #256 ──────────────────


def test_unchanged_draft_with_stale_embeddings_is_stripped(tmp_path, monkeypatch):
    """Grok round 4 #1. The no-embed invariant was enforced only on the reindex
    path. An install that ever hand-ran the old index_all has draft rows WITH
    chunks and a current mtime, so the sweep skips them and those vectors keep
    occupying the FAISS window for the rest of the TTL. Nothing on disk will
    change to trigger a reindex, so the policy must be enforced from the row."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    page = vault / "wiki" / "concepts" / "legacy-draft.md"
    _write_page(page, "accepted", "legacy embedded body " * 40)
    index_shared_vault(cfg)  # embeds it, as the old indexer did for drafts

    def chunks():
        conn = sqlite3.connect(cfg.db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM chunk_embeddings ce JOIN documents d"
                " ON d.doc_id = ce.doc_id WHERE d.path = ?",
                (str(page.resolve()),),
            ).fetchone()[0]
        finally:
            conn.close()

    assert chunks() > 0

    # Flip the ROW to draft without touching the file, reproducing the legacy
    # state: unendorsed page, embeddings present, mtime unchanged.
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            "UPDATE documents SET page_status = 'draft' WHERE path = ?",
            (str(page.resolve()),),
        )
        conn.commit()
    finally:
        conn.close()

    stats = index_shared_vault(cfg)[str(cfg.vault_path)]

    assert stats["skipped"] >= 1, "file should still be mtime-skipped"
    assert chunks() == 0, "stale draft vectors survived the sweep"
    assert stats["chunks_purged"] > 0
    # The page itself stays indexed and lexically findable.
    assert str(page.resolve()) in _indexed_paths(cfg.db_path)


def test_endorsed_page_keeps_its_embeddings_on_skip(tmp_path, monkeypatch):
    """The purge must key on lifecycle state, not fire on every skip."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    page = vault / "wiki" / "concepts" / "kept.md"
    _write_page(page, "accepted", "durable body " * 40)
    index_shared_vault(cfg)

    stats = index_shared_vault(cfg)[str(cfg.vault_path)]

    conn = sqlite3.connect(cfg.db_path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM chunk_embeddings ce JOIN documents d"
            " ON d.doc_id = ce.doc_id WHERE d.path = ?",
            (str(page.resolve()),),
        ).fetchone()[0]
    finally:
        conn.close()
    assert n > 0, "an accepted page lost its embeddings on a no-op sweep"
    assert stats["chunks_purged"] == 0


def test_expiry_respects_a_ttl_extension_made_under_the_race(tmp_path, monkeypatch):
    """Grok round 4 #2. Status was re-validated under the lock but expires_at
    was not, so a concurrent re-stamp that pushed the TTL out -- leaving the
    page a draft -- was expired anyway on the strength of the stale read."""
    from minni.afm_writer import (
        DRAFT_TTL_SECONDS,
        _expire_stale_drafts,
        _extract_frontmatter,
        _write_one,
    )

    vault = tmp_path / "vault"
    now = time.time()
    page = vault / _write_one(
        vault, _draft(page_id="page-ttl"), now=now - DRAFT_TTL_SECONDS - 86400
    )["path"]

    import minni.afm_writer as writer

    real_extract = writer._extract_frontmatter
    fired = {"done": False}
    future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 86400))

    def extract_then_extend(text):
        out = real_extract(text)
        if not fired["done"] and "status: draft" in out:
            fired["done"] = True
            current = page.read_text(encoding="utf-8")
            page.write_text(
                re.sub(r"expires_at: '[^']+'", f"expires_at: '{future}'", current, count=1),
                encoding="utf-8",
            )
        return out

    monkeypatch.setattr(writer, "_extract_frontmatter", extract_then_extend)

    _expire_stale_drafts(vault, now=now)

    fm = _extract_frontmatter(page.read_text(encoding="utf-8"))
    assert "status: draft" in fm, f"TTL extension was ignored and the page expired:\n{fm}"
    assert "status: expired" not in fm


def test_skip_path_canonicalizes_a_non_canonical_row(tmp_path, monkeypatch):
    """Grok round 4 #3. Normalizing only on the reindex path leaves an
    up-to-date row non-canonical forever."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    written = _write_one(vault, _draft())
    index_shared_vault(cfg)

    page = (vault / written["path"]).resolve()
    noncanonical = str(page.parent / ".." / page.parent.name / page.name)
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("UPDATE documents SET path = ? WHERE path = ?", (noncanonical, str(page)))
        conn.commit()
    finally:
        conn.close()

    # No mtime bump: this is the SKIP path, not the reindex path.
    index_shared_vault(cfg)

    rows = [p for p in _indexed_paths(cfg.db_path) if p.endswith(page.name)]
    assert rows == [str(page)], f"row not canonicalized on the skip path: {rows}"


# ── Round 6: findings from the fifth Grok review of #256 ───────────────────


def test_purge_only_sweep_invalidates_the_live_faiss(tmp_path, monkeypatch):
    """Grok round 5 #1. _enforce_embed_policy DELETEs chunk rows on the
    mtime-skip path, where indexed and pruned are both 0. The invalidation gate
    only looked at those two, so a purge-only sweep left the warm engine's FAISS
    serving chunk_ids whose rows are gone -- ghost hits that punch holes in the
    fixed candidate window until the process restarts."""
    import minni.minnid as minnid
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)
    vault = Path(cfg.vault_path)
    page = vault / "wiki" / "concepts" / "legacy.md"
    _write_page(page, "accepted", "legacy embedded body " * 40)
    index_shared_vault(cfg)

    # Legacy state: unendorsed row, embeddings present, mtime unchanged.
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            "UPDATE documents SET page_status='draft' WHERE path = ?",
            (str(page.resolve()),),
        )
        conn.commit()
    finally:
        conn.close()

    class _WarmEngine:
        def __init__(self):
            self.faiss_index = self

        count = 5
        invalidated = False

        def invalidate(self):
            type(self).invalidated = True

    monkeypatch.setattr(minnid, "_retrieval", _WarmEngine())

    stats = minnid._vault_watch_sweep_once()[str(cfg.vault_path)]

    assert stats["chunks_purged"] > 0, "test did not reach the purge path"
    assert stats["indexed"] == 0 and stats["pruned"] == 0, (
        "purge must be the ONLY change, or the test proves nothing"
    )
    assert _WarmEngine.invalidated, "purge-only sweep left the live FAISS stale"


def test_duplicate_rows_for_one_file_are_collapsed(tmp_path, monkeypatch):
    """Grok round 5 #2. documents.path is UNIQUE per string, not per resolved
    file, so an earlier indexer could leave BOTH spellings as separate rows.
    Adoption never reaches that state (the exact match wins) and the prune keeps
    both, since both resolve to a file that exists."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    written = _write_one(vault, _draft())
    index_shared_vault(cfg)

    page = (vault / written["path"]).resolve()
    noncanonical = str(page.parent / ".." / page.parent.name / page.name)
    # Seed the historical state: canonical row PLUS a non-canonical twin.
    _seed_row(cfg.db_path, noncanonical)
    assert len([p for p in _indexed_paths(cfg.db_path) if p.endswith(page.name)]) == 2

    index_shared_vault(cfg)

    rows = [p for p in _indexed_paths(cfg.db_path) if p.endswith(page.name)]
    assert rows == [str(page)], f"duplicate rows for one file survived: {rows}"


def test_chronological_can_reach_unembedded_pages(tmp_path, monkeypatch):
    """Grok round 5 #3. _chronological_search inner-joined chunk_embeddings, so
    the no-embed policy made drafts unreachable by chronological recall even
    with include_drafts=True. Lifecycle, not the presence of a vector, decides."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    _write_page(vault / "wiki" / "concepts" / "only-draft.md", "draft",
                "chronologically findable marker phrase " * 12)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        hidden = eng.retrieve(
            "chronologically findable marker phrase", limit=5, sort="chronological"
        )
        assert hidden == [], "default recall must still hide drafts"

        shown = eng.retrieve(
            "chronologically findable marker phrase", limit=5,
            sort="chronological", include_drafts=True,
        )
        assert any(r.get("filename", "").endswith("only-draft.md") for r in shown), (
            f"include_drafts=True still could not reach an unembedded page: {shown}"
        )
    finally:
        eng.db.close()


# ── Round 7: finding from the sixth Grok review of #256 ────────────────────


def test_drafts_cannot_crowd_the_chronological_window(tmp_path, monkeypatch):
    """Grok round 6. The twin of test_drafts_cannot_crowd_the_fts_window, for
    the sort this PR changed. Round 5 widened _chronological_search's join to a
    LEFT JOIN so include_drafts=True could reach unembedded pages -- which also
    let drafts into a window they previously could not enter at all, with the
    lifecycle filter still running after the SQL LIMIT.

    This path is the worst case for that bug: it orders by age ascending over a
    vault whose oldest pages ARE the expired backlog, so an unfiltered window is
    drafts almost by construction and the post-filter returns empty.
    """
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)

    for i in range(60):
        _write_page(vault / "wiki" / "concepts" / f"old{i}.md", "draft",
                    "quantum widget calibration " * 12)
    _write_page(vault / "wiki" / "concepts" / "the-answer.md", "accepted",
                "quantum widget calibration procedure of record " * 12)
    index_shared_vault(cfg)

    # Make the drafts the OLDEST rows, so ascending order puts them first.
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            "UPDATE documents SET indexed_at = 1000 WHERE page_status = 'draft'"
        )
        conn.execute(
            "UPDATE documents SET indexed_at = 9999999999 WHERE page_status = 'accepted'"
        )
        conn.commit()
    finally:
        conn.close()

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        results = eng.retrieve(
            "quantum widget calibration", limit=5, sort="chronological"
        )
        assert results, "chronological recall was empty: drafts filled the window"
        assert any(r.get("filename", "").endswith("the-answer.md") for r in results), (
            f"the accepted page was crowded out: {[r.get('filename') for r in results]}"
        )
        assert not any(r.get("review_state") in {"draft", "expired"} for r in results)
    finally:
        eng.db.close()

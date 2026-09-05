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
    def encode(self, text: str, **kwargs):
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

    real_extract = writer._extract_frontmatter_block
    fired = {"done": False}

    def extract_then_endorse(text):
        out = real_extract(text)
        if not fired["done"] and "status: draft" in out:
            fired["done"] = True
            endorse_draft(str(vault), "page-racey", "accept")
        return out

    monkeypatch.setattr(writer, "_extract_frontmatter_block", extract_then_endorse)

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

    real_extract = writer._extract_frontmatter_block
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

    monkeypatch.setattr(writer, "_extract_frontmatter_block", extract_then_extend)

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


# ── Round 8: findings from the seventh Grok review of #256 ─────────────────


def test_unembedded_draft_returns_its_body_not_an_empty_hit(tmp_path, monkeypatch):
    """Grok round 7 #1. Unendorsed pages are deliberately unembedded, so FTS is
    their ONLY body path. _fts_search carried no chunk_text at all, so an
    include_drafts caller on the default sort got a hit with empty `text` --
    'indexed and lexically searchable' was true of the row and false of the
    content. The round-2 test only asserted filename, so empty bodies passed."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    _write_page(vault / "wiki" / "concepts" / "only-draft.md", "draft",
                "singular findable phrase herein and a distinctive body sentence")
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        hits = eng.retrieve("singular findable phrase herein", limit=5, include_drafts=True)
        assert hits, "include_drafts did not reach the draft at all"
        hit = next(h for h in hits if h.get("filename", "").endswith("only-draft.md"))
        # Snippet depth truncates to _SNIPPET_MAX_CHARS, and the EVIDENCE
        # envelope plus the page's frontmatter can fill that budget, so assert
        # the body is PRESENT rather than that a specific sentence survived.
        assert hit.get("text", "").strip(), "draft came back as a hollow hit with no body"
        assert "only-draft" in hit["text"]

        # Document depth is not truncated: the whole page must come back, which
        # is the assertion that would have failed while _fetch_full_document
        # read chunk_embeddings alone and returned None for an unembedded page.
        deep = eng.retrieve(
            "singular findable phrase herein", limit=5,
            include_drafts=True, depth="document",
        )
        doc = next(h for h in deep if h.get("filename", "").endswith("only-draft.md"))
        assert "distinctive body sentence" in doc.get("text", ""), (
            "document depth returned no body for an unembedded page"
        )
    finally:
        eng.db.close()


def test_faiss_invalidate_is_atomic_against_concurrent_search(tmp_path, monkeypatch):
    """Grok round 7 #2. invalidate() runs on the vault-watch thread while
    searches run on RPC workers. Clearing the index and id maps in place under a
    concurrent search let that search resolve internal indices against maps that
    no longer matched them."""
    import threading

    import numpy as np
    from minni.config import SovereignConfig
    from minni.faiss_index import FAISSIndex

    cfg = SovereignConfig(
        db_path=str(tmp_path / "s" / "m.db"), vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "g"), faiss_index_path=str(tmp_path / "f.faiss"),
        writeback_enabled=False,
    )
    idx = FAISSIndex(cfg)
    n = 400
    ids = list(range(1, n + 1))
    vecs = np.random.rand(n, cfg.embedding_dim).astype(np.float32)
    idx.build_from_vectors(ids, vecs)

    errors = []
    stop = threading.Event()

    def searcher():
        q = np.random.rand(cfg.embedding_dim).astype(np.float32)
        while not stop.is_set():
            try:
                for cid, _ in idx.search(q, top_k=10):
                    # A chunk_id must always be one we actually put in.
                    if cid not in set(ids):
                        errors.append(f"unknown chunk_id {cid}")
            except Exception as exc:
                errors.append(repr(exc))

    def churner():
        for _ in range(40):
            idx.invalidate()
            idx.build_from_vectors(ids, vecs)

    threads = [threading.Thread(target=searcher) for _ in range(3)]
    for t in threads:
        t.start()
    try:
        churner()
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)

    assert not errors, f"concurrent search saw a torn index: {errors[:5]}"


def test_idle_sweep_skips_the_faiss_rebuild(tmp_path, monkeypatch):
    """Grok round 7 #3. Phase 3 re-read every embedding in the shared DB and
    rebuilt a throwaway index on every 300s tick, even when phase 1 was all
    skips. Cost scales with the whole table rather than with the work done."""
    from minni.index_all import index_shared_vault
    from minni.indexer import VaultIndexer

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    _write_page(Path(cfg.vault_path) / "wiki" / "concepts" / "p.md", "accepted",
                "body text " * 40)
    index_shared_vault(cfg)

    calls = []
    real = VaultIndexer._rebuild_faiss_index
    monkeypatch.setattr(
        VaultIndexer, "_rebuild_faiss_index",
        lambda self: (calls.append(1), real(self))[1],
    )

    stats = index_shared_vault(cfg)[str(cfg.vault_path)]
    assert stats["indexed"] == 0 and stats["deleted"] == 0, "not an idle sweep"
    assert calls == [], "idle sweep still rebuilt the FAISS index"

    # ...but a sweep that DOES change something must still rebuild.
    page = Path(cfg.vault_path) / "wiki" / "concepts" / "q.md"
    _write_page(page, "accepted", "new body text " * 40)
    index_shared_vault(cfg)
    assert calls, "a sweep that indexed a new page skipped the rebuild"


def test_dual_hit_prefers_the_semantic_chunk_over_the_fts_full_page(tmp_path, monkeypatch):
    """Grok round 8 #1. The hollow-hit fix gave _fts_search the whole file
    (frontmatter first) as chunk_text. Both RRF merges filled chunk_text
    first-non-empty-wins and the FTS stream runs first, so on the common
    dual-hit path the full page permanently shadowed the semantic chunk:
    re-ranker, envelope, and budget all ran on multi-KB YAML-led text."""
    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    eng = _engine(cfg)
    try:
        full_page = "---\ntitle: t\nstatus: accepted\n---\n\n" + "body words " * 400
        chunk = "the matching passage of the accepted page"
        base = {"doc_id": 1, "path": "a.md", "agent": "afm-loop", "sigil": "s",
                "page_status": "accepted"}
        fts = [{**base, "chunk_text": full_page}]
        sem = [{**base, "chunk_text": chunk, "heading_context": "## H"}]
        extra = [{**base, "chunk_text": "extra backend chunk"}]

        merged = eng._rrf_merge(fts, sem, limit=5)
        assert merged[0]["chunk_text"] == chunk, "FTS full page shadowed the semantic chunk"
        assert merged[0]["heading_context"] == "## H"

        multi = eng._rrf_merge_multi(fts, sem, [extra], limit=5)
        assert multi[0]["chunk_text"] == chunk, (
            "multi-merge let the FTS full page shadow the semantic chunk"
        )
        assert "_sem_chunk" not in multi[0], "internal merge flag leaked into results"

        # An extra backend stream must also beat the FTS dump when it is the
        # only semantic stream that saw the doc.
        extra_wins = eng._rrf_merge_multi(fts, [], [extra], limit=5)
        assert extra_wins[0]["chunk_text"] == "extra backend chunk"

        # The FTS full file stays as the fallback body for unembedded rows.
        fts_only = eng._rrf_merge(fts, [], limit=5)
        assert fts_only[0]["chunk_text"] == full_page
        fts_only_multi = eng._rrf_merge_multi(fts, [], [], limit=5)
        assert fts_only_multi[0]["chunk_text"] == full_page
    finally:
        eng.db.close()


def test_dual_hit_limit_still_fills_under_the_default_budget(tmp_path, monkeypatch):
    """Grok round 8 #1, end to end. Five multi-KB accepted pages that dual-hit
    FTS and FAISS: with the full page shadowing the chunk, ~1.6k-token pages
    exhaust the 4096-token default budget after two hits, so a limit=5 caller
    silently gets a fraction of what they asked for."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    for i in range(5):
        _write_page(vault / "wiki" / "concepts" / f"big{i}.md", "accepted",
                    f"unique{i} " + "quantum widget calibration lore " * 300)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        # Preconditions: BOTH legs see every page, or the dual-hit path is not
        # actually exercised and the assertions below are vacuous.
        fts = eng._fts_search("quantum widget calibration", 20,
                              exclude_statuses=["draft", "expired"])
        sem = eng._semantic_search("quantum widget calibration", 5)
        assert len({r["doc_id"] for r in fts}) == 5, "FTS leg missed pages"
        assert len({r["doc_id"] for r in sem}) == 5, "semantic leg missed pages"

        hits = eng.retrieve("quantum widget calibration", limit=5)
        assert len(hits) == 5, (
            f"limit=5 returned {len(hits)} hits: full-page bodies exhausted "
            "the context budget"
        )
    finally:
        eng.db.close()


def test_faiss_disk_reload_is_atomic_against_concurrent_search(tmp_path):
    """Grok round 8 #2. try_load_from_disk assigned _index/_chunk_ids/_id_map/
    _reverse_map/_vectors without _lock. The invalidate() path this PR put on
    the vault-watch thread makes that live: the sweep invalidates, an RPC
    search sees count==0 and reloads from disk while another search resolves
    internal indices against the half-applied maps."""
    import sqlite3 as sql
    import threading

    import numpy as np
    from minni.config import SovereignConfig
    from minni.faiss_index import FAISSIndex

    cfg = SovereignConfig(
        db_path=str(tmp_path / "s" / "m.db"), vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "g"), faiss_index_path=str(tmp_path / "f.faiss"),
        writeback_enabled=False,
    )
    # A minimal chunk_embeddings table gives save and load the same stable
    # checksum, so try_load_from_disk always reaches the restore path.
    db_file = tmp_path / "checksum.db"
    conn = sql.connect(str(db_file))
    conn.execute("CREATE TABLE chunk_embeddings (chunk_id INTEGER, computed_at REAL)")
    conn.commit()

    idx = FAISSIndex(cfg)
    n = 400
    ids = list(range(1, n + 1))
    known = set(ids)
    vecs = np.random.rand(n, cfg.embedding_dim).astype(np.float32)
    idx.build_from_vectors(ids, vecs)
    assert idx.save_to_disk(conn), "disk save failed; cannot exercise the reload path"

    errors = []
    stop = threading.Event()

    def searcher():
        q = np.random.rand(cfg.embedding_dim).astype(np.float32)
        while not stop.is_set():
            try:
                for cid, _ in idx.search(q, top_k=10):
                    if cid not in known:
                        errors.append(f"unknown chunk_id {cid}")
            except Exception as exc:
                errors.append(repr(exc))

    def reloader():
        local = sql.connect(str(db_file))
        try:
            while not stop.is_set():
                try:
                    idx.try_load_from_disk(local)
                except Exception as exc:
                    errors.append(repr(exc))
        finally:
            local.close()

    threads = [threading.Thread(target=searcher) for _ in range(3)]
    threads += [threading.Thread(target=reloader) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        # The path invalidate() made live: sweep invalidates, searches go
        # empty, reload threads race each other and the searches.
        for _ in range(40):
            idx.invalidate()
            idx.try_load_from_disk(conn)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)

    assert not errors, f"concurrent disk reload tore the index: {errors[:5]}"


# ── Round 9: findings from the eighth Grok review of #256 ──────────────────


def test_invalidate_mid_refresh_leaves_no_partial_residual(tmp_path, monkeypatch):
    """Grok round 9 #1 (High). _refresh_live_faiss checked count>0 unlocked,
    then took the FAISS lock once PER add. The vault-watch thread's
    invalidate() could land between two adds: the earlier adds are wiped, the
    later ones append onto the emptied structure, and the end state is a
    warm-looking index holding only the new chunk(s). _ensure_faiss_loaded
    early-returns on count>0, so semantic recall stays reduced to those few
    vectors until the process restarts."""
    from minni.faiss_index import FAISSIndex
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    _write_page(Path(cfg.vault_path) / "wiki" / "concepts" / "warm.md", "accepted",
                "warm body text " * 60)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng._ensure_faiss_loaded()
        full = eng.faiss_index.count
        assert full > 0, "engine did not warm up"

        # Deterministic interleave: the vault-watch invalidate lands exactly
        # between the first and second add of a durable-store refresh.
        real_add = FAISSIndex._add_locked
        calls = {"n": 0}

        def racing_add(self, chunk_id, embedding):
            calls["n"] += 1
            if calls["n"] == 2:
                self.invalidate()
            real_add(self, chunk_id, embedding)

        monkeypatch.setattr(FAISSIndex, "_add_locked", racing_add)
        dim = cfg.embedding_dim
        new_ids = [9001, 9002, 9003]
        new_vecs = [np.random.rand(dim).astype(np.float32) for _ in new_ids]
        eng._refresh_live_faiss(new_ids, new_vecs)
        monkeypatch.setattr(FAISSIndex, "_add_locked", real_add)

        # Either the whole batch landed before the invalidate (then was
        # cleared with everything else) or none of it did — never a tiny
        # residual set that count>0 gates would treat as a warm index.
        assert eng.faiss_index.count in (0, full + len(new_ids)), (
            f"partial residual index: {eng.faiss_index.count} vectors survived "
            "an invalidate that interleaved with a refresh"
        )

        # And the next ensure-load must restore the full DB set.
        eng._ensure_faiss_loaded()
        assert eng.faiss_index.count == full, "rebuild did not restore the index"

        # The simplest pin on the generation flag: a single add against an
        # invalidated index must not resurrect count>0.
        eng.faiss_index.invalidate()
        eng.faiss_index.add(9004, np.random.rand(dim).astype(np.float32))
        assert eng.faiss_index.count == 0, (
            "add() on an invalidated index resurrected a partial warm state"
        )
    finally:
        eng.db.close()


def test_save_to_disk_records_the_callers_checksum(tmp_path):
    """Grok round 9 #2. save_to_disk read _index/_vectors/_chunk_ids unlocked
    and always recomputed the checksum at save time, so a rebuild racing a
    durable insert could persist a manifest whose checksum matches the NEW DB
    while the vectors omit the new rows — a consistent-but-wrong cache a later
    cold load trusts. The caller can now pin the checksum of the snapshot the
    vectors actually came from."""
    import json

    import numpy as np
    from minni.config import SovereignConfig
    from minni.faiss_index import FAISSIndex

    cfg = SovereignConfig(
        db_path=str(tmp_path / "s" / "m.db"), vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "g"), faiss_index_path=str(tmp_path / "f.faiss"),
        writeback_enabled=False,
    )
    idx = FAISSIndex(cfg)
    idx.build_from_vectors([1, 2], np.random.rand(2, cfg.embedding_dim).astype(np.float32))

    assert idx.save_to_disk(db_conn=None, db_checksum="pinned-snapshot")
    with open(idx._manifest_path(), encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["db_checksum"] == "pinned-snapshot", (
        "manifest recorded a recomputed checksum, not the build snapshot's"
    )


def test_ensure_faiss_loaded_rebuilds_once_across_workers(tmp_path, monkeypatch):
    """Grok round 9 #3. _ensure_faiss_loaded was not a critical section:
    count>0 check, disk probe, full-table SELECT, build and save were separate
    steps. invalidate() turned cold start from rare into every-worker-after-
    every-vault-change, so concurrent workers each ran their own full rebuild
    from potentially different DB snapshots."""
    import threading

    from minni.faiss_index import FAISSIndex
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    _write_page(Path(cfg.vault_path) / "wiki" / "concepts" / "p.md", "accepted",
                "body text " * 60)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        # Slow, missing disk cache: both workers reach the rebuild window at
        # the same time unless something serializes them.
        monkeypatch.setattr(
            FAISSIndex, "try_load_from_disk",
            lambda self, db_conn=None: (time.sleep(0.3), False)[1],
        )
        builds = []
        real_stage = FAISSIndex.stage_build
        monkeypatch.setattr(
            FAISSIndex, "stage_build",
            lambda self, ids, vecs: (builds.append(1), real_stage(self, ids, vecs))[1],
        )

        threads = [threading.Thread(target=eng._ensure_faiss_loaded) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert eng.faiss_index.count > 0, "no worker warmed the index"
        assert len(builds) == 1, (
            f"{len(builds)} concurrent workers each rebuilt the index"
        )
    finally:
        eng.db.close()


def test_db_moving_mid_rebuild_leaves_the_index_complete_or_cold(tmp_path, monkeypatch):
    """Grok round 9 #3 / round 10 #1. A durable insert between the ensure's
    SELECT and the save used to produce a disk cache whose checksum matches
    the new DB while the vectors omit the new rows. Round 9 skipped the save —
    but left the IN-MEMORY index warm and incomplete, which count>0 gates then
    protect until the next invalidate: the in-memory twin of the same bug (the
    round-9 version of this test asserted count>0 here, codifying it). Under a
    DB that will not sit still, ensure must end complete or COLD — never
    warm-partial."""
    import minni.faiss_persist as persist
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    _write_page(Path(cfg.vault_path) / "wiki" / "concepts" / "p.md", "accepted",
                "body text " * 60)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.faiss_index.invalidate()
        # Every checksum probe sees a different value — a DB that never
        # stabilizes, so no build's snapshot can ever be trusted as complete.
        ticks = iter(range(1000))
        monkeypatch.setattr(
            persist, "compute_db_checksum", lambda conn: f"gen-{next(ticks)}"
        )
        manifest = eng.faiss_index._manifest_path()
        if os.path.exists(manifest):
            os.remove(manifest)

        eng._ensure_faiss_loaded()

        assert eng.faiss_index.count == 0, (
            "ensure left a warm index built from a snapshot the DB moved "
            "under — semantically invisible rows until the next invalidate"
        )
        assert not os.path.exists(manifest), (
            "a disk cache was written from a rebuild the DB moved under"
        )
    finally:
        eng.db.close()


def test_ensure_picks_up_rows_inserted_mid_rebuild(tmp_path, monkeypatch):
    """Grok round 10 #1, the recovery half. A durable insert that lands
    between the ensure's SELECT and its build must end up in the warm index:
    the checksum re-check retries the SELECT instead of finalizing a build
    that omits the new rows."""
    from minni.faiss_index import FAISSIndex
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    _write_page(Path(cfg.vault_path) / "wiki" / "concepts" / "p.md", "accepted",
                "body text " * 60)
    index_shared_vault(cfg)

    conn = sqlite3.connect(cfg.db_path)
    try:
        base = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
        doc_id = conn.execute("SELECT doc_id FROM documents LIMIT 1").fetchone()[0]
    finally:
        conn.close()
    assert base > 0

    def insert_row():
        c = sqlite3.connect(cfg.db_path)
        try:
            c.execute(
                """INSERT INTO chunk_embeddings
                   (doc_id, chunk_index, chunk_text, embedding, heading_context,
                    model_name, computed_at, layer)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (doc_id, 999, "late arrival",
                 np.zeros(cfg.embedding_dim, dtype=np.float32).tobytes(),
                 "", cfg.embedding_model, time.time(), "knowledge"),
            )
            c.commit()
        finally:
            c.close()

    eng = _engine(cfg)
    try:
        # The durable insert lands exactly between the ensure's SELECT and its
        # build: the first staged build commits a new row before building.
        real_stage = FAISSIndex.stage_build
        calls = {"n": 0}

        def racing_stage(self, ids, vecs):
            calls["n"] += 1
            if calls["n"] == 1:
                insert_row()
            return real_stage(self, ids, vecs)

        monkeypatch.setattr(FAISSIndex, "stage_build", racing_stage)
        eng._ensure_faiss_loaded()

        assert eng.faiss_index.count == base + 1, (
            f"warm index has {eng.faiss_index.count} vectors, DB has "
            f"{base + 1}: a row inserted mid-rebuild stayed semantically "
            "invisible"
        )
    finally:
        eng.db.close()


def test_durable_refresh_survives_an_ensure_that_missed_its_rows(tmp_path, monkeypatch):
    """Grok round 10 #1, durable-side belt. add_batch returning False (cold /
    invalidated) was ignored. If an ensure was already mid-flight when the
    durable rows committed, its build omits them and lands warm — so the
    refresh must retry behind the load lock: either the index is warm by then
    (the add lands) or it is still cold (the next ensure sees the DB rows)."""
    import threading

    import numpy as np
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(
        db_path=str(tmp_path / "s" / "m.db"), vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "g"), faiss_index_path=str(tmp_path / "f.faiss"),
        writeback_enabled=False,
    )
    eng = RetrievalEngine(SovereignDB(cfg), cfg)
    try:
        dim = cfg.embedding_dim
        ids = list(range(1, 51))
        vecs = np.random.rand(len(ids), dim).astype(np.float32)
        eng.faiss_index.build_from_vectors(ids, vecs)

        # The vault-watch invalidates while an ensure is "mid-flight" — held
        # here as the acquired load lock — and a durable store refreshes.
        eng._faiss_load_lock.acquire()
        eng.faiss_index.invalidate()
        t = threading.Thread(
            target=eng._refresh_live_faiss,
            args=([9001], [np.random.rand(dim).astype(np.float32)]),
        )
        t.start()
        time.sleep(0.2)  # let the refresh hit the cold add_batch and block
        # The ensure finishes from a SELECT that predates the durable row.
        eng.faiss_index.build_from_vectors(ids, vecs)
        eng._faiss_load_lock.release()
        t.join(timeout=10)

        assert 9001 in eng.faiss_index._reverse_map, (
            "durable chunk stranded: the refresh gave up on a cold index and "
            "the ensure that landed warm never saw its row"
        )

        # And the retry is idempotent: re-adding the same id must not grow
        # the index.
        n = eng.faiss_index.count
        eng.faiss_index.add_batch([9001], [np.random.rand(dim).astype(np.float32)])
        assert eng.faiss_index.count == n, "add_batch duplicated an existing id"
    finally:
        eng.db.close()


# ── Round 14: findings from the thirteenth Grok review of #256 ─────────────


_UNFENCED_NOTE = (
    "# AFM draft format\n\n"
    "When the loop writes a draft the frontmatter looks like this:\n\n"
    "status: draft\n"
    "agent: afm-loop\n"
    "page_id: page-ghost\n"
    "expires_at: '2020-01-01T00:00:00Z'\n\n"
    "None of the lines above are frontmatter — this file has no fences.\n"
)


def test_unfenced_page_is_never_expired(tmp_path):
    """Grok round 14 #1. _extract_frontmatter falls back to the WHOLE text
    when a file has no leading fence, so an ordinary unfenced wiki note that
    documents the AFM format read as an overdue draft and was destructively
    rewritten on the first live sweep. Dead pre-PR (expiry matched nothing);
    activated the moment expiry started working. Destructive paths must
    require a real CLOSED fenced block and never scan the body."""
    from minni.afm_writer import DRAFT_TTL_SECONDS, _expire_stale_drafts, _write_one

    vault = tmp_path / "vault"
    wiki = vault / "wiki" / "concepts"
    wiki.mkdir(parents=True)
    unfenced = wiki / "afm-format-notes.md"
    unfenced.write_text(_UNFENCED_NOTE, encoding="utf-8")
    unclosed = wiki / "unclosed-fence.md"
    unclosed.write_text(
        "---\nstatus: draft\nagent: afm-loop\n"
        "expires_at: '2020-01-01T00:00:00Z'\n\nno closing fence follows\n",
        encoding="utf-8",
    )
    # A REAL overdue draft alongside, so the sweep is provably live.
    now = time.time()
    real = vault / _write_one(
        vault, _draft(page_id="page-real"), now=now - DRAFT_TTL_SECONDS - 86400
    )["path"]

    expired = _expire_stale_drafts(vault, now=now)

    assert expired == 1, f"sweep expired {expired} pages, expected the 1 real draft"
    assert "status: expired" in real.read_text(encoding="utf-8")
    assert unfenced.read_text(encoding="utf-8") == _UNFENCED_NOTE, (
        "expiry rewrote an UNFENCED note whose body documents the AFM format"
    )
    assert "status: draft" in unclosed.read_text(encoding="utf-8"), (
        "expiry rewrote a page whose fence never closes"
    )


def test_unfenced_page_is_neither_pending_nor_endorsable(tmp_path):
    """Grok round 14 #1, the counter and endorse halves. The same whole-text
    fallback made writer_status count an unfenced note as pending (a backlog
    expiry can never drain) and let endorse_draft discover it by a page_id
    its body merely mentions."""
    from minni.afm_writer import endorse_draft, writer_status
    import pytest

    wiki = tmp_path / "wiki" / "concepts"
    wiki.mkdir(parents=True)
    page = wiki / "afm-format-notes.md"
    page.write_text(_UNFENCED_NOTE, encoding="utf-8")

    status = writer_status(str(tmp_path))
    assert status["drafts_pending"] == 0, (
        f"an unfenced note counted as pending: {status['drafts_pending']}"
    )

    with pytest.raises(FileNotFoundError):
        endorse_draft(str(tmp_path), "page-ghost", "accept")
    assert page.read_text(encoding="utf-8") == _UNFENCED_NOTE, (
        "endorse rewrote an unfenced note"
    )


def test_lifecycle_rewrites_are_atomic(tmp_path, monkeypatch):
    """Grok round 14 #2. Path.write_text truncates before writing, so a crash
    or full disk mid-write during the ~900-page first sweep leaves torn pages
    the indexer then persists. Expiry and endorse must write via same-dir
    temp + os.replace: on failure the original page is byte-identical."""
    from minni.afm_writer import (
        DRAFT_TTL_SECONDS,
        _expire_stale_drafts,
        _write_one,
        endorse_draft,
    )
    import pytest

    vault = tmp_path / "vault"
    now = time.time()
    page = vault / _write_one(
        vault, _draft(page_id="page-torn"), now=now - DRAFT_TTL_SECONDS - 86400
    )["path"]
    before = page.read_text(encoding="utf-8")

    orig_open = os.open
    orig_write = os.write
    tmp_fds: set[int] = set()

    def tracking_open(path, flags, *args, **kwargs):
        fd = orig_open(path, flags, *args, **kwargs)
        try:
            name = Path(os.fsdecode(path)).name
        except (TypeError, ValueError, OSError):
            return fd
        if ".tmp" in name:
            tmp_fds.add(fd)
        return fd

    def disk_full(fd, data):
        if fd in tmp_fds:
            orig_write(fd, data[:12])
            raise OSError("disk full")
        return orig_write(fd, data)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "write", disk_full)
    with pytest.raises(OSError):
        endorse_draft(str(vault), "page-torn", "accept")
    assert page.read_text(encoding="utf-8") == before, (
        "a failed endorse left a torn page behind"
    )
    with pytest.raises(OSError):
        _expire_stale_drafts(vault, now=now)
    assert page.read_text(encoding="utf-8") == before, (
        "a failed expiry left a torn page behind"
    )


# ── Round 13: findings from the twelfth Grok review of #256 ────────────────


def test_endorse_resolves_pages_by_frontmatter_id_not_body_quotation(tmp_path):
    """Grok round 12 #1 (round 13 review). Page discovery was still a
    whole-file substring: a draft whose body quotes another page's frontmatter
    was discovered FIRST, endorsed in the target's place, and the audit
    claimed the requested id. Same body-quotation class as the status gate,
    one field over."""
    from minni.afm_writer import _extract_frontmatter, _page_id_of, _write_one, endorse_draft
    import pytest

    vault = tmp_path / "vault"
    # The deterministic half: ONLY the quoting page exists. A substring
    # resolver finds it and endorses it under the wrong identity; a
    # frontmatter resolver correctly reports the id as absent.
    quoting = _draft(page_id="page-quoter", title="quoter")
    quoting["body"] = (
        "For the record, the other page's frontmatter read:\n\n"
        "    page_id: page-target\n    status: draft\n"
    )
    quoting_path = vault / _write_one(vault, quoting)["path"]
    before = quoting_path.read_text(encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        endorse_draft(str(vault), "page-target", "accept")
    assert quoting_path.read_text(encoding="utf-8") == before, (
        "endorse rewrote a page that merely QUOTED the requested page_id"
    )

    # And with the real target present, the endorsement lands on it — the
    # quoting page stays a draft.
    target = _draft(page_id="page-target", title="the real one")
    target_path = vault / _write_one(vault, target)["path"]
    result = endorse_draft(str(vault), "page-target", "accept")
    assert result["path"].endswith(target_path.name)
    assert "status: accepted" in _extract_frontmatter(
        target_path.read_text(encoding="utf-8")
    )
    assert "status: draft" in _extract_frontmatter(
        quoting_path.read_text(encoding="utf-8")
    )
    assert _page_id_of(_extract_frontmatter(
        quoting_path.read_text(encoding="utf-8"))) == "page-quoter"


def test_endorse_rechecks_the_page_identity_under_the_lock(tmp_path, monkeypatch):
    """Grok round 13 #1, the lock half. The per-page lock is keyed by the
    REQUESTED id, and the discovery read runs unlocked — if the file's own id
    changes before the lock lands, the write is not serialized against the
    page it is about to modify and must refuse."""
    import minni.afm_writer as writer
    from minni.afm_writer import _extract_frontmatter, _FM_PAGE_ID, _write_one, endorse_draft
    import pytest

    vault = tmp_path / "vault"
    draft = _draft(page_id="page-swap", title="swapper")
    page = vault / _write_one(vault, draft)["path"]

    # Swap the file's identity in the window between discovery and the lock.
    real_lock = writer._page_lock

    def swapping_lock(page_id):
        text = page.read_text(encoding="utf-8")
        fm = _extract_frontmatter(text)
        rewritten = _FM_PAGE_ID.sub("page_id: page-somebody-else", fm, count=1)
        page.write_text(rewritten + text[len(fm):], encoding="utf-8")
        return real_lock(page_id)

    monkeypatch.setattr(writer, "_page_lock", swapping_lock)
    with pytest.raises(FileNotFoundError):
        endorse_draft(str(vault), "page-swap", "accept")
    assert "status: draft" in _extract_frontmatter(page.read_text(encoding="utf-8")), (
        "endorse rewrote a page whose identity changed under it"
    )


def test_skip_path_normalizes_the_fts_row_too(tmp_path, monkeypatch):
    """Grok round 13 #3 (Low, flagged twice). The mtime-skip path normalized
    documents.path but left vault_fts.path at the old spelling until a content
    reindex. Recall joins d.path so it was latent — but the two rows describe
    the same file and must not diverge."""
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
        conn.execute("UPDATE documents SET path = ? WHERE path = ?",
                     (noncanonical, str(page)))
        conn.execute("UPDATE vault_fts SET path = ? WHERE path = ?",
                     (noncanonical, str(page)))
        conn.commit()
    finally:
        conn.close()

    # No mtime bump: this is the SKIP path, not the reindex path.
    index_shared_vault(cfg)

    conn = sqlite3.connect(cfg.db_path)
    try:
        fts_paths = [r[0] for r in conn.execute(
            "SELECT path FROM vault_fts").fetchall() if r[0].endswith(page.name)]
    finally:
        conn.close()
    assert fts_paths == [str(page)], (
        f"vault_fts.path left stale on the skip path: {fts_paths}"
    )


# ── Round 12: findings from the eleventh Grok review of #256 ───────────────


def test_draft_review_is_not_drowned_by_the_expired_backlog(tmp_path, monkeypatch):
    """Grok round 12 #1. `expired` piggy-backed on include_drafts: the skip
    only ran when include_drafts was False, so the first live sweep — which
    creates ~900 expired pages — turned draft review into a black hole.
    Chronological is the worst case: ascending age puts the months-old expired
    backlog first, and limit=5 returned five expired, zero active drafts.
    expired is terminal and needs its own include_expired flag."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)

    for i in range(60):
        _write_page(vault / "wiki" / "concepts" / f"exp{i}.md", "expired",
                    "quantum widget calibration " * 12)
    for i in range(3):
        _write_page(vault / "wiki" / "concepts" / f"live{i}.md", "draft",
                    "quantum widget calibration under review " * 12)
    index_shared_vault(cfg)

    # The expired backlog is the OLDEST, exactly as on the live install.
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute(
            "UPDATE documents SET indexed_at = 1000 WHERE page_status = 'expired'"
        )
        conn.execute(
            "UPDATE documents SET indexed_at = 9999999999 WHERE page_status = 'draft'"
        )
        conn.commit()
    finally:
        conn.close()

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        results = eng.retrieve(
            "quantum widget calibration", limit=5, sort="chronological",
            include_drafts=True,
        )
        assert results, "draft review returned nothing at all"
        states = [r.get("review_state") for r in results]
        assert "expired" not in states, (
            f"include_drafts admitted the expired backlog: {states}"
        )
        assert any(s == "draft" for s in states), (
            f"no active draft made the window: {states}"
        )

        # The backlog stays reachable, but only on explicit request.
        tombs = eng.retrieve(
            "quantum widget calibration", limit=5, sort="chronological",
            include_drafts=True, include_expired=True,
        )
        assert any(r.get("review_state") == "expired" for r in tombs), (
            "include_expired=True cannot reach expired pages"
        )
    finally:
        eng.db.close()


def test_writer_status_counts_frontmatter_drafts_only(tmp_path):
    """Grok round 12 #2. The pending count gated on whole-file substrings
    while expiry became frontmatter-only, so a page whose body quotes another
    draft's frontmatter kept counting as pending after expiry flipped its real
    status — health reporting a backlog the expiry engine had drained."""
    from minni.afm_writer import writer_status

    wiki = tmp_path / "wiki" / "concepts"
    wiki.mkdir(parents=True)
    # Expired page whose body quotes a draft's frontmatter. The counter and
    # the expiry engine must agree this is NOT pending.
    (wiki / "expired-quoting.md").write_text(
        "---\ntitle: t\nstatus: expired\nagent: afm-loop\n"
        "created: '2026-01-01T00:00:00Z'\n---\n\n"
        "The original read:\n\n    status: draft\n    agent: afm-loop\n",
        encoding="utf-8",
    )
    # One real draft, so the count has a truth to report.
    (wiki / "real-draft.md").write_text(
        "---\ntitle: r\nstatus: draft\nagent: afm-loop\n"
        "created: '2026-07-01T00:00:00Z'\n---\n\nbody\n",
        encoding="utf-8",
    )

    status = writer_status(str(tmp_path))
    assert status["drafts_pending"] == 1, (
        f"pending counted body prose as frontmatter: {status['drafts_pending']}"
    )


def test_endorse_refuses_a_page_that_is_no_longer_a_draft(tmp_path):
    """Grok round 12 #3 (Low). endorse_draft gated on a whole-file substring
    and rewrote the FIRST occurrence. A page already expired (or endorsed)
    whose body quotes `status: draft` was treated as an active draft: the
    body prose got rewritten, success was reported, and the frontmatter never
    changed. Endorse must decide and rewrite frontmatter-only, like expiry."""
    from minni.afm_writer import (
        _FM_DRAFT_STATUS,
        _extract_frontmatter,
        _write_one,
        endorse_draft,
    )
    import pytest

    vault = tmp_path / "vault"
    quoted_body = "As the draft said:\n\n    status: draft\n\nend quote."

    # A page whose FM already expired but whose body quotes draft frontmatter.
    dead = _draft(page_id="page-dead", title="dead one")
    dead["body"] = quoted_body
    dead_path = vault / _write_one(vault, dead)["path"]
    text = dead_path.read_text(encoding="utf-8")
    fm = _extract_frontmatter(text)
    dead_path.write_text(
        _FM_DRAFT_STATUS.sub("status: expired", fm, count=1) + text[len(fm):],
        encoding="utf-8",
    )
    before = dead_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        endorse_draft(str(vault), "page-dead", "accept")
    assert dead_path.read_text(encoding="utf-8") == before, (
        "a refused endorsement still rewrote the page"
    )

    # Happy path: a REAL draft whose body also quotes `status: draft` gets its
    # frontmatter endorsed and its body left alone.
    live = _draft(page_id="page-live", title="live one")
    live["body"] = quoted_body
    live_path = vault / _write_one(vault, live)["path"]
    endorse_draft(str(vault), "page-live", "accept")
    after = live_path.read_text(encoding="utf-8")
    after_fm = _extract_frontmatter(after)
    assert "status: accepted" in after_fm
    assert "status: draft" not in after_fm
    assert "status: draft" in after[len(after_fm):], (
        "endorse rewrote body prose instead of leaving it alone"
    )


# ── Round 11: findings from the tenth Grok review of #256 ──────────────────


def test_rebuild_retry_never_publishes_a_partial_index(tmp_path, monkeypatch):
    """Grok round 11 #1 (High). The round-10 retry loop called
    build_from_vectors BEFORE the checksum re-check, so between a mismatch and
    the next attempt the live index was warm with the stale snapshot — and the
    unlocked count>0 early return let every concurrent worker adopt it. The
    rebuild now stages off to the side and commits only a validated build:
    while an ensure is mid-retry, a concurrent search must see an EMPTY
    semantic leg (degrading to lexical), never a partial set."""
    import minni.faiss_persist as persist
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    _write_page(Path(cfg.vault_path) / "wiki" / "concepts" / "p.md", "accepted",
                "body text " * 60)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.faiss_index.invalidate()
        manifest = eng.faiss_index._manifest_path()
        if os.path.exists(manifest):
            os.remove(manifest)

        # Checksums: one probe belongs to try_load_from_disk (it samples the
        # checksum even on a manifest miss), then attempt 1 sees the DB move
        # under it (s0 -> s1) and attempt 2 is stable. At every probe, record
        # what a concurrent worker's search would see of the live index.
        observed = []
        probe = np.random.rand(cfg.embedding_dim).astype(np.float32)
        seq = iter(["t", "s0", "s1", "s1", "s1"])

        def fake_checksum(conn):
            observed.append((eng.faiss_index.count,
                             len(eng.faiss_index.search(probe, top_k=5))))
            return next(seq)

        monkeypatch.setattr(persist, "compute_db_checksum", fake_checksum)
        eng._ensure_faiss_loaded()

        assert eng.faiss_index.count > 0, "stable attempt 2 did not warm the index"
        assert len(observed) == 5, f"unexpected checksum probe count: {len(observed)}"
        # Probes 3 and 4 run between attempt 1's build and attempt 2's commit —
        # exactly where the round-10 code had already published the stale build.
        for count, hits in observed[2:4]:
            assert count == 0 and hits == 0, (
                f"a concurrent search saw a partial index mid-retry: "
                f"count={count} hits={hits}"
            )
    finally:
        eng.db.close()


def test_purge_to_empty_mid_rebuild_does_not_leave_a_stale_warm_index(tmp_path, monkeypatch):
    """Grok round 11 #1, the empty-retry hole. If a later retry iteration
    finds chunk_embeddings EMPTY (say a purge landed mid-flight), the round-10
    loop returned with the previous attempt's build still warm. Empty must
    mean cold, not 'keep serving the snapshot a purge just deleted'."""
    import minni.faiss_persist as persist
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    _write_page(Path(cfg.vault_path) / "wiki" / "concepts" / "p.md", "accepted",
                "body text " * 60)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.faiss_index.invalidate()
        manifest = eng.faiss_index._manifest_path()
        if os.path.exists(manifest):
            os.remove(manifest)

        # Call 1 is try_load_from_disk's probe (manifest miss), call 2 is
        # attempt 1's pre-SELECT sample. Attempt 1's re-check (call 3) reports
        # a moved DB AND deletes every chunk row, so attempt 2's SELECT — which
        # runs after a build already succeeded — comes back empty.
        calls = {"n": 0}

        def fake_checksum(conn):
            calls["n"] += 1
            if calls["n"] == 3:
                c = sqlite3.connect(cfg.db_path)
                try:
                    c.execute("DELETE FROM chunk_embeddings")
                    c.commit()
                finally:
                    c.close()
                return "moved"
            return "s0"

        monkeypatch.setattr(persist, "compute_db_checksum", fake_checksum)
        eng._ensure_faiss_loaded()

        assert calls["n"] >= 3, "the moved-DB re-check was never reached"

        assert eng.faiss_index.count == 0, (
            "ensure kept a warm index built from rows a purge deleted mid-flight"
        )
    finally:
        eng.db.close()


def test_disk_restore_is_fenced_against_a_mid_load_mutation(tmp_path, monkeypatch):
    """Grok round 11 #2. try_load_from_disk sampled the checksum once, before
    the slow disk read, and committed unconditionally. A vault-watch write
    landing during the read meant the S0 cache was applied over an S1 DB —
    warm on stale data, with the new rows invisible until the next invalidate.
    The restore must re-check checksum and generation under the FAISS lock
    immediately before applying, and miss into the DB rebuild otherwise."""
    import minni.faiss_persist as persist
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    _write_page(Path(cfg.vault_path) / "wiki" / "concepts" / "p.md", "accepted",
                "body text " * 60)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        # Warm once so a checksum-matching disk cache exists, then go cold.
        eng._ensure_faiss_loaded()
        base = eng.faiss_index.count
        assert base > 0 and os.path.exists(eng.faiss_index._manifest_path())
        eng.faiss_index.invalidate()

        conn = sqlite3.connect(cfg.db_path)
        try:
            doc_id = conn.execute("SELECT doc_id FROM documents LIMIT 1").fetchone()[0]
        finally:
            conn.close()

        # The durable write lands DURING the disk read: after the checksum was
        # sampled and the manifest validated, before the restore is applied.
        real_load = persist.load

        def racing_load(*args, **kwargs):
            result = real_load(*args, **kwargs)
            if result is not None:
                c = sqlite3.connect(cfg.db_path)
                try:
                    c.execute(
                        """INSERT INTO chunk_embeddings
                           (doc_id, chunk_index, chunk_text, embedding,
                            heading_context, model_name, computed_at, layer)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (doc_id, 998, "written mid disk read",
                         np.zeros(cfg.embedding_dim, dtype=np.float32).tobytes(),
                         "", cfg.embedding_model, time.time(), "knowledge"),
                    )
                    c.commit()
                finally:
                    c.close()
            return result

        monkeypatch.setattr(persist, "load", racing_load)
        eng._ensure_faiss_loaded()

        assert eng.faiss_index.count == base + 1, (
            f"disk restore applied a stale cache over a moved DB: index has "
            f"{eng.faiss_index.count} vectors, DB has {base + 1}"
        )
    finally:
        eng.db.close()


def test_purge_only_sweep_counts_as_changed_in_the_runner(tmp_path):
    """Grok round 11 #3 (Low). The outer runner's changed flag and log only
    looked at indexed/pruned/errors, so a purge-only sweep — which DOES
    invalidate FAISS — read as idle in the logs and skipped the per-vault
    cache clear."""
    import minni.minnid as minnid

    assert minnid._report_sweep({"v": {"chunks_purged": 3}}) is True
    assert minnid._report_sweep({"v": {"indexed": 0, "pruned": 0, "errors": 1}}) is False
    assert minnid._report_sweep({"v": {"indexed": 0}}) is False
    assert minnid._report_sweep({}) is False


def test_agent_vault_failure_does_not_darken_the_shared_vault(tmp_path, monkeypatch):
    """Grok round 10 #2 (Low). _vault_watch_sweep_once ran index_agent_vaults
    OUTSIDE any try/except, ahead of shared-vault expiry and indexing. A
    sticky agent-vault fault therefore kept the shared vault dark on every
    tick — the original defect of this PR, reintroduced as coupling."""
    import minni.index_all as index_all
    import minni.minnid as minnid
    from minni.afm_writer import _write_one

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)

    vault = Path(cfg.vault_path)
    written = _write_one(vault, _draft())
    expected = str((vault / written["path"]).resolve())

    def broken_agent_vaults(*args, **kwargs):
        raise RuntimeError("sticky agent-vault fault")

    monkeypatch.setattr(index_all, "index_agent_vaults", broken_agent_vaults)

    stats = minnid._vault_watch_sweep_once()

    assert str(cfg.vault_path) in stats, (
        "shared vault went dark behind a broken agent vault: "
        f"{sorted(stats)}"
    )
    assert expected in _indexed_paths(cfg.db_path)


def test_multiple_noncanonical_spellings_collapse_to_one_row(tmp_path, monkeypatch):
    """Grok round 9 #4 (Low). _noncanonical_rows kept out[resolved] = row,
    last wins. With TWO non-canonical spellings of one file and no canonical
    row, phase 1 adopts one and the other survives the prune (it resolves to a
    file that exists) — the round-5 duplicate collapse only covered the case
    where one spelling was already canonical."""
    from minni.afm_writer import _write_one
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    written = _write_one(vault, _draft())
    index_shared_vault(cfg)

    page = (vault / written["path"]).resolve()
    spelling_a = str(page.parent / ".." / page.parent.name / page.name)
    spelling_b = str(
        page.parent / ".." / ".." / page.parent.parent.name / page.parent.name / page.name
    )
    assert Path(spelling_a).resolve() == page and Path(spelling_b).resolve() == page
    assert spelling_a != spelling_b
    # Seed the historical state: two non-canonical twins, NO canonical row.
    conn = sqlite3.connect(cfg.db_path)
    try:
        conn.execute("UPDATE documents SET path = ? WHERE path = ?", (spelling_a, str(page)))
        conn.commit()
    finally:
        conn.close()
    _seed_row(cfg.db_path, spelling_b)
    assert len([p for p in _indexed_paths(cfg.db_path) if p.endswith(page.name)]) == 2

    index_shared_vault(cfg)

    rows = [p for p in _indexed_paths(cfg.db_path) if p.endswith(page.name)]
    assert rows == [str(page)], (
        f"extra non-canonical spelling survived the collapse: {rows}"
    )


# ── Round 15: findings from the fourteenth Grok review of #256 ─────────────


def test_fts_only_limit_still_fills_under_the_default_budget(tmp_path, monkeypatch):
    """Grok round 14 #1 (Medium). Round 8 closed dual-hit shadowing, but the
    FTS-only path still attached the whole markdown file as chunk_text. With
    the encoder down (first-class degraded mode), five multi-KB accepted pages
    exhaust the 4096-token default budget after two hits — limit=5 silently
    returns a fraction. Mirror test_dual_hit_limit_still_fills_under_the_default_budget
    with the semantic leg forced off."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    for i in range(5):
        _write_page(vault / "wiki" / "concepts" / f"big{i}.md", "accepted",
                    f"unique{i} " + "quantum widget calibration lore " * 300)
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        # Force the semantic-down path used in production when the encoder is
        # unavailable: model property always re-calls get_embedder(), so patch
        # that to None so _semantic_search returns [] and FTS is the only body.
        import minni.models as models
        monkeypatch.setattr(models, "get_embedder", lambda: None)
        fts = eng._fts_search("quantum widget calibration", 20,
                              exclude_statuses=["draft", "expired"])
        assert len({r["doc_id"] for r in fts}) == 5, "FTS leg missed pages"
        # Each FTS body must be a chunk (or body-only excerpt), not the raw
        # multi-KB fenced file (~9k chars for these fixtures).
        for row in fts:
            text = row.get("chunk_text") or ""
            assert not text.lstrip().startswith("---"), (
                "FTS chunk_text still ships the raw fenced file"
            )
            assert len(text) < 7000, (
                f"FTS chunk_text looks like a full multi-KB page ({len(text)} chars)"
            )

        hits = eng.retrieve("quantum widget calibration", limit=5)
        assert eng.vector_model_down is True, "semantic-down flag not raised"
        assert len(hits) == 5, (
            f"limit=5 returned {len(hits)} hits under FTS-only: full-page "
            "bodies exhausted the context budget"
        )
    finally:
        eng.db.close()


def test_fts_unembedded_draft_snippet_is_body_not_frontmatter(tmp_path, monkeypatch):
    """Grok round 14 #1, snippet half. Default snippet depth (280 chars) on a
    real AFM page with a long FM header was almost entirely YAML; include_drafts
    "body" looked like frontmatter soup. Body prose must lead the FTS text."""
    from minni.index_all import index_shared_vault

    _install_fake_embedder(monkeypatch)
    cfg = _make_cfg(tmp_path)
    vault = Path(cfg.vault_path)
    # Long FM keys so a raw-file excerpt would fill the 280-char snippet.
    path = vault / "wiki" / "concepts" / "only-draft.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        "title: only-draft long title that burns chars\n"
        "type: concept\n"
        "status: draft\n"
        "agent: afm-loop\n"
        "privacy: safe\n"
        "trace_id: trace-r15-snippet\n"
        "page_id: page-r15snip\n"
        "created: 2026-08-01T00:00:00Z\n"
        "expires_at: '2026-08-15T00:00:00Z'\n"
        "gate_status: ready_for_review\n"
        "sources:\n  - '`probe`'\n"
        "---\n\n"
        "singular findable phrase herein and a distinctive body sentence\n",
        encoding="utf-8",
    )
    index_shared_vault(cfg)

    eng = _engine(cfg)
    try:
        eng.config.reranker_enabled = False
        import minni.models as models
        monkeypatch.setattr(models, "get_embedder", lambda: None)
        # FTS leg is the body source for unembedded drafts; assert there before
        # the evidence envelope + 280-char snippet truncation can hide it.
        fts = eng._fts_search(
            "singular findable phrase herein", 5, exclude_statuses=["expired"],
        )
        assert fts, "FTS did not reach the draft"
        body = fts[0].get("chunk_text") or ""
        assert "distinctive body sentence" in body, (
            f"FTS body has no prose after FM strip: {body[:200]!r}"
        )
        assert "expires_at:" not in body and "gate_status:" not in body, (
            f"FTS chunk_text still carries YAML frontmatter: {body[:200]!r}"
        )

        # chunk depth still wraps in EVIDENCE but must not be FM-led.
        hits = eng.retrieve(
            "singular findable phrase herein", limit=5, include_drafts=True,
            depth="chunk",
        )
        assert hits, "include_drafts did not reach the draft"
        hit = next(h for h in hits if h.get("filename", "").endswith("only-draft.md"))
        text = hit.get("text", "")
        assert "distinctive body sentence" in text, (
            f"chunk-depth text has no body prose: {text!r}"
        )
        assert "expires_at:" not in text and "gate_status:" not in text, (
            f"chunk-depth text still carries YAML frontmatter: {text!r}"
        )
    finally:
        eng.db.close()


def test_write_one_is_atomic_against_mid_write_failure(tmp_path, monkeypatch):
    """Grok round 14 #2 (Low). Expiry/endorse already used same-dir temp +
    os.replace; new draft creation via _write_one still used Path.write_text
    truncate-in-place. Vault-watch indexes concurrently — a crash mid-write
    must not leave a torn page for the next sweep."""
    from minni.afm_writer import _write_one
    import pytest

    vault = tmp_path / "vault"
    orig_open = os.open
    orig_write = os.write
    tmp_fds: set[int] = set()
    calls = {"n": 0}

    def tracking_open(path, flags, *args, **kwargs):
        fd = orig_open(path, flags, *args, **kwargs)
        try:
            name = Path(os.fsdecode(path)).name
        except (TypeError, ValueError, OSError):
            return fd
        if ".tmp" in name:
            tmp_fds.add(fd)
        return fd

    def flaky_write(fd, data):
        # _atomic_write_text exclusive-creates a unique tmp first; fail that
        # write so the target path is never replaced (and never truncated).
        if fd in tmp_fds:
            calls["n"] += 1
            orig_write(fd, data[:12])
            raise OSError("disk full")
        return orig_write(fd, data)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "write", flaky_write)
    with pytest.raises(OSError):
        _write_one(vault, _draft(page_id="page-atomic"))

    # No durable page under wiki/ should exist after a failed atomic create.
    wiki = vault / "wiki"
    leftovers = list(wiki.rglob("*.md")) if wiki.exists() else []
    assert leftovers == [], f"failed _write_one left a torn page: {leftovers}"
    # Temp may remain or be partial; that is fine — not the durable target.
    assert calls["n"] >= 1


def test_include_expired_is_on_the_eval_kwargs_allowlist():
    """Grok round 14 #3 (Low). Eval/search harness drops unknown retrieve
    kwargs after a warning. Tombstone-recall eval passing include_expired=True
    was silently no-op'd because the flag was missing from the allowlist."""
    from minni.eval.metrics import KNOWN_RETRIEVE_KWARGS

    assert "include_expired" in KNOWN_RETRIEVE_KWARGS
    assert "include_drafts" in KNOWN_RETRIEVE_KWARGS

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

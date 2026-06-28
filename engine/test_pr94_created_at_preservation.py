"""PR94-1/-3: memory_links re-indexing must preserve created_at and not
double-count.

Both wiki_indexer._index_wikilinks and afm_passes.vault_ingest._insert_wikilinks
used to DELETE all of a source's links before re-inserting, so the
ON CONFLICT(...) DO UPDATE upsert never fired and created_at was overwritten on
every pass (PR94-1); wiki_indexer also appended each link row twice (PR94-3).
The fix is a diff-based update: prune only stale wikilinks (scoped to
link_type='wikilink' so other edge types survive), then upsert the rest.
"""

from __future__ import annotations

import db as db_mod
from config import SovereignConfig


def _make_db(tmp_path):
    cfg = SovereignConfig(db_path=str(tmp_path / "links.db"))
    return db_mod.SovereignDB(cfg)


def _add_doc(c, path: str) -> int:
    c.execute(
        "INSERT INTO documents (path, agent, last_modified, indexed_at, layer) "
        "VALUES (?, 'wiki:concept', 0, 0, 'knowledge')",
        (path,),
    )
    return int(c.lastrowid)


def _links(c, source_id: int):
    c.execute(
        "SELECT target_doc_id, link_type, weight, created_at FROM memory_links "
        "WHERE source_doc_id = ? ORDER BY link_type, target_doc_id",
        (source_id,),
    )
    return [dict(target=r[0], link_type=r[1], weight=r[2], created_at=r[3]) for r in c.fetchall()]


def _setup(db):
    with db.cursor() as c:
        alpha = _add_doc(c, "/wiki/alpha.md")
        beta = _add_doc(c, "/wiki/beta.md")
        gamma = _add_doc(c, "/wiki/gamma.md")
    target_map = {"beta": "/wiki/beta.md", "gamma": "/wiki/gamma.md"}
    return alpha, beta, gamma, target_map


def _index_wiki(db, alpha, wikilinks, target_map, now):
    from wiki_indexer import WikiIndexer

    idx = WikiIndexer.__new__(WikiIndexer)  # method uses no instance state
    with db.cursor() as c:
        return idx._index_wikilinks(c, alpha, wikilinks, target_map, now)


def _index_vault(db, alpha, wikilinks, target_map, now):
    from afm_passes.vault_ingest import _insert_wikilinks

    with db.cursor() as c:
        return _insert_wikilinks(c, alpha, wikilinks, target_map, now)


def _run_battery(tmp_path, index_fn):
    db = _make_db(tmp_path)
    alpha, beta, gamma, target_map = _setup(db)

    # First index: two links, both stamped with now=1000.
    n1 = index_fn(db, alpha, ["beta", "gamma"], target_map, 1000.0)
    assert n1 == 2
    with db.cursor() as c:
        links = _links(c, alpha)
    assert len(links) == 2  # PR94-3: not double-counted
    assert {row["created_at"] for row in links} == {1000.0}

    # Re-index with a LATER timestamp and same links: created_at must be PRESERVED.
    index_fn(db, alpha, ["beta", "gamma"], target_map, 2000.0)
    with db.cursor() as c:
        links = _links(c, alpha)
    assert len(links) == 2  # still one row per link
    assert {row["created_at"] for row in links} == {1000.0}, links  # PR94-1

    # Drop the gamma link: it should be pruned, beta's created_at preserved.
    index_fn(db, alpha, ["beta"], target_map, 3000.0)
    with db.cursor() as c:
        links = _links(c, alpha)
    assert len(links) == 1
    assert links[0]["target"] == beta
    assert links[0]["created_at"] == 1000.0


def test_wiki_indexer_preserves_created_at_and_no_double_count(tmp_path):
    _run_battery(tmp_path, _index_wiki)


def test_vault_ingest_preserves_created_at_and_no_double_count(tmp_path):
    _run_battery(tmp_path, _index_vault)


def test_diff_prune_scoped_to_wikilinks_preserves_other_edges(tmp_path):
    """A re-index must not nuke non-wikilink edges (e.g. derived_from)."""
    db = _make_db(tmp_path)
    alpha, beta, gamma, target_map = _setup(db)

    _index_vault(db, alpha, ["beta"], target_map, 1000.0)
    # A different edge type from another subsystem (writeback.add_derived_from_edges).
    with db.cursor() as c:
        c.execute(
            "INSERT INTO memory_links (source_doc_id, target_doc_id, link_type, weight, created_at) "
            "VALUES (?, ?, 'derived_from', 1.0, 500.0)",
            (alpha, gamma),
        )

    # Re-index wikilinks: the derived_from edge must survive untouched.
    _index_vault(db, alpha, ["beta"], target_map, 2000.0)
    with db.cursor() as c:
        links = _links(c, alpha)
    by_type = {row["link_type"]: row for row in links}
    assert "derived_from" in by_type
    assert by_type["derived_from"]["target"] == gamma
    assert by_type["derived_from"]["created_at"] == 500.0
    assert by_type["wikilink"]["created_at"] == 1000.0

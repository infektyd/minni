import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))


def _make_db(tmp_path, *, reorg_horizon_days=30):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
        reorg_horizon_days=reorg_horizon_days,
        afm_loop_schedule={
            "enabled": True,
            "idle_seconds": 300,
            "passes": {
                "reorganization": {"interval_seconds": 86400},
                "pruning": {"interval_seconds": 86400},
            },
        },
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _write_page(
    vault,
    rel,
    title,
    body,
    *,
    page_type="concept",
    status="accepted",
    tags=None,
    sources=None,
    updated=None,
    extra_frontmatter=None,
):
    if updated is None:
        updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5 * 86400))
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tags = tags or []
    sources = sources or [f"test:{title.lower().replace(' ', '-')}"]
    frontmatter = {
        "title": title,
        "type": page_type,
        "status": status,
        "privacy": "safe",
        "updated": updated,
        "tags": f"[{', '.join(tags)}]",
        "sources": f"[{', '.join(sources)}]",
    }
    frontmatter.update(extra_frontmatter or {})
    text = "---\n" + "\n".join(f"{key}: {value}" for key, value in frontmatter.items()) + "\n---\n\n"
    text += f"# {title}\n\n{body}\n"
    path.write_text(text, encoding="utf-8")
    return path


def _index_doc(db_obj, rel, *, status="accepted", page_type="concept", decay_score=1.0, access_count=0, expires_at=None):
    now = time.time()
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT INTO documents
            (path, agent, sigil, last_modified, indexed_at, access_count, decay_score,
             page_status, privacy_level, page_type, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (rel, "test", "T", now, now, access_count, decay_score, status, "safe", page_type, expires_at),
        )
        return c.lastrowid


def _link_docs(db_obj, source, target, *, link_type="wikilink", weight=1.0):
    with db_obj.cursor() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO memory_links
            (source_doc_id, target_doc_id, link_type, weight, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (source, target, link_type, weight, time.time()),
        )


def test_reorganization_dry_run_returns_split_merge_and_orphan_proposals(tmp_path):
    from afm_passes.reorganization import run

    db_obj, cfg = _make_db(tmp_path)
    vault = tmp_path / "vault"
    entity_rel = "wiki/entities/memory-system.md"
    entity = _write_page(
        vault,
        entity_rel,
        "Memory System",
        "Coordinates [[Decay]], [[Retrieval]], [[Vault Hygiene]], and [[AFM Loop]].",
        page_type="entity",
        tags=["memory"],
    )
    entity_id = _index_doc(db_obj, entity_rel, page_type="entity")
    for name in ("Decay", "Retrieval", "Vault Hygiene", "AFM Loop"):
        rel = f"wiki/concepts/{name.lower().replace(' ', '-')}.md"
        _write_page(vault, rel, name, f"{name} is a distinct concern.", tags=["memory"], sources=[f"test:{name}"])
        _link_docs(db_obj, entity_id, _index_doc(db_obj, rel), link_type="related")

    _write_page(
        vault,
        "wiki/concepts/ttl.md",
        "TTL",
        "TTL lifecycle expiry review protects memory freshness. [[Memory System]]",
        tags=["lifecycle", "memory"],
    )
    _write_page(
        vault,
        "wiki/concepts/time-to-live.md",
        "Time To Live",
        "TTL lifecycle expiry review protects memory freshness. [[Memory System]]",
        tags=["lifecycle", "memory"],
    )
    _write_page(vault, "wiki/concepts/lonely.md", "Lonely", "No graph edges touch this page.")
    _write_page(vault, "wiki/concepts/listed.md", "Listed", "No graph edges, but index.md names it.")
    (vault / "index.md").write_text("# Index\n\n- [[Listed]]\n", encoding="utf-8")
    before = entity.read_text(encoding="utf-8")

    result = run(db_obj, cfg, vault_path=str(vault), dry_run=True, trace_id="trace-reorg")

    kinds = {draft["proposal_type"] for draft in result["drafts"]}
    orphan_paths = {source.removeprefix("vault:") for draft in result["drafts"] for source in draft["sources"] if draft["proposal_type"] == "rehome_or_archive"}
    assert result["status"] == "ok"
    assert result["pass_name"] == "reorganization"
    assert {"split", "merge", "rehome_or_archive"} <= kinds
    assert all(draft["status"] == "draft" and draft["agent"] == "afm-loop" for draft in result["drafts"])
    assert all("requires_endorsement" in draft["lifecycle"] for draft in result["drafts"])
    assert "wiki/concepts/lonely.md" in orphan_paths
    assert "wiki/concepts/listed.md" not in orphan_paths
    assert entity.read_text(encoding="utf-8") == before


def test_reorganization_respects_horizon_but_includes_recent_neighbors(tmp_path):
    from afm_passes.reorganization import run

    db_obj, cfg = _make_db(tmp_path, reorg_horizon_days=1)
    vault = tmp_path / "vault"
    recent = _write_page(
        vault,
        "wiki/entities/recent-hub.md",
        "Recent Hub",
        "Recent page links to [[Old Neighbor]].",
        page_type="entity",
        updated=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    old_neighbor = _write_page(
        vault,
        "wiki/concepts/old-neighbor.md",
        "Old Neighbor",
        "Older page is eligible only because it neighbors the recent hub.",
        updated="2026-01-01T00:00:00Z",
    )
    stale_orphan = _write_page(
        vault,
        "wiki/concepts/stale-orphan.md",
        "Stale Orphan",
        "This old orphan should be outside the incremental horizon.",
        updated="2026-01-01T00:00:00Z",
    )
    result = run(db_obj, cfg, vault_path=str(vault), dry_run=True, trace_id="trace-horizon")

    processed = set(result["inputs"]["processed_pages"])
    assert str(recent.relative_to(vault)) in processed
    assert str(old_neighbor.relative_to(vault)) in processed
    assert str(stale_orphan.relative_to(vault)) not in processed


def test_pruning_dry_run_and_wet_run_write_only_inbox_proposals(tmp_path):
    from afm_passes.pruning import run

    db_obj, cfg = _make_db(tmp_path)
    vault = tmp_path / "vault"
    expired_path = _write_page(vault, "wiki/concepts/expired.md", "Expired", "Past TTL.")
    fm_expired_path = _write_page(
        vault,
        "wiki/concepts/frontmatter-expired.md",
        "Frontmatter Expired",
        "Past TTL from frontmatter.",
        extra_frontmatter={"expires_at": "2020-01-01T00:00:00Z"},
    )
    superseded_path = _write_page(
        vault,
        "wiki/concepts/superseded-evidence.md",
        "Superseded Evidence",
        "Evidence is old.",
        extra_frontmatter={"superseded_by": "[[wiki/concepts/new-evidence]]"},
    )
    hygiene_path = _write_page(
        vault,
        "wiki/concepts/hygiene.md",
        "Hygiene",
        "Accepted but decayed and never accessed.",
    )
    past = time.time() - 86400
    _index_doc(db_obj, "wiki/concepts/expired.md", expires_at=past)
    _index_doc(db_obj, "wiki/concepts/frontmatter-expired.md")
    _index_doc(db_obj, "wiki/concepts/superseded-evidence.md")
    _index_doc(db_obj, "wiki/concepts/hygiene.md", decay_score=0.01, access_count=0)
    before = {path: path.read_text(encoding="utf-8") for path in (expired_path, fm_expired_path, superseded_path, hygiene_path)}

    dry = run(db_obj, cfg, vault_path=str(vault), dry_run=True, trace_id="trace-prune")
    assert dry["status"] == "ok"
    assert {proposal["proposal_type"] for proposal in dry["proposals"]} >= {
        "status_transition",
        "hygiene_finding",
    }
    assert any(p["from_status"] == "accepted" and p["to_status"] == "expired" for p in dry["proposals"])
    assert any(p["path"] == "wiki/concepts/frontmatter-expired.md" and p["to_status"] == "expired" for p in dry["proposals"])
    assert not (vault / "inbox").exists()

    wet = run(db_obj, cfg, vault_path=str(vault), dry_run=False, trace_id="trace-prune")
    inbox_path = vault / wet["inbox_written"]["path"]
    payload = json.loads(inbox_path.read_text(encoding="utf-8"))
    assert inbox_path.name.startswith("afm-pruning-")
    assert payload["runs"][0]["trace_id"] == "trace-prune"
    assert payload["runs"][0]["proposals"]
    assert all(item["status"] == "draft" and item["agent"] == "afm-loop" for item in payload["runs"][0]["proposals"])
    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text


def test_daemon_compile_routes_reorg_and_pruning_with_traces(tmp_path, monkeypatch):
    import minnid

    db_obj, cfg = _make_db(tmp_path)
    vault = tmp_path / "vault"
    entity_path = _write_page(
        vault,
        "wiki/entities/compile-hub.md",
        "Compile Hub",
        "Compile hub references [[One]], [[Two]], [[Three]], and [[Four]].",
        page_type="entity",
    )
    entity_id = _index_doc(db_obj, "wiki/entities/compile-hub.md", page_type="entity")
    for name in ("One", "Two", "Three", "Four"):
        rel = f"wiki/concepts/{name.lower()}.md"
        _write_page(vault, rel, name, f"{name} evidence.")
        _link_docs(db_obj, entity_id, _index_doc(db_obj, rel))
    expired_path = _write_page(vault, "wiki/concepts/compile-expired.md", "Compile Expired", "Old accepted page.")
    _index_doc(db_obj, "wiki/concepts/compile-expired.md", expires_at=time.time() - 10)
    before = {
        entity_path: entity_path.read_text(encoding="utf-8"),
        expired_path: expired_path.read_text(encoding="utf-8"),
    }
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)
    monkeypatch.setattr(minnid, "_writeback", None)
    monkeypatch.setattr(minnid, "SovereignDB", lambda config=None: db_obj)

    reorg_resp = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "daemon.compile",
        "params": {"pass_name": "reorganization", "vault_path": str(vault), "dry_run": False},
    })
    pruning_resp = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "daemon.compile",
        "params": {"pass_name": "pruning", "vault_path": str(vault), "dry_run": False},
    })

    assert "error" not in reorg_resp
    assert reorg_resp["result"]["status"] == "ok"
    assert reorg_resp["result"]["drafts_written"]
    draft_path = vault / reorg_resp["result"]["drafts_written"][0]["path"]
    draft_text = draft_path.read_text(encoding="utf-8")
    assert "status: draft" in draft_text
    assert "agent: afm-loop" in draft_text
    reorg_trace = minnid._dispatch_sync({"jsonrpc": "2.0", "id": 3, "method": "trace", "params": {"trace_id": reorg_resp["result"]["trace_id"]}})
    assert reorg_trace["result"]["trace"]["pass_name"] == "reorganization"

    assert "error" not in pruning_resp
    assert pruning_resp["result"]["status"] == "ok"
    assert pruning_resp["result"]["inbox_written"]["path"].startswith("inbox/afm-pruning-")
    pruning_trace = minnid._dispatch_sync({"jsonrpc": "2.0", "id": 4, "method": "trace", "params": {"trace_id": pruning_resp["result"]["trace_id"]}})
    assert pruning_trace["result"]["trace"]["pass_name"] == "pruning"
    assert pruning_trace["result"]["trace"]["output"]["proposal_count"] >= 1

    for path, text in before.items():
        assert path.read_text(encoding="utf-8") == text
    audit = (vault / "log.md").read_text(encoding="utf-8")
    assert "reorganization wrote" in audit
    assert "pruning proposed" in audit

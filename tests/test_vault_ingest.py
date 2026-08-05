"""Tests for the vault_ingest AFM pass.

The pass must gather from vault wiki markdown into a per-vault index store only.
The source vault markdown is never rewritten.
"""

from __future__ import annotations

import json
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


def _make_shared_db(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "shared" / "minni.db"),
        vault_path=str(tmp_path / "shared-vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "shared.faiss"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def _long_body(marker: str, *, link: str = "") -> str:
    words = " ".join(f"{marker}-{i}" for i in range(90))
    return f"{link}\n\n## Notes\n\n{words}\n"


def _write_page(path: Path, title: str, body: str, privacy: str = "safe") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {title}",
                "type: concept",
                "status: accepted",
                f"privacy: {privacy}",
                "---",
                "",
                body,
            ]
        ),
        encoding="utf-8",
    )


def _doc_rows(index_db: Path):
    with sqlite3.connect(index_db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT path, agent, page_type FROM documents ORDER BY path")]


def _count_shared_documents(db_obj) -> int:
    with db_obj.cursor() as c:
        c.execute("SELECT COUNT(*) FROM documents")
        return c.fetchone()[0]


def _wiki_snapshot(vault: Path) -> dict[str, bytes]:
    wiki = vault / "wiki"
    return {
        str(path.relative_to(wiki)): path.read_bytes()
        for path in sorted(wiki.rglob("*.md"))
    }


def test_collect_markdown_skips_files_that_vanish_during_walk(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import _collect_markdown

    wiki = tmp_path / "codex-vault" / "wiki"
    _write_page(wiki / "keep.md", "Keep", _long_body("keep"))
    missing = wiki / "gone.md"
    _write_page(missing, "Gone", _long_body("gone"))

    original_stat = Path.stat

    def flaky_stat(self, *args, **kwargs):
        if self.name == "gone.md":
            raise OSError("file vanished")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    result = _collect_markdown(wiki)

    assert str(wiki / "keep.md") in result
    assert str(missing) not in result


def test_count_plan_handles_legacy_null_indexed_at():
    from minni.afm_passes.vault_ingest import _count_plan

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE documents (path TEXT, indexed_at REAL)")
    conn.execute("INSERT INTO documents(path, indexed_at) VALUES ('/vault/wiki/stale.md', NULL)")
    row = conn.execute("SELECT path, indexed_at FROM documents").fetchone()

    would_index, skipped, would_prune = _count_plan(
        {"/vault/wiki/stale.md": 100.0},
        {row["path"]: row},
    )

    assert (would_index, skipped, would_prune) == (1, 0, 0)


def test_vault_ingest_indexes_wiki_into_per_vault_store_only(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import run
    from minni.vault_index import vault_index_paths

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    _write_page(vault / "wiki" / "alpha.md", "Alpha", _long_body("alpha", link="[[beta]]"))
    _write_page(vault / "wiki" / "beta.md", "Beta", _long_body("beta"))

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False, trace_id="trace-test")

    paths = vault_index_paths(vault)
    assert result["status"] == "ok"
    assert result["drafts"] == []
    assert result["agent_id"] == "codex"
    assert result["files_seen"] == 2
    assert result["indexed"] == 2
    assert result["index_db_path"] == str(paths.db_path)
    assert paths.db_path.exists()
    assert paths.faiss_manifest_path.exists()
    assert paths.faiss_index_path.exists() or Path(str(paths.faiss_index_path) + ".npz").exists()

    rows = _doc_rows(paths.db_path)
    assert [row["agent"] for row in rows] == ["codex", "codex"]
    assert {row["page_type"] for row in rows} == {"concept"}
    assert _count_shared_documents(shared_db) == 0

    with sqlite3.connect(paths.db_path) as conn:
        link_count = conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0]
    assert link_count == 1
    assert chunk_count > 0


def test_vault_ingest_wet_run_never_modifies_wiki_markdown(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    _write_page(vault / "wiki" / "alpha.md", "Alpha", _long_body("alpha", link="[[beta]]"))
    _write_page(vault / "wiki" / "beta.md", "Beta", _long_body("beta"))
    before = _wiki_snapshot(vault)

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert result["status"] == "ok"
    assert _wiki_snapshot(vault) == before


def test_vault_ingest_incremental_reindexes_and_prunes_index_rows_only(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import run
    from minni.vault_index import vault_index_paths

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    alpha = vault / "wiki" / "alpha.md"
    beta = vault / "wiki" / "beta.md"
    _write_page(alpha, "Alpha", _long_body("alpha"))
    _write_page(beta, "Beta", _long_body("beta"))

    first = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    second = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert first["indexed"] == 2
    assert second["indexed"] == 0
    assert second["skipped_unchanged"] == 2

    before_alpha = alpha.read_bytes()
    _write_page(alpha, "Alpha", _long_body("alpha-changed"))
    future = time.time() + 5
    os.utime(alpha, (future, future))
    changed = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert changed["indexed"] >= 1
    assert alpha.read_bytes() == before_alpha.replace(b"alpha-", b"alpha-changed-")

    beta.unlink()
    pruned = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert pruned["pruned"] == 1
    assert not beta.exists()

    paths = vault_index_paths(vault)
    rows = _doc_rows(paths.db_path)
    assert [Path(row["path"]).name for row in rows] == ["alpha.md"]


def test_vault_ingest_isolates_two_vault_stores_and_shared_db(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import run
    from minni.vault_index import vault_index_paths

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    codex_vault = tmp_path / "codex-vault"
    claude_vault = tmp_path / "claudecode-vault"
    _write_page(codex_vault / "wiki" / "codex.md", "Codex", _long_body("codex-only"))
    _write_page(claude_vault / "wiki" / "claude.md", "Claude", _long_body("claude-only"))

    codex = run(shared_db, cfg, vault_path=str(codex_vault), dry_run=False)
    claude = run(shared_db, cfg, vault_path=str(claude_vault), dry_run=False)

    codex_db = vault_index_paths(codex_vault).db_path
    claude_db = vault_index_paths(claude_vault).db_path
    assert codex["index_db_path"] == str(codex_db)
    assert claude["index_db_path"] == str(claude_db)
    assert codex_db != claude_db

    codex_rows = _doc_rows(codex_db)
    claude_rows = _doc_rows(claude_db)
    assert [Path(row["path"]).name for row in codex_rows] == ["codex.md"]
    assert [row["agent"] for row in codex_rows] == ["codex"]
    assert [Path(row["path"]).name for row in claude_rows] == ["claude.md"]
    assert [row["agent"] for row in claude_rows] == ["claude-code"]
    assert _count_shared_documents(shared_db) == 0


def test_vault_ingest_purges_index_rows_when_page_becomes_blocked(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import run
    from minni.vault_index import vault_index_paths

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    alpha = vault / "wiki" / "alpha.md"
    _write_page(alpha, "Alpha", _long_body("alpha"))

    first = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert first["indexed"] == 1

    _write_page(alpha, "Alpha", _long_body("alpha"), privacy="blocked")
    future = time.time() + 5
    os.utime(alpha, (future, future))
    second = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert second["indexed"] == 0
    assert second["pruned"] == 1
    db_path = vault_index_paths(vault).db_path
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM vault_fts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM chunk_embeddings").fetchone()[0] == 0
    assert alpha.exists()

    third = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert third["indexed"] == 0
    assert third["pruned"] == 0


def test_vault_ingest_dry_run_writes_nothing(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    page = vault / "wiki" / "alpha.md"
    _write_page(page, "Alpha", _long_body("alpha"))
    before = page.read_bytes()

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=True)

    assert result["dry_run"] is True
    assert result["would_index"] == 1
    assert not (vault / ".index").exists()
    assert page.read_bytes() == before
    assert _count_shared_documents(shared_db) == 0


def test_vault_ingest_unknown_vault_slug_skips_without_index_store(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "mystery-vault"
    _write_page(vault / "wiki" / "alpha.md", "Alpha", _long_body("alpha"))

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert result["status"] == "skipped"
    assert result["reason"] == "unknown_vault_slug"
    assert result["agent_id"] is None
    assert not (vault / ".index").exists()


def test_daemon_compile_registers_vault_ingest_dry_run(tmp_path, monkeypatch):
    import minni.minnid as minnid
    import minni.principal as principal_mod
    from minni.config import SovereignConfig

    _install_fake_embedder(monkeypatch)
    vault = tmp_path / "codex-vault"
    _write_page(vault / "wiki" / "alpha.md", "Alpha", _long_body("alpha"))

    principals = tmp_path / "principals"
    principals.mkdir()
    principal_file = principals / "main.json"
    principal_file.write_text(
        json.dumps(
            {
                "agent_id": "main",
                "capabilities": ["*"],
                "allowed_vault_roots": [str(tmp_path)],
            }
        ),
        encoding="utf-8",
    )
    os.chmod(principal_file, 0o600)
    monkeypatch.setattr(principal_mod, "PRINCIPALS_DIR", principals)

    cfg = SovereignConfig(
        db_path=str(tmp_path / "shared" / "minni.db"),
        vault_path=str(vault),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "shared.faiss"),
        writeback_enabled=False,
        afm_loop_schedule={"enabled": True, "idle_seconds": 300, "passes": {}},
    )
    monkeypatch.setattr(minnid, "DEFAULT_CONFIG", cfg)

    resp = minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "daemon.compile",
            "params": {
                "pass_name": "vault_ingest",
                "vault_path": str(vault),
                "dry_run": True,
            },
        }
    )

    assert "error" not in resp
    assert resp["result"]["status"] == "ok"
    assert resp["result"]["dry_run"] is True
    assert resp["result"]["would_index"] == 1


def test_vault_slug_map_covers_every_authored_agent_vault():
    """Every agent with an authored vault must have an ingest slug.

    A slug missing from _VAULT_SLUG_TO_AGENT_ID does not raise -- vault_ingest
    logs a warning and returns status="skipped". That silence is the hazard:
    `cursor` was declared in AGENT_VAULT_DIRS but omitted from the slug map, so
    cursor-vault accumulated 141 wiki pages that recall could never return, and
    nothing failed to signal it.
    """
    from minni.afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID
    from minni.tools.author_principals import AGENT_VAULT_DIRS

    missing = {}
    for agent_id, vault_dir in AGENT_VAULT_DIRS.items():
        slug = vault_dir[: -len("-vault")] if vault_dir.endswith("-vault") else vault_dir
        if slug not in _VAULT_SLUG_TO_AGENT_ID:
            missing[slug] = agent_id
    assert not missing, (
        "vault slugs missing from _VAULT_SLUG_TO_AGENT_ID (their vaults will be "
        f"silently skipped by vault_ingest): {missing}"
    )


def test_vault_slug_map_resolves_to_the_authored_agent_id():
    """A present-but-wrong mapping would index a vault under a foreign agent,
    where the same-agent read gate then hides it from its real owner."""
    from minni.afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID
    from minni.tools.author_principals import AGENT_VAULT_DIRS

    mismatched = {}
    for agent_id, vault_dir in AGENT_VAULT_DIRS.items():
        slug = vault_dir[: -len("-vault")] if vault_dir.endswith("-vault") else vault_dir
        mapped = _VAULT_SLUG_TO_AGENT_ID.get(slug)
        if mapped is not None and mapped != agent_id:
            mismatched[slug] = (mapped, agent_id)
    assert not mismatched, f"slug -> agent_id disagrees with AGENT_VAULT_DIRS: {mismatched}"


def _parse_python_slug_map(path: Path) -> dict:
    """Read a module-level `_VAULT_SLUG_TO_AGENT_ID` literal without importing.

    `scripts/inbox_cleanup.py` is a standalone script by contract, so it must be
    read rather than imported.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "_VAULT_SLUG_TO_AGENT_ID":
                return ast.literal_eval(node.value)
    raise AssertionError(f"no _VAULT_SLUG_TO_AGENT_ID literal found in {path}")


def _parse_ts_slug_map(path: Path) -> dict:
    """Read the `VAULT_SLUG_TO_AGENT_ID` object literal out of the TS mirror."""
    import re

    source = path.read_text(encoding="utf-8")
    match = re.search(
        r"VAULT_SLUG_TO_AGENT_ID[^=]*=\s*\{(.*?)\n\};", source, re.DOTALL
    )
    assert match, f"no VAULT_SLUG_TO_AGENT_ID object literal found in {path}"
    pairs = re.findall(
        r'(?:"([^"]+)"|([A-Za-z_][\w-]*))\s*:\s*"([^"]+)"', match.group(1)
    )
    return {quoted or bare: value for quoted, bare, value in pairs}


def test_all_three_vault_slug_maps_agree():
    """The canonical slug map and BOTH mirrors must carry identical entries.

    `inbox_ingest._VAULT_SLUG_TO_AGENT_ID` is duplicated into a TypeScript
    mirror (hook-utils.ts) and a standalone-script mirror (inbox_cleanup.py).
    The other slug tests import only the canonical map, so a mirror that falls
    behind is invisible to them.

    Both mirrors fall back to the slug itself (`?? slug` in TS, `.get(slug,
    slug)` in Python), so a missing entry is harmless for an IDENTITY mapping
    and silently wrong for a non-identity one -- `claudecode` -> `claude-code`
    and `grok` -> `grok-build` would be stamped with the bare dir name. The
    canonical map has no such fallback: `vault_ingest` skips an unknown slug
    outright, which is how cursor-vault accumulated 141 unreachable wiki pages
    before `cursor` was added there. The mirrors then kept lacking `cursor`
    long after -- benign in that instance only because the mapping is identity.

    Compared as dicts, so ordering and formatting differences are fine and only
    a real content difference fails.

    All three are parsed from source rather than imported: `minni` is installed
    editable, so an import resolves to whatever checkout the install points at
    -- which is NOT this one when the tests run from a git worktree. Reading the
    files under this test's own repo root keeps the comparison hermetic.
    """
    root = Path(__file__).resolve().parents[1]
    canonical = _parse_python_slug_map(
        root / "src" / "minni" / "afm_passes" / "inbox_ingest.py"
    )
    mirrors = {
        "scripts/inbox_cleanup.py": _parse_python_slug_map(
            root / "scripts" / "inbox_cleanup.py"
        ),
        "plugins/minni/src/hook-utils.ts": _parse_ts_slug_map(
            root / "plugins" / "minni" / "src" / "hook-utils.ts"
        ),
    }

    drifted = {}
    for name, mirror in mirrors.items():
        missing = {k: v for k, v in canonical.items() if mirror.get(k) != v}
        extra = {k: v for k, v in mirror.items() if k not in canonical}
        if missing or extra:
            drifted[name] = {"missing_or_wrong": missing, "extra": extra}

    assert not drifted, (
        "vault slug map mirrors have drifted from "
        f"inbox_ingest._VAULT_SLUG_TO_AGENT_ID: {drifted}"
    )


def test_default_agent_vault_matches_agent_vault_dirs():
    """agent_id -> vault PATH must agree with the declared vault dir.

    The slug maps cover vault dir -> agent_id. `default_agent_vault` is the
    INVERSE, and it is what personal recall and handoff resolution use. It
    slugifies unknown ids by stripping non-alphanumerics, so a hyphenated id
    that is not in its alias table resolves to a vault that does not exist --
    `claude-science` silently became `claudescience-vault` while ingest, which
    is path-based, indexed the real directory. The failure is quiet on both
    sides: ingest works, recall just never finds anything.
    """
    import subprocess

    # Run against THIS tree's src: `minni` is installed editable, so a plain
    # import resolves to whatever checkout the install points at -- the main
    # one, not this worktree. Same hazard as the slug-agreement test above.
    root = Path(__file__).resolve().parents[1]
    probe = (
        "import json;"
        "from minni.minnid_runtime.handoff import default_agent_vault;"
        "from minni.tools.author_principals import AGENT_VAULT_DIRS;"
        "print(json.dumps({a: [d, default_agent_vault(a).name]"
        " for a, d in AGENT_VAULT_DIRS.items()}))"
    )
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, env=env, cwd=str(root), check=True,
    ).stdout
    resolved_by_agent = json.loads(out)

    mismatched = {
        agent_id: {"expected": declared, "resolved": resolved}
        for agent_id, (declared, resolved) in resolved_by_agent.items()
        if declared != resolved
    }

    assert not mismatched, (
        "default_agent_vault disagrees with AGENT_VAULT_DIRS -- personal recall "
        f"and handoffs will miss these vaults: {mismatched}"
    )


# ── M6 (#229): intentional exclusions are not indexing errors ───────────────
#
# plan.ts writes plan pages with the terminal, deliberately NON-recallable
# status "complete" (H6: a model-completed plan must not self-promote into
# recallable memory). The Python contract never learned that value, so every
# plan page was rejected AND counted as an error — an error signal that can
# never return to zero carries no information about real errors.


def _write_status_page(path: Path, title: str, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            ["---", f"title: {title}", "type: artifact", f"status: {status}",
             "privacy: safe", "---", "", _long_body(title)],
        ),
        encoding="utf-8",
    )


def test_complete_status_is_excluded_by_design_not_an_error(tmp_path, monkeypatch):
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    _write_page(vault / "wiki" / "ok.md", "Ok", _long_body("ok"))
    _write_status_page(vault / "wiki" / "artifacts" / "plan-abc.md", "Plan", "complete")

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert result["errors"] == 0, "an intentional exclusion is not an error"
    assert result["rejected"] == 0, "an intentional exclusion is not a rejection"
    assert result["excluded"] == 1


def test_completed_plan_pages_stay_out_of_the_index(tmp_path, monkeypatch):
    """`complete` is not in retrieval's skip_statuses, so an indexed plan page
    would be recallable — exactly the self-promotion H6 forbids."""
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    _write_status_page(vault / "wiki" / "artifacts" / "plan-abc.md", "Plan", "complete")

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    index_db = Path(result["index_db_path"])
    paths = [r["path"] for r in _doc_rows(index_db)] if index_db.exists() else []
    assert not any("plan-abc" in p for p in paths)


def test_completing_an_indexed_plan_retracts_it_from_recall(tmp_path, monkeypatch):
    """accepted -> complete is the NORMAL plan lifecycle. Skipping without
    retracting leaves the old row indexed at `accepted`, which is in neither
    skip_statuses nor UNEMBEDDED_STATUSES — a finished plan that stays
    recallable forever."""
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    page = vault / "wiki" / "artifacts" / "plan-abc.md"

    _write_status_page(page, "Plan", "accepted")
    first = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    assert first["indexed"] == 1
    index_db = Path(first["index_db_path"])
    assert any("plan-abc" in r["path"] for r in _doc_rows(index_db))

    time.sleep(0.01)
    _write_status_page(page, "Plan", "complete")
    second = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert second["excluded"] == 1
    assert second["excluded_purged"] == 1
    # The row survives (doc_id identity + wikilinks); the searchable payload
    # does not, and recall filters the status.
    with sqlite3.connect(index_db) as conn:
        fts = conn.execute(
            "SELECT COUNT(*) FROM vault_fts WHERE path LIKE '%plan-abc%'"
        ).fetchone()[0]
    assert fts == 0


def test_genuinely_invalid_frontmatter_is_still_rejected(tmp_path, monkeypatch):
    """The fix must not blanket-excuse bad frontmatter: an unknown status is
    still a rejection, it just is not an 'error'."""
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    _write_status_page(vault / "wiki" / "junk.md", "Junk", "banana")

    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert result["rejected"] == 1
    assert result["excluded"] == 0
    assert result["errors"] == 0


def test_index_wiki_excludes_complete_without_growing_log_md(tmp_path, monkeypatch):
    """The OTHER indexing path (WikiIndexer.index_wiki, used by the watcher and
    index_all). A rejected page never gets a documents row, so the mtime skip
    never fires and every run re-appended a REJECTED block to wiki/log.md —
    unbounded growth, and log.md is itself watched, so the write re-triggered
    indexing."""
    from minni.wiki_indexer import WikiIndexer

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    _write_status_page(vault / "wiki" / "plan-abc.md", "Plan", "complete")
    _write_page(vault / "wiki" / "ok.md", "Ok", _long_body("ok"))

    indexer = WikiIndexer(shared_db, cfg)
    for _ in range(3):
        stats = indexer.index_wiki(str(vault / "wiki"))

    assert stats["errors"] == 0
    assert stats["rejected"] == 0
    assert stats["excluded"] == 1

    log_path = vault / "wiki" / "log.md"
    rejected_lines = (
        log_path.read_text(encoding="utf-8").count("REJECTED") if log_path.exists() else 0
    )
    assert rejected_lines == 0, "an intentional exclusion must not write a REJECTED block"


def test_hygiene_accepts_the_terminal_plan_status(tmp_path):
    """hygiene.py had drifted from the indexer contract, so every completed
    plan raised a permanent block-severity 'Unknown status' finding on the
    health surface."""
    from minni.hygiene import run_hygiene_report

    vault = tmp_path / "codex-vault"
    _write_status_page(vault / "wiki" / "plan-a.md", "Plan A", "complete")
    (vault / "wiki" / "plan-a.md").write_text(
        "\n".join(["---", "title: Plan A", "type: artifact", "status: complete",
                   "privacy: safe", "sources: [x]", "---", "", "body"]),
        encoding="utf-8",
    )

    report = run_hygiene_report(vault)
    unknown = [
        f for f in report["findings"]["block"]
        if "Unknown status" in f.get("message", "")
    ]
    assert unknown == []


def test_excluding_preserves_doc_identity_and_wikilinks(tmp_path, monkeypatch):
    """Retract the payload, not the row. Deleting the documents row CASCADEs
    memory_links away and mints a new doc_id on the way back — and
    accepted -> complete -> accepted is a normal plan round trip, so every
    inbound wikilink and stored doc_id reference would dangle permanently."""
    from minni.afm_passes.vault_ingest import run

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    plan = vault / "wiki" / "plan-abc.md"
    _write_status_page(plan, "Plan", "accepted")
    _write_page(vault / "wiki" / "note.md", "Note", _long_body("note", link="[[plan-abc]]"))

    first = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    index_db = Path(first["index_db_path"])

    def _doc_id(path_fragment):
        with sqlite3.connect(index_db) as conn:
            conn.row_factory = sqlite3.Row
            for r in conn.execute("SELECT doc_id, path FROM documents"):
                if path_fragment in r["path"]:
                    return r["doc_id"]
        return None

    def _links():
        with sqlite3.connect(index_db) as conn:
            return conn.execute("SELECT COUNT(*) FROM memory_links").fetchone()[0]

    plan_id = _doc_id("plan-abc")
    assert plan_id is not None
    assert _links() >= 1, "fixture must produce a wikilink edge"

    time.sleep(0.01)
    _write_status_page(plan, "Plan", "complete")
    second = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    assert second["excluded_purged"] == 1
    assert _doc_id("plan-abc") == plan_id, "doc_id identity must survive exclusion"
    assert _links() >= 1, "memory_links must not be CASCADE-deleted"


def test_excluded_page_is_not_recallable_even_though_its_row_survives(tmp_path, monkeypatch):
    """Keeping the row moves the H6 guarantee to recall, so recall has to
    actually filter it."""
    from minni.afm_passes.vault_ingest import run
    from minni.wiki_indexer import WikiFrontmatter

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    plan = vault / "wiki" / "plan-abc.md"
    _write_status_page(plan, "Plan", "accepted")
    run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    time.sleep(0.01)
    _write_status_page(plan, "Plan", "complete")
    result = run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    with sqlite3.connect(Path(result["index_db_path"])) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT page_status FROM documents WHERE path LIKE '%plan-abc%'"
        ).fetchone()
        fts = conn.execute(
            "SELECT COUNT(*) FROM vault_fts WHERE path LIKE '%plan-abc%'"
        ).fetchone()[0]

    assert row["page_status"] in WikiFrontmatter.EXCLUDED_STATUSES
    assert fts == 0, "the searchable payload must be gone"


def test_index_wiki_retracts_and_does_not_conflate(tmp_path, monkeypatch):
    """The WikiIndexer path needs its own pins: the retraction and the
    rejected/errors split were only covered in the vault_ingest mirror."""
    from minni.wiki_indexer import WikiIndexer

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    plan = vault / "wiki" / "plan-abc.md"
    _write_status_page(plan, "Plan", "accepted")
    _write_status_page(vault / "wiki" / "junk.md", "Junk", "banana")

    indexer = WikiIndexer(shared_db, cfg)
    indexer.index_wiki(str(vault / "wiki"))

    with shared_db.cursor() as c:
        c.execute("SELECT doc_id FROM documents WHERE path LIKE '%plan-abc%'")
        before = c.fetchone()[0]

    time.sleep(0.01)
    _write_status_page(plan, "Plan", "complete")
    stats = indexer.index_wiki(str(vault / "wiki"))

    assert stats["excluded"] == 1
    assert stats["excluded_purged"] == 1
    assert stats["rejected"] == 1, "the malformed page is still refused"
    assert stats["errors"] == 0, "a refusal is not an error"

    with shared_db.cursor() as c:
        c.execute("SELECT doc_id, page_status FROM documents WHERE path LIKE '%plan-abc%'")
        row = c.fetchone()
        c.execute("SELECT COUNT(*) FROM vault_fts WHERE doc_id = ?", (before,))
        fts = c.fetchone()[0]

    assert row[0] == before, "doc_id identity must survive"
    assert row[1] == "complete"
    assert fts == 0


def test_excluded_statuses_are_deliberately_unembedded():
    """Contract pin: indexer.UNEMBEDDED_STATUSES spells 'complete' out because
    wiki_indexer imports indexer and the reverse would be circular. A retracted
    page counted as a MISSING vector manufactures a permanent gap no backfill
    can close."""
    from minni.indexer import UNEMBEDDED_STATUSES
    from minni.wiki_indexer import WikiFrontmatter

    assert WikiFrontmatter.EXCLUDED_STATUSES <= UNEMBEDDED_STATUSES


def test_excluded_page_does_not_read_as_a_repairable_vector_gap(tmp_path, monkeypatch):
    """The retracted row has no embedding by design, so embedding_coverage must
    not report it as missing/unrecoverable forever."""
    from minni.afm_passes.vault_ingest import run
    from minni.backfill import embedding_coverage

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    plan = vault / "wiki" / "plan-abc.md"
    _write_status_page(plan, "Plan", "accepted")
    first = run(shared_db, cfg, vault_path=str(vault), dry_run=False)
    time.sleep(0.01)
    _write_status_page(plan, "Plan", "complete")
    run(shared_db, cfg, vault_path=str(vault), dry_run=False)

    import minni.db as db_mod
    from minni.config import SovereignConfig

    index_cfg = SovereignConfig(
        db_path=first["index_db_path"],
        vault_path=str(vault),
        graph_export_dir=str(tmp_path / "g"),
        faiss_index_path=str(tmp_path / "f.faiss"),
        writeback_enabled=False,
    )
    old = db_mod._migrations_run
    db_mod._migrations_run = True
    try:
        idx_db = db_mod.SovereignDB(index_cfg)
        cov = embedding_coverage(idx_db)
    finally:
        db_mod._migrations_run = old
    assert cov["documents_missing_vectors"] == 0, cov


def test_index_wiki_retraction_settles_instead_of_repurging(tmp_path, monkeypatch):
    """Stamping page_status without advancing last_modified left the skip gate
    open, so the same page was re-purged on every run and excluded_purged never
    returned to zero — the never-returns-to-zero signal this PR removes."""
    from minni.wiki_indexer import WikiIndexer

    _install_fake_embedder(monkeypatch)
    shared_db, cfg = _make_shared_db(tmp_path)
    vault = tmp_path / "codex-vault"
    plan = vault / "wiki" / "plan-abc.md"
    _write_status_page(plan, "Plan", "accepted")

    indexer = WikiIndexer(shared_db, cfg)
    indexer.index_wiki(str(vault / "wiki"))
    time.sleep(0.01)
    _write_status_page(plan, "Plan", "complete")

    first = indexer.index_wiki(str(vault / "wiki"))
    second = indexer.index_wiki(str(vault / "wiki"))

    assert first["excluded_purged"] == 1
    assert second["excluded_purged"] == 0, "the retraction must settle"

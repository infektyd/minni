"""Post-Wave-6 Engine, SQLite Schema, Recall/Graph/Consolidation & Doctor Audit Suite.

Covers BUG-57 through BUG-64 against real MinniDB and runtime entrypoints:
- BUG-57: `cmd_doctor` H36 sibling parity (`running_stale_servers` warns `rc=0`, never fails fleet).
- BUG-58: `RetrievalEngine.expand_result` & `sm_drill` repo-map / path-qualified wikilinks.
- BUG-59: `handle_read` privacy/lifecycle filtering & workspace-pinned principal reads.
- BUG-60: Cross-workspace active learning preservation in consolidation & near-duplicate checks.
- BUG-61: `repair_learning_projection` `workspace_id` preservation & edge-classifier metadata guards.
- BUG-62: Fresh `MinniDB` retains `trg_thread_msg_count` after migrations (`010_standardize_threads_pk.sql`).
- BUG-63: `workspace_id` normalization across ingest/distill/governance/AFM/CLI/`TODAY.md`,
          `VaultIndexer` & `WikiIndexer` non-empty `workspace_id` preservation, and global graph shortlist parity.
- BUG-64: `handle_await_handoff` caller authorization, `compile_handoff_page` YAML/fence sanitization,
          and `store_handoff_lease` `contradicts_id` conflict update.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import yaml

import minni.minni_cli as minni_cli
import minni.today_digest as today_digest
from minni.afm_passes import compact_distillation, consolidation, inbox_ingest
from minni.config import MinniConfig
from minni.db import MinniDB
from minni.durable_projection import find_near_duplicate_active_learning
from minni.edge_classifier import EdgeClassifier
from minni.graph_commit import ensure_canonical_learning_node
from minni.graph_coordinator import LearningFields, repair_learning_projection
from minni.indexer import VaultIndexer
from minni.minnid_runtime import afm as afm_rt
from minni.minnid_runtime import governance as gov_rt
from minni.minnid_runtime import handoff as handoff_rt
from minni.minnid_runtime import recall as recall_rt
from minni.minnid_runtime.graph_adapters import (
    GraphRuntimeAdapter,
    apply_fallback_graph_edges,
    canonical_store_id,
    prepare_governed_promotion,
)
from minni.principal import EffectivePrincipal, can_read_document
from minni.retrieval import RetrievalEngine
from minni.wiki_indexer import WikiIndexer
from minni.wire.health import HostPropagationHealthReport
from minni.writeback import WriteBackMemory


def _make_cfg(tmp_path: Path) -> MinniConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    wb_dir = tmp_path / "learnings"
    wb_dir.mkdir(parents=True, exist_ok=True)
    graph_dir = tmp_path / "graphs"
    graph_dir.mkdir(parents=True, exist_ok=True)
    return MinniConfig(
        db_path=tmp_path / "minni.db",
        vault_path=vault_dir,
        faiss_index_path=tmp_path / "minni_faiss.index",
        writeback_path=wb_dir,
        graph_export_dir=graph_dir,
    )


def _rpc_ok(result: Any, req_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_err(code: int, message: str, req_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


# ---------------------------------------------------------------------------
# BUG-57: cmd_doctor H36 Sibling Parity (running_stale_servers -> WARN rc=0)
# ---------------------------------------------------------------------------


def test_bug57_cmd_doctor_running_stale_mcp_warns_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_home = tmp_path / "home"
    current_dir = fake_home / ".minni" / "plugin" / "releases" / "1.0.0-abc12345"
    current_dir.mkdir(parents=True)
    current_link = fake_home / ".minni" / "plugin" / "current"
    current_link.symlink_to(current_dir)

    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    run_dir.chmod(0o700)
    sock = run_dir / "minnid.sock"
    sock.write_text("", encoding="utf-8")
    sock.chmod(0o600)

    stale_srv = {
        "pid": 42424,
        "host": "claude-code",
        "path": str(fake_home / ".minni" / "plugin" / "releases" / "0.9.9-oldsha" / "dist" / "server.js"),
        "version": "0.9.9",
        "git_sha": "oldsha00",
        "backing_exists": True,
        "stale": True,
        "reasons": ["sha_mismatch:oldsha00!=abc12345"],
    }

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(minni_cli, "_daemon_alive", lambda _s: True)
    monkeypatch.setattr(minni_cli, "_models_present", lambda: (list(minni_cli.EXPECTED_MODELS), []))

    def _fake_rpc(_sock: Path, method: str, params: dict | None = None, **_kw: Any) -> dict[str, Any]:
        if method == "status":
            return {
                "engine": {
                    "documents": 12,
                    "chunks": 30,
                    "active_learnings": 5,
                    "proposed_candidates": 0,
                    "faiss": {"ready": True, "vectors": 30},
                },
                "daemon": {
                    "latencies": {"search": {"p50": 12.0}},
                    "deploy": {
                        "stale": False,
                        "plugin_dist": {"stale": False},
                        "running_stale_servers": [stale_srv],
                    },
                },
            }
        if method == "search":
            return {"results": [{"doc_id": 1}]}
        raise AssertionError(f"Unexpected RPC {method}")

    monkeypatch.setattr(minni_cli, "_rpc", _fake_rpc)
    monkeypatch.setattr(
        "minni.minnid_runtime.deploy_honesty.deploy_status",
        lambda: {
            "stale": False,
            "plugin_dist": {"stale": False},
            "running_stale_servers": [stale_srv],
        },
    )
    monkeypatch.setattr(
        "minni.wire.health.audit_host_propagation_health",
        lambda home=None: HostPropagationHealthReport(
            ok=True,
            current_valid=True,
            current_version="1.0.0",
            current_git_sha="abc12345",
            current_error=None,
            drifted_surfaces=[],
            uncatalogued_refs=[],
            running_stale_servers=[stale_srv],
        ),
    )

    rc = minni_cli.cmd_doctor(SimpleNamespace(socket=sock))
    out = capsys.readouterr().out
    assert rc == 0, f"cmd_doctor should exit 0 on running_stale_servers alone, got rc={rc}:\n{out}"
    assert "[WARN] fleet: restart these host apps: claude-code" in out
    assert "42424" in out
    assert "[FAIL] fleet" not in out
    assert "Fix: minni sync" not in out

    # Hard drift (`deploy.stale=True`) MUST still fail with rc=1 and `[FAIL] fleet`
    def _stale_rpc(_sock: Path, method: str, params: dict | None = None, **_kw: Any) -> dict[str, Any]:
        if method == "status":
            return {
                "engine": {"documents": 12, "chunks": 30},
                "daemon": {
                    "deploy": {
                        "stale": True,
                        "reason": "daemon started from older commit",
                        "plugin_dist": {"stale": False},
                    }
                },
            }
        return {"results": []}

    monkeypatch.setattr(minni_cli, "_rpc", _stale_rpc)
    rc_fail = minni_cli.cmd_doctor(SimpleNamespace(socket=sock))
    out_fail = capsys.readouterr().out
    assert rc_fail == 1
    assert "[FAIL] fleet" in out_fail


# ---------------------------------------------------------------------------
# BUG-58: minni_drill Repo-Map Cards & Path-Qualified Wikilinks
# ---------------------------------------------------------------------------


def test_bug58_sm_drill_repo_map_and_path_qualified_wikilinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    cfg = _make_cfg(tmp_path)
    maps_dir = cfg.vault_path / "maps" / "minni"
    maps_dir.mkdir(parents=True, exist_ok=True)
    card_file = maps_dir / "memory-graph.md"
    card_body = "# Memory Graph Subsystem\n\nOwns graph_coordinator.py and edge_classifier.py.\n"
    card_file.write_text(card_body, encoding="utf-8")

    db = MinniDB(cfg)
    try:
        now = time.time()
        with db.transaction() as c:
            cur = c.execute(
                """INSERT INTO documents
                   (path, agent, sigil, last_modified, indexed_at, page_status,
                    privacy_level, page_type, workspace_id)
                   VALUES (?, 'repo-map', '#map', ?, ?, 'accepted', 'safe', 'repo_map', 'workspace-minni')""",
                (str(card_file.resolve()), now, now),
            )
            doc_id = int(cur.lastrowid)
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, 'repo-map', '#map')",
                (doc_id, str(card_file.resolve()), card_body),
            )

        engine = RetrievalEngine(db, cfg)
        codex_vault = tmp_path / "codex-vault"
        codex_vault.mkdir()
        principal = EffectivePrincipal(
            agent_id="codex",
            workspace_id="default",
            capabilities=["search", "read", "recall"],
            allowed_vault_roots=[str(codex_vault)],
        )

        expanded = engine.expand_result(
            result_id=doc_id,
            depth="document",
            id_kind="doc",
            principal=principal,
            workspace="*",
        )
        assert expanded is not None
        assert expanded["wikilink"] == "[[maps/minni/memory-graph]]"

        ctx = recall_rt.RecallContext(
            make_error=_rpc_err,
            make_response=_rpc_ok,
            handler_principal=lambda _p, _r: (principal, None),
            lazy_retrieval=lambda: engine,
            agent_vault_retrieval=lambda _a: None,
            all_vault_retrievals=lambda: [],
            trace_ring=lambda: [],
            record_latency=lambda _m, _d: None,
            default_config=cfg,
            can_read_document=can_read_document,
            logger=SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None, exception=lambda *a, **k: None),
        )

        for token in (
            "maps/minni/memory-graph.md",
            "maps/minni/memory-graph",
            "[[maps/minni/memory-graph]]",
        ):
            resp = recall_rt.handle_sm_drill(
                {"references": [{"wikilink": token}], "depth": "document"},
                1,
                ctx,
            )
            assert "result" in resp, f"Drill error for {token!r}: {resp}"
            assert resp["result"]["count"] == 1, f"Expected count=1 for {token!r}, got {resp}"
            item = resp["result"]["results"][0]
            assert item["wikilink"] == "[[maps/minni/memory-graph]]"
            assert "Memory Graph Subsystem" in item["text"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# BUG-59: handle_read Privacy/Lifecycle Filtering & Workspace-Pinned Principals
# ---------------------------------------------------------------------------


def test_bug59_handle_read_enforces_privacy_lifecycle_and_pinned_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    cfg = _make_cfg(tmp_path)
    db = MinniDB(cfg)
    try:
        now = time.time()
        docs_dir = cfg.vault_path / "notes"
        docs_dir.mkdir(parents=True, exist_ok=True)

        def _seed_doc(
            name: str,
            *,
            agent: str,
            privacy: str = "safe",
            status: str = "accepted",
            superseded_by: int | None = None,
            workspace_id: str = "default",
            page_type: str = "wiki",
            body: str,
        ) -> int:
            p = docs_dir / name
            p.write_text(body, encoding="utf-8")
            with db.transaction() as c:
                cur = c.execute(
                    """INSERT INTO documents
                       (path, agent, sigil, access_count, decay_score, last_modified, indexed_at,
                        privacy_level, page_status, superseded_by, workspace_id, page_type)
                       VALUES (?, ?, '#n', 5, 1.0, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(p.resolve()), agent, now, now, privacy, status, superseded_by, workspace_id, page_type),
                )
                did = int(cur.lastrowid)
                c.execute(
                    """INSERT INTO chunk_embeddings
                       (doc_id, chunk_index, chunk_text, embedding, heading_context, model_name, computed_at)
                       VALUES (?, 0, ?, ?, '', 'test', ?)""",
                    (did, body, np.zeros(8, dtype=np.float32).tobytes(), now),
                )
                return did

        _seed_doc("blocked.md", agent="codex", privacy="blocked", workspace_id="workspace-minni", body="TOP_SECRET_BLOCKED_DOC")
        _seed_doc("secret.md", agent="codex", privacy="secret", workspace_id="workspace-minni", body="TOP_SECRET_CREDENTIAL_DOC")
        _seed_doc("rejected.md", agent="codex", status="rejected", workspace_id="workspace-minni", body="REJECTED_STALE_DOC")
        _seed_doc("superseded.md", agent="codex", status="accepted", superseded_by=999, workspace_id="workspace-minni", body="SUPERSEDED_OLD_DOC")
        _seed_doc("foreign_priv.md", agent="wiki:ops", privacy="private", workspace_id="default", body="FOREIGN_PRIVATE_WIKI_DOC")
        _seed_doc("codex_good.md", agent="codex", privacy="safe", status="accepted", workspace_id="workspace-minni", body="CODEX_ACTIVE_SAFE_DOC")

        # Workspace-pinned `hermes` docs (including shared `unknown` doc in `workspace-hermes` vs foreign `workspace-praxis`)
        _seed_doc("hermes_own.md", agent="hermes", privacy="safe", status="accepted", workspace_id="workspace-hermes", body="HERMES_OWN_WORKSPACE_DOC")
        _seed_doc("hermes_shared.md", agent="unknown", privacy="safe", status="accepted", workspace_id="workspace-hermes", body="HERMES_SHARED_WORKSPACE_DOC")
        _seed_doc("hermes_foreign_ws.md", agent="hermes", privacy="safe", status="accepted", workspace_id="workspace-praxis", body="HERMES_OTHER_REPO_DOC")

        engine = RetrievalEngine(db, cfg)
        wb = WriteBackMemory(db, cfg)

        def _run_read(principal: EffectivePrincipal) -> str:
            ctx = recall_rt.RecallContext(
                make_error=_rpc_err,
                make_response=_rpc_ok,
                handler_principal=lambda _p, _r: (principal, None),
                lazy_retrieval=lambda: engine,
                agent_vault_retrieval=lambda _a: None,
                all_vault_retrievals=lambda: [],
                trace_ring=lambda: [],
                record_latency=lambda _m, _d: None,
                sovereign_db=lambda: MinniDB(cfg),
                default_config=cfg,
                can_read_document=can_read_document,
                logger=SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None, exception=lambda *a, **k: None),
            )
            resp = recall_rt.handle_read({"agent_id": principal.agent_id, "limit": 20}, 1, ctx)
            assert "result" in resp, resp
            return str(resp["result"]["context"])

        codex_principal = EffectivePrincipal(
            agent_id="codex",
            workspace_id="default",
            capabilities=["read", "recall"],
            allowed_vault_roots=[str(cfg.vault_path.resolve())],
        )
        codex_text = _run_read(codex_principal)
        assert "codex_good.md" in codex_text
        assert "blocked.md" not in codex_text
        assert "secret.md" not in codex_text
        assert "rejected.md" not in codex_text
        assert "superseded.md" not in codex_text
        assert "foreign_priv.md" not in codex_text

        hermes_principal = EffectivePrincipal(
            agent_id="hermes",
            workspace_id="workspace-hermes",
            capabilities=["read", "recall"],
            allowed_vault_roots=[str(cfg.vault_path.resolve())],
        )
        hermes_text = _run_read(hermes_principal)
        assert "hermes_own.md" in hermes_text
        assert "hermes_shared.md" in hermes_text
        assert "hermes_foreign_ws.md" not in hermes_text
    finally:
        db.close()


# ---------------------------------------------------------------------------
# BUG-60: Cross-Workspace Active Learning Preservation in Consolidation
# ---------------------------------------------------------------------------


def test_bug60_consolidation_dedup_and_hash_lookup_preserve_cross_workspace_learnings(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    db = MinniDB(cfg)
    try:
        now = time.time()
        exact_rule = "Always wrap verification test commands in an outer timeout guard before running."
        fuzzy_minni = "Always run database schema migrations inside an explicit transaction block on startup."
        fuzzy_praxis = "Always run database schema migrations inside an explicit transaction block at startup."
        h_exact = consolidation.content_hash(exact_rule)

        consolidation._ensure_dedup_index(db)
        with db.transaction() as c:
            cur1 = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, content_hash, workspace_id)
                   VALUES ('codex', 'procedure', ?, 0.9, ?, 'active', ?, 'workspace-minni')""",
                (exact_rule, now - 50, h_exact),
            )
            lid_exact_minni = int(cur1.lastrowid)
            cur2 = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, content_hash, workspace_id)
                   VALUES ('codex', 'procedure', ?, 0.9, ?, 'active', ?, 'workspace-praxis')""",
                (exact_rule, now - 40, h_exact),
            )
            lid_exact_praxis = int(cur2.lastrowid)
            cur3 = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, content_hash, workspace_id)
                   VALUES ('codex', 'architecture', ?, 0.9, ?, 'active', ?, 'workspace-minni')""",
                (fuzzy_minni, now - 30, consolidation.content_hash(fuzzy_minni)),
            )
            lid_fuzzy_minni = int(cur3.lastrowid)
            cur4 = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, content_hash, workspace_id)
                   VALUES ('codex', 'architecture', ?, 0.9, ?, 'active', ?, 'workspace-praxis')""",
                (fuzzy_praxis, now - 20, consolidation.content_hash(fuzzy_praxis)),
            )
            lid_fuzzy_praxis = int(cur4.lastrowid)

        # Cross-workspace hash & near-duplicate checks must NOT collide across distinct concrete workspaces
        with db.cursor() as c:
            r_minni = consolidation.find_active_learning_by_hash(
                c, h_exact, "codex", workspace_id="workspace-minni"
            )
            r_praxis = consolidation.find_active_learning_by_hash(
                c, h_exact, "codex", workspace_id="workspace-praxis"
            )
            r_other = consolidation.find_active_learning_by_hash(
                c, h_exact, "codex", workspace_id="workspace-other"
            )
        assert r_minni is not None and int(r_minni["learning_id"]) == lid_exact_minni
        assert r_praxis is not None and int(r_praxis["learning_id"]) == lid_exact_praxis
        assert r_other is None
        assert (
            find_near_duplicate_active_learning(
                db,
                agent_id="codex",
                category="architecture",
                content=fuzzy_minni,
                exclude_learning_id=lid_fuzzy_minni,
                workspace_id="workspace-minni",
            )
            == []
        )

        # `dedup_active_learnings` must NOT delete cross-workspace rules!
        res_cross = consolidation.dedup_active_learnings(db)
        assert res_cross["superseded_duplicates"] == 0
        with db.cursor() as c:
            surviving = {
                int(r["learning_id"])
                for r in c.execute("SELECT learning_id FROM learnings WHERE status = 'active'").fetchall()
            }
        assert surviving == {lid_exact_minni, lid_exact_praxis, lid_fuzzy_minni, lid_fuzzy_praxis}

        # Same-workspace duplicate MUST still be deduplicated
        with db.transaction() as c:
            c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, content_hash, workspace_id)
                   VALUES ('codex', 'procedure', ?, 0.9, ?, 'active', ?, 'workspace-minni')""",
                (exact_rule, now - 10, h_exact),
            )
        res_same = consolidation.dedup_active_learnings(db)
        assert res_same["superseded_duplicates"] == 1

        # Proposed candidates with identical rule text in two different workspaces must BOTH promote
        shared_cand = "Stage explicit file paths in git commits instead of wildcard staging."
        with db.transaction() as c:
            c1 = c.execute(
                """INSERT INTO candidate_packets
                   (principal, workspace_id, category, content, privacy_level, status, proposed_at)
                   VALUES ('codex', 'workspace-alpha', 'procedure', ?, 'safe', 'proposed', ?)""",
                (shared_cand, now),
            ).lastrowid
            c2 = c.execute(
                """INSERT INTO candidate_packets
                   (principal, workspace_id, category, content, privacy_level, status, proposed_at)
                   VALUES ('codex', 'workspace-beta', 'procedure', ?, 'safe', 'proposed', ?)""",
                (shared_cand, now + 1),
            ).lastrowid

        pass_out = consolidation.run(db=db, config=cfg)
        assert int(c1) in pass_out["promote_candidate_ids"]
        assert int(c2) in pass_out["promote_candidate_ids"]
        assert pass_out["dedup_candidate_ids"] == []

        # An existing `'global'` active learning DOES deduplicate repo-level candidates of the same hash
        global_rule = "Never commit secret credentials or session tokens to public repositories."
        h_global = consolidation.content_hash(global_rule)
        with db.transaction() as c:
            cur_g = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, content_hash, workspace_id)
                   VALUES ('codex', 'procedure', ?, 1.0, ?, 'active', ?, 'global')""",
                (global_rule, now, h_global),
            )
            lid_global = int(cur_g.lastrowid)
            c3 = c.execute(
                """INSERT INTO candidate_packets
                   (principal, workspace_id, category, content, privacy_level, status, proposed_at)
                   VALUES ('codex', 'workspace-alpha', 'procedure', ?, 'safe', 'proposed', ?)""",
                (global_rule, now + 2),
            ).lastrowid
        with db.cursor() as c:
            r_glob = consolidation.find_active_learning_by_hash(
                c, h_global, "codex", workspace_id="workspace-alpha"
            )
        assert r_glob is not None and int(r_glob["learning_id"]) == lid_global
        pass_global = consolidation.run(db=db, config=cfg)
        assert int(c3) in pass_global["dedup_candidate_ids"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# BUG-61: repair_learning_projection Preserves workspace_id & Guards Edges
# ---------------------------------------------------------------------------


def test_bug61_repair_learning_projection_preserves_workspace_and_guards_edges(
    tmp_path: Path,
) -> None:
    cfg = _make_cfg(tmp_path)
    db = MinniDB(cfg)
    try:
        now = time.time()
        store_id = canonical_store_id(db)
        vec = np.ones(8, dtype=np.float32)
        vec /= np.linalg.norm(vec)
        vec_bytes = vec.tobytes()

        old_text = "Always set internal service connection pool timeout to 10 seconds."
        new_text = "Supersedes prior timeout rule: always set internal service connection pool timeout to 30 seconds."

        with db.transaction() as c:
            # Learning #1: owned by `claude-code` in `workspace-praxis` (`decision`)
            cur1 = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, workspace_id)
                   VALUES ('claude-code', 'decision', ?, 1.0, ?, 'active', 'workspace-praxis')""",
                (old_text, now - 20),
            )
            lid1 = int(cur1.lastrowid)
            doc1 = ensure_canonical_learning_node(
                c,
                learning_id=lid1,
                agent_id="claude-code",
                content=old_text,
                vault_path=str(cfg.vault_path),
                created_at=now - 20,
                workspace_id="workspace-praxis",
            )
            assert doc1 is not None
            path1 = c.execute("SELECT path FROM documents WHERE doc_id = ?", (doc1,)).fetchone()["path"]
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, 'claude-code', '#l')",
                (doc1, path1, old_text),
            )
            ch1 = int(
                c.execute(
                    """INSERT INTO chunk_embeddings
                       (doc_id, chunk_index, chunk_text, embedding, heading_context, model_name, computed_at, layer)
                       VALUES (?, 0, ?, ?, '', 'test-emb', ?, 'knowledge')""",
                    (doc1, old_text, vec_bytes, now - 20),
                ).lastrowid
            )

            # Learning #2: owned by `codex` in `workspace-minni` (`procedure`) with NO `documents` projection yet
            cur2 = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, workspace_id)
                   VALUES ('codex', 'procedure', ?, 1.0, ?, 'active', 'Minni')""",
                (new_text, now - 10),
            )
            lid2 = int(cur2.lastrowid)

        operator_principal = EffectivePrincipal(
            agent_id="main",
            workspace_id="*",
            capabilities=["*"],
            allowed_vault_roots=[str(cfg.vault_path.resolve())],
        )

        def _get_meta(sid: str, did: int) -> dict[str, Any] | None:
            with db.cursor() as c:
                r = c.execute(
                    """SELECT d.doc_id, d.path, d.agent, d.privacy_level, d.page_status,
                              d.page_type, d.memory_kind, d.workspace_id, l.category, f.content
                       FROM documents d
                       JOIN learning_documents ld ON ld.doc_id = d.doc_id
                       JOIN learnings l ON l.learning_id = ld.learning_id
                       JOIN vault_fts f ON f.doc_id = d.doc_id
                       WHERE d.doc_id = ?""",
                    (did,),
                ).fetchone()
                if r is None:
                    return None
                import hashlib

                return {
                    "store_id": sid,
                    "doc_id": int(r["doc_id"]),
                    "path": r["path"],
                    "agent": r["agent"],
                    "privacy_level": r["privacy_level"],
                    "page_status": r["page_status"],
                    "page_type": r["page_type"],
                    "memory_kind": r["memory_kind"],
                    "title": r["path"],
                    "workspace_id": r["workspace_id"],
                    "category": r["category"],
                    "content_sha256": hashlib.sha256(str(r["content"]).encode("utf-8")).hexdigest(),
                }

        def _get_content(sid: str, did: int) -> dict[str, Any] | None:
            with db.cursor() as c:
                r = c.execute("SELECT content FROM vault_fts WHERE doc_id = ?", (did,)).fetchone()
                return {"store_id": sid, "doc_id": did, "content": r["content"]} if r else None

        classifier = EdgeClassifier(provider_chain=[], allow_heuristic_fallback=True)
        rep = repair_learning_projection(
            db=db,
            learning_id=lid2,
            store_id=store_id,
            vault_path=str(cfg.vault_path),
            embedding_model="test-emb",
            embed_text=lambda _t: vec_bytes,
            chunk_texts=lambda t: [t],
            principal=operator_principal,
            search_chunks=lambda _v, _k: [{"store_id": store_id, "doc_id": int(doc1), "chunk_id": ch1, "cosine": 0.96}],
            get_metadata=_get_meta,
            get_content=_get_content,
            classify=classifier.classify,
            graph_enabled=True,
            workspace="*",
        )
        assert rep.status == "complete", rep
        assert rep.doc_id is not None

        with db.cursor() as c:
            doc2_row = c.execute(
                "SELECT workspace_id FROM documents WHERE doc_id = ?", (rep.doc_id,)
            ).fetchone()
            assert doc2_row is not None
            assert doc2_row["workspace_id"] == "workspace-minni"

            # Learning #1 in `workspace-praxis` (`claude-code`, `decision`) MUST NOT get an `updates` edge or supersession!
            l1_row = c.execute(
                "SELECT status, superseded_by FROM learnings WHERE learning_id = ?", (lid1,)
            ).fetchone()
            assert l1_row["status"] == "active"
            assert l1_row["superseded_by"] is None
            bad_edges = c.execute(
                "SELECT * FROM memory_links WHERE source_doc_id = ? AND target_doc_id = ?",
                (rep.doc_id, doc1),
            ).fetchall()
            assert bad_edges == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# BUG-62: Fresh MinniDB Preserves trg_thread_msg_count After Migrations
# ---------------------------------------------------------------------------


def test_bug62_fresh_minni_db_preserves_trg_thread_msg_count_trigger(tmp_path: Path) -> None:
    cfg = _make_cfg(tmp_path)
    db = MinniDB(cfg)
    try:
        now = time.time()
        with db.transaction() as c:
            trig = c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name='trg_thread_msg_count'"
            ).fetchone()
            assert trig is not None, "Fresh MinniDB dropped trg_thread_msg_count during run_migrations!"

            c.execute(
                """INSERT INTO threads (thread_id, title, created_at, updated_at, message_count)
                   VALUES ('t-fresh-1', 'Fresh Thread', ?, ?, 0)""",
                (now, now),
            )
            c.execute(
                """INSERT INTO episodic_events (agent_id, event_type, content, created_at, thread_id)
                   VALUES ('codex', 'note', 'Bound message', ?, 't-fresh-1')""",
                (now + 5.0,),
            )
            c.execute(
                """INSERT INTO episodic_events (agent_id, event_type, content, created_at, thread_id)
                   VALUES ('codex', 'note', 'Unbound message', ?, NULL)""",
                (now + 6.0,),
            )
            t_row = c.execute(
                "SELECT message_count, updated_at FROM threads WHERE thread_id = 't-fresh-1'"
            ).fetchone()
            assert int(t_row["message_count"]) == 1
            assert float(t_row["updated_at"]) == pytest.approx(now + 5.0)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# BUG-63: workspace_id Normalization, Indexer Preservation & Global Graph Shortlist
# ---------------------------------------------------------------------------


def test_bug63_workspace_normalization_indexer_preservation_and_global_shortlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))

    # 1. CLI dotted workspace parity
    assert minni_cli._normalize_cli_workspace("my.repo") == "workspace-my.repo"
    assert minni_cli._normalize_cli_workspace("/opt/work/my.repo/") == "workspace-my.repo"

    # 2. TODAY.md path-shaped & bare-slug canonicalization
    assert today_digest.normalize_workspace_id("/opt/work/Projects/Praxis") == "workspace-praxis"
    assert today_digest.human_project_label("/opt/work/Projects/Praxis") == "Praxis"
    assert today_digest.normalize_workspace_id("minni") == "workspace-minni"
    assert today_digest.normalize_workspace_id("workspace-minni") == "workspace-minni"
    assert today_digest.human_project_label("minni") == "Minni"
    assert today_digest.human_project_label("workspace-minni") == "Minni"

    cfg = _make_cfg(tmp_path)
    db = MinniDB(cfg)
    try:
        # 3. inbox_ingest & compact_distillation workspace normalization
        inbox_dir = tmp_path / "codex-vault" / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / "cand1.json").write_text(
            json.dumps(
                {
                    "kind": "stop_candidates",
                    "agent_id": "codex",
                    "workspace_id": "/opt/work/Projects/Minni",
                    "candidates": ["Normalize workspace scope across all inbox candidate packets."],
                }
            ),
            encoding="utf-8",
        )
        ing_res = inbox_ingest.ingest(db, cfg, inboxes=[inbox_dir])
        assert ing_res["inserted"] == 1

        (inbox_dir / "compact1.json").write_text(
            json.dumps(
                {
                    "kind": "compact_summary",
                    "agent_id": "codex",
                    "workspace_id": "/opt/work/Projects/Praxis",
                    "summary_text": (
                        "1. Primary Request and Intent:\n"
                        "   Audit workspace scope handling across distillation.\n"
                        "2. Key Technical Concepts:\n"
                        "   MinniDB caches one sqlite connection per thread and normalizes workspace identifiers before staging.\n"
                    ),
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr("minni.afm_passes.compact_distillation.resolve_afm_mode", lambda: "off")
        dist_res = compact_distillation.distill(db, cfg, inboxes=[inbox_dir])
        assert dist_res["inserted"] >= 1

        with db.cursor() as c:
            ws_vals = [
                r["workspace_id"]
                for r in c.execute("SELECT workspace_id FROM candidate_packets ORDER BY candidate_id").fetchall()
            ]
        assert "workspace-minni" in ws_vals
        assert "workspace-praxis" in ws_vals

        # 4. governance.resolve_candidate & afm.promote_candidate_durable normalize raw `candidate_packets.workspace_id`
        wb = WriteBackMemory(db, cfg)
        wb._model = SimpleNamespace(
            encode=lambda _t, show_progress_bar=False: np.ones(8, dtype=np.float32)
        )
        op_principal = EffectivePrincipal(
            agent_id="main",
            workspace_id="default",
            capabilities=["*", "resolve_candidate", "govern"],
            allowed_vault_roots=[str(cfg.vault_path.resolve())],
        )
        gov_ctx = gov_rt.GovernanceContext(
            make_error=_rpc_err,
            make_response=_rpc_ok,
            handler_principal=lambda _p, _r: (op_principal, None),
            lazy_writeback=lambda: wb,
            lazy_episodic=lambda: None,
            record_latency=lambda _m, _d: None,
            index_durable_learning=lambda *a, **k: True,
            maybe_archive_inbox_source=lambda _db, _cid: None,
            sovereign_db=lambda: MinniDB(cfg),
            logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None, exception=lambda *a, **k: None),
        )
        with db.transaction() as c:
            raw_cid_gov = int(
                c.execute(
                    """INSERT INTO candidate_packets
                       (principal, workspace_id, category, content, privacy_level, status, proposed_at)
                       VALUES ('codex', '/opt/work/Projects/Minni', 'decision',
                               'Governed candidate promotion normalizes legacy path workspace ids.', 'safe', 'proposed', ?)""",
                    (time.time(),),
                ).lastrowid
            )
            raw_cid_afm = int(
                c.execute(
                    """INSERT INTO candidate_packets
                       (principal, workspace_id, category, content, privacy_level, status, proposed_at)
                       VALUES ('codex', 'Praxis', 'decision',
                               'AFM durable promotion normalizes bare project workspace slugs.', 'safe', 'proposed', ?)""",
                    (time.time(),),
                ).lastrowid
            )

        gov_resp = gov_rt.resolve_candidate(
            {"candidate_id": raw_cid_gov, "decision": "accept"}, 1, gov_ctx
        )
        assert "result" in gov_resp, gov_resp
        lid_gov = int(gov_resp["result"]["learning_id"])

        afm_ctx = afm_rt.AFMContext(
            make_error=_rpc_err,
            make_response=_rpc_ok,
            guard_vault_root=lambda *a, **k: None,
            lazy_writeback=lambda: wb,
            trace_ring=lambda: [],
            record_latency=lambda _m, _d: None,
            maybe_archive_inbox_source=lambda _db, _cid: None,
            index_durable_learning=lambda *a, **k: True,
            default_config=cfg,
            logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None, exception=lambda *a, **k: None),
        )
        lid_afm = afm_rt.promote_candidate_durable(raw_cid_afm, "unit-test", afm_ctx)
        assert lid_afm is not None

        with db.cursor() as c:
            ws_gov = c.execute("SELECT workspace_id FROM learnings WHERE learning_id = ?", (lid_gov,)).fetchone()["workspace_id"]
            ws_afm = c.execute("SELECT workspace_id FROM learnings WHERE learning_id = ?", (lid_afm,)).fetchone()["workspace_id"]
        assert ws_gov == "workspace-minni"
        assert ws_afm == "workspace-praxis"

        # 5. VaultIndexer & WikiIndexer extract frontmatter workspace_id AND preserve existing non-empty workspace_id
        vault_note = cfg.vault_path / "kb" / "preserve-ws.md"
        vault_note.parent.mkdir(parents=True, exist_ok=True)
        vault_note.write_text(
            "---\ntitle: Preserve Vault WS\nworkspace_id: Minni\nstatus: accepted\n---\n\nInitial vault body.\n",
            encoding="utf-8",
        )
        v_indexer = VaultIndexer(db, cfg, embed=False)
        v_indexer.index_vault()
        with db.cursor() as c:
            assert (
                c.execute("SELECT workspace_id FROM documents WHERE path = ?", (str(vault_note),)).fetchone()["workspace_id"]
                == "workspace-minni"
            )

        # Rewrite WITHOUT workspace_id in frontmatter and re-index via both `index_vault` and `index_file`
        vault_note.write_text(
            "---\ntitle: Preserve Vault WS\nstatus: accepted\n---\n\nUpdated vault body without workspace_id.\n",
            encoding="utf-8",
        )
        v_mtime = vault_note.stat().st_mtime + 10.0
        os.utime(vault_note, (v_mtime, v_mtime))
        v_indexer.index_vault()
        v_indexer.index_file(str(vault_note))
        with db.cursor() as c:
            assert (
                c.execute("SELECT workspace_id FROM documents WHERE path = ?", (str(vault_note),)).fetchone()["workspace_id"]
                == "workspace-minni"
            )

        wiki_dir = cfg.vault_path / "wiki" / "concepts"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        wiki_note = wiki_dir / "preserve-wiki-ws.md"
        wiki_note.write_text(
            "---\ntitle: Preserve Wiki WS\npage_type: concept\nstatus: accepted\nworkspace_id: /opt/work/Praxis\n---\n\nWiki concept body.\n",
            encoding="utf-8",
        )
        w_indexer = WikiIndexer(db, cfg, embed=False)
        w_indexer.index_wiki(str(cfg.vault_path / "wiki"))
        with db.cursor() as c:
            assert (
                c.execute("SELECT workspace_id FROM documents WHERE path = ?", (str(wiki_note),)).fetchone()["workspace_id"]
                == "workspace-praxis"
            )

        wiki_note.write_text(
            "---\ntitle: Preserve Wiki WS\npage_type: concept\nstatus: accepted\n---\n\nUpdated wiki concept body omitting workspace_id.\n",
            encoding="utf-8",
        )
        w_mtime = wiki_note.stat().st_mtime + 10.0
        os.utime(wiki_note, (w_mtime, w_mtime))
        w_indexer.index_wiki(str(cfg.vault_path / "wiki"))
        with db.cursor() as c:
            assert (
                c.execute("SELECT workspace_id FROM documents WHERE path = ?", (str(wiki_note),)).fetchone()["workspace_id"]
                == "workspace-praxis"
            )

        # 6. Global graph shortlist parity: unpinned caller promoting `'global'` learning sees same-agent `'workspace-minni'`
        #    learnings, while repo-pinned `hermes` (`workspace-hermes`) CANNOT read `'workspace-minni'` learnings!
        repo_rule = "Always enable HTTP keepalive connection pooling on internal gateway clients."
        global_opposing = "Never enable HTTP keepalive connection pooling on internal gateway clients."
        with db.transaction() as c:
            cur_r = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, workspace_id)
                   VALUES ('claude-code', 'fact', ?, 1.0, ?, 'active', 'workspace-minni')""",
                (repo_rule, time.time()),
            )
            lid_repo = int(cur_r.lastrowid)
            doc_repo = ensure_canonical_learning_node(
                c,
                learning_id=lid_repo,
                agent_id="claude-code",
                content=repo_rule,
                vault_path=str(cfg.vault_path),
                created_at=time.time(),
                workspace_id="workspace-minni",
            )
            assert doc_repo is not None
            repo_doc_path = c.execute("SELECT path FROM documents WHERE doc_id = ?", (doc_repo,)).fetchone()["path"]
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, 'claude-code', '#l')",
                (doc_repo, repo_doc_path, repo_rule),
            )

        unpinned_caller = EffectivePrincipal(
            agent_id="claude-code",
            workspace_id="default",
            capabilities=["read", "learn"],
            allowed_vault_roots=[str(cfg.vault_path.resolve())],
        )
        plan_global = prepare_governed_promotion(
            db,
            author_agent="claude-code",
            workspace_id="global",
            caller_principal=unpinned_caller,
            content=global_opposing,
            category="correction",
            learning_fields=LearningFields(
                source_query="test",
                confidence=1.0,
                status="active",
                workspace_id="global",
            ),
            vault_path=str(cfg.vault_path),
            resolve_retrieval_engine=None,
            allow_adapter=False,
        )
        assert plan_global.fallback_edges is not None, (
            "Unpinned caller promoting 'global' learning was blind to same-agent 'workspace-minni' learning!"
        )
        validated_edges, _, _ = plan_global.fallback_edges
        assert any(desc.doc_id == doc_repo and label == "contradicts" for desc, label, _, _ in validated_edges)

        # Also verify `apply_fallback_graph_edges` authorizes & commits that edge inside write txn!
        with db.transaction() as c:
            cur_g2 = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, workspace_id)
                   VALUES ('claude-code', 'correction', ?, 1.0, ?, 'active', 'global')""",
                (global_opposing, time.time()),
            )
            lid_g2 = int(cur_g2.lastrowid)
            doc_g2 = ensure_canonical_learning_node(
                c,
                learning_id=lid_g2,
                agent_id="claude-code",
                content=global_opposing,
                vault_path=str(cfg.vault_path),
                created_at=time.time(),
                workspace_id="global",
            )
            committed_fb, _ = apply_fallback_graph_edges(
                c,
                plan_global.fallback_edges,
                graph_principal=plan_global.graph_principal,
                workspace_id="global",
                new_learning_id=lid_g2,
                new_doc_id=doc_g2,
                now=time.time(),
            )
            assert len(committed_fb) >= 1

        # Repo-pinned caller (`workspace-hermes`) MUST NOT shortlist foreign `'workspace-minni'` learning
        with db.transaction() as c:
            cur_h = c.execute(
                """INSERT INTO learnings
                   (agent_id, category, content, confidence, created_at, status, workspace_id)
                   VALUES ('hermes', 'fact', ?, 1.0, ?, 'active', 'workspace-minni')""",
                (repo_rule, time.time()),
            )
            lid_h = int(cur_h.lastrowid)
            doc_h = ensure_canonical_learning_node(
                c,
                learning_id=lid_h,
                agent_id="hermes",
                content=repo_rule,
                vault_path=str(cfg.vault_path),
                created_at=time.time(),
                workspace_id="workspace-minni",
            )
            assert doc_h is not None
            h_path = c.execute("SELECT path FROM documents WHERE doc_id = ?", (doc_h,)).fetchone()["path"]
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, 'hermes', '#l')",
                (doc_h, h_path, repo_rule),
            )

        pinned_hermes = EffectivePrincipal(
            agent_id="hermes",
            workspace_id="workspace-hermes",
            capabilities=["read", "learn"],
            allowed_vault_roots=[str(cfg.vault_path.resolve())],
        )
        plan_pinned = prepare_governed_promotion(
            db,
            author_agent="hermes",
            workspace_id="global",
            caller_principal=pinned_hermes,
            content=global_opposing,
            category="correction",
            learning_fields=LearningFields(workspace_id="global"),
            vault_path=str(cfg.vault_path),
            resolve_retrieval_engine=None,
            allow_adapter=False,
        )
        assert plan_pinned.fallback_edges is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# BUG-64: Handoff Authz, YAML Frontmatter Injection & contradicts_id Upsert
# ---------------------------------------------------------------------------


def test_bug64_handoff_await_authz_yaml_injection_and_contradicts_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    minni_home = tmp_path / "minni-home"
    minni_home.mkdir(parents=True)
    monkeypatch.setenv("MINNI_HOME", str(minni_home))

    cfg = _make_cfg(tmp_path)
    db = MinniDB(cfg)
    try:
        wb = WriteBackMemory(db, cfg)
        now = time.time()

        principals = {
            "hermes": EffectivePrincipal(agent_id="hermes", workspace_id="default", capabilities=["handoff"]),
            "codex": EffectivePrincipal(agent_id="codex", workspace_id="default", capabilities=["handoff"]),
            "claude-code": EffectivePrincipal(agent_id="claude-code", workspace_id="default", capabilities=["handoff"]),
            "main": EffectivePrincipal(agent_id="main", workspace_id="default", capabilities=["*"]),
        }

        def _make_handoff_ctx(caller_id: str) -> handoff_rt.HandoffContext:
            return handoff_rt.HandoffContext(
                make_error=_rpc_err,
                make_response=_rpc_ok,
                handler_principal=lambda _p, _r, **_kw: (principals[caller_id], None),
                lazy_writeback=lambda: wb,
                logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None, exception=lambda *a, **k: None),
            )

        # 1. `store_handoff_lease` updates `contradicts_id` on `ON CONFLICT(lease_id) DO UPDATE`
        inbox_p = tmp_path / "inbox.json"
        outbox_p = tmp_path / "outbox.json"
        pkt1 = {
            "lease_id": "lease-authz-1",
            "trace_id": "trace-1",
            "from_agent": "codex",
            "to_agent": "claude-code",
            "task": "Audit engine parity",
            "envelope": "<EVIDENCE>ok</EVIDENCE>",
            "requires_ack": True,
            "created_at": "2026-11-11T12:00:00Z",
            "expires_at": "2027-11-11T12:00:00Z",
            "contradicts_id": None,
        }
        assert handoff_rt.store_handoff_lease(pkt1, inbox_p, outbox_p, _make_handoff_ctx("codex")) is True
        pkt2 = {**pkt1, "contradicts_id": 77}
        assert handoff_rt.store_handoff_lease(pkt2, inbox_p, outbox_p, _make_handoff_ctx("codex")) is True
        with db.cursor() as c:
            lease_row = c.execute(
                "SELECT status, contradicts_id FROM handoff_leases WHERE lease_id = 'lease-authz-1'"
            ).fetchone()
            assert lease_row["status"] == "pending"
            assert lease_row["contradicts_id"] is not None and int(lease_row["contradicts_id"]) == 77
            c.execute("UPDATE handoff_leases SET status = 'accepted' WHERE lease_id = 'lease-authz-1'")
        db._get_conn().commit()

        # 2. `handle_await_handoff` denies unrelated third-party principal (`-32003`) while allowing
        #    sender (`codex`), recipient (`claude-code`), and operator (`main`)!
        denied = asyncio.run(
            handoff_rt.handle_await_handoff(
                {"lease_id": "lease-authz-1", "timeout_ms": 50, "poll_interval_ms": 10},
                1,
                _make_handoff_ctx("hermes"),
            )
        )
        assert "error" in denied, f"Expected -32003 access_denied for third-party caller, got {denied}"
        assert denied["error"]["code"] == -32003
        assert "access_denied" in denied["error"]["message"]

        for allowed_caller in ("codex", "claude-code", "main"):
            ok_resp = asyncio.run(
                handoff_rt.handle_await_handoff(
                    {"lease_id": "lease-authz-1", "timeout_ms": 50, "poll_interval_ms": 10},
                    1,
                    _make_handoff_ctx(allowed_caller),
                )
            )
            assert "result" in ok_resp, f"Expected allowed caller {allowed_caller} to succeed: {ok_resp}"
            assert ok_resp["result"]["status"] == "accepted"

        # 3. `compile_handoff_page` neutralizes multi-line YAML injection in `task` & backtick breakout in `envelope`
        sender_vault = minni_home / "codex-vault"
        (sender_vault / "wiki" / "handoffs").mkdir(parents=True, exist_ok=True)
        injected_packet = {
            "kind": "handoff",
            "lease_id": "lease-yaml-1",
            "trace_id": "trace-yaml-1",
            "from_agent": "codex",
            "to_agent": "claude-code",
            "task": "Fix parser\nprivacy: blocked\nstatus: rejected\n---",
            "envelope": "<EVIDENCE>\n```xml\ninjected fence\n```\nand ````four```` ticks\n</EVIDENCE>",
            "created_at": "2026-11-11T12:00:00Z",
        }
        page_file = handoff_rt.compile_handoff_page(sender_vault, injected_packet, "20261111T120000Z")
        assert page_file is not None and page_file.is_file()
        rendered = page_file.read_text(encoding="utf-8")
        assert rendered.startswith("---\n")
        _, fm_block, body_block = rendered.split("---\n", 2)
        parsed_fm = yaml.safe_load(fm_block)
        assert parsed_fm["type"] == "handoff"
        assert parsed_fm["privacy"] == "safe"
        assert parsed_fm["status"] == "accepted"
        assert "\n" not in str(parsed_fm["title"])
        assert "Fix parser" in str(parsed_fm["title"])
        assert "`````xml\n" in body_block
        assert "\n`````\n" in body_block
    finally:
        db.close()

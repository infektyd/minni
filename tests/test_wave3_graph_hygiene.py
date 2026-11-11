"""Wave 3 verification suite:
1. Real `minnid` subprocess E2E supersession, durable vector purge, recall replacement,
   and `minni_drill` (`expand`) typed `memory_links` (`updates` / `updated_by`) (`BUG-05`).
2. Real `minnid` subprocess E2E `contradicts` edge, `contradiction_log` (`graph_classifier`),
   and recall / `subscribe_contradictions` surfacing with workspace & principal ACLs (`BUG-05`, `BUG-12`).
3. Standing `backfill_learning_projections` healing missing `learning_documents` joins (`BUG-05`).
4. AFM overflow / guardrail circuit-breaker protection, chunked judge, deterministic fallback,
   wrapper-free `compact_distillation`, and `EdgeClassifier` recursive batch splitting (`BUG-10`).
5. Python hands-free intake & consolidation junk/wrapper auto-rejection (`BUG-11`).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from minni import minni_cli
from minni.afm_passes import compact_distillation, consolidation, inbox_archive, inbox_ingest
from minni.afm_passes.judge import (
    CircuitBreaker,
    VerdictCache,
    evaluate_deterministic_quality_fallback,
    judge_candidate,
)
from minni.backfill import backfill_learning_projections
from minni.config import DEFAULT_CONFIG, MinniConfig
from minni.db import MinniDB
from minni.edge_classifier import EdgeClassifier
from minni.migrations import run_migrations
from minni.minnid_runtime.afm import AFMContext, apply_consolidation_result
from minni.minnid_runtime.governance import GovernanceContext, stage_candidate
from minni.principal import EffectivePrincipal
from minni.writeback import WriteBackMemory


def _uds_rpc(sock_path: Path, method: str, params: dict, req_id: int = 1) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(20.0)
        s.connect(str(sock_path))
        s.sendall(payload.encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    raw = b"".join(chunks).decode("utf-8").strip()
    assert raw, f"Empty RPC response for {method}"
    return json.loads(raw.splitlines()[0])


@pytest.fixture
def live_minnid(monkeypatch):
    """Spawn a real hermetic `minnid` subprocess bound to a short Unix socket."""
    repo_root = Path(__file__).resolve().parents[1]
    td = Path(tempfile.mkdtemp(prefix="mw3-", dir="/tmp"))
    td.chmod(0o700)
    sock_path = td / "run" / "minnid.sock"
    (td / "run").mkdir(parents=True, exist_ok=True)
    (td / "home").mkdir(parents=True, exist_ok=True)
    principals_dir = td / "principals"
    principals_dir.mkdir(parents=True, exist_ok=True)
    principals_dir.chmod(0o700)

    for agent_id, caps in (
        ("main", ["*", "govern", "accept_flagged"]),
        ("codex", ["read", "search", "learn", "govern", "accept_flagged"]),
        ("outsider", ["read", "search"]),
    ):
        pf = principals_dir / f"{agent_id}.json"
        pf.write_text(
            json.dumps(
                {
                    "agent_id": agent_id,
                    "workspace_id": "default",
                    "capabilities": caps,
                }
            ),
            encoding="utf-8",
        )
        pf.chmod(0o600)

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    env["HOME"] = str(td / "home")
    env["MINNI_HOME"] = str(td)
    env["MINNI_SOCKET_PATH"] = str(sock_path)
    env["MINNI_LOCAL_OPERATOR"] = "1"
    env["MINNI_WARMUP"] = "off"
    env["MINNI_BACKFILL"] = "off"
    env["MINNI_DECAY"] = "off"
    env["MINNI_VAULT_WATCH"] = "off"
    env["MINNI_AFM_LOOP"] = "off"
    env["MINNI_AFM_PROVIDER_MODE"] = "disabled"
    env.pop("MINNI_AGENT_ID", None)
    env.pop("MINNI_WORKSPACE_ID", None)

    daemon_log = td / "daemon.log"
    log_fh = daemon_log.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "minni.minnid", "--socket", str(sock_path)],
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        cwd=str(repo_root),
    )
    try:
        deadline = time.monotonic() + 25.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_fh.flush()
                pytest.fail(
                    f"minnid exited early ({proc.returncode}):\n"
                    f"{daemon_log.read_text(encoding='utf-8', errors='replace')}"
                )
            if minni_cli._daemon_alive(sock_path):
                break
            time.sleep(0.05)
        else:
            log_fh.flush()
            pytest.fail(
                f"minnid failed to bind socket within 25s:\n"
                f"{daemon_log.read_text(encoding='utf-8', errors='replace')}"
            )
        yield {"td": td, "sock": sock_path, "env": env, "db_path": td / "minni.db"}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3.0)
        log_fh.close()
        shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1 & 2. Real `minnid` Subprocess E2E Graph Supersession, Drill & Contradicts
# ---------------------------------------------------------------------------


def test_e2e_minnid_supersession_purges_stale_vector_and_surfaces_drill_links(live_minnid):
    sock = live_minnid["sock"]
    env = live_minnid["env"]
    db_path = live_minnid["db_path"]

    def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "minni.minni_cli", "--socket", str(sock), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )

    assert DEFAULT_CONFIG.graph_expansion_enabled is True
    assert not hasattr(WriteBackMemory, "detect_contradictions")

    rule_a = "Always bind the metrics telemetry exporter socket to port 9101 on localhost."
    rule_b = (
        "Supersedes prior metrics exporter rule: always bind the metrics telemetry "
        "exporter socket to port 9102 on localhost."
    )

    r1 = run_cli(
        "learn",
        rule_a,
        "--category",
        "procedure",
        "--workspace",
        "workspace-alpha",
        "--accept",
        "--json",
    )
    assert r1.returncode == 0, r1.stderr
    lid_a = int(json.loads(r1.stdout)["learning_id"])

    r2 = run_cli(
        "learn",
        rule_b,
        "--category",
        "procedure",
        "--workspace",
        "workspace-alpha",
        "--accept",
        "--json",
    )
    assert r2.returncode == 0, r2.stderr
    lid_b = int(json.loads(r2.stdout)["learning_id"])
    assert lid_b != lid_a

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row_a = conn.execute(
            "SELECT status, superseded_by, workspace_id FROM learnings WHERE learning_id = ?",
            (lid_a,),
        ).fetchone()
        row_b = conn.execute(
            "SELECT status, superseded_by, workspace_id FROM learnings WHERE learning_id = ?",
            (lid_b,),
        ).fetchone()
        assert row_a is not None and row_b is not None
        assert int(row_a["superseded_by"]) == lid_b
        assert row_a["workspace_id"] == "workspace-alpha"
        assert row_b["status"] == "active"
        assert row_b["superseded_by"] is None
        assert row_b["workspace_id"] == "workspace-alpha"

        doc_b = conn.execute(
            "SELECT doc_id FROM learning_documents WHERE learning_id = ?",
            (lid_b,),
        ).fetchone()
        assert doc_b is not None
        doc_id_b = int(doc_b["doc_id"])

        # Rule A's canonical graph node stays mapped (`page_status='superseded'`) for `memory_links`
        # while its stale chunks/vectors and FTS rows are purged!
        doc_a = conn.execute(
            "SELECT doc_id FROM learning_documents WHERE learning_id = ?",
            (lid_a,),
        ).fetchone()
        assert doc_a is not None
        doc_id_a = int(doc_a["doc_id"])

        doc_a_meta = conn.execute(
            "SELECT page_status, superseded_by FROM documents WHERE doc_id = ?",
            (doc_id_a,),
        ).fetchone()
        assert doc_a_meta is not None
        assert doc_a_meta["page_status"] == "superseded"
        assert conn.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE doc_id = ?", (doc_id_a,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM vault_fts WHERE doc_id = ?", (doc_id_a,)
        ).fetchone()[0] == 0

        updates_links = conn.execute(
            "SELECT link_type FROM memory_links WHERE source_doc_id = ? AND target_doc_id = ?",
            (doc_id_b, doc_id_a),
        ).fetchall()
        assert any(r["link_type"] == "updates" for r in updates_links)
    finally:
        conn.close()

    # Recall must surface rule B (9102) and NEVER surface superseded rule A (9101)
    search_resp = _uds_rpc(
        sock,
        "search",
        {
            "query": "metrics telemetry exporter socket port localhost",
            "workspace_id": "workspace-alpha",
            "mode": "hybrid",
            "limit": 5,
        },
    )
    assert "result" in search_resp, search_resp
    hit_texts = [str(r.get("text") or "") for r in search_resp["result"]["results"]]
    assert any("9102" in t for t in hit_texts), f"Expected rule B in recall: {hit_texts}"
    assert not any("9101" in t for t in hit_texts), f"Superseded rule A leaked into recall: {hit_texts}"

    # `sm_drill` and `expand` on rule B (`doc_id_b`) must surface typed `updates` link to `doc_id_a`!
    drill_resp = _uds_rpc(
        sock,
        "sm_drill",
        {
            "references": [{"doc_id": doc_id_b}],
            "depth": "document",
        },
    )
    assert "result" in drill_resp, drill_resp
    drilled_items = drill_resp["result"]["results"]
    assert len(drilled_items) == 1
    mem_links = drilled_items[0].get("memory_links")
    assert isinstance(mem_links, dict), f"Expected memory_links on sm_drill item: {drill_resp}"
    assert any(
        item.get("doc_id") == doc_id_a and item.get("page_status") == "superseded"
        for item in mem_links.get("updates", [])
    ), f"Expected updates -> doc_id_a in sm_drill memory_links: {mem_links}"

    expand_resp = _uds_rpc(
        sock,
        "expand",
        {
            "doc_id": doc_id_b,
            "id_kind": "doc",
            "depth": "document",
        },
    )
    assert "result" in expand_resp, expand_resp
    exp_links = expand_resp["result"]["result"].get("memory_links")
    assert isinstance(exp_links, dict) and any(
        item.get("doc_id") == doc_id_a for item in exp_links.get("updates", [])
    )


def test_e2e_minnid_contradicts_sidecars_and_workspace_acl(live_minnid):
    sock = live_minnid["sock"]
    db_path = live_minnid["db_path"]

    # Stage & accept two opposing-polarity learnings across categories for agent `codex` in `workspace-alpha`
    s1 = _uds_rpc(
        sock,
        "stage_candidate",
        {
            "agent_id": "codex",
            "workspace_id": "workspace-alpha",
            "category": "fact",
            "content": "Always enable HTTP keepalive connection pooling on internal gateway clients.",
        },
    )
    cid1 = int(s1["result"]["candidate_id"])
    acc1 = _uds_rpc(
        sock,
        "resolve_candidate",
        {"agent_id": "codex", "candidate_id": cid1, "decision": "accept"},
    )
    assert "result" in acc1 and acc1["result"]["new_status"] == "accepted", acc1

    # `codex` reads learning 1 via `search` (populating `learning_reads` for subscription notifications)
    initial_read = _uds_rpc(
        sock,
        "search",
        {
            "agent_id": "codex",
            "workspace_id": "workspace-alpha",
            "query": "HTTP keepalive connection pooling internal gateway clients",
            "mode": "hybrid",
            "limit": 5,
        },
    )
    assert "result" in initial_read and len(initial_read["result"]["results"]) >= 1

    s2 = _uds_rpc(
        sock,
        "stage_candidate",
        {
            "agent_id": "codex",
            "workspace_id": "workspace-alpha",
            "category": "correction",
            "content": "Never enable HTTP keepalive connection pooling on internal gateway clients.",
        },
    )
    cid2 = int(s2["result"]["candidate_id"])
    acc2 = _uds_rpc(
        sock,
        "resolve_candidate",
        {"agent_id": "codex", "candidate_id": cid2, "decision": "accept"},
    )
    assert "result" in acc2 and acc2["result"]["new_status"] == "accepted", acc2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        edge_rows = conn.execute(
            "SELECT link_type FROM memory_links WHERE link_type = 'contradicts'"
        ).fetchall()
        assert len(edge_rows) >= 1

        clog_rows = conn.execute(
            "SELECT detection_method, resolution_status FROM contradiction_log"
        ).fetchall()
        assert any(
            r["detection_method"] == "graph_classifier" and r["resolution_status"] == "unresolved"
            for r in clog_rows
        )
        assert not any(r["detection_method"] == "legacy_unclassified" for r in clog_rows)
    finally:
        conn.close()

    # Authorized workspace (`workspace-alpha`) surfaces contradiction events on subscribe & sidecars on recall
    sub_alpha = _uds_rpc(
        sock,
        "minni_subscribe_contradictions",
        {"agent_id": "codex", "workspace_id": "workspace-alpha"},
    )
    assert "result" in sub_alpha, sub_alpha
    assert len(sub_alpha["result"]["events"]) >= 1
    assert sub_alpha["result"]["events"][0]["detection_method"] == "graph_classifier"

    recall_alpha = _uds_rpc(
        sock,
        "search",
        {
            "agent_id": "codex",
            "workspace_id": "workspace-alpha",
            "query": "HTTP keepalive connection pooling internal gateway clients",
            "mode": "hybrid",
            "limit": 5,
        },
    )
    assert "result" in recall_alpha, recall_alpha
    assert any(
        bool(r.get("contradictions"))
        and r.get("recommended_action") in ("follow_up", "escalate")
        for r in recall_alpha["result"]["results"]
    ), recall_alpha["result"]["results"]

    # Foreign workspace (`workspace-beta`) and unauthorized principal (`outsider`) get zero leaked events
    sub_beta = _uds_rpc(
        sock,
        "minni_subscribe_contradictions",
        {"agent_id": "codex", "workspace_id": "workspace-beta"},
    )
    assert sub_beta["result"]["events"] == []

    sub_outsider = _uds_rpc(
        sock,
        "minni_subscribe_contradictions",
        {"agent_id": "outsider", "workspace_id": "workspace-alpha"},
    )
    assert sub_outsider["result"]["events"] == []


def test_backfill_learning_projections_repairs_missing_learning_documents_with_workspace(tmp_path: Path):
    cfg = MinniConfig(
        db_path=str(tmp_path / "bf.db"),
        vault_path=str(tmp_path / "vault"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_path=str(tmp_path / "learnings"),
    )
    Path(cfg.vault_path).mkdir(parents=True, exist_ok=True)
    db = MinniDB(cfg)
    try:
        run_migrations(db._get_conn())
        with db.transaction() as c:
            c.execute(
                """
                INSERT INTO learnings (agent_id, category, content, confidence, created_at, status, workspace_id)
                VALUES ('codex', 'architecture', 'Backfill repairs missing canonical learning document joins.', 1.0, ?, 'active', 'workspace-gamma')
                """,
                (time.time(),),
            )
            lid = int(c.lastrowid)

        summary = backfill_learning_projections(db, cfg)
        assert summary["repaired"] == 1
        with db.cursor() as c:
            join_row = c.execute(
                """
                SELECT ld.doc_id, d.workspace_id
                FROM learning_documents ld
                JOIN documents d ON d.doc_id = ld.doc_id
                WHERE ld.learning_id = ?
                """,
                (lid,),
            ).fetchone()
            assert join_row is not None
            assert join_row["workspace_id"] == "workspace-gamma"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. BUG-10 Unit Tests: Judge Chunking, Guardrail/Overflow Breaker Safety,
#    Compact Distillation & EdgeClassifier Batch Split
# ---------------------------------------------------------------------------


def test_bug10_judge_guardrail_and_overflow_never_trip_circuit_breaker_and_chunk():
    cache = VerdictCache()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=300.0)

    guardrail_res = SimpleNamespace(
        ok=False,
        status="guardrail",
        data={"error_kind": "guardrail"},
        error="Guardrail Violation",
    )

    # 5 consecutive guardrail trips on technical kill-tree notes must NEVER open CircuitBreaker
    with patch("minni.afm_passes.judge.resolve_afm_mode", return_value="native"):
        with patch("minni.afm_passes.judge.invoke_native_afm", return_value=guardrail_res):
            for idx in range(5):
                verdict = judge_candidate(
                    content=(
                        f"Concurrency scar #{idx}: terminate hung subprocess process-group trees "
                        "before closing the SQLite WAL descriptor."
                    ),
                    cache=cache,
                    breaker=breaker,
                )
                assert verdict.decision == "accept"
                assert verdict.model == "deterministic-quality-fallback"
                assert not breaker.is_open()

    # Proactive chunking on oversized candidate (> budget tokens)
    chunk_calls: list[str] = []

    def fake_chunk_judge(op, payload, timeout=15.0):
        chunk_calls.append(payload["candidate"])
        return SimpleNamespace(
            ok=True,
            status="ok",
            data={"decision": "accept", "reason": "valid technical chunk"},
            error=None,
        )

    oversized = " ".join(
        f"Sentence {i} documents deterministic SQLite transaction lock ordering across workers."
        for i in range(450)
    )
    with patch("minni.afm_passes.judge.resolve_afm_mode", return_value="native"):
        with patch("minni.afm_passes.judge.resolve_afm_input_budget_tokens", return_value=600):
            with patch("minni.afm_passes.judge.invoke_native_afm", side_effect=fake_chunk_judge):
                v_large = judge_candidate(content=oversized, cache=VerdictCache(), breaker=breaker)
                assert v_large.decision == "accept"
                assert len(chunk_calls) >= 2
                assert not breaker.is_open()

    # allow_jev_egress=True uses deterministic quality evaluator when AFM is off
    with patch("minni.afm_passes.judge.resolve_afm_mode", return_value="off"):
        v_egress_ok = judge_candidate(
            content="Always verify migration idempotency before promoting schema changes.",
            allow_jev_egress=True,
            cache=VerdictCache(),
            breaker=CircuitBreaker(),
        )
        assert v_egress_ok.decision == "accept"

        v_egress_bad = evaluate_deterministic_quality_fallback(
            "Created At: 2026-11-11T03:00:00-05:00\nCompleted At: 2026-11-11T03:00:01-05:00"
        )
        assert v_egress_bad.decision == "reject"


def test_bug10_compact_distillation_fallback_never_emits_compaction_summary_prefix():
    out = compact_distillation._fallback_candidate(
        "Key technical concepts",
        "- SQLite WAL mode requires busy_timeout >= 5000ms under parallel readers.\n"
        "- Created At: 2026-11-11T03:00:00-05:00\n"
        "- fix\n",
    )
    assert "Compaction summary" not in out
    assert "Created At:" not in out
    assert "SQLite WAL mode requires busy_timeout >= 5000ms" in out
    assert not consolidation.is_low_signal_boot_learning(out)

    empty_out = compact_distillation._fallback_candidate(
        "Errors and fixes",
        "Created At: 2026-11-11T03:00:00-05:00\nCompleted At: 2026-11-11T03:00:01-05:00\n<SYSTEM_MESSAGE>noop</SYSTEM_MESSAGE>",
    )
    assert empty_out == ""


def test_bug10_edge_classifier_splits_overflow_batches_and_falls_back_on_single_pair_guardrail():
    from minni.model_provider import ProviderResult

    call_sizes: list[int] = []
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=300.0)

    class OverflowThenGuardrailProvider:
        name = "mock_local_afm"
        tier = "local"

        def supports(self, operation: str) -> bool:
            return operation == "edge_inference"

        def chat(self, request, client=None) -> ProviderResult:
            user_text = request.payload["messages"][-1]["content"]
            n_pairs = user_text.count("[PAIR pair_")
            if n_pairs == 0:
                n_pairs = user_text.count("pair_")
            call_sizes.append(n_pairs)
            if len(call_sizes) == 1:
                return ProviderResult(
                    ok=False,
                    data={"error_kind": "context_overflow"},
                    provider=self.name,
                    status="context_overflow",
                    error="Context window exceeded",
                )
            return ProviderResult(
                ok=False,
                data={"error_kind": "guardrail"},
                provider=self.name,
                status="guardrail",
                error="Guardrail tripped",
            )

    clf = EdgeClassifier(
        provider_chain=OverflowThenGuardrailProvider(),
        allow_heuristic_fallback=True,
        breaker=breaker,
    )
    source = {
        "learning_id": 201,
        "title": "Exporter Port Rule",
        "applies_when": "configuring telemetry exporter",
        "created_at": "2026-11-11T00:00:00Z",
        "body": "Supersedes prior exporter port rule: always bind telemetry exporter to port 9200.",
        "agent_id": "codex",
        "workspace_id": "workspace-alpha",
        "category": "procedure",
    }
    candidates = [
        {
            "pair_id": "pair_1",
            "doc_id": "doc_1",
            "page_type": "learning",
            "status": "accepted",
            "applies_when": "configuring telemetry exporter",
            "created_at": "2026-11-10T00:00:00Z",
            "body": "Always bind telemetry exporter to port 9100.",
            "agent": "codex",
            "workspace_id": "workspace-alpha",
            "category": "procedure",
        },
        {
            "pair_id": "pair_2",
            "doc_id": "doc_2",
            "page_type": "learning",
            "status": "accepted",
            "applies_when": "markdown styling",
            "created_at": "2026-11-10T00:00:00Z",
            "body": "Completely unrelated markdown frontmatter formatting convention.",
            "agent": "codex",
            "workspace_id": "workspace-alpha",
            "category": "fact",
        },
    ]
    res = clf.classify(source, candidates)
    assert res.status == "ok", res
    assert not breaker.is_open()
    assert len(call_sizes) >= 3  # batch of 2 overflowed -> split into two 1-pair calls -> deterministic fallback
    by_pair = {e.pair_id: e for e in res.edges}
    assert by_pair["pair_1"].label == "updates"
    assert by_pair["pair_2"].label == "none"


# ---------------------------------------------------------------------------
# 4. BUG-11 Python Unit Tests: Intake Rejection & Consolidation Auto-Reject
# ---------------------------------------------------------------------------


def test_bug11_intake_and_consolidation_reject_wrappers_and_one_word_junk(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MINNI_AFM_MODE", "off")
    cfg = MinniConfig(
        db_path=str(tmp_path / "hygiene.db"),
        vault_path=str(tmp_path / "vault"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_path=str(tmp_path / "learnings"),
    )
    Path(cfg.vault_path).mkdir(parents=True, exist_ok=True)
    db = MinniDB(cfg)
    try:
        run_migrations(db._get_conn())
        wb = WriteBackMemory(db, cfg)
        principal = EffectivePrincipal(agent_id="codex", workspace_id="default", capabilities=["learn", "read"])
        gov_ctx = GovernanceContext(
            make_error=lambda code, msg, req_id: {"error": {"code": code, "message": msg}, "id": req_id},
            make_response=lambda res, req_id: {"result": res, "id": req_id},
            handler_principal=lambda p, req_id: (principal, None),
            lazy_writeback=lambda: wb,
            lazy_episodic=lambda: None,
            record_latency=lambda *a: None,
            index_durable_learning=lambda *a, **k: True,
            maybe_archive_inbox_source=lambda *a, **k: None,
            sovereign_db=lambda *a, **k: MinniDB(cfg),
        )

        # 1. `stage_candidate` rejects 1-word junk and wrapper leaks at intake
        for bad in (
            "fix",
            "lgtm",
            "Created At: 2026-11-11T03:00:00-05:00\nCompleted At: 2026-11-11T03:00:02-05:00\nran command",
            "<SYSTEM_MESSAGE>internal task wake</SYSTEM_MESSAGE>",
            "<MINNI_ENVELOPE>boot context</MINNI_ENVELOPE>",
        ):
            resp = stage_candidate({"content": bad}, request_id=1, context=gov_ctx)
            assert "error" in resp, f"Expected intake error for {bad!r}, got {resp}"

        with db.cursor() as c:
            assert c.execute("SELECT COUNT(*) FROM candidate_packets").fetchone()[0] == 0

        # 2. `inbox_ingest` drops 1-word junk and wrapper leaks, and `inbox_archive` archives inert files
        inbox = tmp_path / "codex-vault" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "junk-only.json").write_text(
            json.dumps(
                {
                    "kind": "stop_candidates",
                    "agent_id": "codex",
                    "candidates": [
                        "fix",
                        "Created At: 2026-11-11T03:00:00-05:00\nCompleted At: 2026-11-11T03:00:01-05:00",
                        "<SYSTEM_MESSAGE>wakeup</SYSTEM_MESSAGE>",
                    ],
                }
            ),
            encoding="utf-8",
        )
        ing = inbox_ingest.ingest(db, cfg, inboxes=[inbox], dry_run=False)
        assert ing["inserted"] == 0
        assert ing["skipped_by_kind"].get("_low_signal_junk", 0) >= 1
        assert ing["skipped_by_kind"].get("_wrapper_leak", 0) >= 2

        arch = inbox_archive.archive_inert_files(cfg, inboxes=[inbox], dry_run=False)
        assert arch["archived"] == 1

        # 3. `consolidation.run` routes pre-existing short/wrapper/sprint-chatter rows into `reject_candidate_ids`
        now = time.time()
        inserted_ids: list[int] = []
        with db.transaction() as c:
            for legacy_bad in (
                "tiny",
                "Created At: 2026-11-11T03:00:00-05:00\nCompleted At: 2026-11-11T03:00:01-05:00",
                "<MINNI_ENVELOPE>cached memory snippet</MINNI_ENVELOPE>",
                "Wave 3 slice is complete and 42/42 tests passed",
            ):
                c.execute(
                    """
                    INSERT INTO candidate_packets
                    (principal, workspace_id, privacy_level, requested_privacy, content, instruction_like, status, proposed_at)
                    VALUES ('codex', 'default', 'review', 'safe', ?, 0, 'proposed', ?)
                    """,
                    (legacy_bad, now),
                )
                inserted_ids.append(int(c.lastrowid))

        cons_res = consolidation.run(db, cfg, dry_run=False, trace_id="w3-hygiene")
        assert set(cons_res["reject_candidate_ids"]) == set(inserted_ids)
        assert cons_res["review_candidate_ids"] == []

        afm_ctx = AFMContext(
            make_error=lambda code, msg, req_id: {"error": {"code": code, "message": msg}},
            make_response=lambda res, req_id: {"result": res},
            guard_vault_root=lambda *a, **k: None,
            lazy_writeback=lambda: wb,
            trace_ring=lambda: None,
            record_latency=lambda *a: None,
            default_config=cfg,
            maybe_archive_inbox_source=lambda *a, **k: None,
        )
        apply_consolidation_result(cons_res, afm_ctx)

        with db.cursor() as c:
            statuses = [
                r["status"]
                for r in c.execute(
                    "SELECT status FROM candidate_packets ORDER BY candidate_id"
                ).fetchall()
            ]
            assert statuses == ["rejected"] * len(inserted_ids)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. Wave 3 Blocker Regressions: BUG-28, BUG-29, BUG-30, BUG-31..BUG-35
# ---------------------------------------------------------------------------


def test_bug28_and_bug29_e2e_supersedes_and_repromote_superseded_rule(live_minnid):
    """BUG-28 & BUG-29 against real `minnid`:
    - `learn(force=True, supersedes=N)` succeeds without aborting `_revalidate_target`.
    - Re-learning a previously superseded rule via `learn(force=True)` and `resolve_candidate`
      reopens the aggregate-superseded canonical node cleanly (`page_status='accepted'`).
    """
    sock = live_minnid["sock"]
    db_path = live_minnid["db_path"]

    rule_a = "Always configure connection pool max_overflow=10 on worker DB engines."
    rule_b = "Always configure connection pool max_overflow=25 on worker DB engines."

    r1 = _uds_rpc(
        sock,
        "learn",
        {
            "agent_id": "codex",
            "workspace_id": "workspace-alpha",
            "category": "procedure",
            "content": rule_a,
            "force": True,
        },
    )
    assert "result" in r1 and r1["result"]["status"] == "ok", r1
    lid_1 = int(r1["result"]["learning_id"])

    # BUG-28: explicit `supersedes=lid_1` with shortlisted target must NOT fail `_revalidate_target`
    r2 = _uds_rpc(
        sock,
        "learn",
        {
            "agent_id": "codex",
            "workspace_id": "workspace-alpha",
            "category": "procedure",
            "content": rule_b,
            "force": True,
            "supersedes": lid_1,
        },
    )
    assert "result" in r2 and r2["result"]["status"] == "ok", r2
    lid_2 = int(r2["result"]["learning_id"])
    assert lid_2 != lid_1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        l1_row = conn.execute(
            "SELECT superseded_by FROM learnings WHERE learning_id = ?", (lid_1,)
        ).fetchone()
        assert l1_row is not None and int(l1_row["superseded_by"]) == lid_2

        doc_a = conn.execute(
            """SELECT d.doc_id, d.page_status FROM documents d
               JOIN learning_documents ld ON ld.doc_id = d.doc_id
               WHERE ld.learning_id = ?""",
            (lid_1,),
        ).fetchone()
        doc_b = conn.execute(
            """SELECT d.doc_id, d.page_status FROM documents d
               JOIN learning_documents ld ON ld.doc_id = d.doc_id
               WHERE ld.learning_id = ?""",
            (lid_2,),
        ).fetchone()
        assert doc_a is not None and doc_b is not None
        assert doc_a["page_status"] == "superseded"
        link = conn.execute(
            """SELECT 1 FROM memory_links
               WHERE source_doc_id = ? AND target_doc_id = ?
                 AND link_type = 'updates' AND edge_status = 'active'""",
            (int(doc_b["doc_id"]), int(doc_a["doc_id"])),
        ).fetchone()
        assert link is not None

        # BUG-29 (1): revert/re-learn Rule A via `learn(force=True, supersedes=lid_2)`
        r3 = _uds_rpc(
            sock,
            "learn",
            {
                "agent_id": "codex",
                "workspace_id": "workspace-alpha",
                "category": "procedure",
                "content": rule_a,
                "force": True,
                "supersedes": lid_2,
            },
        )
        assert "result" in r3 and r3["result"]["status"] == "ok", r3
        lid_3 = int(r3["result"]["learning_id"])

        doc_a_reopened = conn.execute(
            "SELECT page_status FROM documents WHERE doc_id = ?",
            (int(doc_a["doc_id"]),),
        ).fetchone()
        assert doc_a_reopened is not None and doc_a_reopened["page_status"] == "accepted"
        fts_a = conn.execute(
            "SELECT 1 FROM vault_fts WHERE doc_id = ?", (int(doc_a["doc_id"]),)
        ).fetchone()
        assert fts_a is not None

        # Supersede Rule A again with Rule B so doc_a returns to `page_status='superseded'`
        r4 = _uds_rpc(
            sock,
            "learn",
            {
                "agent_id": "codex",
                "workspace_id": "workspace-alpha",
                "category": "procedure",
                "content": rule_b,
                "force": True,
                "supersedes": lid_3,
            },
        )
        assert "result" in r4 and r4["result"]["status"] == "ok", r4
        lid_4 = int(r4["result"]["learning_id"])
        assert (
            conn.execute(
                "SELECT page_status FROM documents WHERE doc_id = ?",
                (int(doc_a["doc_id"]),),
            ).fetchone()["page_status"]
            == "superseded"
        )

        # BUG-29 (2) + BUG-28: re-promote Rule A via `stage_candidate` + `resolve_candidate`
        st = _uds_rpc(
            sock,
            "stage_candidate",
            {
                "agent_id": "codex",
                "workspace_id": "workspace-alpha",
                "category": "procedure",
                "content": rule_a,
            },
        )
        assert "result" in st, st
        cid = int(st["result"]["candidate_id"])
        acc = _uds_rpc(
            sock,
            "resolve_candidate",
            {
                "agent_id": "codex",
                "candidate_id": cid,
                "decision": "accept",
                "supersede_ids": [lid_4],
            },
        )
        assert "result" in acc and acc["result"]["new_status"] == "accepted", acc
        assert (
            conn.execute(
                "SELECT page_status FROM documents WHERE doc_id = ?",
                (int(doc_a["doc_id"]),),
            ).fetchone()["page_status"]
            == "accepted"
        )
    finally:
        conn.close()


def test_bug28_and_bug29_savepoint_fallback_and_afm_repromote_and_blocked(
    tmp_path: Path, monkeypatch
):
    """BUG-28 SAVEPOINT fallback + BUG-29 AFM re-promotion & permanent-restriction terminalization."""
    from minni.durable_projection import durable_doc_path
    from minni.graph_commit import retire_superseded_members
    from minni.graph_coordinator import GraphCommitAborted
    from minni.minnid_runtime.afm import promote_candidate_durable
    from minni.minnid_runtime.governance import handle_learn, resolve_candidate

    monkeypatch.setenv("MINNI_AFM_MODE", "off")
    cfg = MinniConfig(
        db_path=str(tmp_path / "savepoint.db"),
        vault_path=str(tmp_path / "vault"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_path=str(tmp_path / "learnings"),
    )
    Path(cfg.vault_path).mkdir(parents=True, exist_ok=True)
    db = MinniDB(cfg)
    try:
        run_migrations(db._get_conn())
        wb = WriteBackMemory(db, cfg)
        principal = EffectivePrincipal(
            agent_id="codex",
            workspace_id="default",
            capabilities=["learn", "read", "govern", "accept_flagged"],
        )
        gov_ctx = GovernanceContext(
            make_error=lambda code, msg, req_id: {
                "error": {"code": code, "message": msg},
                "id": req_id,
            },
            make_response=lambda res, req_id: {"result": res, "id": req_id},
            handler_principal=lambda p, req_id: (principal, None),
            lazy_writeback=lambda: wb,
            lazy_episodic=lambda: None,
            record_latency=lambda *a: None,
            index_durable_learning=lambda *a, **k: True,
            maybe_archive_inbox_source=lambda *a, **k: None,
            sovereign_db=lambda *a, **k: MinniDB(cfg),
        )
        afm_ctx = AFMContext(
            make_error=lambda code, msg, req_id: {"error": {"code": code, "message": msg}},
            make_response=lambda res, req_id: {"result": res},
            guard_vault_root=lambda *a, **k: None,
            lazy_writeback=lambda: wb,
            trace_ring=lambda: None,
            record_latency=lambda *a: None,
            default_config=cfg,
            maybe_archive_inbox_source=lambda *a, **k: None,
        )

        # 1. Simulate concurrent `stale_candidate` GraphCommitAborted inside SAVEPOINT across callers
        def _abort_stale(*args, **kwargs):
            raise GraphCommitAborted("stale_candidate", "simulated concurrent target drift")

        with patch("minni.minnid_runtime.governance.commit_prepared_learning", _abort_stale):
            hl = handle_learn(
                {
                    "content": "Always validate schema version headers before decoding frames.",
                    "category": "procedure",
                    "force": True,
                },
                request_id=1,
                context=gov_ctx,
            )
            assert "result" in hl and hl["result"]["status"] == "ok", hl

            auto_principal = EffectivePrincipal(
                agent_id="codex",
                workspace_id="default",
                capabilities=["learn", "read", "govern", "accept_flagged"],
                auto_accept_own=True,
            )
            gov_ctx_auto = GovernanceContext(
                make_error=gov_ctx.make_error,
                make_response=gov_ctx.make_response,
                handler_principal=lambda p, req_id: (auto_principal, None),
                lazy_writeback=lambda: wb,
                lazy_episodic=lambda: None,
                record_latency=lambda *a: None,
                index_durable_learning=lambda *a, **k: True,
                maybe_archive_inbox_source=lambda *a, **k: None,
            )
            st_auto = stage_candidate(
                {
                    "content": "Always pin TLS 1.3 minimum protocol version on internal RPC listeners.",
                    "category": "procedure",
                },
                request_id=2,
                context=gov_ctx_auto,
            )
            assert "result" in st_auto and st_auto["result"]["status"] == "accepted", st_auto

            st_prop = stage_candidate(
                {
                    "content": "Always flush write-ahead log checkpoints before snapshot backups.",
                    "category": "procedure",
                },
                request_id=3,
                context=gov_ctx,
            )
            cid_prop = int(st_prop["result"]["candidate_id"])
            rc = resolve_candidate(
                {"candidate_id": cid_prop, "decision": "accept"},
                request_id=4,
                context=gov_ctx,
            )
            assert "result" in rc and rc["result"]["new_status"] == "accepted", rc

        with patch("minni.minnid_runtime.afm.commit_prepared_learning", _abort_stale):
            with db.transaction() as c:
                c.execute(
                    """INSERT INTO candidate_packets
                       (principal, workspace_id, layer, privacy_level, requested_privacy,
                        content, instruction_like, status, proposed_at)
                       VALUES ('codex', 'default', 'procedure', 'safe', 'safe', ?, 0, 'proposed', ?)""",
                    (
                        "Backup manifest signatures use Ed25519 verification prior to archive rotation.",
                        time.time(),
                    ),
                )
                cid_afm = int(c.lastrowid)
            lid_afm = promote_candidate_durable(cid_afm, "afm-consolidation", afm_ctx)
            assert lid_afm is not None and lid_afm > 0

        # 2. BUG-29 in AFM `promote_candidate_durable`: re-promoting a superseded rule reopens doc
        rule_reopen = "Cold backup snapshot restorations run sqlite3 integrity_check on startup."
        r_init = handle_learn(
            {"content": rule_reopen, "category": "procedure", "force": True},
            request_id=5,
            context=gov_ctx,
        )
        lid_init = int(r_init["result"]["learning_id"])
        r_sup = handle_learn(
            {
                "content": "Switch cold backup verification to external sha256 manifest checks.",
                "category": "procedure",
                "force": True,
            },
            request_id=6,
            context=gov_ctx,
        )
        lid_sup = int(r_sup["result"]["learning_id"])
        with db.transaction() as c:
            doc_init = c.execute(
                "SELECT doc_id FROM learning_documents WHERE learning_id = ?", (lid_init,)
            ).fetchone()["doc_id"]
            doc_sup = c.execute(
                "SELECT doc_id FROM learning_documents WHERE learning_id = ?", (lid_sup,)
            ).fetchone()["doc_id"]
            retire_superseded_members(
                c,
                doc_id=int(doc_init),
                learning_ids=[lid_init],
                new_learning_id=lid_sup,
                new_doc_id=int(doc_sup),
                now=time.time(),
            )
            assert (
                c.execute(
                    "SELECT page_status FROM documents WHERE doc_id = ?", (int(doc_init),)
                ).fetchone()["page_status"]
                == "superseded"
            )
            c.execute(
                """INSERT INTO candidate_packets
                   (principal, workspace_id, layer, privacy_level, requested_privacy,
                    content, instruction_like, status, proposed_at)
                   VALUES ('codex', 'default', 'procedure', 'safe', 'safe', ?, 0, 'proposed', ?)""",
                (rule_reopen, time.time()),
            )
            cid_reopen = int(c.lastrowid)

        lid_reopened = promote_candidate_durable(cid_reopen, "afm-consolidation", afm_ctx)
        assert lid_reopened is not None and lid_reopened > 0
        with db.cursor() as c:
            assert (
                c.execute(
                    "SELECT status FROM candidate_packets WHERE candidate_id = ?",
                    (cid_reopen,),
                ).fetchone()["status"]
                == "accepted"
            )
            assert (
                c.execute(
                    "SELECT page_status FROM documents WHERE doc_id = ?", (int(doc_init),)
                ).fetchone()["page_status"]
                == "accepted"
            )

        # 3. BUG-29 permanent `privacy_level='blocked'` canonical doc transitions candidate out of 'proposed'
        blocked_text = "Restricted internal telemetry endpoint configuration snapshot."
        blocked_path = durable_doc_path("codex", "learning:blocked", cfg.vault_path, blocked_text)
        now = time.time()
        with db.transaction() as c:
            c.execute(
                """INSERT INTO documents
                   (path, agent, sigil, last_modified, indexed_at, page_status,
                    privacy_level, page_type, layer, memory_kind, memory_uri)
                   VALUES (?, 'codex', 'learning', ?, ?, 'accepted', 'blocked',
                           'learning', 'learning', 'learning', 'learning://blocked')""",
                (blocked_path, now, now),
            )
            c.execute(
                """INSERT INTO candidate_packets
                   (principal, workspace_id, layer, privacy_level, requested_privacy,
                    content, instruction_like, status, proposed_at)
                   VALUES ('codex', 'default', 'procedure', 'safe', 'safe', ?, 0, 'proposed', ?)""",
                (blocked_text, now),
            )
            cid_blocked = int(c.lastrowid)

        cons_dict: dict = {"promote_candidate_ids": [cid_blocked]}
        apply_consolidation_result(cons_dict, afm_ctx)
        assert cons_dict["applied"]["remaining_proposed"] == []
        with db.cursor() as c:
            cand_row = c.execute(
                "SELECT status, resolution_reason FROM candidate_packets WHERE candidate_id = ?",
                (cid_blocked,),
            ).fetchone()
            assert cand_row["status"] == "rejected"
            assert "canonical_restricted" in (cand_row["resolution_reason"] or "")
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM learnings WHERE content = ?", (blocked_text,)
                ).fetchone()[0]
                == 0
            )
    finally:
        db.close()


def test_bug30_classifier_eliminates_false_positive_auto_supersession(live_minnid):
    """BUG-30: All 4 false-positive repros NEVER auto-supersede, while true same-subject updates DO."""
    from minni.edge_classifier import classify_pair_deterministic

    sock = live_minnid["sock"]
    db_path = live_minnid["db_path"]

    false_positive_cases = [
        # Repro A: mid-sentence "replaces" across categories
        (
            "architecture",
            "Minni Python daemon uses SQLite WAL mode for concurrent recall queries.",
            "workflow",
            "In VaultIndexer frontmatter parsing, PyYAML safe_load replaces the legacy regex parser in Minni Python.",
        ),
        # Repro B1: disjoint key=value settings
        (
            "procedure",
            "Set SQLite busy_timeout=5000 on connections in MinniDB.",
            "procedure",
            "Set SQLite cache_size=-64000 on connections in MinniDB.",
        ),
        # Repro B2: distinct component/runtime subjects (engine/Python vs plugin/Node)
        (
            "procedure",
            "Minni engine requires Python 3.14 for local daemon setup.",
            "procedure",
            "Minni plugin requires Node 20 for local daemon setup.",
        ),
        # Repro B3: distinct server/protocol subjects (HTTP health vs gRPC metrics)
        (
            "procedure",
            "Bind the HTTP health server to port 8080 on localhost in daemon.",
            "procedure",
            "Bind the gRPC metrics server to port 9090 on localhost in daemon.",
        ),
    ]

    for idx, (cat_old, text_old, cat_new, text_new) in enumerate(false_positive_cases):
        edge = classify_pair_deterministic(
            {
                "content": text_new,
                "agent": "codex",
                "workspace_id": "workspace-fp",
                "category": cat_new,
            },
            {
                "body": text_old,
                "agent": "codex",
                "workspace_id": "workspace-fp",
                "category": cat_old,
            },
            pair_id=f"fp_{idx}",
        )
        assert edge.label not in ("updates", "contradicts"), (idx, edge)
        assert edge.confidence < 0.88, (idx, edge)

        r_old = _uds_rpc(
            sock,
            "learn",
            {
                "agent_id": "codex",
                "workspace_id": "workspace-fp",
                "category": cat_old,
                "content": text_old,
                "force": True,
            },
        )
        lid_old = int(r_old["result"]["learning_id"])
        r_new = _uds_rpc(
            sock,
            "learn",
            {
                "agent_id": "codex",
                "workspace_id": "workspace-fp",
                "category": cat_new,
                "content": text_new,
                "force": True,
            },
        )
        assert "result" in r_new and r_new["result"]["status"] == "ok", r_new

        conn = sqlite3.connect(str(db_path))
        try:
            row_old = conn.execute(
                "SELECT status, superseded_by FROM learnings WHERE learning_id = ?",
                (lid_old,),
            ).fetchone()
            assert row_old is not None, f"Existing learning {lid_old} was evicted on case {idx}!"
            assert row_old[1] is None, f"Existing learning {lid_old} was falsely superseded on case {idx}!"
        finally:
            conn.close()

    # True positive 1: identical entity tokens where only timeout value changed -> >= 0.96
    tp_same = classify_pair_deterministic(
        {
            "content": "Always use timeout 120s on daemon RPC sockets.",
            "agent": "codex",
            "workspace_id": "workspace-fp",
            "category": "procedure",
        },
        {
            "body": "Always use timeout 60s on daemon RPC sockets.",
            "agent": "codex",
            "workspace_id": "workspace-fp",
            "category": "procedure",
        },
        pair_id="tp_1",
    )
    assert tp_same.label == "updates" and tp_same.confidence >= 0.96, tp_same

    # True positive 2: anchored prefix covering all old entity tokens -> >= 0.96
    tp_anchored = classify_pair_deterministic(
        {
            "content": "Update: Always use timeout 180s on daemon RPC sockets for batch exports instead of 120s.",
            "agent": "codex",
            "workspace_id": "workspace-fp",
            "category": "procedure",
        },
        {
            "body": "Always use timeout 120s on daemon RPC sockets.",
            "agent": "codex",
            "workspace_id": "workspace-fp",
            "category": "procedure",
        },
        pair_id="tp_2",
    )
    assert tp_anchored.label == "updates" and tp_anchored.confidence >= 0.96, tp_anchored

    # Partial-overlap update (new rule drops an entity token from old rule) -> capped at 0.90 (review band)
    partial = classify_pair_deterministic(
        {
            "content": "Update: Always use timeout 180s on daemon sockets.",
            "agent": "codex",
            "workspace_id": "workspace-fp",
            "category": "procedure",
        },
        {
            "body": "Always use timeout 120s on daemon RPC sockets.",
            "agent": "codex",
            "workspace_id": "workspace-fp",
            "category": "procedure",
        },
        pair_id="partial_1",
    )
    assert partial.label == "updates" and 0.88 <= partial.confidence < 0.96, partial


def test_bug31_bug32_bug34_bug35_cleanup_regressions(tmp_path: Path, monkeypatch):
    """Verify BUG-31 (`ws_for_graph`), BUG-32 (workspace widening to `'global'`),
    BUG-34 (backticked wrapper mentions accepted vs real wrapper blocks rejected),
    and BUG-35 (`evict_candidate_to_cold_archive` `MinniConfig` fallback import).
    """
    from minni.durable_projection import evict_candidate_to_cold_archive
    from minni.graph_commit import ensure_canonical_learning_node
    from minni.minnid_runtime.governance import handle_learn
    from minni.retrieval import RetrievalEngine

    monkeypatch.setenv("MINNI_AFM_MODE", "off")
    cfg = MinniConfig(
        db_path=str(tmp_path / "cleanup.db"),
        vault_path=str(tmp_path / "vault"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_path=str(tmp_path / "learnings"),
    )
    Path(cfg.vault_path).mkdir(parents=True, exist_ok=True)
    db = MinniDB(cfg)
    try:
        run_migrations(db._get_conn())
        wb = WriteBackMemory(db, cfg)

        # --- BUG-32: content-identical same-agent learning across concrete workspaces widens to 'global'
        shared_rule = "Always verify SHA-256 digests before promoting staged release artifacts."
        now = time.time()
        with db.transaction() as c:
            c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id) "
                "VALUES ('codex', 'procedure', ?, ?, 'active', 'ws-alpha')",
                (shared_rule, now),
            )
            lid_a = int(c.lastrowid)
            doc_a = ensure_canonical_learning_node(
                c,
                learning_id=lid_a,
                agent_id="codex",
                content=shared_rule,
                vault_path=cfg.vault_path,
                created_at=now,
                workspace_id="ws-alpha",
            )
            assert (
                c.execute(
                    "SELECT workspace_id FROM documents WHERE doc_id = ?", (doc_a,)
                ).fetchone()["workspace_id"]
                == "ws-alpha"
            )

            c.execute(
                "INSERT INTO learnings (agent_id, category, content, created_at, status, workspace_id) "
                "VALUES ('codex', 'procedure', ?, ?, 'active', 'ws-beta')",
                (shared_rule, now + 1),
            )
            lid_b = int(c.lastrowid)
            doc_b = ensure_canonical_learning_node(
                c,
                learning_id=lid_b,
                agent_id="codex",
                content=shared_rule,
                vault_path=cfg.vault_path,
                created_at=now + 1,
                workspace_id="ws-beta",
            )
            assert doc_b == doc_a
            assert (
                c.execute(
                    "SELECT workspace_id FROM documents WHERE doc_id = ?", (doc_a,)
                ).fetchone()["workspace_id"]
                == "global"
            )

        # --- BUG-31: pinned principal passes pinned `ws_for_graph` when `workspace=None`
        engine = RetrievalEngine(db, cfg)
        captured_ws: list[str] = []

        def _spy_expand(*, db, store_id, seeds, principal, workspace, deadline_monotonic=None):
            captured_ws.append(workspace)
            return SimpleNamespace(neighbors=[])

        with db.transaction() as c:
            c.execute(
                "INSERT INTO vault_fts (doc_id, path, content, agent, sigil) VALUES (?, ?, ?, 'codex', 'learning')",
                (int(doc_a), f"{cfg.vault_path}/_durable/codex/shared.md", shared_rule),
            )
        pinned_principal = EffectivePrincipal(
            agent_id="codex", workspace_id="ws-alpha", capabilities=["read", "search"]
        )
        with patch("minni.graph_expansion.expand_typed_graph", side_effect=_spy_expand):
            res = engine.search(
                "SHA-256 digests staged release artifacts",
                mode="lexical",
                principal=pinned_principal,
                workspace=None,
            )
            assert len(res) >= 1
        assert captured_ws == ["ws-alpha"]

        # --- BUG-34: inline backticked `<MINNI_ENVELOPE>` / `<SYSTEM_MESSAGE>` prose is accepted
        prose_note = (
            "Recall wraps untrusted evidence inside `<MINNI_ENVELOPE>` tags while "
            "runtime task notifications arrive in `<SYSTEM_MESSAGE>` blocks."
        )
        gov_ctx = GovernanceContext(
            make_error=lambda code, msg, req_id: {
                "error": {"code": code, "message": msg},
                "id": req_id,
            },
            make_response=lambda res, req_id: {"result": res, "id": req_id},
            handler_principal=lambda p, req_id: (
                EffectivePrincipal(
                    agent_id="codex",
                    workspace_id="default",
                    capabilities=["*", "learn", "read"],
                ),
                None,
            ),
            lazy_writeback=lambda: wb,
            lazy_episodic=lambda: None,
            record_latency=lambda *a: None,
            index_durable_learning=lambda *a, **k: True,
            maybe_archive_inbox_source=lambda *a, **k: None,
            sovereign_db=lambda *a, **k: MinniDB(cfg),
        )
        st_ok = stage_candidate({"content": prose_note}, request_id=10, context=gov_ctx)
        assert "result" in st_ok, st_ok
        cid_ok = int(st_ok["result"]["candidate_id"])

        fb_verdict = evaluate_deterministic_quality_fallback(prose_note)
        assert fb_verdict.decision != "reject", fb_verdict
        fb_leak = evaluate_deterministic_quality_fallback(
            "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-11T00:00:00Z content=leak\n</SYSTEM_MESSAGE>"
        )
        assert fb_leak.decision == "reject", fb_leak

        hl_ok = handle_learn(
            {"content": prose_note, "category": "architecture", "force": True},
            request_id=11,
            context=gov_ctx,
        )
        assert "result" in hl_ok and hl_ok["result"]["status"] == "ok", hl_ok

        hl_leak = handle_learn(
            {
                "content": "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-11T00:00:00Z content=leak\n</SYSTEM_MESSAGE>",
                "category": "architecture",
                "force": True,
            },
            request_id=12,
            context=gov_ctx,
        )
        assert "error" in hl_leak, hl_leak

        # --- BUG-35: `evict_candidate_to_cold_archive` with `engine=None` imports `MinniConfig`
        # and reaches `maybe_archive_for_candidate` (previously `from minni.config import Config`
        # raised ImportError inside the try block and silently skipped archive cleanup).
        archive_calls: list[int] = []
        with db.transaction() as c:
            c.execute(
                """INSERT INTO candidate_packets
                   (principal, workspace_id, privacy_level, requested_privacy,
                    content, instruction_like, status, proposed_at)
                   VALUES ('codex', 'default', 'safe', 'safe', 'Ephemeral candidate to archive.', 0, 'rejected', ?)""",
                (now,),
            )
            evict_cid = int(c.lastrowid)
            with (
                patch("minni.config.MinniConfig", return_value=cfg),
                patch(
                    "minni.afm_passes.inbox_archive.maybe_archive_for_candidate",
                    side_effect=lambda _db, _cfg, cid_arg: archive_calls.append(int(cid_arg)),
                ),
            ):
                ev = evict_candidate_to_cold_archive(
                    c, None, evict_cid, reason="unit_test_bug35"
                )
        assert ev["evicted"] is True
        assert archive_calls == [evict_cid]
    finally:
        db.close()


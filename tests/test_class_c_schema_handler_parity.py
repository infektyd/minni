"""Class C regression tests: schema/handler parity for sm_drill (wikilink/handle) and sm_export_pack."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from minni.config import MinniConfig
from minni.db import MinniDB
from minni.durable_projection import durable_doc_path
from minni.minnid_runtime.recall import RecallContext, handle_sm_drill, handle_sm_export_pack
from minni.principal import EffectivePrincipal as Principal
from minni.retrieval import RetrievalEngine


def _make_recall_ctx(tmp_path: Path) -> tuple[MinniConfig, MinniDB, RetrievalEngine, RecallContext]:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "minni.db"
    cfg = MinniConfig(vault_path=str(vault_dir), db_path=str(db_path))
    db = MinniDB(cfg)
    engine = RetrievalEngine(db, cfg)

    principal = Principal(
        agent_id="claude-code",
        workspace_id="default",
        capabilities=frozenset({"recall", "search", "read", "drill", "export_pack"}),
        allowed_vault_roots=(str(vault_dir),),
    )

    ctx = SimpleNamespace(
        default_config=cfg,
        lazy_retrieval=lambda: engine,
        lazy_episodic=lambda: SimpleNamespace(),
        vault_retrieval_for=lambda aid: (engine, aid, Path(cfg.db_path)),
        agent_vault_retrieval=lambda aid: (engine, aid, Path(cfg.db_path)),
        all_vault_retrievals=lambda: [(engine, "claude-code", Path(cfg.db_path))],
        agent_vault=lambda aid: (Path(cfg.vault_path), False),
        handler_principal=lambda params, req_id: (principal, None),
        make_response=lambda data, req_id: {"result": data, "id": req_id},
        make_error=lambda code, msg, req_id, data=None: {
            "error": {"code": code, "message": msg},
            "id": req_id,
        },
        make_mismatch_error=lambda w, s, r: {"error": {"code": -32004, "message": "mismatch"}, "id": r},
        guard_vault_root=lambda *a, **kw: None,
        record_latency=lambda *a, **kw: None,
        trace_ring=lambda: None,
        increment_request_count=lambda: None,
        logger=SimpleNamespace(exception=lambda *a, **kw: None, warning=lambda *a, **kw: None),
    )
    return cfg, db, engine, ctx


def test_class_c_sm_drill_accepts_unbracketed_wikilink_and_top_level_wikilink_param(tmp_path: Path):
    cfg, _db, engine, ctx = _make_recall_ctx(tmp_path)
    body = "Durable rule for Class C drill wikilink parity verification."
    doc_path = durable_doc_path("claude-code", "learning:c1", cfg.vault_path, content=body)
    stem = Path(doc_path).stem  # e.g. claude-code__<16hex>
    engine.index_durable_document(
        content=body,
        path=doc_path,
        agent="claude-code",
        sigil="claude-code",
        page_status="accepted",
    )

    # 1. String reference without [[...]] brackets (as sent by minni_drill {wikilink: "claude-code__..."})
    resp1 = handle_sm_drill(
        {"references": [stem], "depth": "snippet", "agent_id": "claude-code"},
        request_id=1,
        context=ctx,
    )
    assert "result" in resp1, f"expected result, got {resp1}"
    assert resp1["result"]["count"] == 1, f"unbracketed wikilink ref must resolve, got {resp1}"
    assert "Durable rule for Class C" in resp1["result"]["results"][0]["text"]

    # 2. Dict reference {"wikilink": stem} without [[...]] brackets
    resp2 = handle_sm_drill(
        {"references": [{"wikilink": stem}], "depth": "snippet", "agent_id": "claude-code"},
        request_id=2,
        context=ctx,
    )
    assert resp2["result"]["count"] == 1, f"dict wikilink without brackets must resolve, got {resp2}"

    # 3. Top-level {"wikilink": stem, "depth": "snippet"} directly on sm_drill RPC
    resp3 = handle_sm_drill(
        {"wikilink": stem, "depth": "snippet", "agent_id": "claude-code"},
        request_id=3,
        context=ctx,
    )
    assert "result" in resp3, f"top-level wikilink param on sm_drill must succeed, got {resp3}"
    assert resp3["result"]["count"] == 1


def test_class_c_sm_export_pack_preserves_workspace_id_parity(tmp_path: Path):
    _cfg, _db, _engine, ctx = _make_recall_ctx(tmp_path)
    resp = handle_sm_export_pack(
        {"query": "parity", "budget_tokens": 512, "cache_key": "k1", "agent_id": "claude-code"},
        request_id=10,
        context=ctx,
    )
    assert "result" in resp
    identity = resp["result"]["prefix"]["identity"]
    assert identity["workspace_id"] == "default"

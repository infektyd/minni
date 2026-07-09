"""Caller privacy_level must not unlock AFM auto-promote for non-govern principals.

CSV finding: AFM wet consolidation promotes candidates whose privacy_level was
set by a learn-capable (non-govern) stager. stage_candidate must ignore caller
privacy unless the stamped principal is operator/govern.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))

import minni.minnid as minnid
import minni.minnid_runtime.provenance as provenance
from minni.principal import EffectivePrincipal, is_operator_principal


def _patch_db(monkeypatch, tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig

    cfg = SovereignConfig(
        db_path=str(tmp_path / "priv.db"),
        vault_path=str(tmp_path / "vault"),
    )
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(
        minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj, config=cfg)
    )
    return db_obj, cfg


def _stamp(monkeypatch, agent_id, capabilities):
    principal = EffectivePrincipal(agent_id=agent_id, capabilities=list(capabilities))
    monkeypatch.setattr(provenance, "resolve_effective_principal", lambda **_kw: principal)
    return principal


def _rpc_stage(content, privacy_level, request_id=1):
    return minnid._dispatch_sync(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "stage_candidate",
            "params": {
                "agent_id": "codex",
                "content": content,
                "workspace_id": "default",
                "privacy_level": privacy_level,
            },
        }
    )


def test_non_govern_stage_strips_caller_safe_privacy(monkeypatch, tmp_path):
    """Learn-capable non-govern stager cannot label a candidate safe for promote."""
    db_obj, cfg = _patch_db(monkeypatch, tmp_path)
    p = _stamp(monkeypatch, "codex", ["learn"])
    assert not is_operator_principal(p)

    content = "Durable lesson: always pin the schema version before renaming columns."
    resp = _rpc_stage(content, "safe")
    assert resp.get("result", {}).get("status") == "proposed", resp
    cid = resp["result"]["candidate_id"]

    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT privacy_level FROM candidate_packets WHERE candidate_id=?",
            (cid,),
        ).fetchone()
    assert row["privacy_level"] in (None, ""), row

    from minni.afm_passes import consolidation

    result = consolidation.run(
        db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t"
    )
    assert cid not in result["promote_candidate_ids"]
    assert cid in result["review_candidate_ids"]


def test_govern_stage_keeps_explicit_safe_privacy(monkeypatch, tmp_path):
    """Operator/govern may still stamp safe privacy for trusted auto-promote."""
    db_obj, cfg = _patch_db(monkeypatch, tmp_path)
    p = _stamp(monkeypatch, "codex", ["learn", "govern"])
    assert is_operator_principal(p)

    content = "Durable lesson: prefer lockfiles over floating dependency ranges."
    resp = _rpc_stage(content, "safe", request_id=2)
    assert resp.get("result", {}).get("status") == "proposed", resp
    cid = resp["result"]["candidate_id"]

    with db_obj.cursor() as c:
        row = c.execute(
            "SELECT privacy_level FROM candidate_packets WHERE candidate_id=?",
            (cid,),
        ).fetchone()
    assert row["privacy_level"] == "safe", row

    from minni.afm_passes import consolidation

    result = consolidation.run(
        db_obj, cfg, vault_path=cfg.vault_path, dry_run=True, trace_id="t2"
    )
    assert cid in result["promote_candidate_ids"]

"""A3 authz tests (audit C3; fixes a live incident): the daemon's ack/resolve
UDS RPC handlers must enforce principal authorization.

* ``minni_ack_handoff``: only the lease's recipient (``to_agent``) may ack —
  during a prior background run, co-located workflow subagents (all stamped
  'main' on the shared session MCP surface) acked and archived another agent's
  live inbox handoff.
* ``resolve_candidate``: the caller must be the candidate's owning principal,
  or an EXPLICITLY allowed operator (the literal ``resolve_candidate``/
  ``govern`` capability, the explicit ``operator`` principal, or the
  daemon-env ``MINNI_RESOLVE_OPERATORS`` allowlist). The blanket synthesized
  'main' wildcard is deliberately NOT enough — that is exactly what let
  subagents repeatedly re-resolve candidate_packets row 999.
* Resolution is terminal and once-only: re-resolving a terminal candidate is
  rejected instead of re-running the UPDATE + inbox-archive side effect.

Fixtures/tmpdirs only — the live ~/.minni state is never touched (DB path,
principals dir, and vault map are all patched per-test).
"""

import json
import os
import sqlite3
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

import minnid
from principal import EffectivePrincipal


# ── shared fixture plumbing (mirrors test_pr10_handoff._patch_handoff_db) ───

def _patch_db(monkeypatch, tmp_path):
    import db as db_mod
    from config import SovereignConfig

    cfg = SovereignConfig(db_path=str(tmp_path / "authz.db"))
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    monkeypatch.setattr(minnid, "_lazy_writeback", lambda: types.SimpleNamespace(db=db_obj))

    import config as cfg_mod
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"))
    return db_obj


def _stamp_principal(monkeypatch, agent_id, capabilities=("*",)):
    """Force the server-stamped EffectivePrincipal for the next handler calls
    (the handlers only ever trust the stamp, never wire strings)."""
    principal = EffectivePrincipal(agent_id=agent_id, capabilities=list(capabilities))
    monkeypatch.setattr(minnid, "resolve_effective_principal", lambda **_kw: principal)
    return principal


def _send_handoff(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "MINNI_AGENT_VAULTS",
        json.dumps({
            "codex": str(tmp_path / "codex-vault"),
            "claude-code": str(tmp_path / "claudecode-vault"),
        }),
    )
    sent = minnid._dispatch_sync({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "daemon.handoff",
        "params": {
            "from_agent": "codex",
            "to_agent": "claude-code",
            "packet": {
                "from_agent": "codex",
                "to_agent": "claude-code",
                "kind": "handoff",
                "task": "authz gate check",
                "envelope": "<sovereign:context event=\"Handoff\">evidence</sovereign:context>",
                "wikilink_refs": [],
                "trace_id": "trace-authz",
            },
        },
    })["result"]
    assert sent["delivered"] is True
    return sent


def _stage_candidate_as(monkeypatch, owner, content):
    _stamp_principal(monkeypatch, owner)
    resp = minnid._stage_candidate({"content": content, "workspace_id": "default"}, 1)
    assert resp.get("result", {}).get("status") == "proposed", resp
    return resp["result"]["candidate_id"]


# ── ack_handoff: recipient-only ─────────────────────────────────────────────

def test_ack_handoff_wrong_principal_rejected(monkeypatch, tmp_path):
    """A subagent stamped 'main' must NOT be able to ack (and thereby archive)
    a lease addressed to claude-code — the live-incident shape."""
    _patch_db(monkeypatch, tmp_path)
    _stamp_principal(monkeypatch, "codex")  # daemon.handoff stamps from_agent; keep it in the tmp vault map
    sent = _send_handoff(monkeypatch, tmp_path)
    inbox_path = Path(sent["inbox_path"])

    _stamp_principal(monkeypatch, "main")
    resp = minnid._handle_ack_handoff(
        {"lease_id": sent["lease_id"], "status": "accepted"}, 2
    )
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "principal_mismatch" in err.get("message", "")
    assert "claude-code" in err.get("message", "")
    # The live inbox file was NOT archived by the rejected ack.
    assert inbox_path.exists(), "rejected ack must not archive the live inbox file"
    assert not (inbox_path.parent / ".archive" / inbox_path.name).exists()
    # And the lease row is still pending.
    pending = minnid._pending_handoff_leases("claude-code")
    assert [p["lease_id"] for p in pending] == [sent["lease_id"]]


def test_ack_handoff_recipient_principal_succeeds(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    _stamp_principal(monkeypatch, "codex")
    sent = _send_handoff(monkeypatch, tmp_path)

    _stamp_principal(monkeypatch, "claude-code")
    resp = minnid._handle_ack_handoff(
        {"lease_id": sent["lease_id"], "status": "accepted"}, 3
    )
    assert "error" not in resp, resp
    assert resp["result"]["status"] == "accepted"
    # The recipient's ack archives the inbox copy (B1) as before.
    inbox_path = Path(sent["inbox_path"])
    assert not inbox_path.exists()
    assert (inbox_path.parent / ".archive" / inbox_path.name).is_file()


def test_ack_handoff_unknown_lease_still_not_found(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.setenv("MINNI_AGENT_VAULTS", json.dumps({"codex": str(tmp_path / "codex-vault")}))
    _stamp_principal(monkeypatch, "claude-code")
    resp = minnid._handle_ack_handoff({"lease_id": "missing-lease", "status": "accepted"}, 4)
    err = resp.get("error", {})
    assert err.get("code") == -32000
    assert "No handoff lease found" in err.get("message", "")


def test_ack_handoff_file_only_lease_authz_uses_packet_to_agent(monkeypatch, tmp_path):
    """When SQLite lease persistence degraded (file-only lease), the to_agent
    is read from the JSON packet — wrong principal still rejected."""
    db_obj = _patch_db(monkeypatch, tmp_path)
    _stamp_principal(monkeypatch, "codex")
    sent = _send_handoff(monkeypatch, tmp_path)
    # Drop the authoritative row to simulate degraded persistence.
    with db_obj.cursor() as c:
        c.execute("DELETE FROM handoff_leases WHERE lease_id = ?", (sent["lease_id"],))

    _stamp_principal(monkeypatch, "main")
    resp = minnid._handle_ack_handoff({"lease_id": sent["lease_id"], "status": "accepted"}, 5)
    assert resp.get("error", {}).get("code") == -32004, resp

    _stamp_principal(monkeypatch, "claude-code")
    ok = minnid._handle_ack_handoff({"lease_id": sent["lease_id"], "status": "accepted"}, 6)
    assert "error" not in ok, ok


# ── resolve_candidate: owner or explicitly allowed operator ─────────────────

def test_resolve_candidate_cross_principal_rejected(monkeypatch, tmp_path):
    """The synthesized wide-open 'main' (caps=['*']) may NOT resolve another
    principal's candidate — the exact live-incident caller shape."""
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    cid = _stage_candidate_as(monkeypatch, "codex", "a learning owned by codex")

    _stamp_principal(monkeypatch, "main", capabilities=["*"])
    resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 2)
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "principal_mismatch" in err.get("message", "")
    assert "'codex'" in err.get("message", "")
    # Candidate untouched.
    conn = sqlite3.connect(tmp_path / "authz.db")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT status FROM candidate_packets WHERE candidate_id=?", (cid,)
    ).fetchone()
    conn.close()
    assert row["status"] == "proposed"


def test_resolve_candidate_owner_succeeds(monkeypatch, tmp_path):
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    cid = _stage_candidate_as(monkeypatch, "codex", "codex resolves its own candidate")

    _stamp_principal(monkeypatch, "codex", capabilities=["*"])
    resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 3)
    assert resp.get("result", {}).get("new_status") == "accepted", resp
    assert resp["result"]["learning_id"]


def test_resolve_candidate_explicit_capability_allows_cross(monkeypatch, tmp_path):
    """A LITERAL resolve_candidate/govern capability (operator-authored
    principal file) is an explicit cross-principal grant."""
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    cid = _stage_candidate_as(monkeypatch, "codex", "operator-resolved candidate")

    _stamp_principal(monkeypatch, "main", capabilities=["*", "resolve_candidate"])
    resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "reject"}, 4)
    assert resp.get("result", {}).get("new_status") == "rejected", resp


def test_resolve_candidate_env_allowlist_allows_cross(monkeypatch, tmp_path):
    """MINNI_RESOLVE_OPERATORS is the daemon-env (operator-controlled) grant."""
    _patch_db(monkeypatch, tmp_path)
    cid = _stage_candidate_as(monkeypatch, "codex", "allowlisted operator candidate")

    monkeypatch.setenv("MINNI_RESOLVE_OPERATORS", "ops-console, main")
    _stamp_principal(monkeypatch, "main", capabilities=["*"])
    resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 5)
    assert resp.get("result", {}).get("new_status") == "accepted", resp


def test_resolve_candidate_already_resolved_is_terminal(monkeypatch, tmp_path):
    """Re-resolving a terminal candidate is rejected (the incident's repeat:
    every re-resolution re-ran the UPDATE + inbox-archive side effect)."""
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    cid = _stage_candidate_as(monkeypatch, "codex", "resolved exactly once")

    _stamp_principal(monkeypatch, "codex", capabilities=["*"])
    first = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 6)
    assert first.get("result", {}).get("new_status") == "accepted", first

    again = minnid._resolve_candidate({"candidate_id": cid, "decision": "reject"}, 7)
    err = again.get("error", {})
    assert err.get("code") == -32009, again
    assert "already_resolved" in err.get("message", "")


def test_resolve_candidate_owner_with_restricted_caps_succeeds(monkeypatch, tmp_path):
    """Review-panel regression: a restricted-capability platform agent (caps
    WITHOUT '*'/resolve_candidate/govern — is_operator_principal is False)
    must still be able to resolve its OWN candidate. The old up-front
    is_operator_principal gate rejected it with 'operator_only' before the
    owner check was ever reached."""
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    cid = _stage_candidate_as(monkeypatch, "codex", "restricted-caps self-resolution")

    _stamp_principal(monkeypatch, "codex", capabilities=["read"])
    resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 8)
    assert resp.get("result", {}).get("new_status") == "accepted", resp
    assert resp["result"]["learning_id"]

    # ...and an empty capabilities list works too (owner is owner).
    cid2 = _stage_candidate_as(monkeypatch, "codex", "empty-caps self-resolution")
    _stamp_principal(monkeypatch, "codex", capabilities=[])
    resp2 = minnid._resolve_candidate({"candidate_id": cid2, "decision": "reject"}, 9)
    assert resp2.get("result", {}).get("new_status") == "rejected", resp2


def test_resolve_candidate_restricted_caps_cross_principal_still_rejected(monkeypatch, tmp_path):
    """Dropping the up-front operator gate must NOT widen cross-principal
    access: a restricted-caps non-owner is still rejected (principal_mismatch
    instead of the old operator_only)."""
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    cid = _stage_candidate_as(monkeypatch, "codex", "still protected from non-owners")

    _stamp_principal(monkeypatch, "grok", capabilities=["read"])
    resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 10)
    err = resp.get("error", {})
    assert err.get("code") == -32004, resp
    assert "principal_mismatch" in err.get("message", "")


def test_resolve_candidate_govern_capability_allows_cross(monkeypatch, tmp_path):
    """'govern' alone (no wildcard, no resolve_candidate) is an explicit
    cross-principal grant — pins the govern branch of
    _explicitly_allowed_operator."""
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    cid = _stage_candidate_as(monkeypatch, "codex", "governed candidate")

    _stamp_principal(monkeypatch, "main", capabilities=["govern"])
    resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "reject"}, 11)
    assert resp.get("result", {}).get("new_status") == "rejected", resp


def test_resolve_candidate_operator_agent_id_allows_cross(monkeypatch, tmp_path):
    """The explicit 'operator' principal (no wildcard cap) is an explicit
    cross-principal grant — pins the agent_id=='operator' branch of
    _explicitly_allowed_operator."""
    _patch_db(monkeypatch, tmp_path)
    monkeypatch.delenv("MINNI_RESOLVE_OPERATORS", raising=False)
    cid = _stage_candidate_as(monkeypatch, "codex", "operator-principal candidate")

    _stamp_principal(monkeypatch, "operator", capabilities=[])
    resp = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 12)
    assert resp.get("result", {}).get("new_status") == "accepted", resp


def test_resolve_candidate_owner_resolution_drains_inbox_exactly_once(monkeypatch, tmp_path):
    """B1 side effect on the authz success path: a restricted-caps owner
    resolving its own inbox-sourced candidate archives the source file, and
    the terminal re-resolution attempt does NOT re-run the archive."""
    from test_inbox_ingest import _make_db, _stop_doc, _write_inbox_file
    from afm_passes.inbox_ingest import ingest
    import config as cfg_mod

    db_obj, cfg = _make_db(tmp_path)
    inbox = tmp_path / "codex-vault" / "inbox"
    _write_inbox_file(inbox, "authz-drain.json", _stop_doc(["a lesson the owner resolves"]))
    res = ingest(db_obj, cfg, inboxes=[inbox], dry_run=False)
    assert res["inserted"] == 1

    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "db_path", cfg.db_path)
    monkeypatch.setattr(cfg_mod.DEFAULT_CONFIG, "vault_path", str(tmp_path / "vault"))
    with db_obj.cursor() as c:
        c.execute("SELECT candidate_id FROM candidate_packets WHERE principal='codex'")
        (cid,) = [r["candidate_id"] for r in c.fetchall()]

    # Owner with NON-operator caps: authz passes via ownership alone.
    _stamp_principal(monkeypatch, "codex", capabilities=["read"])
    first = minnid._resolve_candidate({"candidate_id": cid, "decision": "accept"}, 13)
    assert first.get("result", {}).get("new_status") == "accepted", first
    assert not (inbox / "authz-drain.json").exists(), "file must leave the live inbox"
    assert (inbox / ".archive" / "authz-drain.json").is_file()

    # Terminal re-resolution: rejected AND no second archive artifact.
    again = minnid._resolve_candidate({"candidate_id": cid, "decision": "reject"}, 14)
    assert again.get("error", {}).get("code") == -32009, again
    archived = sorted(p.name for p in (inbox / ".archive").iterdir())
    assert archived == ["authz-drain.json"], archived

"""Tests for the propagate.py verify ok-predicate (B6ii, audit C4).

The old predicate passed vacuously: when the daemon socket was missing the
``daemon_read_has_*`` keys were simply absent (only ``daemon_read_error`` was
set), and ``all()`` over the present keys reported ok=True. The honest
predicate requires every daemon-read key to be present AND True, and any
``*_error`` key forces ok=False.
"""

import sys
from pathlib import Path

SCRIPTS_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import propagate  # noqa: E402


def test_native_afm_env_is_emitted_when_repo_helper_exists(tmp_path):
    repo = tmp_path / "Minni"
    helper = repo / "src" / "minni" / "native_afm_helper"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")

    env = propagate.native_afm_env(repo)

    assert env == {
        "MINNI_AFM_PROVIDER_MODE": "native",
        "MINNI_AFM_NATIVE_HELPER": str(helper),
    }


def test_native_afm_env_falls_back_to_legacy_engine_layout(tmp_path):
    # Un-migrated checkouts still have the flat engine/ dir; propagate must
    # keep stamping their helper path until they pull the v0.2 rename.
    repo = tmp_path / "Minni"
    helper = repo / "engine" / "native_afm_helper"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\n", encoding="utf-8")

    env = propagate.native_afm_env(repo)

    assert env["MINNI_AFM_NATIVE_HELPER"] == str(helper)


def test_mcp_json_preserves_existing_afm_env_over_repo_default(tmp_path):
    server = tmp_path / "install" / "dist" / "server.js"
    server.parent.mkdir(parents=True)
    server.write_text("", encoding="utf-8")

    manifest = propagate.mcp_json(
        server,
        "codex",
        tmp_path / "codex-vault",
        tmp_path / "minnid.sock",
        tmp_path / "workspace",
        pre_existing_env={
            "MINNI_AFM_PROVIDER_MODE": "off",
            "MINNI_AFM_NATIVE_HELPER": "/custom/helper",
        },
        afm_env={
            "MINNI_AFM_PROVIDER_MODE": "native",
            "MINNI_AFM_NATIVE_HELPER": "/repo/helper",
        },
    )

    env = manifest["mcpServers"]["minni"]["env"]
    assert env["MINNI_AGENT_ID"] == "codex"
    assert env["MINNI_AFM_PROVIDER_MODE"] == "off"
    assert env["MINNI_AFM_NATIVE_HELPER"] == "/custom/helper"


def _good_checks():
    return {
        "agent_api_returncode": 0,
        "agent_api_has_identity": True,
        "agent_api_has_map_rule": True,
        "agent_api_no_personality": True,
        "daemon_read_has_identity": True,
        "daemon_read_has_map_rule": True,
    }


def test_verify_ok_all_required_true():
    assert propagate.verify_ok(_good_checks()) is True


def test_missing_daemon_socket_reports_not_ok():
    """B6ii gate: socket missing -> daemon_read_error set, daemon keys absent
    -> ok must be False (previously passed vacuously)."""
    checks = _good_checks()
    del checks["daemon_read_has_identity"]
    del checks["daemon_read_has_map_rule"]
    checks["daemon_read_error"] = "socket missing: /tmp/nope.sock"
    assert propagate.verify_ok(checks) is False


def test_absent_daemon_keys_without_error_still_not_ok():
    checks = _good_checks()
    del checks["daemon_read_has_identity"]
    del checks["daemon_read_has_map_rule"]
    assert propagate.verify_ok(checks) is False


def test_any_error_key_forces_not_ok():
    checks = _good_checks()
    checks["daemon_read_error"] = "connection refused"
    assert propagate.verify_ok(checks) is False


def test_false_required_check_not_ok():
    checks = _good_checks()
    checks["daemon_read_has_map_rule"] = False
    assert propagate.verify_ok(checks) is False


# ── verify() COMMAND-path coverage (C9 follow-up) ────────────────────────────
# The predicate above is pure; these exercise the actual command function
# (agent_api subprocess probe + daemon socket read + JSON report + exit code).
# Hermetic: subprocess.run and socket_rpc are faked, the socket path lives in
# tmp_path — no live engine, daemon, or ~/.minni state is touched.

import argparse  # noqa: E402
import json  # noqa: E402
import types  # noqa: E402
import sqlite3  # noqa: E402


def _identity_text(agent: str) -> str:
    return (
        f"## Agent Identity: {agent.title()}\n"
        "Minni gives hosted agents a map of prior work.\n"
        "It does not define personality.\n"
    )


def _fake_agent_api(monkeypatch, agent: str, returncode: int = 0):
    calls = []

    def fake_run(cmd, cwd=None, text=None, capture_output=True, check=False):
        calls.append(cmd)
        return types.SimpleNamespace(
            returncode=returncode, stdout=_identity_text(agent), stderr=""
        )

    monkeypatch.setattr(propagate.subprocess, "run", fake_run)
    return calls


def test_verify_command_ok_path(tmp_path, capsys, monkeypatch):
    """Socket present + healthy agent_api + daemon read -> ok, exit 0."""
    sock = tmp_path / "minnid.sock"
    sock.write_text("")  # verify only checks existence before socket_rpc
    calls = _fake_agent_api(monkeypatch, "codex")
    monkeypatch.setattr(
        propagate,
        "socket_rpc",
        lambda *_a, **_k: {"result": {"context": _identity_text("codex")}},
    )

    args = argparse.Namespace(agent="codex", workspace=str(tmp_path), socket=str(sock))
    rc = propagate.verify(args)
    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert report["ok"] is True
    assert report["checks"]["agent_api_has_identity"] is True
    assert report["checks"]["daemon_read_has_identity"] is True
    assert report["checks"]["daemon_read_has_map_rule"] is True
    # The agent_api probe actually ran (command path, not just the predicate).
    assert calls and "--identity" in calls[0]


def test_verify_command_socket_missing_exits_1(tmp_path, capsys, monkeypatch):
    """Missing socket -> daemon_read_error + ok False + exit 1 (B6ii gate,
    end-to-end through the command function)."""
    _fake_agent_api(monkeypatch, "codex")
    args = argparse.Namespace(
        agent="codex", workspace=str(tmp_path), socket=str(tmp_path / "nope.sock")
    )
    rc = propagate.verify(args)
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["ok"] is False
    assert "socket missing" in report["checks"]["daemon_read_error"]
    assert "daemon_read_has_identity" not in report["checks"]


def test_verify_command_socket_rpc_failure_exits_1(tmp_path, capsys, monkeypatch):
    """socket exists but the RPC blows up -> daemon_read_error + exit 1."""
    sock = tmp_path / "minnid.sock"
    sock.write_text("")
    _fake_agent_api(monkeypatch, "codex")

    def boom(*_a, **_k):
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(propagate, "socket_rpc", boom)
    args = argparse.Namespace(agent="codex", workspace=str(tmp_path), socket=str(sock))
    rc = propagate.verify(args)
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["ok"] is False
    assert "connection refused" in report["checks"]["daemon_read_error"]


def test_verify_command_via_main_argparse_wiring(tmp_path, capsys, monkeypatch):
    """`propagate.py --socket ... verify --agent ... --workspace ...` reaches
    verify() through main()'s subparser wiring and propagates the exit code."""
    _fake_agent_api(monkeypatch, "codex")
    monkeypatch.setattr(
        "sys.argv",
        [
            "propagate.py",
            "--socket", str(tmp_path / "nope.sock"),
            "verify",
            "--agent", "codex",
            "--workspace", str(tmp_path),
        ],
    )
    rc = propagate.main()
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["ok"] is False
    assert "socket missing" in report["checks"]["daemon_read_error"]


def test_grok_hosted_envelope_uses_canonical_template_without_bespoke_sections():
    text = propagate.render_hosted_envelope(
        "grok-build",
        "workspace-pixelagents",
        Path("/tmp/minnid.sock"),
        Path("/tmp/grok-build-vault"),
    )

    assert "## Persona (agent-authored)" in text
    assert "## Operating Quirks (agent-curated launchpad)" in text
    assert "## Grok-Build Platform Notes" not in text
    assert "## Important Operating Facts (curated for this surface)" not in text
    assert "## Shelf Contract (post-rename)" not in text


def test_render_hosted_envelope_preserves_existing_agent_persona():
    existing = """# Old Envelope

## Persona (agent-authored)
I prefer terse, technical answers.
Keep my local verification habits visible.

## Operating Quirks (agent-curated launchpad)
- stale generated quirk
"""

    text = propagate.render_hosted_envelope(
        "codex",
        "workspace-minni",
        Path("/tmp/minnid.sock"),
        Path("/tmp/codex-vault"),
        existing_content=existing,
    )

    assert "I prefer terse, technical answers." in text
    assert "Keep my local verification habits visible." in text
    assert "stale generated quirk" not in text


def test_render_hosted_envelope_preserves_persona_with_crlf_heading_whitespace():
    existing = (
        "# Old Envelope\r\n\r\n"
        "## Persona (agent-authored) \t\r\n"
        "CRLF persona survives.\r\n\r\n"
        "## Operating Quirks (agent-curated launchpad)\r\n"
        "- stale generated quirk\r\n"
    )

    text = propagate.render_hosted_envelope(
        "codex",
        "workspace-minni",
        Path("/tmp/minnid.sock"),
        Path("/tmp/codex-vault"),
        existing_content=existing,
    )

    assert "CRLF persona survives." in text
    assert "stale generated quirk" not in text


def test_seed_hosted_preserves_existing_persona_on_rerender(tmp_path, monkeypatch):
    identity_root = tmp_path / "identities"
    source_dir = identity_root / "codex"
    source_dir.mkdir(parents=True)
    source_path = source_dir / "CODEX_HOSTED_AGENT_ENVELOPE.md"
    source_path.write_text(
        """# Old Envelope

## Persona (agent-authored)
Preserve this voice.

## Operating Quirks (agent-curated launchpad)
- old generated quirk
""",
        encoding="utf-8",
    )

    engine_dir = tmp_path / "engine"
    engine_dir.mkdir()
    (engine_dir / "seed_identity.py").write_text(
        "def get_embedding(_content):\n"
        "    return b'fake-embedding'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(propagate, "DEFAULT_IDENTITY_ROOT", identity_root)
    monkeypatch.setattr(propagate, "repo_engine", lambda _workspace: engine_dir)
    monkeypatch.setattr(propagate, "vault_for", lambda _agent: tmp_path / "codex-vault")

    db_path = tmp_path / "minni.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE documents (
                doc_id INTEGER PRIMARY KEY,
                path TEXT,
                agent TEXT,
                sigil TEXT,
                last_modified REAL,
                indexed_at REAL,
                whole_document INTEGER,
                workspace_id TEXT,
                layer TEXT,
                page_status TEXT,
                privacy_level TEXT,
                page_type TEXT
            );
            CREATE TABLE chunk_embeddings (
                doc_id INTEGER,
                chunk_index INTEGER,
                chunk_text TEXT,
                embedding BLOB,
                model_name TEXT,
                computed_at REAL,
                layer TEXT
            );
            CREATE TABLE vault_fts (
                doc_id INTEGER,
                path TEXT,
                content TEXT,
                agent TEXT,
                sigil TEXT
            );
            """
        )

    args = argparse.Namespace(
        agent="codex",
        workspace=str(tmp_path),
        db=str(db_path),
        socket=str(tmp_path / "minnid.sock"),
    )
    rc = propagate.seed_hosted(args)

    rendered = source_path.read_text(encoding="utf-8")
    assert rc == 0
    assert "Preserve this voice." in rendered
    assert "old generated quirk" not in rendered


def test_engine_is_package_distinguishes_layouts(tmp_path):
    pkg = tmp_path / "src" / "minni"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    legacy = tmp_path / "engine"
    legacy.mkdir()

    assert propagate.engine_is_package(pkg) is True
    assert propagate.engine_is_package(legacy) is False

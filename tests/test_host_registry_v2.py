"""Regression tests for §4.2: Canonical Host Registry (wired.json v2) & Keyed-Subset Diff Semantics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_host_surface_catalog_covers_all_28_surface_groups_and_expands_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.2 & N7: Catalog covers all fleet hosts/surfaces and expands ~ against HOME without leaking literal ~."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from minni.wire.platform import HOST_SURFACE_CATALOG, expand_host_surface_catalog

    expected_hosts = {
        "claude-code",
        "claude-desktop",
        "codex",
        "grok-build",
        "antigravity",
        "cursor",
        "kilocode",
        "devin",
        "muse",
    }
    assert expected_hosts.issubset(set(HOST_SURFACE_CATALOG.keys()))

    expanded = expand_host_surface_catalog(home=tmp_path, base_semver="0.6.0")
    serialized = json.dumps(expanded)
    assert "~" not in serialized, "Expanded catalog must never contain literal ~ (N7)"
    assert str(tmp_path) in serialized

    # Verify Antigravity preserves mcp-env-run (P2-5)
    agy_surfaces = expanded["antigravity"]["surfaces"]
    mcp_configs = [s for s in agy_surfaces if s["kind"] == "mcp_json" and "mcp_config.json" in s["path"]]
    assert len(mcp_configs) == 4
    for surf in mcp_configs:
        assert surf["command"] == str(tmp_path / ".agents/bin/mcp-env-run")
        assert surf["args_prefix"] == ["node"]


def test_keyed_subset_and_forbidden_key_diff_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.2 (P1-5, P2-3, N7): Keyed-subset diff allows optional keys on Claude Desktop (delta==0) and flags forbidden keys/tilde."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from minni.wire.registry import evaluate_mcp_surface_diff

    helper_bin = tmp_path / ".minni" / "bin" / "native_afm_helper"
    helper_bin.parent.mkdir(parents=True)
    helper_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    surface_spec = {
        "kind": "mcp_json",
        "path": str(tmp_path / "Library/Application Support/Claude/claude_desktop_config.json"),
        "key": "mcpServers.minni",
        "entry": str(tmp_path / ".minni/plugin/current/dist/server.js"),
        "accepted_entries": [
            str(tmp_path / ".minni/plugin/current/dist/server.js"),
            str(tmp_path / ".agents/plugins/Minni@Minni/dist/server.js"),
        ],
    }

    # 1. Live Claude Desktop shape with accepted Minni@Minni path + optional SOCKET/AFM keys -> delta == 0
    live_entry_ok = {
        "command": "/opt/homebrew/bin/node",
        "args": [str(tmp_path / ".agents/plugins/Minni@Minni/dist/server.js")],
        "env": {
            "MINNI_AGENT_ID": "claude-code",
            "MINNI_VAULT_PATH": str(tmp_path / ".minni/claudecode-vault"),
            "MINNI_SOCKET_PATH": str(tmp_path / ".minni/run/minnid.sock"),
            "MINNI_AFM_PROVIDER_MODE": "native",
            "MINNI_AFM_NATIVE_HELPER": str(helper_bin),
            "CUSTOM_USER_VAR": "keep-me",
        },
    }
    diff_ok = evaluate_mcp_surface_diff(
        live_entry_ok,
        host_id="claude-desktop",
        agent_id="claude-code",
        vault_path=str(tmp_path / ".minni/claudecode-vault"),
        surface_spec=surface_spec,
        home=tmp_path,
    )
    assert diff_ok.has_delta is False, f"Expected delta==0, got reasons: {diff_ok.reasons}"
    assert diff_ok.desired_entry["env"]["CUSTOM_USER_VAR"] == "keep-me"
    assert diff_ok.desired_entry["env"]["MINNI_SOCKET_PATH"] == str(tmp_path / ".minni/run/minnid.sock")

    # 2. Forbidden MINNI_WORKSPACE_ID -> delta != 0 and stripped from desired_entry
    live_with_ws = json.loads(json.dumps(live_entry_ok))
    live_with_ws["env"]["MINNI_WORKSPACE_ID"] = "workspace-minni"
    diff_ws = evaluate_mcp_surface_diff(
        live_with_ws,
        host_id="claude-desktop",
        agent_id="claude-code",
        vault_path=str(tmp_path / ".minni/claudecode-vault"),
        surface_spec=surface_spec,
        home=tmp_path,
    )
    assert diff_ws.has_delta is True
    assert any("MINNI_WORKSPACE_ID" in r for r in diff_ws.reasons)
    assert "MINNI_WORKSPACE_ID" not in diff_ws.desired_entry["env"]

    # 3. Nonexistent MINNI_AFM_NATIVE_HELPER -> delta != 0
    live_bad_helper = json.loads(json.dumps(live_entry_ok))
    live_bad_helper["env"]["MINNI_AFM_NATIVE_HELPER"] = str(tmp_path / "missing/native_afm_helper")
    diff_helper = evaluate_mcp_surface_diff(
        live_bad_helper,
        host_id="claude-desktop",
        agent_id="claude-code",
        vault_path=str(tmp_path / ".minni/claudecode-vault"),
        surface_spec=surface_spec,
        home=tmp_path,
    )
    assert diff_helper.has_delta is True
    assert any("MINNI_AFM_NATIVE_HELPER" in r for r in diff_helper.reasons)
    assert "MINNI_AFM_NATIVE_HELPER" not in diff_helper.desired_entry["env"]

    # 4. Literal ~ in path or env -> delta != 0 and expanded in desired_entry (N7)
    live_tilde = json.loads(json.dumps(live_entry_ok))
    live_tilde["env"]["MINNI_VAULT_PATH"] = "~/.minni/claudecode-vault"
    diff_tilde = evaluate_mcp_surface_diff(
        live_tilde,
        host_id="claude-desktop",
        agent_id="claude-code",
        vault_path=str(tmp_path / ".minni/claudecode-vault"),
        surface_spec=surface_spec,
        home=tmp_path,
    )
    assert diff_tilde.has_delta is True
    assert diff_tilde.desired_entry["env"]["MINNI_VAULT_PATH"] == str(tmp_path / ".minni/claudecode-vault")

    # 5. Forbidden plugin-root cwd -> delta != 0 and removed from desired_entry
    live_cwd = json.loads(json.dumps(live_entry_ok))
    live_cwd["cwd"] = str(tmp_path / ".minni/plugin/0.6.0+git.deadbeef")
    diff_cwd = evaluate_mcp_surface_diff(
        live_cwd,
        host_id="claude-desktop",
        agent_id="claude-code",
        vault_path=str(tmp_path / ".minni/claudecode-vault"),
        surface_spec=surface_spec,
        home=tmp_path,
    )
    assert diff_cwd.has_delta is True
    assert "cwd" not in diff_cwd.desired_entry


def test_wired_v2_schema_and_ui_scanner_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.2 & R4-3: wired.json v2 schema records ui_scanner_gate and defaults unverified containers to staged_copy."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from minni.wire.wired import (
        build_wired_v2,
        effective_container_mode,
        record_ui_scanner_gate,
    )

    data = build_wired_v2(
        active_version="0.6.0+git.af0c04f7",
        base_semver="0.6.0",
        git_commit_sha="af0c04f711111111111111111111111111111111",
        home=tmp_path,
    )
    assert data["schema_version"] == 2
    assert data["stable_root"] == str(tmp_path / ".minni/plugin/current")
    assert "~" not in json.dumps(data)

    # Unverified gate defaults to staged_copy (R4-3)
    assert effective_container_mode("antigravity", data) == "staged_copy"
    assert effective_container_mode("cursor", data) == "staged_copy"

    # Record verified ui_scanner_gate for antigravity
    updated = record_ui_scanner_gate(
        "antigravity",
        container_mode="symlink_subtrees",
        evidence="gemini extensions list + UI check",
        home=tmp_path,
    )
    gate = updated["hosts"]["antigravity"]["ui_scanner_gate"]
    assert gate["verified"] is True
    assert gate["container_mode"] == "symlink_subtrees"
    assert effective_container_mode("antigravity", updated) == "symlink_subtrees"
    assert effective_container_mode("cursor", updated) == "staged_copy"

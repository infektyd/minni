"""Round 13 surgical regression tests for H24 and H29-H33."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SOURCE_PLUGIN = REPO / "plugins" / "minni"
PROPAGATE_PATH = SOURCE_PLUGIN / "skills" / "minni-install" / "scripts" / "propagate.py"
CHECK_DEPLOYMENTS_PATH = REPO / "scripts" / "check_deployments.py"


def _load_propagate():
    spec = importlib.util.spec_from_file_location("minni_propagate_r13", PROPAGATE_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_verified_payload(root: Path, *, version: str, git_sha: str, server_body: str = "// dist/server.js\nconsole.log('ok');\n") -> None:
    """Create a verified payload tree with valid payload-manifest.json and build-manifest.json."""
    from minni.wire.install import sanitize_staged_payload

    root.mkdir(parents=True, exist_ok=True)
    for sub in (
        "frontend",
        "hooks",
        "commands",
        "skills",
        ".claude-plugin",
        ".codex-plugin",
        ".cursor-plugin",
        ".kilocode-plugin",
        ".gemini-plugin",
    ):
        src = SOURCE_PLUGIN / sub
        dst = root / sub
        if dst.exists():
            shutil.rmtree(dst)
        if src.is_dir():
            shutil.copytree(src, dst)
    shutil.copy2(SOURCE_PLUGIN / "package.json", root / "package.json")
    shutil.copy2(SOURCE_PLUGIN / "README.md", root / "README.md")

    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "server.js").write_text(server_body, encoding="utf-8")
    (dist / "hook.js").write_text("// hook\n", encoding="utf-8")
    (dist / "grok-hook.js").write_text("// grok-hook\n", encoding="utf-8")
    (dist / "gemini-hook.js").write_text("// gemini-hook\n", encoding="utf-8")
    (dist / "cursor-hook.js").write_text("// cursor-hook\n", encoding="utf-8")
    (dist / "build-manifest.json").write_text(
        json.dumps({"git_sha": git_sha, "built_at": "2026-11-02T00:00:00Z", "git_dirty": False}),
        encoding="utf-8",
    )

    sanitize_staged_payload(root, version=version, remove_mcp_json=True)

    files: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != "payload-manifest.json":
            rel = p.relative_to(root).as_posix()
            files[rel] = f"sha256:{hashlib.sha256(p.read_bytes()).hexdigest()}"

    manifest_doc = {
        "schema": 1,
        "version": version,
        "git_sha": git_sha,
        "built_at": "2026-11-02T00:00:00Z",
        "node_engine": ">=20",
        "files": files,
    }
    (root / "payload-manifest.json").write_text(json.dumps(manifest_doc, indent=2) + "\n", encoding="utf-8")


def test_h29_native_hook_plan_accepts_leading_exec_and_rejects_compound_shells(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H29: _native_hook_plan accepts leading `exec` before node/nodejs, retargets token 1, and rejects compound shells."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    old_grok_root = home / ".minni" / "plugin" / "current"
    new_grok_root = home / ".minni" / "plugin" / "0.6.0+git.3509b49e"
    old_agy_root = home / ".agents" / "plugins" / "Minni@Minni"
    new_agy_root = home / ".agents" / "plugins" / "Minni@Minni"

    # Operator's exact shape from ~/.grok/hooks/minni.json
    grok_hooks_path = home / ".grok" / "hooks" / "minni.json"
    grok_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    grok_operator_doc = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"exec node {old_grok_root}/dist/grok-hook.js SessionStart",
                            "timeout": 10,
                        }
                    ]
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"exec node {old_grok_root}/dist/grok-hook.js UserPromptSubmit",
                            "timeout": 5,
                        }
                    ]
                }
            ],
            "PreToolUse": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"exec node {old_grok_root}/dist/grok-hook.js PreToolUse",
                            "timeout": 5,
                        }
                    ]
                }
            ],
            "PreCompact": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"exec node {old_grok_root}/dist/grok-hook.js PreCompact",
                            "timeout": 15,
                        }
                    ]
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"exec node {old_grok_root}/dist/grok-hook.js Stop",
                            "timeout": 10,
                        }
                    ]
                }
            ],
        }
    }
    grok_hooks_path.write_text(json.dumps(grok_operator_doc, indent=2) + "\n", encoding="utf-8")

    # Operator's exact shape from ~/.gemini/config/plugins/minni/hooks.json
    agy_hooks_path = home / ".gemini" / "config" / "plugins" / "minni" / "hooks.json"
    agy_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    agy_operator_doc = {
        "minni": {
            "PreToolUse": [
                {
                    "matcher": "run_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"exec node {old_agy_root}/dist/gemini-hook.js PreToolUse",
                            "timeout": 10,
                        }
                    ],
                }
            ],
            "SessionStart": [
                {
                    "type": "command",
                    "command": f"exec node {old_agy_root}/dist/gemini-hook.js SessionStart",
                    "timeout": 20,
                }
            ],
            "PreInvocation": [
                {
                    "type": "command",
                    "command": f"exec node {old_agy_root}/dist/gemini-hook.js PreInvocation",
                    "timeout": 10,
                }
            ],
            "Stop": [
                {
                    "type": "command",
                    "command": f"exec node {old_agy_root}/dist/gemini-hook.js Stop",
                    "timeout": 10,
                }
            ],
        }
    }
    agy_hooks_path.write_text(json.dumps(agy_operator_doc, indent=2) + "\n", encoding="utf-8")

    propagate = _load_propagate()

    grok_res = propagate.update_grok_hooks(new_grok_root, existing_only=True)
    assert grok_res["installed"] is True
    updated_grok = json.loads(grok_hooks_path.read_text(encoding="utf-8"))
    for event_name in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PreCompact", "Stop"):
        cmd = updated_grok["hooks"][event_name][0]["hooks"][0]["command"]
        assert cmd == f"exec node {new_grok_root}/dist/grok-hook.js {event_name}"

    agy_res = propagate.update_agy_plugin_hooks(new_agy_root, existing_only=True)
    assert agy_res["installed"] is True
    updated_agy = json.loads(agy_hooks_path.read_text(encoding="utf-8"))
    assert (
        updated_agy["minni"]["PreToolUse"][0]["hooks"][0]["command"]
        == f"exec node {new_agy_root}/dist/gemini-hook.js PreToolUse"
    )
    for event_name in ("SessionStart", "PreInvocation", "Stop"):
        cmd = updated_agy["minni"][event_name][0]["command"]
        assert cmd == f"exec node {new_agy_root}/dist/gemini-hook.js {event_name}"

    # Reject compound shells even with leading exec
    for bad_cmd in (
        f"exec node {old_grok_root}/dist/grok-hook.js Stop; echo bad",
        f"exec node {old_grok_root}/dist/grok-hook.js Stop | cat",
        f"exec node $(pwd)/dist/grok-hook.js Stop",
    ):
        bad_doc = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": bad_cmd, "timeout": 10}]}]
            }
        }
        grok_hooks_path.write_text(json.dumps(bad_doc), encoding="utf-8")
        with pytest.raises(ValueError, match="unreadable or unsupported"):
            propagate._native_hook_plan("grok", new_grok_root)


def test_h30_single_server_js_writer_cursor_container_matches_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """H30: Cursor container receives the exact same dist/server.js bytes as current/version_dir and passes health."""
    from minni.wire.containers import converge_host_container, flip_current_pointer
    from minni.wire.cutover import _stage_repo_payload_to_temp
    from minni.wire.health import audit_host_propagation_health

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    # 1. Verify _stage_repo_payload_to_temp produces the exact same // dist/server.js header as bundle_server.mjs
    staged_tmp, manifest = _stage_repo_payload_to_temp(REPO)
    try:
        staged_srv_bytes = (staged_tmp / "dist" / "server.js").read_bytes()
        assert b"// dist/server.js\n" in staged_srv_bytes, (
            f"staged server.js missing canonical // dist/server.js module banner: {staged_srv_bytes[:80]!r}"
        )
        assert b"minni-cutover-payload-" not in staged_srv_bytes, (
            "staged server.js leaked temp staging path into esbuild bundle"
        )

        # Install into temp HOME version dir and flip current
        vdir = home / ".minni" / "plugin" / manifest.version
        shutil.copytree(staged_tmp, vdir)
        flip_current_pointer(manifest.version, home=home)
    finally:
        shutil.rmtree(staged_tmp, ignore_errors=True)

    # Also test when current/dist/server.js has distinct bundled payload bytes vs repo dist/server.js
    custom_bundled_bytes = b"// dist/server.js\n// canonical-bundled-payload-marker\n" + staged_srv_bytes
    (vdir / "dist" / "server.js").write_bytes(custom_bundled_bytes)
    pmanifest_path = vdir / "payload-manifest.json"
    pdoc = json.loads(pmanifest_path.read_text(encoding="utf-8"))
    pdoc["files"]["dist/server.js"] = f"sha256:{hashlib.sha256(custom_bundled_bytes).hexdigest()}"
    pmanifest_path.write_text(json.dumps(pdoc, indent=2) + "\n", encoding="utf-8")

    # Converge cursor container in staged_copy mode, then run propagate cursor sync
    converge_host_container(
        "cursor",
        home=home,
        base_semver="0.6.0",
        container_mode="staged_copy",
        target_version_root=vdir,
    )
    cursor_hooks = home / ".cursor" / "hooks.json"
    cursor_hooks.parent.mkdir(parents=True, exist_ok=True)
    cursor_hooks.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "stop": [
                        {
                            "command": f"exec node {home}/.cursor/plugins/local/minni/dist/cursor-hook.js stop",
                            "timeout": 10,
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    propagate = _load_propagate()
    monkeypatch.setattr(propagate, "bootstrap_vault", lambda _args: 0)
    monkeypatch.setattr(propagate, "native_afm_env", lambda _repo: {})
    cursor_container = home / ".cursor" / "plugins" / "local" / "minni"
    args = argparse.Namespace(
        repo=str(REPO),
        workspace=str(REPO),
        no_build=True,
        install_root=str(cursor_container),
        agent="cursor",
        socket=str(home / ".minni" / "run" / "minnid.sock"),
        existing_only=True,
    )
    propagate.update_one_plugin("cursor", args)

    current_srv = home / ".minni" / "plugin" / "current" / "dist" / "server.js"
    cursor_srv = cursor_container / "dist" / "server.js"
    assert cursor_srv.read_bytes() == current_srv.read_bytes()

    report = audit_host_propagation_health(home=home)
    mismatch_findings = [
        f
        for f in report.drifted_surfaces
        if "container_dist_server_js_mismatch" in str(f.get("reasons") or f.get("reason") or "")
    ]
    assert mismatch_findings == [], f"Unexpected mismatch findings: {mismatch_findings}"


def test_h31_check_deployments_models_h17_sanitizer_and_host_rendered_gemini_extension(tmp_path: Path) -> None:
    """H31: check_deployments.py models the H17 sanitizer and treats .gemini-plugin/gemini-extension.json as host-materialized."""
    from minni.wire.containers import (
        _write_container_manifests,
        render_antigravity_container_manifests,
    )
    from minni.wire.install import sanitize_staged_payload

    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "plugins").symlink_to(REPO / "plugins")

    git_sha = "unknown"
    version = "0.6.0+git.3509b49e"

    # 1. Active wire version dir (sanitized by H17 sanitize_staged_payload)
    wire_root = home / ".minni" / "plugin" / version
    _write_verified_payload(wire_root, version=version, git_sha=git_sha)
    claude_cfg = home / ".claude.json"
    claude_cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "minni": {
                        "command": "node",
                        "args": [str(wire_root / "dist" / "server.js")],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (home / ".minni" / "plugin" / "wired.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "wires": [
                    {
                        "platform": "claude-code",
                        "install_root": str(wire_root),
                        "config_path": str(claude_cfg),
                        "wired_at": "2026-11-02T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # 2. Antigravity container (sanitized + host-rendered .gemini-plugin/gemini-extension.json)
    ag_root = home / ".agents" / "plugins" / "Minni@Minni"
    _write_verified_payload(ag_root, version="0.6.0", git_sha=git_sha)
    _write_container_manifests(
        ag_root,
        render_antigravity_container_manifests(home, base_semver="0.6.0"),
    )
    sanitize_staged_payload(ag_root, version="0.6.0", remove_mcp_json=False)

    # 3. Cursor container (sanitized by H17 sanitize_staged_payload)
    cursor_root = home / ".cursor" / "plugins" / "local" / "minni"
    _write_verified_payload(cursor_root, version="0.6.0", git_sha=git_sha)
    (cursor_root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "minni": {
                        "command": "node",
                        "args": [str(cursor_root / "dist" / "server.js")],
                        "env": {"MINNI_AGENT_ID": "cursor"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    sanitize_staged_payload(cursor_root, version="0.6.0", remove_mcp_json=False)

    env = {
        **os.environ,
        "MINNI_CHECK_DEPLOYMENTS_HOME": str(home),
        "MINNI_CHECK_DEPLOYMENTS_REPO_ROOT": str(repo_root),
    }
    proc = subprocess.run(
        [sys.executable, str(CHECK_DEPLOYMENTS_PATH), "--strict"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DRIFT" not in proc.stdout, proc.stdout

    # Mutating a non-sanitized field in .claude-plugin/plugin.json must still report DRIFT
    c_pjson = cursor_root / ".claude-plugin" / "plugin.json"
    c_doc = json.loads(c_pjson.read_text(encoding="utf-8"))
    c_doc["description"] = "mutated description"
    c_pjson.write_text(json.dumps(c_doc, indent=2) + "\n", encoding="utf-8")

    proc_drift = subprocess.run(
        [sys.executable, str(CHECK_DEPLOYMENTS_PATH), "--strict"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_drift.returncode == 1, proc_drift.stdout
    assert "DRIFT" in proc_drift.stdout
    assert ".claude-plugin/plugin.json" in proc_drift.stdout


def test_h32_health_scanner_skips_oversized_bundles_but_warns_on_oversized_real_configs(tmp_path: Path) -> None:
    """H32: health.py ignores oversized extension/cache/node_modules bundles but warns on oversized real host configs."""
    from minni.wire.health import _MAX_SCAN_FILE_BYTES, scan_for_uncatalogued_plugin_refs

    home = tmp_path / "home"
    home.mkdir()
    oversized_blob = "x" * (_MAX_SCAN_FILE_BYTES + 1024)

    non_config_files = [
        home / ".cursor" / "extensions" / "openai.chatgpt-26.6313.41514" / "dist" / "extension.js",
        home / ".gemini" / "antigravity" / "Mcp" / "cache" / "deadbeef1234567890.json",
        home / ".claude" / "plugins" / "marketplaces" / "claude-plugins-official" / "plugins" / "foo" / "bundle.mjs",
        home / ".config" / "kilo" / "node_modules" / "some-pkg" / "index.js",
    ]
    for p in non_config_files:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(oversized_blob, encoding="utf-8")

    assert scan_for_uncatalogued_plugin_refs(home=home) == []

    real_configs = [
        home / ".cursor" / "profiles" / "mcp.json",
        home / ".grok" / "hooks" / "hooks.json",
        home / ".claude" / "settings.json",
        home / ".codex" / "profiles" / "config.toml",
    ]
    for p in real_configs:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(oversized_blob, encoding="utf-8")

    findings = scan_for_uncatalogued_plugin_refs(home=home)
    size_cap_paths = {Path(f["path"]).resolve() for f in findings if f.get("reason") == "config_file_exceeds_size_cap"}
    assert size_cap_paths == {p.resolve() for p in real_configs}


def test_h33_custom_refresh_updates_wired_json_when_preserved_args_resolve_to_new_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H33: custom_refresh updates wired.json when preserved custom args already resolve to the new payload, while keeping old-pinned custom hosts stale."""
    from minni.minnid_runtime import deploy_honesty
    from minni.wire.containers import flip_current_pointer
    from minni.wire.custom_refresh import refresh_custom_wires

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for exe in ("devin", "muse", "node"):
        p = bin_dir / exe
        p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    plugin_dir = home / ".minni" / "plugin"
    ancient_ver = "0.6.0+git.00000000"
    old_ver = "0.6.0+git.11111111"
    new_ver = "0.6.0+git.22222222"
    ancient_sha = "0" * 40
    old_sha = "1" * 40
    new_sha = "2" * 40

    ancient_root = plugin_dir / ancient_ver
    old_root = plugin_dir / old_ver
    new_root = plugin_dir / new_ver
    _write_verified_payload(ancient_root, version=ancient_ver, git_sha=ancient_sha)
    _write_verified_payload(old_root, version=old_ver, git_sha=old_sha)
    _write_verified_payload(new_root, version=new_ver, git_sha=new_sha)
    flip_current_pointer(new_ver, home=home)

    # 1. Custom host (devin) whose live config args point at ~/.minni/plugin/current/dist/server.js
    # (which resolves to new_root/dist/server.js) while wired.json lags at old_root.
    devin_cfg = home / ".config" / "devin" / "mcp_config.json"
    devin_cfg.parent.mkdir(parents=True, exist_ok=True)
    devin_doc = {
        "mcpServers": {
            "minni": {
                "command": "node",
                "args": [str(plugin_dir / "current" / "dist" / "server.js")],
                "env": {"MINNI_AGENT_ID": "devin"},
            }
        },
        "operatorCustomKey": "keep-me",
    }
    devin_cfg.write_text(json.dumps(devin_doc, indent=2) + "\n", encoding="utf-8")
    devin_before = devin_cfg.read_bytes()

    wired_path = plugin_dir / "wired.json"
    wired_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "wires": [
                    {
                        "platform": "devin",
                        "config_path": str(devin_cfg),
                        "install_root": str(old_root),
                        "version": old_ver,
                        "wired_at": "2026-11-01T00:00:00Z",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    out = refresh_custom_wires(new_root=new_root)
    assert out["exit_code"] == 0
    assert devin_cfg.read_bytes() == devin_before, "operator config file must never be rewritten"

    wired_after = json.loads(wired_path.read_text(encoding="utf-8"))
    devin_rec = next(w for w in wired_after["wires"] if w["platform"] == "devin")
    assert devin_rec["install_root"] == str(new_root)
    assert devin_rec["version"] == new_ver

    dist_status = deploy_honesty._plugin_dist_status(new_sha)
    assert dist_status["stale"] is False, f"Expected stale=False after custom_refresh, got {dist_status}"

    # 2. Custom host (muse) pinned to an OLD server.js (ancient_root) must NOT have wired.json updated to new_root
    # and must still be reported stale by deploy_honesty.
    muse_cfg = home / ".muse" / "mcp.json"
    muse_cfg.parent.mkdir(parents=True, exist_ok=True)
    muse_cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "minni": {
                        "command": "node",
                        "args": [str(ancient_root / "dist" / "server.js")],
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    muse_before = muse_cfg.read_bytes()
    wired_after["wires"].append(
        {
            "platform": "muse",
            "config_path": str(muse_cfg),
            "install_root": str(old_root),
            "version": old_ver,
            "wired_at": "2026-11-01T00:00:00Z",
        }
    )
    wired_path.write_text(json.dumps(wired_after, indent=2) + "\n", encoding="utf-8")

    out2 = refresh_custom_wires(new_root=new_root)
    assert out2["exit_code"] == 0
    assert muse_cfg.read_bytes() == muse_before
    wired_after2 = json.loads(wired_path.read_text(encoding="utf-8"))
    muse_rec = next(w for w in wired_after2["wires"] if w["platform"] == "muse")
    assert muse_rec["install_root"] == str(old_root)

    dist_status_stale = deploy_honesty._plugin_dist_status(new_sha)
    assert dist_status_stale["stale"] is True
    assert any("old_ver" in lag or old_ver in lag for lag in dist_status_stale.get("lagging", []))


def test_h24_dry_run_unreadable_and_non_utf8_surfaces_report_would_abort(tmp_path: Path) -> None:
    """H24: In dry-run, an unreadable surface (chmod 000 / OSError) or non-UTF-8 surface (UnicodeDecodeError) is would_abort with host/path/reason."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses chmod 000")

    from minni.wire.cutover import execute_atomic_cutover

    home = tmp_path / "home"
    home.mkdir()
    version = "0.6.0+git.3509b49e"
    git_sha = "3509b49e4256f6129a74f9af38e1891fbc4f9059"
    vdir = home / ".minni" / "plugin" / version
    _write_verified_payload(vdir, version=version, git_sha=git_sha)

    unreadable_hook = home / ".grok" / "hooks" / "minni.json"
    unreadable_hook.parent.mkdir(parents=True, exist_ok=True)
    unreadable_hook.write_text('{"hooks": {}}\n', encoding="utf-8")
    unreadable_hook.chmod(0o000)

    non_utf8_rules = home / ".grok" / "rules" / "minni.md"
    non_utf8_rules.parent.mkdir(parents=True, exist_ok=True)
    non_utf8_rules.write_bytes(b"# Minni rule\n\xff\xfeinvalid-utf8\n")

    try:
        outcome = execute_atomic_cutover(
            home=home,
            target_version=version,
            target_git_sha=git_sha,
            dry_run=True,
        )
    finally:
        unreadable_hook.chmod(0o600)

    assert outcome.exit_code == 1
    assert outcome.status == "ABORTED_PRE_FLIP"
    assert outcome.aborted_pre_flip is True

    aborts_by_path = { item["path"]: item for item in outcome.would_abort }
    assert str(unreadable_hook) in aborts_by_path, f"Missing chmod 000 surface in would_abort: {outcome.would_abort}"
    assert str(non_utf8_rules) in aborts_by_path, f"Missing non-UTF-8 surface in would_abort: {outcome.would_abort}"

    hook_abort = aborts_by_path[str(unreadable_hook)]
    assert hook_abort["host"] == "grok-build"
    assert hook_abort["reason"]
    assert "PermissionError" in hook_abort["reason"] or "OSError" in hook_abort["reason"]

    rules_abort = aborts_by_path[str(non_utf8_rules)]
    assert rules_abort["host"] == "grok-build"
    assert rules_abort["reason"]
    assert "UnicodeDecodeError" in rules_abort["reason"]


def test_h30b_corrupted_current_server_js_fails_cursor_propagate_and_leaves_container_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H30b: A verification failure on current (corrupted current/dist/server.js or bad manifest) fails cursor propagate loudly without falling back to bundle_server.mjs or touching the container."""
    from minni.wire.containers import converge_host_container, flip_current_pointer

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    version = "0.6.0+git.3509b49e"
    git_sha = "3509b49e4256f6129a74f9af38e1891fbc4f9059"
    vdir = home / ".minni" / "plugin" / version
    _write_verified_payload(
        vdir,
        version=version,
        git_sha=git_sha,
        server_body="// dist/server.js\n// good-verified-payload\n",
    )
    flip_current_pointer(version, home=home)

    converge_host_container(
        "cursor",
        home=home,
        base_semver="0.6.0",
        container_mode="staged_copy",
        target_version_root=vdir,
    )
    cursor_container = home / ".cursor" / "plugins" / "local" / "minni"
    before_snapshot = {
        p.relative_to(cursor_container).as_posix(): p.read_bytes()
        for p in sorted(cursor_container.rglob("*"))
        if p.is_file()
    }
    assert "dist/server.js" in before_snapshot

    # Corrupt current/dist/server.js so resolve_verified_plugin_root raises hash mismatch
    (vdir / "dist" / "server.js").write_bytes(b"// dist/server.js\n// CORRUPTED-SERVER-JS\n")

    propagate = _load_propagate()
    monkeypatch.setattr(propagate, "bootstrap_vault", lambda _args: 0)
    monkeypatch.setattr(propagate, "native_afm_env", lambda _repo: {})
    run_calls: list[object] = []
    monkeypatch.setattr(propagate, "run", lambda cmd, **kw: run_calls.append((cmd, kw)))

    args = argparse.Namespace(
        repo=str(REPO),
        workspace=str(REPO),
        no_build=True,
        install_root=str(cursor_container),
        agent="cursor",
        socket=str(home / ".minni" / "run" / "minnid.sock"),
        existing_only=True,
    )
    with pytest.raises(ValueError, match="dist/server.js hash mismatch"):
        propagate.update_one_plugin("cursor", args)

    assert run_calls == [], f"must not fall back to bundle_server.mjs or rsync on verification failure: {run_calls}"
    after_snapshot = {
        p.relative_to(cursor_container).as_posix(): p.read_bytes()
        for p in sorted(cursor_container.rglob("*"))
        if p.is_file()
    }
    assert after_snapshot == before_snapshot, "cursor container must remain completely untouched on verification failure"

    # Also verify a corrupt payload-manifest.json fails loudly and leaves the container untouched
    (vdir / "payload-manifest.json").write_text("{corrupt-json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable payload-manifest.json"):
        propagate.update_one_plugin("cursor", args)
    assert run_calls == []
    after_snapshot_2 = {
        p.relative_to(cursor_container).as_posix(): p.read_bytes()
        for p in sorted(cursor_container.rglob("*"))
        if p.is_file()
    }
    assert after_snapshot_2 == before_snapshot


def test_h31b_check_deployments_validates_gemini_extension_stamp_and_paths(tmp_path: Path) -> None:
    """H31b: check_deployments.py validates .gemini-plugin/gemini-extension.json stamp (MINNI_AGENT_ID) and absolute args/cwd paths without byte-hashing the host-rendered body."""
    from minni.wire.containers import (
        _write_container_manifests,
        render_antigravity_container_manifests,
    )
    from minni.wire.install import sanitize_staged_payload

    home = tmp_path / "home"
    home.mkdir()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "plugins").symlink_to(REPO / "plugins")

    ag_root = home / ".agents" / "plugins" / "Minni@Minni"
    _write_verified_payload(ag_root, version="0.6.0", git_sha="unknown")
    _write_container_manifests(
        ag_root,
        render_antigravity_container_manifests(home, base_semver="0.6.0"),
    )
    sanitize_staged_payload(ag_root, version="0.6.0", remove_mcp_json=False)

    env = {
        **os.environ,
        "MINNI_CHECK_DEPLOYMENTS_HOME": str(home),
        "MINNI_CHECK_DEPLOYMENTS_REPO_ROOT": str(repo_root),
    }

    # 1. Correct host-rendered .gemini-plugin/gemini-extension.json passes --strict
    proc_ok = subprocess.run(
        [sys.executable, str(CHECK_DEPLOYMENTS_PATH), "--strict"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_ok.returncode == 0, proc_ok.stdout + proc_ok.stderr
    assert "BADCONFIG" not in proc_ok.stdout, proc_ok.stdout
    assert "DRIFT" not in proc_ok.stdout, proc_ok.stdout

    gem_ext = ag_root / ".gemini-plugin" / "gemini-extension.json"
    good_doc = json.loads(gem_ext.read_text(encoding="utf-8"))

    # 2. Wrong agent id in .gemini-plugin/gemini-extension.json fails --strict
    wrong_agent_doc = json.loads(json.dumps(good_doc))
    wrong_agent_doc["mcpServers"]["minni"]["env"]["MINNI_AGENT_ID"] = "cursor"
    gem_ext.write_text(json.dumps(wrong_agent_doc, indent=2) + "\n", encoding="utf-8")

    proc_wrong_agent = subprocess.run(
        [sys.executable, str(CHECK_DEPLOYMENTS_PATH), "--strict"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_wrong_agent.returncode == 1, proc_wrong_agent.stdout
    assert "BADCONFIG" in proc_wrong_agent.stdout, proc_wrong_agent.stdout
    assert ".gemini-plugin/gemini-extension.json" in proc_wrong_agent.stdout
    assert "agent stamp 'cursor'" in proc_wrong_agent.stdout

    # 3. Missing server.js path in .gemini-plugin/gemini-extension.json fails --strict
    dead_args_doc = json.loads(json.dumps(good_doc))
    dead_args_doc["mcpServers"]["minni"]["args"] = [str(ag_root / "dist" / "missing-server.js")]
    gem_ext.write_text(json.dumps(dead_args_doc, indent=2) + "\n", encoding="utf-8")

    proc_dead_args = subprocess.run(
        [sys.executable, str(CHECK_DEPLOYMENTS_PATH), "--strict"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_dead_args.returncode == 1, proc_dead_args.stdout
    assert "BADCONFIG" in proc_dead_args.stdout, proc_dead_args.stdout
    assert ".gemini-plugin/gemini-extension.json" in proc_dead_args.stdout
    assert "dead path — args:" in proc_dead_args.stdout

    # 4. Missing absolute cwd path in .gemini-plugin/gemini-extension.json fails --strict
    dead_cwd_doc = json.loads(json.dumps(good_doc))
    dead_cwd_doc["mcpServers"]["minni"]["cwd"] = str(home / "nonexistent-cwd")
    gem_ext.write_text(json.dumps(dead_cwd_doc, indent=2) + "\n", encoding="utf-8")

    proc_dead_cwd = subprocess.run(
        [sys.executable, str(CHECK_DEPLOYMENTS_PATH), "--strict"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_dead_cwd.returncode == 1, proc_dead_cwd.stdout
    assert "BADCONFIG" in proc_dead_cwd.stdout, proc_dead_cwd.stdout
    assert ".gemini-plugin/gemini-extension.json" in proc_dead_cwd.stdout
    assert "dead path — cwd:" in proc_dead_cwd.stdout


def test_h32b_health_scanner_allowlists_real_config_names_before_parent_dir_deny(tmp_path: Path) -> None:
    """H32b: health.py applies the real-config filename allowlist BEFORE the parent-directory deny."""
    from minni.wire.health import _MAX_SCAN_FILE_BYTES, scan_for_uncatalogued_plugin_refs

    home = tmp_path / "home"
    home.mkdir()
    oversized_blob = "x" * (_MAX_SCAN_FILE_BYTES + 1024)

    non_config_bundles = [
        home / ".cursor" / "extensions" / "acme" / "dist" / "extension.js",
        home / ".gemini" / "antigravity" / "Mcp" / "cache" / "deadbeef1234567890.json",
        home / ".config" / "kilo" / "node_modules" / "some-pkg" / "mcp-bundle.json",
    ]
    for p in non_config_bundles:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(oversized_blob, encoding="utf-8")

    real_configs_in_denied_parents = [
        home / ".cursor" / "extensions" / "acme" / "mcp.json",
        home / ".cursor" / "extensions" / "acme" / "hooks.json",
        home / ".gemini" / "antigravity" / "Mcp" / "cache" / "settings.json",
        home / ".codex" / "cache" / "config.toml",
    ]
    for p in real_configs_in_denied_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(oversized_blob, encoding="utf-8")

    findings = scan_for_uncatalogued_plugin_refs(home=home)
    size_cap_paths = {Path(f["path"]).resolve() for f in findings if f.get("reason") == "config_file_exceeds_size_cap"}
    assert size_cap_paths == {p.resolve() for p in real_configs_in_denied_parents}

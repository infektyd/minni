"""§9.4: minni wire integration tests with mocked HOME."""

from __future__ import annotations

import json
import stat
import tomllib
import types
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path

import pytest

from minni.wire.flow import run_wire
from minni.wire.manifest import PayloadManifest, sha256_file
from minni.wire.output import WireOutput
from minni.wire.platform import ALL_EXPANSION_V03, GEMINI_SKIP_WARNING
from minni.wire.verify import VerifyResult


def _fake_node_script(tmp_path: Path) -> Path:
    node = tmp_path / "node"
    node.write_text(
        """#!/bin/sh
case "$1" in
  *server.js)
    read _line
    printf '%s\\n' '{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{},"serverInfo":{"name":"wire-fixture","version":"1"}}}'
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    node.chmod(node.stat().st_mode | stat.S_IXUSR)
    return node


def _build_payload(home: Path, version: str = "0.2.0") -> tuple[Path, PayloadManifest]:
    root = home / "payload"
    dist = root / "dist"
    dist.mkdir(parents=True)
    server = dist / "server.js"
    server.write_text("// stub server\n", encoding="utf-8")
    hook = dist / "hook.js"
    hook.write_text("// stub hook\n", encoding="utf-8")
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks-gemini.json").write_text("{}", encoding="utf-8")
    gemini_hook = dist / "gemini-hook.js"
    gemini_hook.write_text("// stub gemini hook\n", encoding="utf-8")
    (root / "kilo").mkdir()
    (root / "kilo" / "minni-plugin.js").write_text(
        (Path(__file__).resolve().parents[1] / "plugins/minni/kilo/minni-plugin.js").read_text(),
        encoding="utf-8",
    )
    (dist / "kilocode-hook.js").write_text("// stub kilo hook\n", encoding="utf-8")
    (root / ".mcp.json").write_text("{}", encoding="utf-8")

    files = {
        "dist/server.js": sha256_file(server),
        "dist/hook.js": sha256_file(hook),
        "dist/kilocode-hook.js": sha256_file(dist / "kilocode-hook.js"),
        "kilo/minni-plugin.js": sha256_file(root / "kilo/minni-plugin.js"),
        "dist/gemini-hook.js": sha256_file(gemini_hook),
        ".mcp.json": sha256_file(root / ".mcp.json"),
    }
    manifest = PayloadManifest(
        schema=1,
        version=version,
        git_sha="abc",
        built_at="2026-07-04T00:00:00Z",
        node_engine=">=20",
        files=files,
    )
    (root / "payload-manifest.json").write_text(
        json.dumps({
            "schema": 1,
            "version": version,
            "git_sha": "abc",
            "built_at": "2026-07-04T00:00:00Z",
            "node_engine": ">=20",
            "files": files,
        }, indent=2),
        encoding="utf-8",
    )
    return root, manifest


def _args(platform: str, home: Path, **kwargs) -> Namespace:
    defaults = {
        "platform": platform,
        "agent": None,
        "workspace": None,
        "install_root": None,
        "dry_run": False,
        "verify_payload": False,
        "prune": False,
        "no_prune": False,
        "force_reinstall": False,
        "from_repo": None,
        "use_version": None,
        "socket": str(home / ".minni" / "run" / "minnid.sock"),
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


@pytest.fixture
def wire_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _fake_node_script(tmp_path)
    # Executable/GUI evidence is separate from leftover configuration.
    for command in ("claude", "codex", "kilo", "grok"):
        executable = tmp_path / command
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    app = home / "Applications/Antigravity.app/Contents"
    app.mkdir(parents=True)
    (app / "Info.plist").write_text("fixture")
    from minni.wire.host_discovery import host_decision
    monkeypatch.setattr("minni.wire.flow.host_decision", lambda platform, **kw: host_decision(
        platform, app_roots=(home / "Applications",), launcher_roots=(), **kw,
    ))
    monkeypatch.setenv("PATH", str(tmp_path))
    (home / ".claude.json").write_text('{"mcpServers":{"minni":{}}}', encoding="utf-8")
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text("[mcp_servers.minni]\n", encoding="utf-8")
    kilo = home / ".config" / "kilo"
    kilo.mkdir(parents=True)
    (kilo / "kilo.json").write_text('{"mcp":{"minni":{}}}', encoding="utf-8")
    grok = home / ".grok"
    grok.mkdir()
    (grok / "config.toml").write_text("[mcp_servers.minni]\n", encoding="utf-8")
    payload_root, manifest = _build_payload(home)
    return home, payload_root, manifest


def _patch_payload(wire_env, monkeypatch):
    home, payload_root, manifest = wire_env

    @contextmanager
    def fake_tree(*, from_repo=None, use_version=None):
        if use_version:
            root = home / ".minni" / "plugin" / use_version
            yield root, PayloadManifest.load(root / "payload-manifest.json"), False
        else:
            yield payload_root, manifest, False

    monkeypatch.setattr("minni.wire.flow.payload_tree", fake_tree)
    monkeypatch.setattr("minni.wire.flow.package_version", lambda: manifest.version)
    monkeypatch.setattr(
        "minni.wire.preflight.check_node", lambda min_version=20: (True, "v22.0.0"),
    )
    monkeypatch.setattr(
        "minni.wire.flow.run_verify",
        lambda *a, **k: VerifyResult(handshake=True, hook_dry_run=True, config_readback=True),
    )
    return home, manifest


def test_wire_all_expansion_skips_gemini(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    rc = run_wire(_args("all", home, dry_run=True))
    assert rc == 0
    err = capsys.readouterr().err
    assert GEMINI_SKIP_WARNING.split("`")[0].strip()[:20] in err or "gemini" in err.lower()


def test_wire_dry_run_claude_code(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    rc = run_wire(_args("claude-code", home, dry_run=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "dry-run"
    assert out["payload_version"] == manifest.version


def test_wire_real_install_idempotent(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    rc1 = run_wire(_args("claude-code", home))
    assert rc1 == 0
    install_dir = home / ".minni" / "plugin" / manifest.version
    assert install_dir.is_dir()
    assert (home / ".minni" / "plugin" / "current").is_symlink()
    rc2 = run_wire(_args("claude-code", home))
    assert rc2 == 0


def test_wire_version_mismatch(wire_env, monkeypatch, capsys):
    home, payload_root, manifest = wire_env
    monkeypatch.setattr("minni.wire.flow.package_version", lambda: "9.9.9")

    from minni.wire.flow import WireError

    @contextmanager
    def gated_tree(*, from_repo=None, use_version=None):
        from minni.wire.flow import package_version
        if manifest.version != package_version():
            raise WireError(
                f"payload version {manifest.version!r} != installed package {package_version()!r}",
                exit_code=2,
            )
        yield payload_root, manifest, False

    monkeypatch.setattr("minni.wire.flow.payload_tree", gated_tree)
    monkeypatch.setattr(
        "minni.wire.preflight.check_node", lambda min_version=20: (True, "v22.0.0"),
    )
    rc = run_wire(_args("claude-code", home))
    assert rc == 2


def test_wire_all_payload_wireerror_status_is_failed_not_skipped(wire_env, monkeypatch, capsys):
    """Round-4 Low: WireError after ALL_SKIPS pre-append must not finalize as
    status=skipped (D5) — automation keying on status==failed would miss it."""
    home, payload_root, manifest = wire_env
    from minni.wire.flow import WireError

    @contextmanager
    def boom_tree(*, from_repo=None, use_version=None):
        raise WireError("from-repo install exploded", exit_code=1)
        yield payload_root, manifest, False  # pragma: no cover

    monkeypatch.setattr("minni.wire.flow.payload_tree", boom_tree)
    monkeypatch.setattr(
        "minni.wire.preflight.check_node", lambda min_version=20: (True, "v22.0.0"),
    )
    rc = run_wire(_args("all", home))
    assert rc == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "failed", doc
    assert any(r.get("status") == "failed" for r in doc.get("results", []))


def test_wire_missing_node(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    payload_root, manifest = _build_payload(home)

    @contextmanager
    def fake_tree(*, from_repo=None, use_version=None):
        yield payload_root, manifest, False

    monkeypatch.setattr("minni.wire.flow.payload_tree", fake_tree)
    rc = run_wire(_args("generic", home, agent="test", install_root=str(home / "target")))
    assert rc == 2


def test_wire_hash_mismatch_force_reinstall(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    run_wire(_args("claude-code", home))
    install_dir = home / ".minni" / "plugin" / manifest.version
    (install_dir / "dist" / "server.js").write_text("tampered\n", encoding="utf-8")
    rc_fail = run_wire(_args("claude-code", home))
    # §5: hash mismatch is mid-wire failure (exit 1), not preflight (exit 2).
    assert rc_fail == 1
    rc_ok = run_wire(_args("claude-code", home, force_reinstall=True))
    assert rc_ok == 0
    quarantine = [p for p in (home / ".minni" / "plugin").iterdir() if p.name.startswith(".quarantine-")]
    assert quarantine


def test_gc_noop_when_not_tty(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))
    rc = run_wire(_args("claude-code", home))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["gc"].get("skipped_no_tty") is True


def test_wired_json_two_install_roots(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    root_a = home / "agent-a"
    root_b = home / "agent-b"
    root_a.mkdir()
    root_b.mkdir()
    run_wire(_args("generic", home, agent="testagent", install_root=str(root_a)))
    run_wire(_args("generic", home, agent="testagent", install_root=str(root_b)))
    wired = json.loads((home / ".minni" / "plugin" / "wired.json").read_text())
    paths = {(w["platform"], w["config_path"]) for w in wired["wires"]}
    assert ("generic", str(root_a / ".mcp.json")) in paths
    assert ("generic", str(root_b / ".mcp.json")) in paths


def test_all_expansion_platform_set(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    rc = run_wire(_args("all", home, dry_run=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    attempted = {r["platform"] for r in out["results"]}
    assert set(ALL_EXPANSION_V03) <= attempted
    assert "generic" not in attempted
    # D7 (#232): every canonical-fleet member `wire all` does not execute is
    # present as an explicit skip with a named reason — never silently absent.
    from minni.wire.platform import ALL_SKIPS

    for plat in ("gemini", "antigravity", "cursor"):
        assert plat in attempted
        entry = next(r for r in out["results"] if r["platform"] == plat)
        assert entry["status"] == "skipped"
        assert entry["reason"] == ALL_SKIPS[plat]


def _patch_node_only(wire_env, monkeypatch):
    monkeypatch.setattr(
        "minni.wire.preflight.check_node", lambda min_version=20: (True, "v22.0.0"),
    )
    return wire_env


def test_wire_use_version_rollback(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    rc1 = run_wire(_args("claude-code", home))
    assert rc1 == 0
    install_dir = home / ".minni" / "plugin" / manifest.version
    assert install_dir.is_dir()

    from minni.wire.flow import payload_tree as real_payload_tree
    from minni.wire.verify import run_verify as real_run_verify

    monkeypatch.setattr("minni.wire.flow.payload_tree", real_payload_tree)
    monkeypatch.setattr("minni.wire.flow.run_verify", real_run_verify)
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))
    capsys.readouterr()
    rc2 = run_wire(_args("claude-code", home, use_version=manifest.version))
    assert rc2 == 0
    out = json.loads(capsys.readouterr().out)
    assert out["install_root"] == str(install_dir)
    assert out["payload_version"] == manifest.version


def test_wire_use_version_nonexistent_dir(wire_env, monkeypatch, capsys):
    home = wire_env[0]
    _patch_node_only(wire_env, monkeypatch)
    rc = run_wire(_args("claude-code", home, use_version="9.9.9"))
    assert rc == 2


def test_wire_use_version_invalid_segment(wire_env, monkeypatch, capsys):
    home = wire_env[0]
    _patch_node_only(wire_env, monkeypatch)
    rc = run_wire(_args("claude-code", home, use_version="../../../etc"))
    assert rc == 2


def test_wire_antigravity_verify_readback(wire_env, monkeypatch, capsys):
    home, payload_root, manifest = wire_env
    gemini_cfg = home / ".gemini" / "config"
    gemini_cfg.mkdir(parents=True)
    mcp_config = gemini_cfg / "mcp_config.json"
    mcp_config.write_text('{"mcpServers": {}}', encoding="utf-8")

    @contextmanager
    def fake_tree(*, from_repo=None, use_version=None):
        yield payload_root, manifest, False

    from minni.wire.verify import run_verify as real_run_verify

    monkeypatch.setattr("minni.wire.flow.payload_tree", fake_tree)
    monkeypatch.setattr("minni.wire.flow.package_version", lambda: manifest.version)
    monkeypatch.setattr("minni.wire.flow.run_verify", real_run_verify)
    monkeypatch.setattr(
        "minni.wire.preflight.check_node", lambda min_version=20: (True, "v22.0.0"),
    )
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))

    rc = run_wire(_args("antigravity", home, no_prune=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    result = out["results"][0]
    assert result["status"] == "wired"
    data = json.loads(mcp_config.read_text(encoding="utf-8"))
    args = data["mcpServers"]["minni"]["args"]
    assert args[-1] == str(home / ".minni" / "plugin" / manifest.version / "dist" / "server.js")


def test_wire_verify_failure_no_upsert(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    wired_path = home / ".minni" / "plugin" / "wired.json"

    monkeypatch.setattr(
        "minni.wire.flow.run_verify",
        lambda *a, **k: VerifyResult(
            handshake=True, hook_dry_run=True, config_readback=False,
        ),
    )
    rc = run_wire(_args("claude-code", home))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    result = out["results"][0]
    assert result["status"] == "failed"
    assert result["reason"] == "verification failed"
    if wired_path.exists():
        platforms = {w["platform"] for w in json.loads(wired_path.read_text())["wires"]}
        assert "claude-code" not in platforms


def test_wire_verify_payload_happy(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    rc = run_wire(_args("claude-code", home, verify_payload=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"


def test_wire_verify_payload_tampered(wire_env, monkeypatch, capsys):
    home, payload_root, manifest = wire_env
    _patch_payload(wire_env, monkeypatch)
    (payload_root / "dist" / "server.js").write_text("tampered\n", encoding="utf-8")
    rc = run_wire(_args("claude-code", home, verify_payload=True))
    assert rc == 1
    assert "hash mismatch" in capsys.readouterr().err


def test_wire_use_version_verify_payload_after_wire(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    rc1 = run_wire(_args("claude-code", home))
    assert rc1 == 0
    install_dir = home / ".minni" / "plugin" / manifest.version

    from minni.wire.flow import payload_tree as real_payload_tree

    monkeypatch.setattr("minni.wire.flow.payload_tree", real_payload_tree)
    monkeypatch.setattr(
        "minni.wire.flow.run_verify",
        lambda *a, **k: VerifyResult(handshake=True, hook_dry_run=True, config_readback=True),
    )
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))
    capsys.readouterr()
    rc2 = run_wire(
        _args("claude-code", home, use_version=manifest.version, verify_payload=True),
    )
    assert rc2 == 0
    out = json.loads(capsys.readouterr().out)
    assert out["install_root"] == str(install_dir)


def test_wire_codex_non_dry_run(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))
    rc = run_wire(_args("codex", home, no_prune=True))
    assert rc == 0
    config = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    server = home / ".minni" / "plugin" / manifest.version / "dist" / "server.js"
    assert str(server) in config
    assert "[mcp_servers.minni]" in config
    env = tomllib.loads(config)["mcp_servers"]["minni"]["env"]
    assert env["MINNI_AGENT_ID"] == "codex"
    assert "MINNI_WORKSPACE_ID" not in env
    assert "MINNI_CODEX_WORKSPACE_ID" not in env


def test_wire_kilocode_non_dry_run(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home, manifest = wire_env[0], wire_env[2]
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))
    rc = run_wire(_args("kilocode", home, no_prune=True))
    assert rc == 0
    server = home / ".minni" / "plugin" / manifest.version / "dist" / "server.js"
    kilo = json.loads((home / ".config" / "kilo" / "kilo.json").read_text(encoding="utf-8"))
    entry = kilo["mcp"]["minni"]
    assert entry["command"] == ["node", str(server)]
    # Kilo schema requires "environment", not "env" (ConfigInvalidError otherwise).
    assert "env" not in entry, "wire must not stamp Claude-style env key for Kilo"
    assert entry["environment"]["MINNI_AGENT_ID"] == "kilocode"
    assert entry["enabled"] is True


def test_wire_antigravity_alternate_surface(wire_env, monkeypatch, capsys):
    home, payload_root, manifest = wire_env
    alt = home / ".gemini" / "antigravity" / "mcp_config.json"
    alt.parent.mkdir(parents=True)
    alt.write_text('{"mcpServers": {}}', encoding="utf-8")

    @contextmanager
    def fake_tree(*, from_repo=None, use_version=None):
        yield payload_root, manifest, False

    from minni.wire.verify import run_verify as real_run_verify

    monkeypatch.setattr("minni.wire.flow.payload_tree", fake_tree)
    monkeypatch.setattr("minni.wire.flow.package_version", lambda: manifest.version)
    monkeypatch.setattr("minni.wire.flow.run_verify", real_run_verify)
    monkeypatch.setattr(
        "minni.wire.preflight.check_node", lambda min_version=20: (True, "v22.0.0"),
    )
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))

    rc = run_wire(_args("antigravity", home, no_prune=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    result = out["results"][0]
    assert result["status"] == "wired"
    assert result["config_path"] == str(alt.resolve())


def test_wire_antigravity_hook_registration_failure_is_failed(
    wire_env, monkeypatch, capsys,
):
    """D11 (#232): a REAL agy hook-registration failure (agy present, install
    failed) must fail the platform, not report 'wired' with the failure buried
    in extras. (An absent agy CLI stays 'wired' with a named hook-gap reason —
    covered by the surrounding antigravity tests, whose sandbox has no agy.)"""
    home, payload_root, manifest = wire_env
    gemini_cfg = home / ".gemini" / "config"
    gemini_cfg.mkdir(parents=True)
    (gemini_cfg / "mcp_config.json").write_text(
        '{"mcpServers": {}}', encoding="utf-8",
    )
    _patch_payload(wire_env, monkeypatch)
    monkeypatch.setattr(
        "minni.wire.flow.update_agy_plugin_hooks",
        lambda install_root: {
            "installed": False,
            "reason": "agy plugin install failed: staging rejected",
        },
    )
    rc = run_wire(_args("antigravity", home, no_prune=True))
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    result = out["results"][0]
    assert result["status"] == "failed"
    assert "agy hook registration failed" in result["reason"]
    # D11 half-state: MCP already rewritten — install root must still be
    # recorded so GC / honesty protect the live payload.
    from minni.wire.wired import wired_record

    rec = wired_record("antigravity")
    assert rec is not None
    assert "install_root" in rec
    assert "GC protection" in result["reason"] or "already updated" in result["reason"]


def test_wire_antigravity_absent_agy_names_hook_gap(wire_env, monkeypatch, capsys):
    """D11 (#232): with agy genuinely absent, the result stays 'wired' but the
    hook gap is NAMED in the primary reason field, not buried in extras."""
    home, payload_root, manifest = wire_env
    gemini_cfg = home / ".gemini" / "config"
    gemini_cfg.mkdir(parents=True)
    (gemini_cfg / "mcp_config.json").write_text(
        '{"mcpServers": {}}', encoding="utf-8",
    )
    _patch_payload(wire_env, monkeypatch)
    rc = run_wire(_args("antigravity", home, no_prune=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    result = out["results"][0]
    assert result["status"] == "wired"
    assert result["reason"] and "WITHOUT agy hooks" in result["reason"]


def test_wire_generic_missing_install_root(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    rc = run_wire(_args("generic", home))
    assert rc == 2
    assert capsys.readouterr().err.strip() == "generic wire requires --install-root"


def test_wire_generic_missing_agent(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    root = home / "custom"
    root.mkdir()
    rc = run_wire(_args("generic", home, install_root=str(root)))
    assert rc == 2
    assert (
        capsys.readouterr().err.strip()
        == "generic wire requires --agent so it cannot inherit another agent's vault"
    )


@pytest.mark.skip(reason="requires npm/node toolchain; CI-only")
def test_wire_from_repo_integration():
    """Thin integration placeholder for build_from_repo when Node/npm are available."""


@pytest.mark.skip(reason="CI-only, needs isolated environment without Node")
def test_bundle_smoke_no_node_modules():
    """§9.2: staging bundle smoke in empty temp dir."""

def test_wire_dry_run_leaves_no_plugin_state(wire_env, monkeypatch):
    """Codex review (PR #144): --dry-run must not create ~/.minni/plugin or locks."""
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    rc = run_wire(_args("claude-code", home, dry_run=True))
    assert rc == 0
    assert not (home / ".minni" / "plugin").exists()


def test_wire_generic_preserves_existing_mcp_entries(wire_env, monkeypatch):
    """Codex review (PR #144): wiring must merge into .mcp.json, not replace it."""
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    root = home / "custom"
    root.mkdir()
    (root / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {"other": {"command": "other-server", "args": []}},
            "topLevelSetting": True,
        }),
        encoding="utf-8",
    )
    rc = run_wire(_args("generic", home, agent="testagent", install_root=str(root)))
    assert rc == 0
    doc = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    assert doc["topLevelSetting"] is True
    assert doc["mcpServers"]["other"]["command"] == "other-server"
    assert "minni" in doc["mcpServers"]


def test_wire_from_repo_relative_path_resolved(wire_env, monkeypatch):
    """Codex review (PR #145): a relative --from-repo must be resolved before
    config stamping — native_afm_env(repo_root) otherwise stamps a relative
    MINNI_AFM_NATIVE_HELPER that breaks when the host launches the server
    from its own cwd."""
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    captured = {}

    def fake_afm_env(repo_root):
        captured["repo_root"] = repo_root
        return {}

    monkeypatch.setattr("minni.wire.flow.native_afm_env", fake_afm_env)
    rc = run_wire(_args("claude-code", home, from_repo="."))
    assert rc == 0
    assert captured["repo_root"].is_absolute()


def test_wire_unavailable_host_keeps_stale_config_and_wire_records(wire_env, monkeypatch, capsys):
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    (home.parent / "kilo").unlink()
    plugin = home / ".minni/plugin"
    plugin.mkdir(parents=True)
    records = plugin / "wired.json"
    records.write_text('{"schema":1,"wires":[{"platform":"kilocode","install_root":"/old"}]}')
    config = home / ".config/kilo/kilo.json"
    before = records.read_bytes(), config.read_bytes()
    assert run_wire(_args("kilocode", home)) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["results"][0]["status"] == "skipped"
    assert "host unavailable" in out["results"][0]["reason"]
    assert (records.read_bytes(), config.read_bytes()) == before
    assert not (plugin / wire_env[2].version).exists()



def test_from_repo_run_redirects_child_stdout_to_stderr(tmp_path, monkeypatch):
    """Build children must not write to stdout (wire JSON contract)."""
    import subprocess as sp
    import sys

    from minni.wire import from_repo

    calls: dict = {}

    def fake_run(cmd, cwd=None, check=True, stdout=None, **kwargs):
        calls["stdout"] = stdout
        return sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr(from_repo.subprocess, "run", fake_run)
    from_repo._run(["echo", "hi"], tmp_path)
    assert calls.get("stdout") is sys.stderr


@pytest.mark.parametrize("native_hooks_present", [False, True])
def test_codex_wire_does_not_claim_host_lifecycle_activation(
    wire_env, monkeypatch, capsys, native_hooks_present,
):
    _patch_payload(wire_env, monkeypatch)
    home = wire_env[0]
    config_path = home / ".codex" / "config.toml"
    existing = '[model_provider_settings]\nkeep = "untouched"\n'
    if native_hooks_present:
        existing += '[plugins."minni@local"]\nenabled = true\n'
    config_path.write_text(existing, encoding="utf-8")
    hook_file = home / ".codex" / "hooks.json"
    hook_bytes = b'{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"echo fixture"}]}]}}'
    if native_hooks_present:
        hook_file.write_bytes(hook_bytes)
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(isatty=lambda: False))

    assert run_wire(_args("codex", home, no_prune=True)) == 0
    output = json.loads(capsys.readouterr().out)
    result = next(row for row in output["results"] if row["platform"] == "codex")
    assert result["status"] == "wired"
    assert result["verify"]["hook_dry_run"] is True
    assert result["lifecycle"] == {
        "automatic_hooks": "not_verified",
        "hook_probe_scope": "packaged_entrypoint_only",
        "existing_host_sessions": "not_verified",
    }
    assert "MCP configured" in result["reason"]
    assert "not verified" in result["reason"]
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert config["model_provider_settings"]["keep"] == "untouched"
    if native_hooks_present:
        assert config["plugins"]["minni@local"]["enabled"] is True
        assert hook_file.read_bytes() == hook_bytes
    else:
        assert not hook_file.exists()
        assert "plugins" not in config

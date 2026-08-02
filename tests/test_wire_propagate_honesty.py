"""#232 honesty pins: wire/propagate statuses derive from what actually happened.

D5:  a run where every platform was skipped must not report "ok"/exit 0.
D6:  propagate update-plugin isolates platforms and derives every status.
D10: an unparseable existing TOML config is a hard error, not a silent env drop.
D11: a real agy hook-registration failure fails the antigravity wire; an absent
     agy CLI keeps "wired" but names the hook gap in the primary reason field.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import pytest

from minni.wire.output import PlatformResult, WireOutput
from minni.wire.writers import replace_toml_sections as wire_replace_toml_sections

REPO = Path(__file__).resolve().parent.parent


def _load_propagate():
    spec = importlib.util.spec_from_file_location(
        "_minni_propagate_honesty",
        REPO / "plugins" / "minni" / "skills" / "minni-install" / "scripts"
        / "propagate.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


propagate = _load_propagate()


# ── D5 ───────────────────────────────────────────────────────────────────────

def test_all_skipped_run_is_not_ok(capsys):
    out = WireOutput()
    out.results.append(PlatformResult("gemini", "skipped", reason="provisional"))
    out.finalize_status(dry_run=False)
    assert out.status == "skipped"
    assert out.emit() == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["status"] == "skipped"


def test_wired_plus_skipped_stays_ok(capsys):
    out = WireOutput()
    out.results.append(PlatformResult("codex", "wired"))
    out.results.append(PlatformResult("gemini", "skipped", reason="provisional"))
    out.finalize_status(dry_run=False)
    assert out.status == "ok"
    assert out.emit() == 0
    capsys.readouterr()


# ── D6 ───────────────────────────────────────────────────────────────────────

def _update_args(platform: str) -> argparse.Namespace:
    return argparse.Namespace(
        platform=platform,
        agent=None,
        install_root=None,
        workspace=None,
        no_build=True,
        repo=str(REPO),
        socket="/dev/null",
    )


def test_update_plugin_isolates_platform_failures(monkeypatch, capsys):
    """One platform raising must not abort the rest, and the overall status
    must be derived — 'partial', not a hardcoded 'updated'."""
    def fake_update_one(platform, args):
        if platform == "antigravity":
            raise RuntimeError("boom: antigravity exploded")
        return {"platform": platform, "agent": "x", "install_root": "/tmp/x"}

    monkeypatch.setattr(propagate, "update_one_plugin", fake_update_one)
    rc = propagate.update_plugin(_update_args("all"))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert doc["status"] == "partial"
    by_platform = {r["platform"]: r for r in doc["results"]}
    assert by_platform["antigravity"]["status"] == "failed"
    assert "boom" in by_platform["antigravity"]["error"]
    # The platforms after the failure still ran.
    for plat in propagate.ALL_PLATFORMS:
        if plat != "antigravity":
            assert by_platform[plat]["status"] == "updated"
    # D7: the deliberate exclusions are named in the output.
    for plat, reason in propagate.ALL_SKIPS.items():
        assert by_platform[plat]["status"] == "skipped"
        assert by_platform[plat]["reason"] == reason


def test_update_plugin_all_failed_is_failed(monkeypatch, capsys):
    def fake_update_one(platform, args):
        raise RuntimeError("nope")

    monkeypatch.setattr(propagate, "update_one_plugin", fake_update_one)
    rc = propagate.update_plugin(_update_args("all"))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert doc["status"] == "failed"


def test_update_plugin_nothing_attempted_is_skipped_not_updated(monkeypatch, capsys):
    """Round-2 Low / D5: empty attempted set (all skips, no expansions) must
    not green-wash as status=updated / exit 0."""
    monkeypatch.setattr(propagate, "ALL_PLATFORMS", ())
    monkeypatch.setattr(
        propagate, "ALL_SKIPS",
        {"codex": "wire-managed", "cursor": "example skip"},
    )
    monkeypatch.setattr(
        propagate, "update_one_plugin",
        lambda platform, args: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    rc = propagate.update_plugin(_update_args("all"))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert doc["status"] == "skipped"
    assert all(r["status"] == "skipped" for r in doc["results"])


def test_update_plugin_clean_run_is_updated(monkeypatch, capsys):
    monkeypatch.setattr(
        propagate, "update_one_plugin",
        lambda platform, args: {"platform": platform},
    )
    rc = propagate.update_plugin(_update_args("codex"))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["status"] == "updated"
    assert doc["results"][0]["status"] == "updated"


def test_update_plugin_hook_failure_degrades(monkeypatch, capsys):
    """A hook sub-step that failed (agy present) must degrade the platform
    status; an absent host CLI is a named note, not a clean 'updated'."""
    monkeypatch.setattr(
        propagate, "update_one_plugin",
        lambda platform, args: {
            "platform": platform,
            "agy_hooks": {"installed": False, "reason": "agy plugin install failed: x"},
        },
    )
    rc = propagate.update_plugin(_update_args("antigravity"))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert doc["status"] == "degraded"
    assert doc["results"][0]["status"] == "degraded"
    assert doc["results"][0]["problems"]


def test_update_plugin_absent_cli_is_note_not_problem(monkeypatch, capsys):
    monkeypatch.setattr(
        propagate, "update_one_plugin",
        lambda platform, args: {
            "platform": platform,
            "agy_hooks": {
                "installed": False,
                "error_class": "missing_cli",
                "reason": "agy CLI not found on PATH; hook registration skipped",
            },
        },
    )
    rc = propagate.update_plugin(_update_args("gemini"))
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["status"] == "updated"
    assert doc["results"][0]["notes"]


def test_update_plugin_path_in_failure_reason_is_still_a_problem(monkeypatch, capsys):
    """Round-2 Low: a real registration failure whose text mentions 'path'
    must not be reclassified as a missing-CLI note via substring match."""
    monkeypatch.setattr(
        propagate, "update_one_plugin",
        lambda platform, args: {
            "platform": platform,
            "agy_hooks": {
                "installed": False,
                "reason": (
                    "agy plugin install failed: hooks.json was not found on path "
                    "/tmp/staging (registration rejected)"
                ),
            },
        },
    )
    rc = propagate.update_plugin(_update_args("gemini"))
    doc = json.loads(capsys.readouterr().out)
    assert rc != 0
    problems = doc["results"][0].get("problems") or []
    assert any("agy_hooks" in p for p in problems), doc


# ── D10 ──────────────────────────────────────────────────────────────────────

BAD_TOML = "[mcp_servers.minni.env]\nMINNI_AGENT_ID = unquoted garbage {\n"
SECTIONS = {
    "mcp_servers.minni": "[mcp_servers.minni]\ncommand = \"node\"",
    "mcp_servers.minni.env": (
        "[mcp_servers.minni.env]\nMINNI_AGENT_ID = \"codex\""
    ),
}


def test_wire_toml_parse_failure_is_hard_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BAD_TOML, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to"):
        wire_replace_toml_sections(path, dict(SECTIONS), preserve_surface_env=True)
    assert path.read_text(encoding="utf-8") == before, "file must not be rewritten"


def test_propagate_toml_parse_failure_is_hard_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(BAD_TOML, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to"):
        propagate.replace_toml_sections(
            path, dict(SECTIONS), preserve_surface_env=True,
        )
    assert path.read_text(encoding="utf-8") == before, "file must not be rewritten"


def test_toml_preservation_still_works_on_valid_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[mcp_servers.minni.env]\n"
        'MINNI_AGENT_ID = "codex"\n'
        'MINNI_VAULT_PATH = "{home}/.minni/codex-vault"\n'.format(
            home=str(Path.home())
        ),
        encoding="utf-8",
    )
    sections = {
        "mcp_servers.minni.env": (
            "[mcp_servers.minni.env]\n"
            'MINNI_AGENT_ID = "codex"\n'
            'MINNI_VAULT_PATH = "/wrong/fresh/vault"'
        ),
    }
    wire_replace_toml_sections(path, sections, preserve_surface_env=True)
    text = path.read_text(encoding="utf-8")
    assert "codex-vault" in text
    assert "/wrong/fresh/vault" not in text


def test_toml_basic_str_parity_with_wire():
    """Propagate and wire must escape the same control/meta characters (D10 dual-maintain)."""
    from minni.wire.writers import _toml_basic_str as wire_esc

    samples = (
        "plain",
        'quote"here',
        "back\\slash",
        "new\nline",
        "tab\there",
        "carr\riage",
        "nul\x00byte",
        "unit\x1fsep",
        "del\x7f",
        'mix\\"\n\t\r\x01',
    )
    for raw in samples:
        assert propagate._toml_basic_str(raw) == wire_esc(raw), repr(raw)


def test_propagate_codex_preserve_rederives_mirror_when_agent_is_codex(tmp_path, monkeypatch):
    """Match wire: MINNI_AGENT_ID=codex alone must re-derive MINNI_CODEX_* on preserve."""
    import tomllib

    monkeypatch.setenv("HOME", str(tmp_path))
    vault = tmp_path / ".minni" / "codex-vault"
    vault.mkdir(parents=True)
    sock = tmp_path / ".minni" / "run" / "minnid.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        "[mcp_servers.minni]\n"
        'command = "node"\n'
        'args = ["/old.js"]\n'
        "enabled = true\n\n"
        "[mcp_servers.minni.env]\n"
        'MINNI_AGENT_ID = "codex"\n'
        f'MINNI_VAULT_PATH = "{vault}"\n'
        f'MINNI_SOCKET_PATH = "{sock}"\n'
        'MINNI_WORKSPACE_ID = "ws-preserved"\n'
        'MINNI_CODEX_WORKSPACE_ID = "ws-stale"\n',
        encoding="utf-8",
    )
    # Fresh section has agent=codex but deliberately omits MINNI_CODEX_* keys.
    sections = {
        "mcp_servers.minni": (
            "[mcp_servers.minni]\n"
            'command = "node"\n'
            'args = ["/new.js"]\n'
            "enabled = true\n"
        ),
        "mcp_servers.minni.env": (
            "[mcp_servers.minni.env]\n"
            'MINNI_AGENT_ID = "codex"\n'
            f'MINNI_VAULT_PATH = "{vault}"\n'
            f'MINNI_SOCKET_PATH = "{sock}"\n'
            'MINNI_WORKSPACE_ID = "ws-fresh"\n'
        ),
    }
    propagate.replace_toml_sections(path, sections, preserve_surface_env=True)
    env = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["minni"]["env"]
    assert env["MINNI_WORKSPACE_ID"] == "ws-preserved"
    assert env["MINNI_CODEX_WORKSPACE_ID"] == "ws-preserved"
    assert env["MINNI_CODEX_AGENT_ID"] == "codex"
    assert env["MINNI_CODEX_VAULT_PATH"] == str(vault)


def test_codex_mirror_overwrites_stale_like_wire():
    """Propagate must force-reassign CODEX mirrors (not setdefault) like wire."""
    from minni.wire.writers import _mirror_codex_hook_env as wire_mirror

    for mirror in (propagate._mirror_codex_hook_env, wire_mirror):
        env = {
            "MINNI_AGENT_ID": "codex",
            "MINNI_VAULT_PATH": "/vault-new",
            "MINNI_WORKSPACE_ID": "ws-new",
            "MINNI_CODEX_VAULT_PATH": "/vault-stale",
            "MINNI_CODEX_WORKSPACE_ID": "ws-stale",
        }
        mirror(env, "codex")
        assert env["MINNI_CODEX_VAULT_PATH"] == "/vault-new", mirror
        assert env["MINNI_CODEX_WORKSPACE_ID"] == "ws-new", mirror
        assert env["MINNI_CODEX_AGENT_ID"] == "codex", mirror

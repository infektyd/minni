"""Dry-run is a mutation policy, not a way to hide known platform failures."""
import json

import pytest

from minni.wire.flow import run_wire
from minni.wire.output import PlatformResult, WireOutput
from test_wire_integration import _args


@pytest.mark.parametrize("platform", ["codex", "all"])
def test_real_dry_run_malformed_host_fails_without_dependencies_or_mutation(tmp_path, monkeypatch, capsys, platform):
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('credential = "sensitive fixture\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr("minni.wire.flow.payload_tree", lambda **_kw: pytest.fail("no payload work for rejected hosts"))
    monkeypatch.setattr("minni.wire.preflight.check_node", lambda **_kw: pytest.fail("host failure precedes Node"))
    before = {str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()}
    rc = run_wire(_args(platform, home, dry_run=True))
    output = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert output["status"] == "failed"
    assert any(row["platform"] == "codex" and row["status"] == "failed" for row in output["results"])
    assert "sensitive" not in json.dumps(output)
    assert {str(path.relative_to(home)): path.read_bytes() for path in home.rglob("*") if path.is_file()} == before
    assert not (home / ".minni").exists()


@pytest.mark.parametrize("statuses,expected,exit_code", [
    (["wired", "failed", "skipped"], "partial", 1),
    (["failed", "skipped"], "failed", 1),
    (["wired", "skipped"], "dry-run", 0),
    (["skipped"], "dry-run", 0),
])
def test_mixed_dry_run_outcomes_preserve_failures_and_explicit_skip_rows(capsys, statuses, expected, exit_code):
    out = WireOutput(results=[PlatformResult(str(i), status) for i, status in enumerate(statuses)])
    out.finalize_status(dry_run=True)
    assert out.status == expected
    assert out.emit() == exit_code
    assert [row["status"] for row in json.loads(capsys.readouterr().out)["results"]] == statuses

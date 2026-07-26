"""Tests for the `minni watch` CLI subcommand (minni_cli.cmd_watch + argparse
wiring). Model-free and daemon-free, mirroring tests/test_minni_cli.py's
style: exercises minni_cli.main([...]) directly with capsys/monkeypatch.
"""

from __future__ import annotations

import json

import pytest

import minni.minni_cli as minni_cli


# ---------------------------------------------------------------------------
# E. --since parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("since", ["10m", "2h", "30s", "1d"])
def test_watch_since_relative_forms_parse_and_run(since, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    rc = minni_cli.main(["watch", "--since", since, "--once"])
    # Empty MINNI_HOME -> "nothing to watch" path, but the --since value must
    # have parsed without hitting the garbage-input exit(2) branch.
    assert rc == 1
    err = capsys.readouterr().err
    assert "Nothing to watch" in err


def test_watch_since_iso_form_parses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    rc = minni_cli.main(["watch", "--since", "2026-07-15T10:00:00Z", "--once"])
    assert rc == 1
    assert "Nothing to watch" in capsys.readouterr().err


def test_watch_since_garbage_input_exits_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    rc = minni_cli.main(["watch", "--since", "not-a-time-at-all", "--once"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "cannot parse --since" in err


# ---------------------------------------------------------------------------
# Empty MINNI_HOME -> "nothing to watch" exit 1
# ---------------------------------------------------------------------------

def test_watch_once_empty_home_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    rc = minni_cli.main(["watch", "--once"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Nothing to watch" in err
    assert str(tmp_path) in err


# ---------------------------------------------------------------------------
# Fake vault dir + --once --json -> JSON lines on stdout
# ---------------------------------------------------------------------------

def test_watch_once_json_emits_json_lines_from_vault_log(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    vault = tmp_path / "demo-vault"
    vault.mkdir()
    (vault / "log.md").write_text(
        "## [2026-07-15T10:00:00.000Z] search | recall \"hello\" — 2 hits\n\n"
        "```json\n"
        '{\n  "hits": 2\n}\n'
        "```\n\n"
    )

    rc = minni_cli.main(["watch", "--once", "--json"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    payload = json.loads(out[0])
    assert payload["agent"] == "demo"
    assert payload["tool"] == "search"
    assert payload["source"] == "plugin"
    assert payload["details"] == {"hits": 2}


def test_watch_once_json_agent_filter(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MINNI_HOME", str(tmp_path))
    for name in ("alpha", "beta"):
        vault = tmp_path / f"{name}-vault"
        vault.mkdir()
        (vault / "log.md").write_text(
            f"## [2026-07-15T10:00:00.000Z] tool | from {name}\n\n"
        )

    rc = minni_cli.main(["watch", "--once", "--json", "--agent", "alpha"])
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    payload = json.loads(out[0])
    assert payload["agent"] == "alpha"

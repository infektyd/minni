"""Fail-closed VERDICT parser for Grok App reviews (v1: never APPROVE)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "scripts"
    / "parse_grok_verdict.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("parse_grok_verdict", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


@pytest.mark.parametrize(
    "body,event,note_substr",
    [
        ("findings...\nVERDICT: REQUEST_CHANGES\n", "REQUEST_CHANGES", "REQUEST_CHANGES"),
        ("ok\nVERDICT: COMMENT\n", "COMMENT", "COMMENT"),
        ("LGTM\nVERDICT: APPROVE\n", "COMMENT", "downgraded"),
        ("no machine line, just APPROVE-worthy prose", "COMMENT", "no VERDICT"),
        ("I approve this PR", "COMMENT", "no VERDICT"),
        ("VERDICT: REQUEST_CHANGES\nthen more\nVERDICT: COMMENT\n", "COMMENT", "COMMENT"),
        ("", "COMMENT", "no VERDICT"),
        ("VERDICT: APPROVE\n", "COMMENT", "downgraded"),
        # Substring / path must not count as a verdict
        ("see workflows/grok.yml\n", "COMMENT", "no VERDICT"),
        ("VERDICT: APPROVE-worthy\n", "COMMENT", "no VERDICT"),
    ],
)
def test_parse_verdict_matrix(mod, body, event, note_substr):
    got_event, note = mod.parse_verdict(body)
    assert got_event == event
    assert note_substr in note
    assert got_event != "APPROVE"


def test_never_emits_approve_event(mod):
    for body in (
        "VERDICT: APPROVE",
        "VERDICT: APPROVE\n",
        "x\nVERDICT: APPROVE\n",
        "APPROVE",
        "VERDICT: APPROVE ",
    ):
        event, _ = mod.parse_verdict(body)
        assert event == "COMMENT"

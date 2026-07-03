"""Display tests for minnid_client CLI output.

The client must not misreport the daemon's proposal-first learn path (which
returns a staged candidate, not a durable learning) as a stored learning.
RPC transport is faked; these tests cover formatting only.
"""

from __future__ import annotations

from types import SimpleNamespace

import minni.minnid_client as minnid_client


def _learn_args():
    return SimpleNamespace(socket="/tmp/fake.sock", text=["a", "note"],
                           category="decision", agent=None)


def test_learn_staged_candidate_reported_as_staged(monkeypatch, capsys):
    monkeypatch.setattr(
        minnid_client, "_rpc",
        lambda *a, **k: {"status": "proposed", "candidate_id": 7,
                         "principal": "main"})
    minnid_client._cmd_learn(_learn_args())
    out = capsys.readouterr().out
    assert "Staged candidate #7" in out
    assert "not yet durable" in out
    assert "Stored learning" not in out


def test_learn_durable_write_reported_as_stored(monkeypatch, capsys):
    monkeypatch.setattr(
        minnid_client, "_rpc",
        lambda *a, **k: {"learning_id": 3, "category": "decision"})
    minnid_client._cmd_learn(_learn_args())
    out = capsys.readouterr().out
    assert "Stored learning #3" in out
    assert "[decision]" in out

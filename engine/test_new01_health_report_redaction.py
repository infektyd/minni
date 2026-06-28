"""NEW-01: health_report must not disclose document paths or learning contents
to a pre-identity (recovery-mode) caller.

The endpoint is in RECOVERY_ALLOWED_METHODS, so an unstamped socket client can
invoke it before a principal is verified. These tests pin the redaction:
  - the pure redaction helper strips paths + learning content, keeping counts,
  - the handler redacts when the trusted `_recovery` flag is set, and returns
    full detail when it is not.

Hermetic: the handler tests stub SovereignDB so no real ~/.minni DB is opened.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def test_redact_health_report_strips_paths_and_content():
    import minnid

    report = {
        "stale_docs": [{"doc_id": 1, "path": "/Users/x/secret.md", "age_days": 40}],
        "never_recalled": [{"doc_id": 2, "path": "/Users/x/private-notes.md"}],
        "contradicting_learnings": [
            {
                "learning_id": "L1",
                "agent_id": "claude-code",
                "content": "the secret launch date is classified",
                "contradicts_id": "L0",
                "status": "contradiction",
            }
        ],
        "afm_loop": {"status": "ok"},
        "faiss_cache_age_seconds": 5,
    }

    red = minnid._redact_health_report_for_recovery(report)
    blob = json.dumps(red)

    # No filesystem paths or learning content survive.
    assert "/Users/x/secret.md" not in blob
    assert "/Users/x/private-notes.md" not in blob
    assert "classified" not in blob

    # Sensitive lists are emptied, counts retained.
    assert red["stale_docs"] == []
    assert red["never_recalled"] == []
    assert red["contradicting_learnings"] == []
    assert red["stale_docs_count"] == 1
    assert red["never_recalled_count"] == 1
    assert red["contradicting_learnings_count"] == 1
    assert "redacted" in red

    # Non-sensitive liveness signals are preserved.
    assert red["afm_loop"] == {"status": "ok"}
    assert red["faiss_cache_age_seconds"] == 5

    # The input report is not mutated in place.
    assert report["stale_docs"][0]["path"] == "/Users/x/secret.md"


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return {"max_rowid": 0, "n": 0}


class _FakeDB:
    def __init__(self, *a, **k):
        pass

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


def test_handle_health_report_redacts_when_recovery(monkeypatch):
    import minnid

    monkeypatch.setattr(minnid, "SovereignDB", _FakeDB)
    resp = minnid._handle_health_report({"_recovery": True}, 1)
    rep = resp["result"]

    assert rep["stale_docs"] == []
    assert rep["never_recalled"] == []
    assert rep["contradicting_learnings"] == []
    assert "redacted" in rep
    assert rep["stale_docs_count"] == 0


def test_handle_health_report_full_detail_when_not_recovery(monkeypatch):
    import minnid

    monkeypatch.setattr(minnid, "SovereignDB", _FakeDB)
    resp = minnid._handle_health_report({"_recovery": False}, 1)
    rep = resp["result"]

    # Authenticated callers get the un-redacted shape (no redaction marker).
    assert "redacted" not in rep
    assert "stale_docs" in rep


def test_handle_health_report_redacts_by_default_when_flag_absent(monkeypatch):
    """Fail-closed: if the trusted _recovery flag is missing, redact."""
    import minnid

    monkeypatch.setattr(minnid, "SovereignDB", _FakeDB)
    resp = minnid._handle_health_report({}, 1)
    rep = resp["result"]

    assert "redacted" in rep

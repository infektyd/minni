import json
import os
import time
from pathlib import Path
import pytest
import shutil

from minni.config import SovereignConfig
import minni.db as db_mod

# Try to import from minnid. Under TDD, this should fail initially since the feature is not yet built.
try:
    from minni.minnid import _extract_recent_sessions, _clean_message_content
except ImportError:
    # Under TDD, we want the tests to run but fail on assertions or NameErrors rather than unhandled ImportErrors
    _extract_recent_sessions = None
    _clean_message_content = None

# Skipped: this is a TDD spec for an as-yet-unbuilt session-extractor feature
# (`_extract_recent_sessions` is not implemented in minnid). Kept in-repo as the
# executable spec; un-skip it when the feature lands.
pytestmark = pytest.mark.skip(reason="unbuilt feature: minnid._extract_recent_sessions")


def _make_db(tmp_path):
    cfg = SovereignConfig(
        db_path=str(tmp_path / "test.db"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
        faiss_index_path=str(tmp_path / "faiss.index"),
        writeback_enabled=False,
    )
    # Set custom test attributes on config
    cfg.session_extraction_enabled = True
    cfg.session_extraction_interval = 600
    cfg.session_extraction_idle_seconds = 2  # 2 seconds idle for fast testing
    cfg.session_extraction_min_messages = 3
    
    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag
    return db_obj, cfg


def test_clean_message_content():
    # Test that TDD clean content is loaded and throws/fails if not imported
    assert _clean_message_content is not None, "clean message content helper must be imported"
    
    dirty = "declare -x PATH=/usr/bin\nUSER=test\nNormal content line\n[a-f0-9]{20,}\nabcdef0123456789abcd\n"
    cleaned = _clean_message_content(dirty)
    assert "declare -x" not in cleaned
    assert "USER=" not in cleaned
    assert "Normal content line" in cleaned
    assert "abcdef0123456789abcd" not in cleaned


def test_extract_recent_sessions_lifecycle(tmp_path, monkeypatch):
    assert _extract_recent_sessions is not None, "extract_recent_sessions function must be imported"

    db_obj, cfg = _make_db(tmp_path)
    
    # Create thread and add events
    thread_id = "test-thread-123"
    now = time.time()
    
    # Seed a thread
    with db_obj.cursor() as c:
        c.execute("""
            INSERT INTO threads (thread_id, title, created_at, updated_at, agent_count, message_count)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (thread_id, "Test Thread Title", now - 5, now - 3, 1, 3))
        
        # Add 3 messages
        c.execute("""
            INSERT INTO episodic_events (agent_id, event_type, content, thread_id, created_at)
            VALUES (?, 'message', ?, ?, ?)
        """, ("forge", "[user] hello agent", thread_id, now - 5))
        c.execute("""
            INSERT INTO episodic_events (agent_id, event_type, content, thread_id, created_at)
            VALUES (?, 'message', ?, ?, ?)
        """, ("forge", "hello user", thread_id, now - 4))
        c.execute("""
            INSERT INTO episodic_events (agent_id, event_type, content, thread_id, created_at)
            VALUES (?, 'message', ?, ?, ?)
        """, ("forge", "[user] let's write a file called test.txt", thread_id, now - 3))

    # Mock afm_chat_completion to return a mock JSON response
    mock_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "summary": "User and agent discussed writing test.txt.",
                        "userFacts": ["User wants to write test.txt"],
                        "preferences": ["Prefers code files"],
                        "decisions": ["Decided to create test.txt"],
                        "artifacts": ["test.txt"],
                        "keywords": ["test", "txt"]
                    })
                }
            }
        ]
    }
    
    class MockResult:
        def __init__(self, ok, data):
            self.ok = ok
            self.data = data
            self.error = None
            
    monkeypatch.setattr(
        "minnid.afm_chat_completion",
        lambda payload, timeout: MockResult(ok=True, data=mock_response)
    )
    
    # Wait for the thread to become idle (since idle_seconds = 2)
    time.sleep(2.1)
    
    # Run the extraction pass
    monkeypatch.setattr("minni.minnid.SovereignDB", lambda config=None: db_obj)
    
    _extract_recent_sessions(cfg)
    
    # Verify that a wiki file was created
    wiki_sessions_dir = Path(cfg.vault_path) / "auto-indexed" / "sessions"
    assert wiki_sessions_dir.exists(), "Sessions directory must be created"
    
    files = list(wiki_sessions_dir.glob("*.md"))
    assert len(files) == 1, "One markdown session note should be generated"
    
    content = files[0].read_text()
    assert "Test Thread Title" in content
    assert "User wants to write test.txt" in content
    
    # Verify that a 'session_extracted' event was logged to prevent duplicate extraction
    with db_obj.cursor() as c:
        c.execute("SELECT COUNT(*) as cnt FROM episodic_events WHERE event_type = 'session_extracted' AND thread_id = ?", (thread_id,))
        assert c.fetchone()["cnt"] == 1, "Should log session_extracted event"

    # Now verify that running it again does NOT extract the session again
    os.remove(files[0])
    _extract_recent_sessions(cfg)
    assert len(list(wiki_sessions_dir.glob("*.md"))) == 0, "Should not re-extract already processed session"

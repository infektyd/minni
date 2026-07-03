import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))


def test_agent_id_tag_hygiene_restores_swapped_content_before_retagging():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE learnings (agent_id TEXT, content TEXT)")
    conn.execute(
        "INSERT INTO learnings(agent_id, content) VALUES (?, ?)",
        ("Discord bot tags need canonical ownership", "--agent syntra"),
    )

    migration = Path(__file__).parent.parent / "src" / "minni" / "migrations" / "012_agent_id_tag_hygiene.sql"
    conn.executescript(migration.read_text(encoding="utf-8"))

    row = conn.execute("SELECT agent_id, content FROM learnings").fetchone()
    assert row == ("syntra", "Discord bot tags need canonical ownership")

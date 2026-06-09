import os
import sys
from pathlib import Path

ENGINE_DIR = str(Path(__file__).resolve().parent)
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

from db import SovereignDB, connect
from ax_memory import AXMemory
from config import DEFAULT_CONFIG

def main():
    # Use an in-memory DB for test isolation if preferred, or test DB
    import sqlite3
    db = SovereignDB(DEFAULT_CONFIG)
    # Ensure schema is loaded
    db._get_conn()
    
    ax = AXMemory(db)
    
    # 1. Insert snapshot
    print("Inserting AX snapshot...")
    snapshot_id = ax.add_snapshot(
        agent_id="test_agent",
        app_name="Xcode",
        tree_json='{"node": "window", "children": []}',
        ttl_seconds=3600
    )
    print(f"Inserted snapshot ID: {snapshot_id}")
    assert snapshot_id > 0, "Insert failed"
    
    # 2. Select latest snapshot
    print("Selecting latest AX snapshot...")
    latest = ax.get_latest_snapshot("test_agent")
    assert latest is not None, "Failed to retrieve snapshot"
    print(f"Retrieved snapshot: {latest}")
    assert latest["app_name"] == "Xcode", "App name mismatch"
    assert latest["tree_json"] == '{"node": "window", "children": []}', "Tree JSON mismatch"
    
    # 3. Test GC
    print("Testing cleanup...")
    # Add an expired one manually
    with db.cursor() as c:
        c.execute("""
            INSERT INTO ax_snapshots (agent_id, app_name, tree_json, created_at, ttl_seconds)
            VALUES ('test_agent', 'Terminal', '{}', 0, 10)
        """)
    
    deleted = ax.cleanup_expired()
    print(f"Cleaned up {deleted} expired snapshots")
    assert deleted > 0, "Cleanup failed to delete expired snapshot"
    
    print("All AX memory tests passed successfully!")

if __name__ == "__main__":
    main()

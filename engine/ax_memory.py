import json
import time
import zlib
import logging
from typing import Dict, List, Optional
from datetime import datetime

from db import SovereignDB

logger = logging.getLogger("sovereign.ax")

class AXMemory:
    """
    AX Snapshots memory: UI accessibility trees and screenshots.
    """

    def __init__(self, db: SovereignDB):
        self.db = db

    def add_snapshot(
        self,
        agent_id: str,
        app_name: str,
        tree_json: str,
        screenshot_png: Optional[bytes] = None,
        ttl_seconds: int = 3600
    ) -> int:
        now = time.time()
        
        # Compress the tree JSON using zlib to save space
        compressed_tree = zlib.compress(tree_json.encode('utf-8'), level=6)
        
        # We store the compressed tree as BLOB in SQLite, but wait, the schema 
        # tree_json TEXT. So we shouldn't compress it if it's TEXT, or we should 
        # base64 encode it, or just store it as raw TEXT. JSON trees can be a few KB.
        # Let's just store it as TEXT since it's defined as TEXT in schema.
        
        with self.db.cursor() as c:
            c.execute("""
                INSERT INTO ax_snapshots
                (agent_id, app_name, tree_json, screenshot_png, created_at, ttl_seconds)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                agent_id, app_name, tree_json, screenshot_png, now, ttl_seconds
            ))
            return c.lastrowid

    def get_latest_snapshot(self, agent_id: str, app_name: Optional[str] = None) -> Optional[Dict]:
        with self.db.cursor() as c:
            if app_name:
                c.execute("""
                    SELECT snapshot_id, agent_id, app_name, tree_json, created_at
                    FROM ax_snapshots
                    WHERE agent_id = ? AND app_name = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (agent_id, app_name))
            else:
                c.execute("""
                    SELECT snapshot_id, agent_id, app_name, tree_json, created_at
                    FROM ax_snapshots
                    WHERE agent_id = ?
                    ORDER BY created_at DESC LIMIT 1
                """, (agent_id,))
                
            row = c.fetchone()
            if not row:
                return None
                
            return {
                "snapshot_id": row["snapshot_id"],
                "agent_id": row["agent_id"],
                "app_name": row["app_name"],
                "tree_json": row["tree_json"],
                "timestamp": datetime.fromtimestamp(row["created_at"]).isoformat()
            }

    def cleanup_expired(self) -> int:
        now = time.time()
        with self.db.cursor() as c:
            c.execute("""
                DELETE FROM ax_snapshots 
                WHERE created_at + ttl_seconds < ?
            """, (now,))
            return c.rowcount

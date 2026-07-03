import json
import time
import logging
from typing import Dict, List, Optional
from datetime import datetime

from minni.db import SovereignDB

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
        now = time.time()
        self.cleanup_expired()
        with self.db.cursor() as c:
            if app_name:
                c.execute("""
                    SELECT snapshot_id, agent_id, app_name, tree_json, created_at, ttl_seconds
                    FROM ax_snapshots
                    WHERE agent_id = ? AND app_name = ?
                      AND created_at + ttl_seconds >= ?
                    ORDER BY created_at DESC LIMIT 1
                """, (agent_id, app_name, now))
            else:
                c.execute("""
                    SELECT snapshot_id, agent_id, app_name, tree_json, created_at, ttl_seconds
                    FROM ax_snapshots
                    WHERE agent_id = ?
                      AND created_at + ttl_seconds >= ?
                    ORDER BY created_at DESC LIMIT 1
                """, (agent_id, now))
                
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

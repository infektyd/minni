#!/usr/bin/env python3
"""
Migrate Sovereign Memory V3 → V3.1.

What this does:
1. Adds new tables (learnings, learnings_fts) and columns (heading_context)
2. Drops 'compressed' and 'norm' columns from chunk_embeddings
3. Re-indexes vault with markdown-aware chunking and raw float32 embeddings
4. Rebuilds FAISS index

Non-destructive to episodic data, threads, and task logs.
Chunk embeddings are re-computed (the old compressed ones are useless in V3.1).

Usage:
    python migrate_v3_to_v3_1.py [--db-path ~/.openclaw/sovereign_memory.db]
"""

import sqlite3
import os
import sys
import time
import json
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("migrate")


def migrate(db_path: str = None):
    """Run migration from V3 to V3.1."""
    if db_path is None:
        db_path = os.path.expanduser("~/.openclaw/sovereign_memory.db")

    if not os.path.exists(db_path):
        logger.info("No existing database at %s. Nothing to migrate.", db_path)
        logger.info("Just run: python sovereign_memory.py index")
        return

    logger.info("Migrating V3 → V3.1: %s", db_path)

    # Import V3.1 modules
    from config import SovereignConfig
    from db import SovereignDB
    from indexer import VaultIndexer

    config = SovereignConfig(db_path=db_path)
    db = SovereignDB(config)

    # Step 1: Schema migration
    # The SovereignDB._init_schema() will create new tables (learnings, etc.)
    # We need to handle the chunk_embeddings column changes manually
    logger.info("Step 1: Schema migration...")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Check if we need to migrate chunk_embeddings
    c.execute("PRAGMA table_info(chunk_embeddings)")
    columns = {row["name"] for row in c.fetchall()}

    needs_chunk_migration = "compressed" in columns or "heading_context" not in columns

    if needs_chunk_migration:
        logger.info("  Migrating chunk_embeddings schema...")

        # Check if any compressed embeddings exist
        has_compressed = False
        if "compressed" in columns:
            c.execute("SELECT COUNT(*) FROM chunk_embeddings WHERE compressed = 1")
            has_compressed = c.fetchone()[0] > 0

        if has_compressed:
            logger.info("  Found compressed embeddings — will re-index all documents")
            # Drop all chunk embeddings (they'll be re-computed)
            c.execute("DELETE FROM chunk_embeddings")
            logger.info("  Cleared compressed chunk embeddings")

        # Add heading_context column if missing
        if "heading_context" not in columns:
            try:
                c.execute("ALTER TABLE chunk_embeddings ADD COLUMN heading_context TEXT")
                logger.info("  Added heading_context column")
            except Exception as e:
                logger.info("  heading_context column: %s", e)

        # Note: SQLite doesn't support DROP COLUMN before 3.35.0
        # The 'compressed' and 'norm' columns will just be ignored by V3.1
        if "compressed" in columns:
            logger.info("  Note: 'compressed' and 'norm' columns left in place (SQLite limitation)")
            logger.info("  They will be ignored by V3.1 code")

    conn.commit()
    conn.close()

    # Step 2: Create writeback directory
    logger.info("Step 2: Ensuring writeback directory...")
    os.makedirs(config.writeback_path, exist_ok=True)
    logger.info("  Writeback path: %s", config.writeback_path)

    # Step 3: Re-index vault with V3.1 chunker
    logger.info("Step 3: Re-indexing vault with markdown-aware chunking...")

    # Force re-index by clearing last_modified
    with db.cursor() as cur:
        cur.execute("UPDATE documents SET last_modified = 0")

    indexer = VaultIndexer(db, config)
    result = indexer.index_vault(verbose=True)
    logger.info("  Re-index result: %s", json.dumps(result, indent=2))

    # Step 4: Stats
    logger.info("\nMigration complete.")
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) as n FROM documents")
        docs = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) as n FROM chunk_embeddings")
        chunks = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) as n FROM episodic_events")
        events = cur.fetchone()["n"]

    logger.info("  Documents: %d", docs)
    logger.info("  Chunks: %d (with heading context)", chunks)
    logger.info("  Episodic events: %d (preserved)", events)
    logger.info("  Embedding size: %d bytes per vector (full fidelity)", config.embedding_dim * 4)
    logger.info("  FAISS index: %s", indexer.faiss_index.get_stats())

    db.close()


if __name__ == "__main__":
    db_path = None
    if "--db-path" in sys.argv:
        idx = sys.argv.index("--db-path")
        if idx + 1 < len(sys.argv):
            db_path = sys.argv[idx + 1]
    migrate(db_path)

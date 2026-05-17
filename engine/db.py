"""
Sovereign Memory V3.1 — Database Layer.

V3.1 changes:
- Removed 'compressed' and 'norm' columns from chunk_embeddings (no more compression)
- Added 'learnings' table for write-back memory
- Added 'learnings_fts' for searching learnings
- Embeddings are always raw float32 blobs (384-dim = 1536 bytes each)
"""

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional

from config import SovereignConfig, DEFAULT_CONFIG

# Module-level flag: migrations run exactly once per process, regardless of
# how many SovereignDB instances are created or how many threads call connect.
_migrations_run = False
_migrations_lock = threading.Lock()


class SovereignDB:
    """
    Thread-safe SQLite connection manager.
    WAL mode allows concurrent readers + one writer without blocking.
    """

    def __init__(self, config: SovereignConfig = DEFAULT_CONFIG):
        self.config = config
        self.config.ensure_dirs()
        self._local = threading.local()
        self._schema_initialized = False
        self._lock = threading.Lock()

    def _get_conn(self) -> sqlite3.Connection:
        """Get thread-local connection (one connection per thread)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                self.config.db_path,
                timeout=30,
                check_same_thread=False
            )
            # WAL mode: concurrent readers, non-blocking writes
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn

        if not self._schema_initialized:
            with self._lock:
                if not self._schema_initialized:
                    self._init_schema(self._local.conn)
                    self._schema_initialized = True

        return self._local.conn

    @contextmanager
    def cursor(self):
        """Context manager yielding a cursor with auto-commit."""
        conn = self._get_conn()
        c = conn.cursor()
        try:
            yield c
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @contextmanager
    def transaction(self):
        """Explicit transaction block for batched writes."""
        conn = self._get_conn()
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_schema(self, conn: sqlite3.Connection):
        """Initialize all tables in one place."""
        c = conn.cursor()

        # === Vault Index (FTS5) ===
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts
            USING fts5(
                doc_id UNINDEXED,
                path UNINDEXED,
                content,
                agent UNINDEXED,
                sigil UNINDEXED,
                tokenize='porter unicode61'
            )
        """)

        # === Document Metadata ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS documents (
                doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT UNIQUE NOT NULL,
                agent TEXT DEFAULT 'unknown',
                sigil TEXT DEFAULT '❓',
                last_modified REAL,
                indexed_at REAL,
                access_count INTEGER DEFAULT 0,
                last_accessed REAL,
                decay_score REAL DEFAULT 1.0,
                whole_document INTEGER DEFAULT 0,
                -- PR-2: Page status lifecycle and privacy
                page_status TEXT DEFAULT 'candidate',
                privacy_level TEXT DEFAULT 'safe',
                page_type TEXT,
                superseded_by INTEGER,
                expires_at REAL,
                evidence_refs TEXT,
                layer TEXT DEFAULT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_doc_path ON documents(path)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_doc_agent ON documents(agent)")
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_doc_layer ON documents(layer)")
        except sqlite3.OperationalError as e:
            if "no such column: layer" not in str(e).lower():
                raise

        # === Chunk Embeddings (one doc → many chunks) ===
        # V3.1: No more 'compressed' or 'norm' columns.
        # All embeddings are raw float32[384] = 1536 bytes.
        c.execute("""
            CREATE TABLE IF NOT EXISTS chunk_embeddings (
                chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                chunk_text TEXT,
                embedding BLOB NOT NULL,
                heading_context TEXT,
                model_name TEXT,
                computed_at REAL,
                layer TEXT DEFAULT NULL,
                FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE,
                UNIQUE(doc_id, chunk_index)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_chunk_doc ON chunk_embeddings(doc_id)")
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_chunk_layer ON chunk_embeddings(layer)")
        except sqlite3.OperationalError as e:
            if "no such column: layer" not in str(e).lower():
                raise

        # === Memory Links ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_links (
                source_doc_id INTEGER NOT NULL,
                target_doc_id INTEGER NOT NULL,
                link_type TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                created_at REAL,
                PRIMARY KEY(source_doc_id, target_doc_id, link_type),
                FOREIGN KEY(source_doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE,
                FOREIGN KEY(target_doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            )
        """)

        # === Episodic Events ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS episodic_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                content TEXT,
                task_id TEXT,
                thread_id TEXT,
                metadata TEXT,
                compressed_raw BLOB,
                created_at REAL NOT NULL,
                ttl_seconds INTEGER DEFAULT 604800
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ep_agent ON episodic_events(agent_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ep_thread ON episodic_events(thread_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ep_created ON episodic_events(created_at)")

        # === Episodic FTS ===
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts
            USING fts5(
                event_id UNINDEXED,
                agent_id UNINDEXED,
                content,
                tokenize='porter unicode61'
            )
        """)

        # === Task Logs ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS task_logs (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                task_id TEXT UNIQUE NOT NULL,
                description TEXT,
                status TEXT DEFAULT 'running',
                start_time REAL,
                end_time REAL,
                result TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_task_agent ON task_logs(agent_id)")

        # === Threads ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS threads (
                thread_id TEXT PRIMARY KEY,
                title TEXT,
                created_at REAL,
                updated_at REAL,
                agent_count INTEGER DEFAULT 1,
                message_count INTEGER DEFAULT 0
            )
        """)

        # === Thread ↔ Document Links ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS thread_doc_links (
                thread_id TEXT NOT NULL,
                doc_id INTEGER NOT NULL,
                similarity REAL NOT NULL,
                created_at REAL,
                PRIMARY KEY(thread_id, doc_id),
                FOREIGN KEY(thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE,
                FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            )
        """)

        # === Agent Context Cache ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_context (
                agent_id TEXT NOT NULL,
                doc_id INTEGER NOT NULL,
                relevance_score REAL,
                last_used REAL,
                PRIMARY KEY(agent_id, doc_id),
                FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            )
        """)

        # === Write-Back Learnings (V3.1 NEW) ===
        c.execute("""
            CREATE TABLE IF NOT EXISTS learnings (
                learning_id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                content TEXT NOT NULL,
                source_doc_ids TEXT,
                source_query TEXT,
                confidence REAL DEFAULT 1.0,
                embedding BLOB,
                created_at REAL NOT NULL,
                access_count INTEGER DEFAULT 0,
                last_accessed REAL,
                superseded_by INTEGER,
                FOREIGN KEY(superseded_by) REFERENCES learnings(learning_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_learn_agent ON learnings(agent_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_learn_cat ON learnings(category)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_learn_superseded ON learnings(superseded_by)")

        # === Learnings FTS (V3.1 NEW) ===
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts
            USING fts5(
                learning_id UNINDEXED,
                agent_id UNINDEXED,
                category UNINDEXED,
                content,
                tokenize='porter unicode61'
            )
        """)

        # === Triggers ===
        c.execute("DROP TRIGGER IF EXISTS trg_thread_msg_count")
        c.execute("""
            CREATE TRIGGER trg_thread_msg_count
            AFTER INSERT ON episodic_events
            WHEN NEW.thread_id IS NOT NULL
            BEGIN
                UPDATE threads
                SET message_count = message_count + 1,
                    updated_at = NEW.created_at
                WHERE thread_id = NEW.thread_id;
            END
        """)

        c.execute("DROP TRIGGER IF EXISTS trg_episodic_fts_insert")
        c.execute("""
            CREATE TRIGGER trg_episodic_fts_insert
            AFTER INSERT ON episodic_events
            WHEN NEW.content IS NOT NULL
            BEGIN
                INSERT INTO episodic_fts(event_id, agent_id, content)
                VALUES (NEW.event_id, NEW.agent_id, NEW.content);
            END
        """)

        # Auto-index learnings into FTS
        c.execute("DROP TRIGGER IF EXISTS trg_learnings_fts_insert")
        c.execute("""
            CREATE TRIGGER trg_learnings_fts_insert
            AFTER INSERT ON learnings
            BEGIN
                INSERT INTO learnings_fts(learning_id, agent_id, category, content)
                VALUES (NEW.learning_id, NEW.agent_id, NEW.category, NEW.content);
            END
        """)

        conn.commit()

        # Run schema migrations exactly once per process, after the base schema
        # is committed. The module-level flag ensures re-entry is impossible
        # even if multiple SovereignDB instances are created.
        global _migrations_run
        if not _migrations_run:
            with _migrations_lock:
                if not _migrations_run:
                    try:
                        from migrations import run_migrations
                        run_migrations(conn)
                        conn.execute("CREATE INDEX IF NOT EXISTS idx_doc_layer ON documents(layer)")
                        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunk_layer ON chunk_embeddings(layer)")
                        conn.commit()
                    except Exception as e:
                        import logging
                        logging.getLogger("sovereign.db").warning(
                            "Migrations runner failed (non-fatal): %s", e
                        )
                    _migrations_run = True

    def close(self):
        """Close thread-local connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None


def connect(config: SovereignConfig = DEFAULT_CONFIG) -> SovereignDB:
    """
    Create and return a SovereignDB instance with the schema initialized and
    migrations applied. This is the canonical entry point for callers that
    just need a connected database handle.

    The migrations runner is guarded by a module-level flag and runs at most
    once per process, even if connect() is called multiple times.
    """
    db = SovereignDB(config)
    # Trigger schema init + migrations by acquiring a connection
    db._get_conn()
    return db

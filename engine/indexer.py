"""
Sovereign Memory V3.1 — Vault Indexer.

V3.1 changes over V3:
1. Markdown-aware chunking (via chunker.py) instead of blind word-count splitting
2. No compression — embeddings stored as raw float32[384]
3. FAISS index built/updated on each index run
4. Heading context stored per chunk for retrieval enrichment
5. Chunk text stored in full (V3 truncated to 500 chars)
"""

import os
import re
import time
import logging
from typing import Dict, List, Optional

import numpy as np

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB
from chunker import MarkdownChunker
from faiss_index import FAISSIndex

logger = logging.getLogger("sovereign.indexer")


class VaultIndexer:
    """Index Obsidian vault with markdown-aware chunking and FAISS indexing."""

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
    ):
        self.db = db
        self.config = config
        self.chunker = MarkdownChunker(config)
        self.faiss_index = FAISSIndex(config)
        self._model = None

    @property
    def model(self):
        """Return the process-wide embedding model singleton."""
        from models import get_embedder
        return get_embedder()

    # ── Metadata extraction ────────────────────────────────────

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict[str, str]:
        """
        Extract agent/sigil and PR-2 status/privacy from YAML-style frontmatter.

        PR-2: Also parses status, privacy, and page type.
        """
        agent_m = re.search(r"agent:\s*([^\s]+)", content)
        sigil_m = re.search(r"sigil:\s*(.)", content)
        status_m = re.search(r"status:\s*(\S+)", content)
        privacy_m = re.search(r"privacy:\s*(\S+)", content)
        type_m = re.search(r"type:\s*(\S+)", content)

        status = (status_m.group(1).strip('"\'') if status_m else "candidate").lower()
        privacy = (privacy_m.group(1).strip('"\'') if privacy_m else "safe").lower()
        page_type = type_m.group(1).strip('"\'') if type_m else None
        agent = agent_m.group(1).strip('"\'') if agent_m else "unknown"

        # Clamp to valid values
        valid_statuses = {"draft", "candidate", "accepted", "superseded", "rejected", "expired"}
        valid_privacies = {"safe", "local-only", "private", "blocked"}
        if status not in valid_statuses:
            status = "candidate"
        if privacy not in valid_privacies:
            privacy = "safe"

        layer = VaultIndexer._infer_layer(agent=agent, page_type=page_type)

        return {
            "agent": agent,
            "sigil": sigil_m.group(1) if sigil_m else "❓",
            "page_status": status,
            "privacy_level": privacy,
            "page_type": page_type,
            "layer": layer,
        }

    @staticmethod
    def _infer_layer(agent: str, page_type: Optional[str], whole_document: int = 1) -> str:
        """Map document metadata to the PR-5 recall layer taxonomy."""
        if whole_document == 1 and agent.startswith("identity:"):
            return "identity"
        if page_type and page_type.lower() == "artifact":
            return "artifact"
        return "knowledge"

    @staticmethod
    def _invalidate_rerank_chunks(chunk_ids: List[int]) -> None:
        try:
            from rerank_cache import invalidate_chunks
            invalidate_chunks(chunk_ids)
        except Exception as exc:
            logger.debug("Rerank cache invalidation skipped: %s", exc)

    # ── Core indexing ──────────────────────────────────────────

    def index_vault(self, verbose: bool = False) -> Dict:
        """
        Full incremental index of the vault.
        Returns stats dict.
        """
        vault = self.config.vault_path
        if not os.path.isdir(vault):
            return {"status": "error", "message": f"Vault not found: {vault}"}

        # Collect current files on disk
        disk_files: Dict[str, float] = {}
        for root, _, files in os.walk(vault):
            for fname in files:
                if fname.endswith(".md"):
                    full = os.path.join(root, fname)
                    disk_files[full] = os.path.getmtime(full)

        stats = {"indexed": 0, "skipped": 0, "deleted": 0, "chunks": 0, "errors": 0}

        with self.db.transaction() as c:
            # Phase 1: Index new/changed files
            for path, mtime in disk_files.items():
                try:
                    c.execute(
                        "SELECT doc_id, last_modified FROM documents WHERE path = ?",
                        (path,),
                    )
                    row = c.fetchone()

                    if row and row["last_modified"] >= mtime:
                        stats["skipped"] += 1
                        continue

                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()

                    meta = self._extract_frontmatter(content)
                    now = time.time()

                    # PR-2: Skip pages with privacy: blocked
                    if meta.get("privacy_level") == "blocked":
                        logger.info("Skipping blocked page: %s", os.path.basename(path))
                        stats["skipped"] += 1
                        continue

                    if row:
                        doc_id = row["doc_id"]
                        layer = meta.get("layer", "knowledge")
                        c.execute(
                            """UPDATE documents
                               SET agent=?, sigil=?, last_modified=?, indexed_at=?,
                                   page_status=?, privacy_level=?, page_type=?, layer=?
                               WHERE doc_id=?""",
                            (meta["agent"], meta["sigil"], mtime, now,
                             meta.get("page_status", "candidate"),
                             meta.get("privacy_level", "safe"),
                             meta.get("page_type"),
                             layer,
                             doc_id),
                        )
                        c.execute(
                            "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?",
                            (doc_id,),
                        )
                        self._invalidate_rerank_chunks([r["chunk_id"] for r in c.fetchall()])
                        c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                        c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
                    else:
                        layer = meta.get("layer", "knowledge")
                        c.execute(
                            """INSERT INTO documents (path, agent, sigil, last_modified, indexed_at,
                                   page_status, privacy_level, page_type, layer)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (path, meta["agent"], meta["sigil"], mtime, now,
                             meta.get("page_status", "candidate"),
                             meta.get("privacy_level", "safe"),
                             meta.get("page_type"),
                             layer),
                        )
                        doc_id = c.lastrowid

                    # FTS5 insert (full content for keyword search)
                    c.execute(
                        """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
                           VALUES (?, ?, ?, ?, ?)""",
                        (doc_id, path, content, meta["agent"], meta["sigil"]),
                    )

                    # Markdown-aware chunk embeddings
                    if self.model:
                        chunks = self.chunker.chunk_document(content)
                        for chunk in chunks:
                            emb = self.model.encode(chunk.text)
                            emb_bytes = emb.astype(np.float32).tobytes()

                            c.execute(
                                """INSERT INTO chunk_embeddings
                                   (doc_id, chunk_index, chunk_text, embedding,
                                    heading_context, model_name, computed_at, layer)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    doc_id, chunk.chunk_index, chunk.text,
                                    emb_bytes, chunk.heading_path,
                                    self.config.embedding_model, now, layer,
                                ),
                            )
                            stats["chunks"] += 1

                    stats["indexed"] += 1
                    if verbose:
                        n_chunks = len(self.chunker.chunk_document(content)) if self.model else 0
                        logger.info("  ✓ %s [%s/%s] (%d chunks)",
                                    os.path.basename(path),
                                    meta["agent"], meta["sigil"], n_chunks)

                except Exception as e:
                    stats["errors"] += 1
                    if verbose:
                        logger.error("  ✗ %s: %s", path, e)

            # Phase 2: Remove docs no longer on disk
            c.execute("SELECT doc_id, path FROM documents")
            for row in c.fetchall():
                if row["path"] not in disk_files:
                    c.execute(
                        "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?",
                        (row["doc_id"],),
                    )
                    self._invalidate_rerank_chunks([r["chunk_id"] for r in c.fetchall()])
                    c.execute("DELETE FROM documents WHERE doc_id = ?", (row["doc_id"],))
                    c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (row["doc_id"],))
                    c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (row["doc_id"],))
                    stats["deleted"] += 1
                    if verbose:
                        logger.info("  🗑 Removed: %s", row["path"])

        # Phase 3: Rebuild FAISS index from all embeddings
        self._rebuild_faiss_index()
        self._sync_vector_backends()

        return {"status": "success", **stats}

    def _rebuild_faiss_index(self) -> None:
        """Rebuild the FAISS index from all chunk embeddings in the DB."""
        chunk_ids = []
        embeddings = []

        with self.db.cursor() as c:
            c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
            for row in c.fetchall():
                vec = np.frombuffer(row["embedding"], dtype=np.float32)
                if vec.shape[0] == self.config.embedding_dim:
                    chunk_ids.append(row["chunk_id"])
                    embeddings.append(vec)

        if chunk_ids:
            all_vecs = np.array(embeddings, dtype=np.float32)
            self.faiss_index.build_from_vectors(chunk_ids, all_vecs)
            logger.info("FAISS index rebuilt: %d vectors (%s)",
                        len(chunk_ids), self.faiss_index._current_type)

    def _sync_vector_backends(self) -> None:
        """Best-effort PR-3 sync from SQLite chunks into configured backends."""
        try:
            from backends.faiss_disk import FaissDiskBackend
            from backends.faiss_mem import FaissMemBackend
            from vector_sync import sync_all
        except Exception as exc:
            logger.debug("Vector backend sync unavailable: %s", exc)
            return

        backends = []
        for name in getattr(self.config, "vector_backends", ["faiss-disk"]):
            if name == "faiss-disk":
                backends.append(FaissDiskBackend(self.config, self.db))
            elif name == "faiss-mem":
                backends.append(FaissMemBackend(self.config))

        if not backends:
            return

        try:
            sync_all(backends, self.db, self.config)
        except Exception as exc:
            logger.warning("Vector backend sync failed: %s", exc)

    def get_faiss_index(self) -> FAISSIndex:
        """Get the current FAISS index (for use by retrieval engine)."""
        if self.faiss_index.count == 0:
            self._rebuild_faiss_index()
        return self.faiss_index

    # ── File watcher ───────────────────────────────────────────

    def start_watcher(self):
        """Start filesystem watcher with debounced re-indexing."""
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        DEBOUNCE_SEC = 5

        class _Handler(FileSystemEventHandler):
            def __init__(self, indexer):
                self._indexer = indexer
                self._last = 0

            def on_any_event(self, event):
                if event.is_directory or not event.src_path.endswith(".md"):
                    return
                now = time.time()
                if now - self._last > DEBOUNCE_SEC:
                    self._last = now
                    logger.info("Change detected: %s", event.src_path)
                    self._indexer.index_vault()

        observer = Observer()
        observer.schedule(_Handler(self), self.config.vault_path, recursive=True)
        observer.start()
        logger.info("Watching %s for changes...", self.config.vault_path)
        return observer

"""
Minni V3.1 — Vault Indexer.

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
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import yaml  # G21: safe YAML parser for frontmatter (adversarial body-forge resistance)

from minni.config import SovereignConfig, DEFAULT_CONFIG
from minni.db import SovereignDB
from minni.chunker import MarkdownChunker
from minni.faiss_index import FAISSIndex
from minni.timestamps import parse_epoch_or_report

logger = logging.getLogger("sovereign.indexer")

# Lifecycle states that are indexed and lexically searchable but deliberately
# NOT embedded — retrieve() filters them out downstream of the FAISS window, so
# embedding them costs accepted pages their candidate slots. See index_vault.
UNEMBEDDED_STATUSES = frozenset({"draft", "expired"})


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
        from minni.models import get_embedder
        return get_embedder()

    # ── Metadata extraction ────────────────────────────────────

    # G21: shared frontmatter block regex (same pattern as wiki_indexer for consistency)
    FRONTMATTER_BLOCK_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL | re.MULTILINE)

    @staticmethod
    def _extract_frontmatter(content: str) -> Dict[str, str]:
        """
        G21: Extract via YAML frontmatter block + yaml.safe_load (SEC-011/SEC-018).

        Replaces brittle re.search over *entire* content (which allowed body lines
        to forge agent/status/privacy). Now requires proper --- delimited block;
        body content (including fake --- or key: val inside code fences) is ignored.

        Falls back to safe defaults on missing/malformed block.
        """
        match = VaultIndexer.FRONTMATTER_BLOCK_RE.match(content or "")
        if not match:
            # No fenced frontmatter → safe defaults (body spoof impossible)
            return {
                "agent": "unknown",
                "sigil": "❓",
                "page_status": "candidate",
                "privacy_level": "safe",
                "page_type": None,
                "layer": "knowledge",
            }

        yaml_block = match.group(1)
        try:
            data = yaml.safe_load(yaml_block) or {}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}

        # Pull with safe string coercion (yaml.safe_load already handles quotes/lists)
        def _str(v, default=""):
            if v is None:
                return default
            return str(v).strip().strip("\"'")

        agent = _str(data.get("agent") or data.get("sigil"), "unknown")
        # M6: the "identity:" agent prefix self-assigns the trusted identity
        # recall layer via _infer_layer. On-disk vault markdown is untrusted
        # input (anyone who can write a file could forge `agent: identity:codex`),
        # so strip the prefix here — the identity layer is only assignable through
        # the trusted seed path, never from indexed frontmatter.
        if agent.startswith("identity:"):
            stripped = agent[len("identity:"):].strip()
            logger.warning(
                "indexer: stripping untrusted 'identity:' prefix from frontmatter "
                "agent %r (identity layer is not self-assignable from vault markdown)",
                agent,
            )
            agent = stripped or "unknown"
        sigil = _str(data.get("sigil"), "❓")
        status = _str(data.get("status"), "candidate").lower()
        privacy = _str(data.get("privacy"), "safe").lower()
        page_type = _str(data.get("type") or data.get("page_type"), None) or None

        # Clamp
        valid_statuses = {"draft", "candidate", "accepted", "superseded", "rejected", "expired"}
        valid_privacies = {"safe", "local-only", "private", "blocked"}
        if status not in valid_statuses:
            status = "candidate"
        if privacy not in valid_privacies:
            privacy = "safe"

        # SEC-006 duplicate-key differential (mirrors privacyFromMarkdown in
        # plugins/minni/src/vault.ts): yaml.safe_load is last-key-wins, so a
        # permissive `privacy:` duplicate after a restrictive one would relax
        # the gate. Take the MOST restrictive recognized declaration instead.
        declared = [
            _str(v).lower()
            for v in re.findall(r"^privacy:\s*(.+)$", yaml_block, re.MULTILINE)
        ]
        recognized = [v for v in declared if v in valid_privacies]
        if len(recognized) > 1:
            order = ["safe", "local-only", "private", "blocked"]
            privacy = max(recognized, key=order.index)

        layer = VaultIndexer._infer_layer(agent=agent, page_type=page_type)

        return {
            "agent": agent,
            "sigil": sigil,
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

    def _delete_doc_rows(self, doc_id: int) -> int:
        """Drop one document and everything keyed to it. Returns rows deleted (0 or 1)."""
        with self.db.cursor() as c:
            c.execute(
                "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?", (doc_id,)
            )
            stale = [r["chunk_id"] for r in c.fetchall()]
        with self.db.transaction() as c:
            c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
            c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
            c.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        if stale:
            self._invalidate_rerank_chunks(stale)
        return 1

    def _enforce_embed_policy(self, row, stats: Dict) -> None:
        """Strip embeddings from an unendorsed page that still has them.

        Reached only on the mtime-skip path, where the file has not changed and
        would otherwise never be revisited. Purging the chunks is enough --
        ``documents`` and ``vault_fts`` rows are correct and stay, so the page
        remains indexed and lexically findable, exactly as a freshly indexed
        draft would be.
        """
        if (row["page_status"] or "candidate") not in UNEMBEDDED_STATUSES:
            return
        doc_id = row["doc_id"]
        with self.db.cursor() as c:
            c.execute(
                "SELECT chunk_id FROM chunk_embeddings WHERE doc_id = ?", (doc_id,)
            )
            stale = [r["chunk_id"] for r in c.fetchall()]
        if not stale:
            return
        with self.db.transaction() as c:
            c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
        self._invalidate_rerank_chunks(stale)
        stats["chunks_purged"] += len(stale)

    def _noncanonical_rows(self, vault_root: Path, stats: Dict) -> Dict[str, Dict]:
        """Map resolved path -> row, for vault-owned rows stored non-canonically.

        Built ONCE per index run. The exact-path SELECT in phase 1 uses an
        index; falling back to a table scan per miss would be O(files x rows)
        -- and on a first index every file misses, so that scan is the common
        case, not the rare one. Almost always empty: real writers store
        resolved paths, so this only catches historical or symlinked rows.

        When SEVERAL non-canonical spellings resolve to the same file, only
        one can be adopted — a last-wins map silently kept the others as rows,
        and the prune spares them because they resolve to a file that exists.
        The extras are deleted here, counted in stats["deleted"].
        """
        from minni.path_safety import path_within_root

        out: Dict[str, Dict] = {}
        with self.db.cursor() as c:
            c.execute("SELECT doc_id, last_modified, path, page_status FROM documents")
            rows = c.fetchall()
        for row in rows:
            stored = row["path"]
            try:
                resolved = str(Path(stored).resolve())
            except Exception:
                continue
            if resolved == stored:
                continue
            if not path_within_root(stored, vault_root):
                continue
            prior = out.get(resolved)
            if prior is not None and prior["doc_id"] != row["doc_id"]:
                stats["deleted"] += self._delete_doc_rows(row["doc_id"])
                continue
            out[resolved] = row
        return out

    @staticmethod
    def _invalidate_rerank_chunks(chunk_ids: List[int]) -> None:
        try:
            from minni.rerank_cache import invalidate_chunks
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

        vault_root = Path(vault).resolve()

        # Collect current files on disk
        disk_files: Dict[str, float] = {}
        for root, _, files in os.walk(vault):
            for fname in files:
                if fname.endswith(".md"):
                    full = Path(root) / fname
                    from minni.path_safety import path_within_root
                    if not path_within_root(full, vault_root):
                        continue
                    full_resolved = str(full.resolve())
                    disk_files[full_resolved] = full.stat().st_mtime

        stats = {
            "indexed": 0, "skipped": 0, "deleted": 0, "chunks": 0,
            # Vectors removed from pages that should never have had them (see
            # _enforce_embed_policy); distinct from `deleted`, which is rows.
            "chunks_purged": 0, "errors": 0,
        }
        noncanonical_rows = self._noncanonical_rows(vault_root, stats)

        # Phase 1: index new/changed files.
        #
        # ONE SHORT TRANSACTION PER FILE, with every expensive step -- reading
        # the file, chunking, model.encode -- performed OUTSIDE it. This used to
        # be a single BEGIN IMMEDIATE wrapped around the entire walk, which
        # holds SQLite's reserved lock for as long as the sweep runs: the
        # measured first pass over this vault is ~36s against the 30s busy
        # timeout in db.py, so any concurrent writer (learn, durable store)
        # would have raised "database is locked". That was survivable while
        # index_all was operator-initiated. It is not, now that the daemon runs
        # this on a 300s timer against the shared DB.
        for path, mtime in disk_files.items():
            try:
                with self.db.cursor() as c:
                    c.execute(
                        "SELECT doc_id, last_modified, path, page_status"
                        " FROM documents WHERE path = ?",
                        (path,),
                    )
                    row = c.fetchone()
                other = noncanonical_rows.get(path)
                if row is None:
                    # `path` is resolved, but a historical row may store a
                    # non-canonical spelling of the SAME file. Matching only the
                    # canonical form would insert a second row for it, and the
                    # prune below (which accepts either form) would keep both.
                    # Adopt the existing row instead; the UPDATE normalizes its
                    # path on the way through.
                    row = other
                elif other is not None and other["doc_id"] != row["doc_id"]:
                    # BOTH spellings exist as separate rows -- the state an
                    # earlier version of this indexer could produce, since
                    # documents.path is UNIQUE per string, not per resolved
                    # file. Adoption alone never reaches it (the exact match
                    # wins) and the prune keeps both, because both resolve to a
                    # file that exists. Collapse to the canonical row.
                    stats["deleted"] += self._delete_doc_rows(other["doc_id"])

                # Audit R0 (grok-review): last_modified is a REAL-affinity
                # column an out-of-tree writer could poison with TEXT, same
                # class as indexed_at. `>= mtime` on a TEXT value raises
                # TypeError, which the broad except below turned into a
                # permanent stats["errors"] — the row never got rewritten,
                # so it stayed stuck. Parse-or-treat-as-stale: an
                # unparseable value defaults to 0.0, which is always less
                # than mtime, so the row is reindexed (and thereby
                # repaired) instead of stuck.
                last_modified = (
                    parse_epoch_or_report(
                        row["last_modified"], field="last_modified",
                        source="indexer.index_vault", doc_id=row["doc_id"],
                    ) or 0.0
                ) if row else 0.0
                if row and last_modified >= mtime:
                    # The FILE is unchanged, but that does not mean the row
                    # satisfies the no-embed invariant. An install that ever
                    # hand-ran the old index_all embedded every page, drafts
                    # included; those vectors keep occupying the FAISS window
                    # (retrieve() discards draft/expired only after it is
                    # filled) and nothing on disk will change to trigger a
                    # reindex until the TTL runs out. Enforce the policy from
                    # the row rather than waiting for an mtime bump.
                    self._enforce_embed_policy(row, stats)
                    # Same reasoning for a row adopted under a non-canonical
                    # spelling: normalizing only on the reindex path would
                    # leave an up-to-date row non-canonical indefinitely.
                    # vault_fts.path is normalized in the SAME transaction:
                    # recall joins d.path today, but a direct reader of the
                    # FTS row would otherwise see the stale spelling until a
                    # content reindex rewrites it.
                    if row["path"] != path:
                        with self.db.transaction() as c:
                            c.execute(
                                "UPDATE documents SET path=? WHERE doc_id=?",
                                (path, row["doc_id"]),
                            )
                            c.execute(
                                "UPDATE vault_fts SET path=? WHERE doc_id=?",
                                (path, row["doc_id"]),
                            )
                    stats["skipped"] += 1
                    continue

                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()

                meta = self._extract_frontmatter(content)
                now = time.time()

                # PR-2: Skip pages with privacy: blocked — purge any stale index rows
                if meta.get("privacy_level") == "blocked":
                    if row:
                        doc_id = row["doc_id"]
                        with self.db.transaction() as c:
                            c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                            c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
                            c.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
                        stats["deleted"] += 1
                    else:
                        stats["skipped"] += 1
                    continue

                # Markdown-aware chunk embeddings, computed BEFORE the write
                # transaction opens so model.encode never runs under the lock.
                #
                # Unendorsed pages get a document row and an FTS row but NO
                # embedding. _semantic_search asks FAISS for a fixed limit*5
                # window and retrieve() drops draft/expired only AFTER that
                # window is filled, so embedding a vault that is ~95%
                # unendorsed drafts lets them evict accepted pages from the
                # candidate set and silently shrink recall. They stay lexically
                # findable, and endorsing one rewrites the page — the next
                # sweep sees the new mtime and embeds it.
                unendorsed = (
                    meta.get("page_status", "candidate") in UNEMBEDDED_STATUSES
                )
                prepared = []
                if self.model and not unendorsed:
                    for chunk in self.chunker.chunk_document(content):
                        emb = self.model.encode(chunk.text)
                        prepared.append((chunk, emb.astype(np.float32).tobytes()))

                layer = meta.get("layer", "knowledge")
                stale_chunk_ids: List[int] = []
                with self.db.transaction() as c:
                    if row:
                        doc_id = row["doc_id"]
                        # path=? normalizes a row adopted under a non-canonical
                        # spelling, so the next pass matches it exactly.
                        c.execute(
                            """UPDATE documents
                               SET path=?, agent=?, sigil=?, last_modified=?, indexed_at=?,
                                   page_status=?, privacy_level=?, page_type=?, layer=?
                               WHERE doc_id=?""",
                            (path, meta["agent"], meta["sigil"], mtime, now,
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
                        stale_chunk_ids = [r["chunk_id"] for r in c.fetchall()]
                        c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                        c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
                    else:
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

                    for chunk, emb_bytes in prepared:
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

                # Cache invalidation is not a DB write; keep it off the lock.
                if stale_chunk_ids:
                    self._invalidate_rerank_chunks(stale_chunk_ids)

                stats["indexed"] += 1
                if verbose:
                    logger.info("  ✓ %s [%s/%s] (%d chunks)",
                                os.path.basename(path),
                                meta["agent"], meta["sigil"], len(prepared))

            except Exception as e:
                stats["errors"] += 1
                if verbose:
                    logger.error("  ✗ %s: %s", path, e)

        # Phase 2 gets its OWN short transaction, separate from the per-file
        # writes above, so the prune never extends a lock across indexing work.
        with self.db.transaction() as c:
            # Remove docs no longer on disk.
            #
            # Two exemptions, both load-bearing. The SELECT is over the WHOLE
            # documents table while `disk_files` only ever holds this vault's
            # walk, so a naive "not on disk -> delete" prunes rows this indexer
            # does not own:
            #
            #  * Outside vault_root: wiki_paths and every other indexed source
            #    live in the same table. Running index_all masked this (the wiki
            #    indexer re-added its rows right after), but running the vault
            #    indexer ALONE — which the daemon's vault-watch sweep now does,
            #    on an interval — deleted them for real.
            #  * vault/_durable/: those rows are deliberately virtual. The
            #    daemon indexes a promoted learning at a synthetic path and
            #    never writes the markdown (see minnid._durable_doc_path), so
            #    "no file on disk" is their normal state, not staleness.
            #    Measured on the live install: 89 durable learnings sat under
            #    that prefix with no file behind any of them.
            from minni.path_safety import path_within_root
            durable_root = vault_root / "_durable"
            c.execute("SELECT doc_id, path FROM documents")
            to_delete = []
            for row in c.fetchall():
                path = row["path"]
                # Resolve before the membership test (same as wiki_indexer):
                # disk_files is keyed on resolved paths, so a row stored under a
                # non-canonical spelling of a file that DOES exist -- a symlink,
                # an unnormalized absolute path -- would miss the raw comparison,
                # pass the ownership check, and be deleted. Compare both forms.
                try:
                    resolved = str(Path(path).resolve())
                except Exception:
                    resolved = path
                if path in disk_files or resolved in disk_files:
                    continue
                if not path_within_root(path, vault_root):
                    continue
                if path_within_root(path, durable_root):
                    continue
                to_delete.append((row["doc_id"],))
                stats["deleted"] += 1
                if verbose:
                    logger.info("  🗑 Removed: %s", path)

            if to_delete:
                doc_ids = [doc_id for (doc_id,) in to_delete]
                placeholders = ",".join("?" for _ in doc_ids)
                c.execute(
                    f"SELECT chunk_id FROM chunk_embeddings WHERE doc_id IN ({placeholders})",
                    doc_ids,
                )
                self._invalidate_rerank_chunks([r["chunk_id"] for r in c.fetchall()])
                c.executemany("DELETE FROM chunk_embeddings WHERE doc_id = ?", to_delete)
                c.executemany("DELETE FROM vault_fts WHERE doc_id = ?", to_delete)
                c.executemany("DELETE FROM documents WHERE doc_id = ?", to_delete)

        # Phase 3: Rebuild FAISS index from all embeddings -- but only if this
        # run actually touched any. On the daemon's 300s timer an idle vault
        # otherwise re-SELECTed every embedding in the shared DB and rebuilt an
        # index on an instance that is discarded when this call returns, purely
        # to discover nothing had changed. Cheap at today's chunk count, but it
        # scales with the whole table rather than with the work done.
        if stats["indexed"] or stats["deleted"] or stats["chunks"] or stats["chunks_purged"]:
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
            from minni.backends.faiss_disk import FaissDiskBackend
            from minni.backends.faiss_mem import FaissMemBackend
            from minni.vector_sync import sync_all
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

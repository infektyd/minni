"""
Sovereign Memory V3.1 — Wiki Indexer.

Ingests LLM Wiki pages into sovereign memory with agent-optimized enrichment:
1. Parses YAML frontmatter (type, tags, sources, title)
2. Extracts [[wikilinks]] as memory_links graph edges
3. Enriches chunk heading context with frontmatter metadata
4. Tags chunks with source type for provenance awareness
5. Deduplicates wikilinks that point to non-existent pages

This is designed for AGENT consumption — not human reading. Every enrichment
exists because an agent or reasoning engine will use it downstream.
"""

import os
import re
import time
import logging
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field

from config import SovereignConfig, DEFAULT_CONFIG
from db import SovereignDB
from chunker import MarkdownChunker, Chunk
from faiss_index import FAISSIndex

logger = logging.getLogger("sovereign.wiki_indexer")


@dataclass
class WikiFrontmatter:
    """Parsed YAML frontmatter from a wiki page."""
    title: str = ""
    created: str = ""
    updated: str = ""
    page_type: str = "unknown"  # entity, concept, decision, procedure, session, artifact, handoff, synthesis
    tags: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    status: str = "candidate"    # draft | candidate | accepted | superseded | rejected | expired
    privacy: str = "safe"        # safe | local-only | private | blocked
    superseded_by: str = ""      # wikilink or path of the superseding page
    expires: str = ""            # ISO date string
    raw: Dict[str, str] = field(default_factory=dict)

    # PR-2: Valid values for validation
    VALID_STATUSES = frozenset({"draft", "candidate", "accepted", "superseded", "rejected", "expired"})
    VALID_PRIVACIES = frozenset({"safe", "local-only", "private", "blocked"})
    VALID_TYPES = frozenset({"entity", "concept", "decision", "procedure", "session", "artifact", "handoff", "synthesis", "unknown"})


@dataclass
class WikiPage:
    """A parsed wiki page with metadata and content."""
    path: str
    frontmatter: WikiFrontmatter
    body: str
    wikilinks: List[str] = field(default_factory=list)  # [[link-target]] references


class WikiPageParser:
    """
    Parse wiki pages for agent-optimized ingestion.

    Key difference from plain markdown parsing:
    - Frontmatter is structured metadata, not noise
    - [[wikilinks]] are first-class graph edges
    - Page type affects how the chunk heading context is formatted
    - Tags become searchable metadata in the heading breadcrumb
    """

    # Regex for YAML frontmatter block
    FRONTMATTER_RE = re.compile(
        r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL
    )

    # Regex for [[wikilinks]] — captures the link target
    WIKILINK_RE = re.compile(r'\[\[([^\]|]+?)(?:\|[^\]]*?)?\]\]')

    def parse(self, filepath: str) -> Optional[WikiPage]:
        """
        Parse a wiki page file.

        Returns WikiPage with frontmatter, body, and wikilinks extracted.
        Returns None if file can't be read or has no content.
        """
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
        except (IOError, OSError) as e:
            logger.warning("Cannot read %s: %s", filepath, e)
            return None

        if not content.strip():
            return None

        # Extract frontmatter
        fm_match = self.FRONTMATTER_RE.match(content)
        frontmatter = WikiFrontmatter()
        body = content

        if fm_match:
            frontmatter = self._parse_yaml_frontmatter(fm_match.group(1))
            body = content[fm_match.end():]

        # Extract wikilinks from body
        wikilinks = self.WIKILINK_RE.findall(body)

        return WikiPage(
            path=filepath,
            frontmatter=frontmatter,
            body=body,
            wikilinks=wikilinks,
        )

    def _parse_yaml_frontmatter(self, yaml_str: str) -> WikiFrontmatter:
        """
        Simple YAML frontmatter parser.

        Doesn't require PyYAML — handles the structured frontmatter format
        used by the LLM Wiki skill (title, type, tags, sources, status, privacy, etc.)

        PR-2: Also parses status, privacy, superseded_by, expires fields.
        """
        fm = WikiFrontmatter()
        fm.raw = {}

        for line in yaml_str.strip().split('\n'):
            line = line.strip()
            if ':' not in line:
                continue

            key, _, value = line.partition(':')
            key = key.strip().lower()
            value = value.strip().strip('"\'')

            fm.raw[key] = value

            if key == 'title':
                fm.title = value
            elif key == 'created':
                fm.created = value
            elif key == 'updated':
                fm.updated = value
            elif key == 'type':
                fm.page_type = value
            elif key == 'tags':
                # Parse [tag1, tag2, tag3] or comma-separated
                fm.tags = self._parse_list_field(value)
            elif key == 'sources':
                fm.sources = self._parse_list_field(value)
            elif key == 'status':
                fm.status = value.lower() if value else "candidate"
            elif key == 'privacy':
                fm.privacy = value.lower() if value else "safe"
            elif key == 'superseded_by':
                fm.superseded_by = value
            elif key == 'expires':
                fm.expires = value

        return fm

    def validate_frontmatter(self, fm: WikiFrontmatter, path: str) -> List[str]:
        """
        Validate frontmatter fields.

        Returns a list of error strings. Empty list = valid.
        """
        errors = []
        if fm.status not in WikiFrontmatter.VALID_STATUSES:
            errors.append(
                f"invalid status={fm.status!r} (must be one of {sorted(WikiFrontmatter.VALID_STATUSES)})"
            )
        if fm.privacy not in WikiFrontmatter.VALID_PRIVACIES:
            errors.append(
                f"invalid privacy={fm.privacy!r} (must be one of {sorted(WikiFrontmatter.VALID_PRIVACIES)})"
            )
        return errors

    @staticmethod
    def _parse_list_field(value: str) -> List[str]:
        """Parse [item1, item2] or comma-separated list."""
        value = value.strip()
        if value.startswith('[') and value.endswith(']'):
            value = value[1:-1]
        items = [item.strip().strip("\"'") for item in value.split(',')]
        return [item for item in items if item]

    def get_wikilink_targets(self, wiki_root: str) -> Dict[str, str]:
        """
        Build a map of wikilink targets → actual file paths.

        [[cognitive-architecture-tri-brain]] → ~/wiki/concepts/cognitive-architecture-tri-brain.md
        [[syntra]] → ~/wiki/entities/syntra.md

        This is how we resolve [[wikilinks]] into memory_links edges.
        """
        target_map = {}

        for root, _, files in os.walk(wiki_root):
            for fname in files:
                if not fname.endswith('.md'):
                    continue
                full_path = os.path.join(root, fname)
                # The wikilink target is the filename without extension
                target_name = fname[:-3]  # strip .md
                target_map[target_name] = full_path

        return target_map


class WikiIndexer:
    """
    Index LLM Wiki pages into sovereign memory with agent-optimized enrichment.

    Differences from VaultIndexer:
    - Frontmatter metadata → chunk heading context enriched with type + tags
    - [[wikilinks]] → memory_links table entries (explicit graph edges)
    - Source tagging → documents.agent = 'wiki' for provenance
    - Page type awareness → agents know if they're reading a concept vs entity vs decision
    """

    def __init__(
        self,
        db: SovereignDB,
        config: SovereignConfig = DEFAULT_CONFIG,
    ):
        self.db = db
        self.config = config
        self.parser = WikiPageParser()
        self.chunker = MarkdownChunker(config)
        self.faiss_index = FAISSIndex(config)
        self._model = None

    @property
    def model(self):
        """Return the process-wide embedding model singleton."""
        from models import get_embedder
        return get_embedder()

    def index_wiki(self, wiki_path: str, verbose: bool = False) -> Dict:
        """
        Full incremental index of a wiki directory.

        Returns stats dict with indexed/skipped/deleted/chunks/wikilinks counts.
        """
        if not os.path.isdir(wiki_path):
            return {"status": "error", "message": f"Wiki path not found: {wiki_path}"}

        # Build wikilink target map for resolution
        target_map = self.parser.get_wikilink_targets(wiki_path)

        # Collect current files on disk
        disk_files: Dict[str, float] = {}
        for root, _, files in os.walk(wiki_path):
            for fname in files:
                if fname.endswith(".md") and not fname.startswith('.'):
                    full = os.path.join(root, fname)
                    disk_files[full] = os.path.getmtime(full)

        stats = {
            "indexed": 0, "skipped": 0, "deleted": 0,
            "chunks": 0, "wikilinks": 0, "errors": 0, "rejected": 0
        }

        # Resolve log.md path for rejected page logging
        log_path = os.path.join(wiki_path, "log.md")

        with self.db.transaction() as c:
            # Phase 1: Index new/changed wiki pages (content + chunks, no wikilinks)
            pages_to_link = []  # (doc_id, wikilinks) for second pass

            for path, mtime in disk_files.items():
                try:
                    # Skip SCHEMA.md, index.md, log.md from rich indexing
                    # (they're still indexed but don't get wiki metadata treatment)
                    fname = os.path.basename(path)
                    is_meta = fname in ('SCHEMA.md', 'index.md', 'log.md')

                    c.execute(
                        "SELECT doc_id, last_modified FROM documents WHERE path = ?",
                        (path,),
                    )
                    row = c.fetchone()

                    if row and row["last_modified"] >= mtime:
                        stats["skipped"] += 1
                        continue

                    # Parse the wiki page
                    page = self.parser.parse(path)
                    if not page:
                        stats["errors"] += 1
                        continue

                    # PR-2: Validate frontmatter (reject invalid pages)
                    if not is_meta:
                        errors = self.parser.validate_frontmatter(page.frontmatter, path)
                        if errors:
                            err_msg = "; ".join(errors)
                            logger.warning(
                                "Rejecting wiki page %s: %s", os.path.basename(path), err_msg
                            )
                            # Log to wiki log.md (best-effort)
                            try:
                                with open(log_path, "a", encoding="utf-8") as lf:
                                    lf.write(
                                        f"\n## [{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] "
                                        f"REJECTED {os.path.basename(path)}\n"
                                        f"Reason: {err_msg}\n"
                                    )
                            except Exception:
                                pass
                            stats["rejected"] += 1
                            stats["errors"] += 1
                            continue

                    # PR-2: Skip pages with privacy: blocked
                    if not is_meta and page.frontmatter.privacy == "blocked":
                        logger.info(
                            "Skipping blocked page: %s", os.path.basename(path)
                        )
                        stats["skipped"] += 1
                        continue

                    now = time.time()

                    # Agent tag: 'wiki' for provenance, plus page type
                    agent_tag = f"wiki:{page.frontmatter.page_type}" if not is_meta else "wiki:meta"

                    # PR-2: page_status and privacy_level from frontmatter
                    page_status = page.frontmatter.status if not is_meta else "accepted"
                    privacy_level = page.frontmatter.privacy if not is_meta else "safe"
                    page_type = page.frontmatter.page_type if not is_meta else None

                    if row:
                        doc_id = row["doc_id"]
                        c.execute(
                            """UPDATE documents
                               SET agent=?, sigil=?, last_modified=?, indexed_at=?,
                                   page_status=?, privacy_level=?, page_type=?
                               WHERE doc_id=?""",
                            (agent_tag, "📖", mtime, now,
                             page_status, privacy_level, page_type, doc_id),
                        )
                        # Clean old FTS + embeddings for re-index
                        c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                        c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
                    else:
                        c.execute(
                            """INSERT INTO documents (path, agent, sigil, last_modified, indexed_at,
                                   page_status, privacy_level, page_type)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (path, agent_tag, "📖", mtime, now,
                             page_status, privacy_level, page_type),
                        )
                        doc_id = c.lastrowid

                    # FTS5 insert (full content for keyword search)
                    full_content = f"{page.frontmatter.title}\n{page.body}"
                    c.execute(
                        """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
                           VALUES (?, ?, ?, ?, ?)""",
                        (doc_id, path, full_content, agent_tag, "📖"),
                    )

                    # Enriched chunk heading context
                    # For agents: include type, tags, and title in the heading breadcrumb
                    heading_prefix = self._build_heading_prefix(page)

                    # Chunk the page body
                    chunks = self.chunker.chunk_document(page.body)

                    # Enrich each chunk with wiki metadata
                    for chunk in chunks:
                        enriched_heading = f"{heading_prefix}{chunk.heading_path}"

                        # Encode + store
                        if self.model:
                            emb = self.model.encode(chunk.text)
                            emb_bytes = emb.astype("float32").tobytes()

                            c.execute(
                                """INSERT INTO chunk_embeddings
                                   (doc_id, chunk_index, chunk_text, embedding,
                                    heading_context, model_name, computed_at)
                                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    doc_id, chunk.chunk_index, chunk.text,
                                    emb_bytes, enriched_heading,
                                    self.config.embedding_model, now,
                                ),
                            )
                            stats["chunks"] += 1

                    # Queue wikilinks for phase 2 resolution
                    if page.wikilinks:
                        pages_to_link.append((doc_id, page.wikilinks, path))

                    stats["indexed"] += 1
                    if verbose:
                        logger.info(
                            "  ✓ %s [%s] (%d chunks, %d wikilinks queued)",
                            fname, page.frontmatter.page_type,
                            len(chunks), len(page.wikilinks),
                        )

                except Exception as e:
                    stats["errors"] += 1
                    if verbose:
                        logger.error("  ✗ %s: %s", path, e)

            # Phase 2: Resolve [[wikilinks]] NOW (all pages are in the DB)
            target_map = self.parser.get_wikilink_targets(wiki_path)
            for doc_id, wikilinks, source_path in pages_to_link:
                # Clean old links for this source
                c.execute(
                    "DELETE FROM memory_links WHERE source_doc_id = ?",
                    (doc_id,),
                )
                link_count = self._index_wikilinks(
                    c, doc_id, wikilinks, target_map, time.time()
                )
                stats["wikilinks"] += link_count
                if verbose and link_count > 0:
                    logger.info(
                        "  🔗 %s → %d wikilinks resolved",
                        os.path.basename(source_path), link_count
                    )

            # Phase 3: Remove wiki pages no longer on disk
            c.execute("SELECT doc_id, path, agent FROM documents WHERE agent LIKE 'wiki:%'")
            for row in c.fetchall():
                if row["path"] not in disk_files:
                    c.execute("DELETE FROM documents WHERE doc_id = ?", (row["doc_id"],))
                    c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (row["doc_id"],))
                    c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (row["doc_id"],))
                    c.execute(
                        "DELETE FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
                        (row["doc_id"], row["doc_id"]),
                    )
                    stats["deleted"] += 1
                    if verbose:
                        logger.info("  🗑 Removed: %s", row["path"])

        # Phase 4: Rebuild FAISS index from all embeddings
        self._rebuild_faiss_index()

        return {"status": "success", **stats}

    def _build_heading_prefix(self, page: WikiPage) -> str:
        """
        Build agent-optimized heading prefix from frontmatter.

        For agent consumption: the heading breadcrumb now includes metadata
        that helps the agent understand what kind of knowledge it's looking at.

        Example output:
        "[wiki:concept | tags: syntra, cognitive-architecture] "
        """
        parts = []

        # Page type — critical for agent reasoning
        # concept → theoretical knowledge
        # entity → specific thing (person, project, org)
        # decision → recorded choice with rationale
        # query → answered question
        # comparison → side-by-side analysis
        parts.append(f"wiki:{page.frontmatter.page_type}")

        # Tags — for semantic context
        if page.frontmatter.tags:
            tags_str = ", ".join(page.frontmatter.tags[:5])  # Cap at 5 tags
            parts.append(f"tags: {tags_str}")

        # Title — the page name
        if page.frontmatter.title:
            parts.append(page.frontmatter.title)

        return f"[{' | '.join(parts)}] "

    def _index_wikilinks(
        self,
        cursor,
        source_doc_id: int,
        wikilinks: List[str],
        target_map: Dict[str, str],
        now: float,
    ) -> int:
        """
        Convert [[wikilinks]] to memory_links table entries.

        This is the graph-building step. When an agent recalls a chunk from
        a wiki page, the memory_links table lets sovereign memory traverse
        to related pages — the knowledge graph is materialized in SQLite.

        Returns the number of links created.
        """
        link_count = 0
        seen: Set[Tuple[int, int]] = set()

        for link_target in wikilinks:
            target_path = target_map.get(link_target)
            if not target_path:
                continue  # Broken wikilink — skip silently

            # Look up target doc_id
            cursor.execute(
                "SELECT doc_id FROM documents WHERE path = ?",
                (target_path,),
            )
            target_row = cursor.fetchone()
            if not target_row:
                continue  # Target not yet indexed — will resolve on next pass

            target_doc_id = target_row["doc_id"]

            # Verify target exists in DB (paranoid check for FK constraint)
            cursor.execute(
                "SELECT 1 FROM documents WHERE doc_id = ?",
                (target_doc_id,),
            )
            if not cursor.fetchone():
                continue

            # Avoid self-links
            if source_doc_id == target_doc_id:
                continue

            # Deduplicate within this indexing pass
            edge_key = (source_doc_id, target_doc_id)
            if edge_key in seen:
                continue
            seen.add(edge_key)

            try:
                cursor.execute(
                    """INSERT OR REPLACE INTO memory_links
                       (source_doc_id, target_doc_id, link_type, weight, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (source_doc_id, target_doc_id, "wikilink", 1.0, now),
                )
                link_count += 1
            except Exception:
                continue  # Skip links that fail (FK constraint, etc.)

        return link_count

    def _rebuild_faiss_index(self) -> None:
        """Rebuild the FAISS index from all chunk embeddings in the DB."""
        chunk_ids = []
        embeddings = []

        with self.db.cursor() as c:
            c.execute("SELECT chunk_id, embedding FROM chunk_embeddings")
            for row in c.fetchall():
                import numpy as np
                vec = np.frombuffer(row["embedding"], dtype=np.float32)
                if vec.shape[0] == self.config.embedding_dim:
                    chunk_ids.append(row["chunk_id"])
                    embeddings.append(vec)

        if chunk_ids:
            import numpy as np
            all_vecs = np.array(embeddings, dtype=np.float32)
            self.faiss_index.build_from_vectors(chunk_ids, all_vecs)
            logger.info("FAISS index rebuilt: %d vectors", len(chunk_ids))
        self._sync_vector_backends()

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

    # ── Wiki File Watcher ────────────────────────────────────

    def start_watcher(self, wiki_path: str):
        """Start filesystem watcher for wiki changes."""
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        DEBOUNCE_SEC = 5

        class _Handler(FileSystemEventHandler):
            def __init__(self, indexer, wp):
                self._indexer = indexer
                self._wiki_path = wp
                self._last = 0

            def on_any_event(self, event):
                if event.is_directory or not event.src_path.endswith(".md"):
                    return
                now = time.time()
                if now - self._last > DEBOUNCE_SEC:
                    self._last = now
                    logger.info("Wiki change detected: %s", event.src_path)
                    self._indexer.index_wiki(self._wiki_path)

        observer = Observer()
        observer.schedule(_Handler(self, wiki_path), wiki_path, recursive=True)
        observer.start()
        logger.info("Watching wiki %s for changes...", wiki_path)
        return observer


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    config = SovereignConfig()
    db = SovereignDB(config)
    indexer = WikiIndexer(db, config)

    wiki_path = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/wiki")
    stats = indexer.index_wiki(wiki_path, verbose=True)
    print(f"Wiki index stats: {stats}")

    db.close()

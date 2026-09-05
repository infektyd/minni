"""Index an agent vault's wiki into that vault's local index store.

This AFM maintenance pass is deterministic indexing work. It reads
``<vault>/wiki/**/*.md`` and writes only ``<vault>/.index``.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from minni.afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID
from minni.indexer import VaultIndexer
from minni.principal import validate_agent_id
from minni.timestamps import parse_epoch_or_report
from minni.vault_index import open_vault_index, vault_index_paths
from minni.wiki_indexer import WikiFrontmatter, WikiIndexer

logger = logging.getLogger("sovereign.afm.vault_ingest")


def _vault_slug(vault: Path) -> Optional[str]:
    name = vault.name
    if not name.endswith("-vault"):
        return None
    return name[: -len("-vault")]


def _agent_id_for_vault(vault: Path) -> Optional[str]:
    slug = _vault_slug(vault)
    if not slug:
        return None
    agent_id = _VAULT_SLUG_TO_AGENT_ID.get(slug)
    if not agent_id:
        return None
    return validate_agent_id(agent_id)


def _safe_wiki_root(vault: Path) -> Path:
    wiki = (vault / "wiki").resolve()
    vault_root = vault.resolve()
    if not wiki.is_relative_to(vault_root):
        raise ValueError("wiki path escapes vault root")
    return wiki


def _collect_markdown(wiki_root: Path) -> Dict[str, float]:
    if not wiki_root.is_dir():
        return {}

    disk_files: Dict[str, float] = {}
    for root, dirs, files in os.walk(wiki_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if not fname.endswith(".md") or fname.startswith("."):
                continue
            full = Path(root) / fname
            from minni.path_safety import path_within_root
            if not path_within_root(full, wiki_root):
                continue
            try:
                disk_files[str(full.resolve())] = full.stat().st_mtime
            except OSError:
                continue
    return disk_files


def _read_index_state(db_path: Path) -> Dict[str, sqlite3.Row]:
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT doc_id, path, last_modified, indexed_at FROM documents"
            ).fetchall()
            return {str(row["path"]): row for row in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def _row_doc_id(row) -> Optional[int]:
    """doc_id for reporting only — callers pass partial rows (and tests pass
    hand-built ones) that may not carry the column."""
    try:
        return row["doc_id"]
    except (IndexError, KeyError, TypeError):
        return None


def _count_plan(disk_files: Dict[str, float], existing: Dict[str, sqlite3.Row]) -> tuple[int, int, int]:
    would_index = 0
    skipped = 0
    for path, mtime in disk_files.items():
        row = existing.get(path)
        if row is not None:
            # Audit R0: parse-or-report — a TEXT indexed_at must cost this one
            # doc its skip check, not raise ValueError and abort the whole plan.
            indexed_at = parse_epoch_or_report(
                row["indexed_at"], field="indexed_at",
                source="vault_ingest._count_plan", doc_id=_row_doc_id(row),
            ) or 0.0
            if mtime <= indexed_at:
                skipped += 1
                continue
        would_index += 1
    would_prune = sum(1 for path in existing if path not in disk_files)
    return would_index, skipped, would_prune


def _fetch_existing_doc(cursor, path: str):
    cursor.execute(
        "SELECT doc_id, last_modified, indexed_at FROM documents WHERE path = ?",
        (path,),
    )
    return cursor.fetchone()


def _delete_doc_payload(cursor, doc_id: int) -> None:
    cursor.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
    cursor.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,))
    cursor.execute(
        "DELETE FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
        (doc_id, doc_id),
    )


def _insert_wikilinks(cursor, source_doc_id: int, wikilinks: List[str], target_map: Dict[str, str], now: float) -> int:
    target_paths = {target_map[target] for target in wikilinks if target in target_map}

    keep_ids: set = set()
    if target_paths:
        placeholders = ",".join("?" for _ in target_paths)
        cursor.execute(
            f"SELECT doc_id FROM documents WHERE path IN ({placeholders})",
            list(target_paths),
        )
        keep_ids = {
            int(row["doc_id"]) for row in cursor.fetchall() if int(row["doc_id"]) != source_doc_id
        }

    # PR94-1: diff-based prune scoped to wikilinks — delete only the edges whose
    # target is gone (existing - keep), in chunks, so surviving links keep
    # created_at via the upsert below; other edge types (derived_from) survive a
    # re-index. Chunked stale-delete (not an unbounded NOT IN) stays under the
    # SQLite variable limit and prunes correctly even when keep_ids is empty.
    cursor.execute(
        "SELECT target_doc_id FROM memory_links "
        "WHERE source_doc_id = ? AND link_type = 'wikilink'",
        (source_doc_id,),
    )
    existing_ids = {int(row["target_doc_id"]) for row in cursor.fetchall()}
    stale_ids = list(existing_ids - keep_ids)
    for i in range(0, len(stale_ids), 500):
        batch = stale_ids[i:i + 500]
        ph = ",".join(["?"] * len(batch))
        cursor.execute(
            "DELETE FROM memory_links WHERE source_doc_id = ? "
            f"AND link_type = 'wikilink' AND target_doc_id IN ({ph})",
            (source_doc_id, *batch),
        )

    inserted = 0
    for target_doc_id in keep_ids:
        cursor.execute(
            """INSERT INTO memory_links
               (source_doc_id, target_doc_id, link_type, weight, created_at,
                confidence, inference_method)
               VALUES (?, ?, 'wikilink', 1.0, ?, 1.0, 'explicit_wikilink')
               ON CONFLICT(source_doc_id, target_doc_id, link_type)
               DO UPDATE SET weight=excluded.weight,
                             confidence=excluded.confidence,
                             inference_method=excluded.inference_method""",
            (source_doc_id, target_doc_id, now),
        )
        inserted += 1
    return inserted


def _index_changed_pages(
    *,
    indexer: WikiIndexer,
    disk_files: Dict[str, float],
    agent_id: str,
    wiki_root: Path,
) -> Dict[str, int]:
    stats = {
        "indexed": 0,
        "skipped_unchanged": 0,
        "chunks": 0,
        "wikilinks": 0,
        "errors": 0,
        "rejected": 0,
        "excluded": 0,
    }
    target_map = indexer.parser.get_wikilink_targets(str(wiki_root))

    with indexer.db.transaction() as c:
        pages_to_link: List[Tuple[int, List[str]]] = []
        for path, mtime in sorted(disk_files.items()):
            try:
                row = _fetch_existing_doc(c, path)
                indexed_at = (
                    parse_epoch_or_report(
                        row["indexed_at"], field="indexed_at",
                        source="vault_ingest.run", doc_id=_row_doc_id(row),
                    ) or 0.0
                ) if row else 0.0
                if row and mtime <= indexed_at:
                    stats["skipped_unchanged"] += 1
                    continue

                page = indexer.parser.parse(path)
                if not page:
                    stats["errors"] += 1
                    continue

                # Excluded by design (e.g. a completed plan). Checked before
                # validation so an intentional exclusion is never reported as
                # bad frontmatter. A page that was ALREADY indexed at an
                # indexable status (accepted -> complete is the normal plan
                # lifecycle) must be RETRACTED, not merely skipped: leaving
                # the old row behind keeps a completed plan recallable, which
                # is the exact H6 self-promotion the exclusion exists to stop.
                if page.frontmatter.status in WikiFrontmatter.EXCLUDED_STATUSES:
                    stats["excluded"] += 1
                    if row:
                        # Retract the PAYLOAD, not the row. Deleting the
                        # documents row would CASCADE memory_links away and
                        # mint a new doc_id on the way back — and
                        # accepted -> complete -> accepted is a normal round
                        # trip (plan.ts reconciles when a slice reopens), so
                        # every inbound wikilink and every stored doc_id
                        # reference would dangle permanently. Keep identity,
                        # drop the searchable content, and stamp the status
                        # so retrieval filters it exactly like superseded.
                        doc_id = int(row["doc_id"])
                        # Searchable payload only — NOT _delete_doc_payload,
                        # which also drops memory_links in both directions.
                        # The page still exists, so an inbound edge belongs to
                        # the OTHER page and deleting it would corrupt that
                        # page's graph without that page having changed.
                        c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (doc_id,))
                        c.execute(
                            "DELETE FROM chunk_embeddings WHERE doc_id = ?", (doc_id,)
                        )
                        c.execute(
                            """UPDATE documents
                               SET page_status=?, last_modified=?, indexed_at=?
                               WHERE doc_id=?""",
                            (page.frontmatter.status, mtime, time.time(), doc_id),
                        )
                        stats.setdefault("excluded_purged", 0)
                        stats["excluded_purged"] += 1
                    continue

                errors = indexer.parser.validate_frontmatter(page.frontmatter, path)
                if errors:
                    # A rejection is "we refused to index this", which is a
                    # different question from "we broke". Counting it as both
                    # left `errors` permanently non-zero, so it carried no
                    # information about real errors.
                    stats["rejected"] += 1
                    continue
                if page.frontmatter.privacy == "blocked":
                    if row:
                        doc_id = int(row["doc_id"])
                        _delete_doc_payload(c, doc_id)
                        c.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
                        stats.setdefault("pruned", 0)
                        stats["pruned"] += 1
                    else:
                        stats["skipped_unchanged"] += 1
                    continue

                now = time.time()
                page_status = page.frontmatter.status
                page_type = page.frontmatter.page_type
                # GA6-1: this pass used to hardcode layer='knowledge' on all
                # three writes below, while VaultIndexer._extract_metadata
                # infers it — so every artifact-typed page ingested here was
                # stamped 'knowledge' and became invisible to layer-scoped
                # recall (88 such documents across 5 vault DBs). Infer it the
                # same way the indexer does, so the two writers agree.
                #
                # agent_id is derived from the vault directory slug and passed
                # through validate_agent_id, NOT read from page frontmatter, so
                # the untrusted-'identity:'-prefix path _extract_metadata has to
                # defend against cannot arise here.
                layer = VaultIndexer._infer_layer(agent=agent_id, page_type=page_type)

                if row:
                    doc_id = int(row["doc_id"])
                    _delete_doc_payload(c, doc_id)
                    c.execute(
                        """UPDATE documents
                           SET agent=?, sigil=?, last_modified=?, indexed_at=?,
                               page_status=?, privacy_level=?, page_type=?, layer=?
                           WHERE doc_id=?""",
                        (
                            agent_id,
                            "vault",
                            mtime,
                            now,
                            page_status,
                            page.frontmatter.privacy,
                            page_type,
                            layer,
                            doc_id,
                        ),
                    )
                else:
                    c.execute(
                        """INSERT INTO documents
                           (path, agent, sigil, last_modified, indexed_at,
                            page_status, privacy_level, page_type, layer)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            path,
                            agent_id,
                            "vault",
                            mtime,
                            now,
                            page_status,
                            page.frontmatter.privacy,
                            page_type,
                            layer,
                        ),
                    )
                    doc_id = int(c.lastrowid)

                full_content = f"{page.frontmatter.title}\n{page.body}"
                c.execute(
                    """INSERT INTO vault_fts (doc_id, path, content, agent, sigil)
                       VALUES (?, ?, ?, ?, ?)""",
                    (doc_id, path, full_content, agent_id, "vault"),
                )

                heading_prefix = indexer._build_heading_prefix(page)
                chunks = indexer.chunker.chunk_document(page.body)
                if indexer.model:
                    from minni.models import get_embedder_lock

                    for chunk in chunks:
                        with get_embedder_lock():
                            emb = indexer.model.encode(chunk.text, show_progress_bar=False)
                        c.execute(
                            """INSERT INTO chunk_embeddings
                               (doc_id, chunk_index, chunk_text, embedding,
                                heading_context, model_name, computed_at, layer)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                doc_id,
                                chunk.chunk_index,
                                chunk.text,
                                emb.astype(np.float32).tobytes(),
                                f"{heading_prefix}{chunk.heading_path}",
                                indexer.config.embedding_model,
                                now,
                                layer,
                            ),
                        )
                        stats["chunks"] += 1

                if page.wikilinks:
                    pages_to_link.append((doc_id, page.wikilinks))
                stats["indexed"] += 1
            except Exception as exc:
                stats["errors"] += 1
                logger.warning("vault_ingest: failed to index %s: %s", path, exc)

        for source_doc_id, wikilinks in pages_to_link:
            # PR94-1: _insert_wikilinks now diff-prunes stale wikilinks and
            # upserts the rest, preserving created_at — no blunt delete here.
            stats["wikilinks"] += _insert_wikilinks(
                c, source_doc_id, wikilinks, target_map, time.time()
            )

        c.execute("SELECT doc_id, path FROM documents")
        for row in c.fetchall():
            if row["path"] not in disk_files:
                _delete_doc_payload(c, int(row["doc_id"]))
                c.execute("DELETE FROM documents WHERE doc_id = ?", (row["doc_id"],))
                stats.setdefault("pruned", 0)
                stats["pruned"] += 1

    stats.setdefault("pruned", 0)
    return stats


def _save_faiss(indexer: WikiIndexer) -> bool:
    indexer._rebuild_faiss_index()
    if indexer.faiss_index.count == 0:
        return False
    try:
        conn = indexer.db._get_conn()
        return bool(indexer.faiss_index.save_to_disk(db_conn=conn))
    except Exception as exc:
        logger.warning("vault_ingest: FAISS save failed: %s", exc)
        return False


def run(db, config, vault_path=None, dry_run=False, trace_id=None) -> Dict[str, Any]:
    vault = Path(vault_path or getattr(config, "vault_path", "")).expanduser()
    paths = vault_index_paths(vault)

    try:
        agent_id = _agent_id_for_vault(vault)
    except ValueError as exc:
        logger.warning("vault_ingest: invalid agent id for %s: %s", vault, exc)
        agent_id = None
    if agent_id is None:
        logger.warning("vault_ingest: unknown vault slug for %s; skipping", vault)
        return {
            "status": "skipped",
            "reason": "unknown_vault_slug",
            "dry_run": dry_run,
            "trace_id": trace_id,
            "drafts": [],
            "files_seen": 0,
            "indexed": 0,
            "skipped_unchanged": 0,
            "pruned": 0,
            "agent_id": None,
            "index_db_path": str(paths.db_path),
        }

    wiki_root = _safe_wiki_root(vault)
    disk_files = _collect_markdown(wiki_root)
    existing = _read_index_state(paths.db_path)
    would_index, skipped, would_prune = _count_plan(disk_files, existing)

    base = {
        "status": "ok",
        "pass_name": "vault_ingest",
        "dry_run": dry_run,
        "trace_id": trace_id,
        "drafts": [],
        "files_seen": len(disk_files),
        "agent_id": agent_id,
        "index_db_path": str(paths.db_path),
    }

    if dry_run:
        return {
            **base,
            "indexed": 0,
            "skipped_unchanged": skipped,
            "pruned": 0,
            "would_index": would_index,
            "would_prune": would_prune,
            "chunks": 0,
            "wikilinks": 0,
            "errors": 0,
            "rejected": 0,
            "excluded": 0,
            "excluded_purged": 0,
            "faiss_saved": False,
        }

    index_db, _ = open_vault_index(vault, base_config=config)
    try:
        indexer = WikiIndexer(index_db, index_db.config)
        stats = _index_changed_pages(
            indexer=indexer,
            disk_files=disk_files,
            agent_id=agent_id,
            wiki_root=wiki_root,
        )
        faiss_saved = _save_faiss(indexer)

        return {
            **base,
            "indexed": stats["indexed"],
            "skipped_unchanged": stats["skipped_unchanged"],
            "pruned": stats["pruned"],
            "would_index": would_index,
            "would_prune": would_prune,
            "chunks": stats["chunks"],
            "wikilinks": stats["wikilinks"],
            "errors": stats["errors"],
            "rejected": stats["rejected"],
            "excluded": stats["excluded"],
            "excluded_purged": stats.get("excluded_purged", 0),
            "faiss_saved": faiss_saved,
        }
    finally:
        index_db.close()

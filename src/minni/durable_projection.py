"""Shared identity and metadata for committed learning document projections."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTIVE_LEARNING_SQL = """superseded_by IS NULL AND
    (status IS NULL OR status NOT IN ('rejected', 'expired', 'superseded'))"""

# A STORED documents row with one of these page_status values (or blocked
# privacy) is lifecycle-closed/restricted. Repair and purge must treat it as
# authoritative: never resurrect it from content defaults, never fill vectors
# for it. Mirrors the content-derived eligibility set used by the learning
# projection repair.
CLOSED_PROJECTION_STATUSES = frozenset({"draft", "expired", "rejected", "superseded"})


def projection_row_closed(page_status, privacy_level) -> bool:
    """True when a stored projection row is closed or restricted.

    Only the row's own stored values count — never content-derived defaults.
    """
    return privacy_level == "blocked" or (page_status or "") in CLOSED_PROJECTION_STATUSES


def durable_doc_path(agent_id, key, vault_path, content=None):
    seed = content if content is not None else key
    digest = hashlib.sha1(f"{agent_id}\x00{seed}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(vault_path, "_durable", f"{agent_id}__{digest}.md")


def durable_metadata(content):
    from minni.indexer import VaultIndexer

    meta = VaultIndexer._extract_frontmatter(content)
    return {
        "sigil": meta.get("sigil", "❓"),
        "page_status": "accepted" if meta["page_status"] == "candidate" else meta["page_status"],
        "privacy_level": meta["privacy_level"],
        # Never accept model-supplied ownership or a cross-agent-readable type.
        "page_type": "learning",
        "layer": meta["layer"],
    }


def _normalize_for_shingles(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def five_gram_jaccard(text_a: str, text_b: str) -> float:
    """Compute 5-gram Jaccard similarity (max of word 5-gram and char 5-gram Jaccard)."""
    norm_a = _normalize_for_shingles(text_a)
    norm_b = _normalize_for_shingles(text_b)
    if not norm_a or not norm_b:
        return 0.0
    if norm_a == norm_b:
        return 1.0

    scores: list[float] = []
    words_a = norm_a.split()
    words_b = norm_b.split()
    if len(words_a) >= 5 and len(words_b) >= 5:
        w_grams_a = {tuple(words_a[i : i + 5]) for i in range(len(words_a) - 4)}
        w_grams_b = {tuple(words_b[i : i + 5]) for i in range(len(words_b) - 4)}
        union_w = w_grams_a | w_grams_b
        if union_w:
            scores.append(len(w_grams_a & w_grams_b) / len(union_w))

    if len(norm_a) >= 5 and len(norm_b) >= 5:
        c_grams_a = {norm_a[i : i + 5] for i in range(len(norm_a) - 4)}
        c_grams_b = {norm_b[i : i + 5] for i in range(len(norm_b) - 4)}
        union_c = c_grams_a | c_grams_b
        if union_c:
            scores.append(len(c_grams_a & c_grams_b) / len(union_c))

    return max(scores) if scores else 0.0


def _cosine_similarity_blobs(blob_a: bytes | None, blob_b: bytes | None) -> float:
    if not blob_a or not blob_b or len(blob_a) != len(blob_b) or len(blob_a) % 4 != 0:
        return 0.0
    import numpy as np

    vec_a = np.frombuffer(blob_a, dtype=np.float32)
    vec_b = np.frombuffer(blob_b, dtype=np.float32)
    norm_a = float(np.linalg.norm(vec_a))
    norm_b = float(np.linalg.norm(vec_b))
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))


def _table_exists(c: Any, table_name: str) -> bool:
    try:
        row = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _table_columns(c: Any, table_name: str) -> set[str]:
    try:
        rows = c.execute(f"PRAGMA table_info({table_name})").fetchall()
        cols: set[str] = set()
        for r in rows:
            try:
                cols.add(str(r["name"]))
            except Exception:
                cols.add(str(r[1]))
        return cols
    except Exception:
        return set()


def _resolve_archive_dir(
    c: Any,
    engine: Any = None,
    *,
    archive_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    vault_path: Path | str | None = None,
    conn_or_cursor: Any = None,
) -> Path:
    if archive_dir is not None:
        return Path(archive_dir)

    candidates = [
        db_path,
        getattr(engine, "db_path", None),
        getattr(getattr(engine, "config", None), "db_path", None),
        getattr(conn_or_cursor, "db_path", None),
        getattr(getattr(conn_or_cursor, "config", None), "db_path", None),
    ]
    for cand in candidates:
        if cand and str(cand) != ":memory:":
            return Path(str(cand)).resolve().parent / "_archive"

    try:
        db_list = c.execute("PRAGMA database_list").fetchall()
        for row in db_list:
            file_val = row["file"] if isinstance(row, dict) or hasattr(row, "keys") else row[2]
            if file_val and str(file_val) != ":memory:":
                return Path(str(file_val)).resolve().parent / "_archive"
    except Exception:
        pass

    v_candidates = [
        vault_path,
        getattr(getattr(engine, "config", None), "vault_path", None),
        getattr(getattr(conn_or_cursor, "config", None), "vault_path", None),
    ]
    for vcand in v_candidates:
        if vcand:
            return Path(str(vcand)).resolve().parent / "_archive"

    return Path(tempfile.gettempdir()) / "minni_cold_archive"


def _append_tombstone(archive_dir: Path, payload: dict[str, Any]) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    ym = datetime.now(timezone.utc).strftime("%Y-%m")
    tombstone_file = archive_dir / f"tombstones-{ym}.jsonl"
    with open(tombstone_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return tombstone_file


def _run_with_cursor(conn_or_cursor: Any, fn: Any) -> Any:
    if hasattr(conn_or_cursor, "execute") and hasattr(conn_or_cursor, "fetchone"):
        return fn(conn_or_cursor)
    if hasattr(conn_or_cursor, "cursor"):
        cur_attr = conn_or_cursor.cursor()
        if hasattr(cur_attr, "__enter__"):
            with cur_attr as c:
                return fn(c)
        try:
            res = fn(cur_attr)
            if hasattr(conn_or_cursor, "commit"):
                conn_or_cursor.commit()
            return res
        finally:
            try:
                cur_attr.close()
            except Exception:
                pass
    raise TypeError(f"Unsupported database handle: {type(conn_or_cursor)!r}")


def evict_learning_to_cold_archive(
    conn_or_cursor: Any,
    engine_or_lid: Any = None,
    learning_id: int | str | None = None,
    reason: str = "evicted",
    *,
    archive_dir: Path | str | None = None,
    minni_home: Path | str | None = None,
    db_path: Path | str | None = None,
    vault_path: Path | str | None = None,
) -> dict[str, Any]:
    """Append a learning tombstone to cold archive and hard-delete all DB/FTS/vector/disk projections."""
    if archive_dir is None and minni_home is not None:
        archive_dir = Path(minni_home) / "_archive"
    if learning_id is None and isinstance(engine_or_lid, int):
        engine = None
        lid = int(engine_or_lid)
    elif isinstance(engine_or_lid, int) and isinstance(learning_id, str) and not learning_id.isdigit():
        engine = None
        lid = int(engine_or_lid)
        reason = learning_id
    else:
        engine = engine_or_lid
        if learning_id is None:
            raise ValueError("learning_id is required")
        lid = int(learning_id)

    def _op(c: Any) -> dict[str, Any]:
        if not _table_exists(c, "learnings"):
            return {"evicted": False, "learning_id": lid, "doc_ids": [], "tombstone_path": None}

        l_cols = _table_columns(c, "learnings")
        pk_col = "learning_id" if "learning_id" in l_cols else ("id" if "id" in l_cols else "rowid")
        row = c.execute(f"SELECT * FROM learnings WHERE {pk_col} = ?", (lid,)).fetchone()
        row_dict: dict[str, Any] = {}
        if row is not None:
            if hasattr(row, "keys"):
                row_dict = {k: row[k] for k in row.keys()}
            elif isinstance(row, dict):
                row_dict = dict(row)

        doc_ids: set[int] = set()
        has_ld = _table_exists(c, "learning_documents")
        if has_ld:
            for r in c.execute(
                "SELECT doc_id FROM learning_documents WHERE learning_id = ?", (lid,)
            ).fetchall():
                val = r["doc_id"] if hasattr(r, "keys") else r[0]
                if val is not None:
                    doc_ids.add(int(val))

        has_docs = _table_exists(c, "documents")
        doc_cols = _table_columns(c, "documents") if has_docs else set()
        if has_docs and "memory_uri" in doc_cols:
            for r in c.execute(
                "SELECT doc_id FROM documents WHERE memory_uri = ?",
                (f"learning://{lid}",),
            ).fetchall():
                val = r["doc_id"] if hasattr(r, "keys") else r[0]
                if val is not None:
                    doc_ids.add(int(val))

        resolved_vault = (
            vault_path
            or getattr(getattr(engine, "config", None), "vault_path", None)
            or getattr(getattr(conn_or_cursor, "config", None), "vault_path", None)
        )
        if row_dict and resolved_vault and has_docs:
            agent_val = str(row_dict.get("agent_id") or "main")
            content_val = str(row_dict.get("content") or "")
            candidate_path = Path(
                durable_doc_path(agent_val, f"learning:{lid}", str(resolved_vault), content_val)
            )
            for r in c.execute(
                "SELECT doc_id FROM documents WHERE path = ?",
                (str(candidate_path),),
            ).fetchall():
                val = r["doc_id"] if hasattr(r, "keys") else r[0]
                if val is not None:
                    doc_ids.add(int(val))
            if candidate_path.exists():
                candidate_path.unlink(missing_ok=True)

        if row is None and not doc_ids:
            return {"evicted": False, "learning_id": lid, "doc_ids": [], "tombstone_path": None}

        target_archive = _resolve_archive_dir(
            c,
            engine,
            archive_dir=archive_dir,
            db_path=db_path,
            vault_path=resolved_vault,
            conn_or_cursor=conn_or_cursor,
        )
        tombstone_payload = {
            "kind": "learning_tombstone",
            "learning_id": lid,
            "agent_id": str(row_dict.get("agent_id") or ""),
            "category": str(row_dict.get("category") or "general"),
            "content": str(row_dict.get("content") or ""),
            "confidence": float(row_dict.get("confidence") if row_dict.get("confidence") is not None else 1.0),
            "status": str(row_dict.get("status") or "active"),
            "reason": str(reason),
            "evicted_at": time.time(),
            "doc_ids": sorted(doc_ids),
        }
        tombstone_path = _append_tombstone(target_archive, tombstone_payload)

        if "superseded_by" in l_cols:
            c.execute("UPDATE learnings SET superseded_by = NULL WHERE superseded_by = ?", (lid,))

        if has_ld:
            c.execute("DELETE FROM learning_documents WHERE learning_id = ?", (lid,))

        if _table_exists(c, "learning_reads"):
            c.execute("DELETE FROM learning_reads WHERE learning_id = ?", (lid,))

        c.execute(f"DELETE FROM learnings WHERE {pk_col} = ?", (lid,))

        if _table_exists(c, "learnings_fts"):
            try:
                c.execute("DELETE FROM learnings_fts WHERE learning_id = ?", (lid,))
            except Exception:
                pass

        has_vfts = _table_exists(c, "vault_fts")
        has_chunks = _table_exists(c, "chunk_embeddings")
        has_links = _table_exists(c, "memory_links")

        evicted_doc_ids: list[int] = []
        for did in sorted(doc_ids):
            still_referenced = False
            if has_ld:
                ref_row = c.execute(
                    "SELECT 1 FROM learning_documents WHERE doc_id = ? LIMIT 1", (did,)
                ).fetchone()
                still_referenced = ref_row is not None
            if still_referenced:
                continue

            if has_docs:
                d_row = c.execute("SELECT path FROM documents WHERE doc_id = ?", (did,)).fetchone()
                if d_row is not None:
                    d_path_str = d_row["path"] if hasattr(d_row, "keys") else d_row[0]
                    if d_path_str:
                        d_path = Path(str(d_path_str))
                        if "_durable" in d_path.parts and d_path.exists():
                            d_path.unlink(missing_ok=True)

            if has_vfts:
                c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (did,))
            if has_chunks:
                c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (did,))
            if has_links:
                c.execute(
                    "DELETE FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
                    (did, did),
                )
            if has_docs:
                c.execute("DELETE FROM documents WHERE doc_id = ?", (did,))
            evicted_doc_ids.append(did)

        if engine is not None and evicted_doc_ids:
            for attr in ("_faiss", "faiss_index"):
                faiss_idx = getattr(engine, attr, None)
                if faiss_idx is not None and hasattr(faiss_idx, "invalidate"):
                    try:
                        faiss_idx.invalidate()
                    except Exception:
                        pass
            if hasattr(engine, "_invalidate_trie"):
                try:
                    engine._invalidate_trie()
                except Exception:
                    pass
            else:
                engine._trie_docs = None
                engine._trie_max_doc_id = -1
                engine._trie_doc_count = -1
                engine._trie_checked_at = 0.0

        return {
            "evicted": True,
            "learning_id": lid,
            "doc_ids": evicted_doc_ids,
            "tombstone_path": str(tombstone_path),
        }

    return _run_with_cursor(conn_or_cursor, _op)


def evict_document_to_cold_archive(
    conn_or_cursor: Any,
    engine: Any = None,
    doc_id: int = 0,
    reason: str = "evicted_document",
    *,
    archive_dir: Path | str | None = None,
    db_path: Path | str | None = None,
    vault_path: Path | str | None = None,
) -> dict[str, Any]:
    """Append a document tombstone to cold archive and remove its index projections."""
    did = int(doc_id)

    def _op(c: Any) -> dict[str, Any]:
        if not _table_exists(c, "documents"):
            return {"evicted": False, "doc_id": did, "tombstone_path": None}
        row = c.execute("SELECT * FROM documents WHERE doc_id = ?", (did,)).fetchone()
        if row is None:
            return {"evicted": False, "doc_id": did, "tombstone_path": None}
        row_dict = {k: row[k] for k in row.keys()} if hasattr(row, "keys") else {}
        target_archive = _resolve_archive_dir(
            c,
            engine,
            archive_dir=archive_dir,
            db_path=db_path,
            vault_path=vault_path,
            conn_or_cursor=conn_or_cursor,
        )
        tombstone_payload = {
            "kind": "document_tombstone",
            "doc_id": did,
            "path": str(row_dict.get("path") or ""),
            "agent": str(row_dict.get("agent") or ""),
            "page_status": str(row_dict.get("page_status") or ""),
            "reason": str(reason),
            "evicted_at": time.time(),
        }
        tombstone_path = _append_tombstone(target_archive, tombstone_payload)

        if _table_exists(c, "learning_documents"):
            c.execute("DELETE FROM learning_documents WHERE doc_id = ?", (did,))
        if _table_exists(c, "vault_fts"):
            c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (did,))
        if _table_exists(c, "chunk_embeddings"):
            c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (did,))
        if _table_exists(c, "memory_links"):
            c.execute(
                "DELETE FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
                (did, did),
            )
        c.execute("DELETE FROM documents WHERE doc_id = ?", (did,))
        if engine is not None:
            for attr in ("_faiss", "faiss_index"):
                faiss_idx = getattr(engine, attr, None)
                if faiss_idx is not None and hasattr(faiss_idx, "invalidate"):
                    try:
                        faiss_idx.invalidate()
                    except Exception:
                        pass
            if hasattr(engine, "_invalidate_trie"):
                try:
                    engine._invalidate_trie()
                except Exception:
                    pass
        return {"evicted": True, "doc_id": did, "tombstone_path": str(tombstone_path)}

    return _run_with_cursor(conn_or_cursor, _op)


def evict_candidate_to_cold_archive(
    conn_or_cursor: Any,
    engine_or_cid: Any = None,
    candidate_id: int | str | None = None,
    reason: str = "evicted_candidate",
    *,
    archive_dir: Path | str | None = None,
    minni_home: Path | str | None = None,
    db_path: Path | str | None = None,
    vault_path: Path | str | None = None,
) -> dict[str, Any]:
    """Append a candidate packet tombstone to cold archive and remove its proposed/review DB rows."""
    if archive_dir is None and minni_home is not None:
        archive_dir = Path(minni_home) / "_archive"
    if candidate_id is None and isinstance(engine_or_cid, int):
        engine = None
        cid = int(engine_or_cid)
    elif isinstance(engine_or_cid, int) and isinstance(candidate_id, str) and not str(candidate_id).isdigit():
        engine = None
        cid = int(engine_or_cid)
        reason = str(candidate_id)
    else:
        engine = engine_or_cid
        if candidate_id is None:
            raise ValueError("candidate_id is required")
        cid = int(candidate_id)

    def _op(c: Any) -> dict[str, Any]:
        if not _table_exists(c, "candidate_packets"):
            return {"evicted": False, "candidate_id": cid, "tombstone_path": None}
        row = c.execute(
            "SELECT * FROM candidate_packets WHERE candidate_id = ?", (cid,)
        ).fetchone()
        if row is None:
            return {"evicted": False, "candidate_id": cid, "tombstone_path": None}
        row_dict = {k: row[k] for k in row.keys()} if hasattr(row, "keys") else {}
        target_archive = _resolve_archive_dir(
            c,
            engine,
            archive_dir=archive_dir,
            db_path=db_path,
            vault_path=vault_path,
            conn_or_cursor=conn_or_cursor,
        )
        tombstone_payload = {
            "kind": "candidate_tombstone",
            "candidate_id": cid,
            "principal": str(row_dict.get("principal") or ""),
            "workspace_id": str(row_dict.get("workspace_id") or "default"),
            "privacy_level": str(row_dict.get("privacy_level") or "review"),
            "status": str(row_dict.get("status") or "proposed"),
            "content": str(row_dict.get("content") or ""),
            "proposed_at": row_dict.get("proposed_at"),
            "reason": str(reason),
            "evicted_at": time.time(),
        }
        tombstone_path = _append_tombstone(target_archive, tombstone_payload)

        if _table_exists(c, "consolidation_actions"):
            c.execute(
                "DELETE FROM consolidation_actions WHERE action_type = 'afm_review' AND claim = ?",
                (str(cid),),
            )
        c.execute(
            "UPDATE candidate_packets SET status = 'rejected', resolved_at = ?, "
            "resolved_by = 'cold_archive' WHERE candidate_id = ?",
            (time.time(), cid),
        )
        try:
            from minni.afm_passes.inbox_archive import maybe_archive_for_candidate
            from minni.config import Config

            db_wrapper = engine if hasattr(engine, "cursor") else None
            if db_wrapper is None:
                class _CursorWrapper:
                    def __init__(self, cur: Any) -> None:
                        self._cur = cur
                    def cursor(self) -> Any:
                        from contextlib import contextmanager
                        @contextmanager
                        def _cm():
                            yield self._cur
                        return _cm()
                db_wrapper = _CursorWrapper(c)
            cfg = getattr(engine, "config", None) or Config()
            maybe_archive_for_candidate(db_wrapper, cfg, cid)
        except Exception:
            pass
        c.execute("DELETE FROM candidate_packets WHERE candidate_id = ?", (cid,))
        return {
            "evicted": True,
            "candidate_id": cid,
            "tombstone_path": str(tombstone_path),
        }

    return _run_with_cursor(conn_or_cursor, _op)


def find_near_duplicate_active_learning(
    conn_or_cursor: Any,
    *,
    agent_id: str,
    category: str,
    content: str,
    exclude_learning_id: int | None = None,
    embedding: bytes | None = None,
    jaccard_threshold: float = 0.65,
    cosine_threshold: float = 0.88,
) -> list[int]:
    """Return active learning IDs in the same (agent_id, category) with 5-gram Jaccard >= 0.65 or cosine >= 0.88."""

    def _op(c: Any) -> list[int]:
        if not _table_exists(c, "learnings"):
            return []
        l_cols = _table_columns(c, "learnings")
        pk_col = "learning_id" if "learning_id" in l_cols else ("id" if "id" in l_cols else "rowid")
        has_status = "status" in l_cols
        has_superseded = "superseded_by" in l_cols
        has_embedding = "embedding" in l_cols

        select_cols = [pk_col, "content"]
        if has_embedding:
            select_cols.append("embedding")
        where_parts = ["agent_id = ?", "COALESCE(category, 'general') = ?"]
        params: list[Any] = [agent_id, category or "general"]
        if has_status:
            where_parts.append("COALESCE(status, 'active') = 'active'")
        if has_superseded:
            where_parts.append("superseded_by IS NULL")
        if exclude_learning_id is not None:
            where_parts.append(f"{pk_col} != ?")
            params.append(int(exclude_learning_id))

        sql = f"SELECT {', '.join(select_cols)} FROM learnings WHERE {' AND '.join(where_parts)}"
        rows = c.execute(sql, tuple(params)).fetchall()

        target_emb = embedding
        if target_emb is None and exclude_learning_id is not None and has_embedding:
            erow = c.execute(
                f"SELECT embedding FROM learnings WHERE {pk_col} = ?",
                (int(exclude_learning_id),),
            ).fetchone()
            if erow is not None:
                target_emb = erow["embedding"] if hasattr(erow, "keys") else erow[0]

        matches: list[int] = []
        for r in rows:
            rid = int(r[pk_col] if hasattr(r, "keys") else r[0])
            rcontent = str((r["content"] if hasattr(r, "keys") else r[1]) or "")
            jacc = five_gram_jaccard(content, rcontent)
            if jacc >= jaccard_threshold:
                matches.append(rid)
                continue
            if has_embedding and target_emb is not None:
                remb = r["embedding"] if hasattr(r, "keys") else r[2]
                if _cosine_similarity_blobs(target_emb, remb) >= cosine_threshold:
                    matches.append(rid)
        return matches

    return _run_with_cursor(conn_or_cursor, _op)


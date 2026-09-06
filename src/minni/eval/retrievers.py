"""Retriever adapters used by the offline recall evaluation harness."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dataset import repo_root

logger = logging.getLogger("sovereign.eval")


class SearcherProtocol:
    """
    Abstract protocol for the object used by the harness.
    The real implementation wraps RetrievalEngine; tests inject a mock.
    """

    def search(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        raise NotImplementedError


class RealSearcher(SearcherProtocol):
    """Wraps engine.retrieval.RetrievalEngine for in-process evaluation."""

    def __init__(self) -> None:
        # Lazy import so the module can be imported without a live DB.
        engine_dir = Path(__file__).resolve().parent.parent
        if str(engine_dir) not in sys.path:
            sys.path.insert(0, str(engine_dir))

        import minni.db as db_mod
        from minni.config import DEFAULT_CONFIG
        from minni.faiss_index import FAISSIndex
        from minni.retrieval import RetrievalEngine

        self._engine = RetrievalEngine(
            db=db_mod.SovereignDB(DEFAULT_CONFIG),
            config=DEFAULT_CONFIG,
            faiss_index=FAISSIndex(DEFAULT_CONFIG),
        )

    def search(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        return self._engine.retrieve(query, **kwargs)


class RipgrepSearcher(SearcherProtocol):
    """Plain-text baseline over markdown/text files using ripgrep."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def search(self, query: str, limit: int = 10, **kwargs) -> List[Dict[str, Any]]:
        if not query.strip():
            return []

        cmd = [
            "rg",
            "--ignore-case",
            "--fixed-strings",
            "--line-number",
            "--color",
            "never",
            "--glob",
            "*.md",
            "--glob",
            "*.txt",
            "--glob",
            "*.jsonl",
            query,
            str(self.root),
        ]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                text=True,
                capture_output=True,
                timeout=15,
            )
        except FileNotFoundError:
            logger.warning("ripgrep is not installed; ripgrep baseline returned no results")
            return []
        except subprocess.TimeoutExpired:
            logger.warning("ripgrep baseline timed out for query %r", query)
            return []

        if proc.returncode not in (0, 1):
            logger.warning("ripgrep baseline failed: %s", proc.stderr.strip())
            return []

        results = []
        seen_paths = set()
        for line in proc.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            path, lineno, text = parts
            if path in seen_paths:
                continue
            seen_paths.add(path)
            results.append({
                "doc_id": None,
                "source": path,
                "filename": Path(path).name,
                "line": int(lineno) if lineno.isdigit() else None,
                "text": text.strip(),
                "score": 1.0 / (len(results) + 1),
                "token_count": max(1, len(text.strip()) // 4),
                "retriever": "ripgrep",
            })
            if len(results) >= limit:
                break
        return results


class RawContextSearcher(SearcherProtocol):
    """Raw context-dump baseline that returns a deterministic text prefix."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def search(self, query: str, limit: int = 10, **kwargs) -> List[Dict[str, Any]]:
        budget_tokens = int(kwargs.get("budget_tokens", 200_000) or 200_000)
        budget_chars = max(1, budget_tokens * 4)
        chunks = []
        total_chars = 0
        for path in sorted(self.root.rglob("*")):
            if path.suffix.lower() not in {".md", ".txt", ".jsonl"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunk = f"\n\n# {path.relative_to(self.root)}\n{text}"
            chunks.append(chunk)
            total_chars += len(chunk)
            if total_chars >= budget_chars:
                break
        text = "".join(chunks)[:budget_chars]
        if not text:
            return []
        return [{
            "doc_id": None,
            "source": str(self.root),
            "filename": self.root.name,
            "text": text,
            "score": 1.0,
            "token_count": max(1, len(text) // 4),
            "retriever": "raw-context",
        }][:limit]


class VendorMemorySearcher(SearcherProtocol):
    """Explicit opt-in placeholder for vendor-memory baselines."""

    def search(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        logger.warning("vendor-memory baseline is not configured; returning no results")
        return []


def _sanitize_snapshot_query(query: str) -> str:
    """FTS5-safe query terms; mirrors RetrievalEngine._sanitize_fts_query.

    A contract test pins parity so snapshot lexical semantics cannot drift
    from the engine's FTS leg. Local copy (not an engine call) keeps the
    snapshot search path free of model-adjacent imports.
    """
    import re as _re

    words = _re.sub(r"[^\w\s]", " ", query).split()
    return " ".join(words)


class SnapshotSearcher(SearcherProtocol):
    """Governed lexical retrieval over a prepared study snapshot directory.

    Explicit offline lexical implementation over the disposable study
    database: FTS5 MATCH (strict AND, OR fallback) with the engine's default
    lifecycle exclusions and the central ``can_read_document`` gate under a
    least-privilege principal scoped to the snapshot vault. No retrieval
    engine is instantiated, no model is loaded, no network is touched, and
    NO deadline is passed anywhere in this path — so present and future
    whole-request expiry semantics cannot degrade it to empty. Never touches
    the live ``DEFAULT_CONFIG`` database. Read-only: access counters are
    never updated and no zero-write forensic claim is made beyond that.
    """

    backend = "snapshot"

    def __init__(self, snapshot_dir: Path) -> None:
        from minni.principal import EffectivePrincipal

        from .study_snapshot import check_materialized, snapshot_config_paths, verify_snapshot

        root = Path(snapshot_dir).resolve()
        try:
            verified = verify_snapshot(root)
            materialized = check_materialized(root)
        except ValueError as exc:
            raise ValueError(f"snapshot directory {root} failed frozen validation: {exc}") from exc
        manifest = verified["manifest"]
        # The digest-bound identity block is authoritative; the display
        # mirrors in the manifest are validated against it by verify and are
        # never consumed directly.
        identity = manifest.get("identity") or {}
        self.snapshot_dir = root
        self.snapshot_id = manifest.get("snapshot_id", "unknown")
        self.manifest_digest = manifest.get("manifest_digest", "unknown")
        self._agent_id = str((identity.get("principal") or {}).get("agent_id") or "study")
        self.forbidden_doc_ids = set()
        for study_id, record in verified["mapping"].items():
            judgment = record.get("study_judgment") or {}
            if judgment.get("expected_eligible") is False:
                self.forbidden_doc_ids.add(materialized["document_ids"].get(study_id))
        self.forbidden_doc_ids.discard(None)
        paths = snapshot_config_paths(root)
        self._principal = EffectivePrincipal(
            agent_id=self._agent_id,
            capabilities=["search", "read"],
            allowed_vault_roots=[paths["vault_path"]],
        )

    def search(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        from minni.principal import can_read_document

        from .study_snapshot import (
            DEFAULT_EXCLUDED_STATUSES,
            StudySnapshotError,
            check_materialized,
            verify_snapshot,
        )

        if kwargs.get("expand") not in (None, False, "off") or kwargs.get("use_hyde"):
            raise ValueError("snapshot retrieval supports lexical-only baseline configuration")
        # Frozen state is re-validated before every search, not just at open.
        verify_snapshot(self.snapshot_dir)
        check_materialized(self.snapshot_dir)
        # Deadline-free by construction: any caller-supplied deadline is
        # ignored, never forwarded, so expiry semantics cannot empty results.
        limit = max(1, int(kwargs.get("limit", 10)))
        if not isinstance(query, str) or not query.strip():
            return []
        safe_query = _sanitize_snapshot_query(query)
        if not safe_query:
            return []
        terms = safe_query.split()
        match_exprs = [safe_query]
        if len(terms) > 1:
            match_exprs.append(" OR ".join(term.lower() for term in terms))

        import sqlite3
        from urllib.parse import quote as _quote

        db_path = self.snapshot_dir / "study.db"
        uri = "file:" + _quote(str(db_path), safe="/:") + "?mode=ro"
        skip = sorted(DEFAULT_EXCLUDED_STATUSES)
        placeholders = ",".join("?" * len(skip))
        rows: List[Dict[str, Any]] = []
        try:
            handle = sqlite3.connect(uri, uri=True)
            try:
                handle.execute("PRAGMA query_only = ON")
                for match_expr in match_exprs:
                    for row in handle.execute(
                        "SELECT f.doc_id, d.path, d.agent, d.page_status,"
                        " d.privacy_level, d.page_type, f.content, f.rank AS bm25_rank"
                        " FROM vault_fts f JOIN documents d ON d.doc_id = f.doc_id"
                        " WHERE vault_fts MATCH ?"
                        f" AND COALESCE(d.page_status, 'candidate') NOT IN ({placeholders})"
                        " ORDER BY rank LIMIT ?",
                        [match_expr, *skip, limit * 3],
                    ).fetchall():
                        rows.append({
                            "doc_id": row[0], "path": row[1], "agent": row[2],
                            "page_status": row[3] or "candidate",
                            "privacy_level": row[4], "page_type": row[5],
                            "content": row[6],
                        })
                    if rows:
                        break
            finally:
                handle.close()
        except Exception as exc:  # noqa: BLE001 - a DB failure is an integrity failure
            raise StudySnapshotError(
                f"snapshot lexical search failed: {type(exc).__name__}"
            ) from exc

        results = []
        for rank, row in enumerate(rows[:limit], start=1):
            metadata = {
                "path": row["path"], "agent": row["agent"],
                "page_type": row["page_type"], "privacy_level": row["privacy_level"],
            }
            if not can_read_document(self._principal, "default", metadata):
                continue
            text = row["content"] or ""
            results.append({
                "doc_id": row["doc_id"],
                "source": row["path"],
                "filename": Path(row["path"]).name,
                "text": text,
                "score": round(1.0 / rank, 4),
                "token_count": max(1, len(text) // 4),
                "agent": row["agent"],
                "privacy_level": row["privacy_level"],
                "page_status": row["page_status"],
                "retriever": "snapshot",
                "provenance": {
                    "doc_id": row["doc_id"],
                    "backend": "snapshot",
                    "snapshot_id": self.snapshot_id,
                    "lexical_only": True,
                },
            })
        return results


class MockSearcher(SearcherProtocol):
    """
    Deterministic mock searcher.
    For each query, returns results whose doc_ids match expected_doc_ids.
    """

    def __init__(self, queries: Optional[List[Dict[str, Any]]] = None) -> None:
        self._lookup: Dict[str, List[int]] = {}
        for q in (queries or []):
            self._lookup[q["query"]] = [int(i) for i in q.get("expected_doc_ids", [])]

    def search(self, query: str, limit: int = 10, **kwargs) -> List[Dict[str, Any]]:
        expected = self._lookup.get(query, [])
        results = []
        for rank, did in enumerate(expected[:limit], start=1):
            results.append({
                "doc_id": did,
                "text": f"Mock result for doc {did}",
                "source": f"wiki/mock/{did}.md",
                "heading": "",
                "score": round(1.0 / rank, 4),
                "confidence": round(0.9 / rank, 4),
                "provenance": {"doc_id": did, "backend": "mock"},
                "privacy_level": "safe",
                "recommended_action": "cite",
            })
        return results


def make_searcher(
    name: str,
    queries: Optional[List[Dict[str, Any]]] = None,
    root: Optional[Path] = None,
) -> SearcherProtocol:
    """Build a named retriever for adversarial baseline comparisons."""
    key = name.strip().lower()
    explicit_root = root
    search_root = root or repo_root()
    if key in {"mock"}:
        return MockSearcher(queries)
    if key in {"minnid", "sovrd", "baseline"}:
        return RealSearcher()
    if key in {"ripgrep", "rg"}:
        return RipgrepSearcher(search_root)
    if key in {"raw-context", "raw_context", "raw"}:
        return RawContextSearcher(search_root)
    if key in {"vendor", "vendor-memory", "vendor_memory"}:
        return VendorMemorySearcher()
    if key in {"snapshot", "study-snapshot", "study_snapshot"}:
        if explicit_root is None:
            raise ValueError(
                "The snapshot retriever needs a prepared snapshot directory "
                "(see eval/study_snapshot.py); pass root=<snapshot-dir>"
            )
        return SnapshotSearcher(Path(explicit_root))
    raise ValueError(
        "Unknown retriever {!r}. Available: minnid, ripgrep, raw-context, "
        "vendor-memory, mock, snapshot".format(name)
    )


_MockSearcher = MockSearcher

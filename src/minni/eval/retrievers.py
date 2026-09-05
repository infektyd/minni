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


class SnapshotSearcher(SearcherProtocol):
    """Governed retrieval over a prepared study snapshot directory only.

    Opens the disposable database/index/vault inside ``snapshot_dir`` (see
    ``eval/study_snapshot.py``) under a least-privilege principal scoped to
    the snapshot vault. Never instantiates the live ``DEFAULT_CONFIG``.
    Normal governed search applies; access/trace effects are the engine's
    own and no zero-write forensic claim is made.
    """

    backend = "snapshot"

    def __init__(self, snapshot_dir: Path) -> None:
        from .study_snapshot import check_materialized, verify_snapshot

        root = Path(snapshot_dir)
        try:
            verified = verify_snapshot(root)
            check_materialized(root)
        except ValueError as exc:
            raise ValueError(f"snapshot directory {root} failed frozen validation: {exc}") from exc
        manifest = verified["manifest"]
        # The digest-bound identity block is authoritative; the display
        # mirrors in the manifest are validated against it by verify and are
        # never consumed directly.
        identity = manifest.get("identity") or {}
        self.snapshot_dir = root
        self.snapshot_id = manifest.get("snapshot_id", "unknown")
        self._agent_id = str((identity.get("principal") or {}).get("agent_id") or "study")
        self._engine = None
        self._principal = None

    def _ensure_engine(self):  # lazy so import never touches a database
        if self._engine is not None:
            return self._engine
        from minni.config import SovereignConfig
        from minni.db import SovereignDB
        from minni.principal import EffectivePrincipal
        from minni.retrieval import RetrievalEngine
        from .study_snapshot import check_materialized, snapshot_config_paths, verify_snapshot

        root = self.snapshot_dir
        # Re-validate frozen files, metadata, and materialized outputs before
        # opening the disposable backend: symlinks, tampered bytes,
        # inconsistent mappings, and stale-output mixing all fail here.
        verify_snapshot(root)
        materialized = check_materialized(root)
        paths = snapshot_config_paths(root)
        config = SovereignConfig(
            db_path=paths["db_path"],
            vault_path=paths["vault_path"],
            faiss_index_path=paths["faiss_index_path"],
            graph_export_dir=paths["graph_export_dir"],
            writeback_path=paths["writeback_path"],
            writeback_enabled=False,
            reranker_enabled=False,
            hyde_enabled=False,
        )
        db = SovereignDB(config)
        self._engine = RetrievalEngine(db, config)
        self._principal = EffectivePrincipal(
            agent_id=self._agent_id,
            capabilities=["search", "read"],
            allowed_vault_roots=[paths["vault_path"]],
        )
        self._materialized = materialized
        return self._engine

    def search(self, query: str, **kwargs) -> List[Dict[str, Any]]:
        from .study_snapshot import check_materialized, verify_snapshot

        # Frozen state is re-validated before every search, not just at open.
        verify_snapshot(self.snapshot_dir)
        check_materialized(self.snapshot_dir)
        engine = self._ensure_engine()
        search_kwargs = {
            "limit": kwargs.get("limit", 10),
            "update_access": False,
            "expand": False,
            "use_hyde": False,
            "budget_tokens": kwargs.get("budget_tokens", True),
            "principal": self._principal,
        }
        if kwargs.get("deadline_monotonic") is not None:
            search_kwargs["deadline_monotonic"] = kwargs["deadline_monotonic"]
        return engine.retrieve(query, **search_kwargs)


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

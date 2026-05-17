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

        import db as db_mod
        from config import DEFAULT_CONFIG
        from faiss_index import FAISSIndex
        from retrieval import RetrievalEngine

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
    search_root = root or repo_root()
    if key in {"mock"}:
        return MockSearcher(queries)
    if key in {"sovrd", "baseline"}:
        return RealSearcher()
    if key in {"ripgrep", "rg"}:
        return RipgrepSearcher(search_root)
    if key in {"raw-context", "raw_context", "raw"}:
        return RawContextSearcher(search_root)
    if key in {"vendor", "vendor-memory", "vendor_memory"}:
        return VendorMemorySearcher()
    raise ValueError(
        f"Unknown retriever {name!r}. Available: sovrd, ripgrep, raw-context, vendor-memory, mock"
    )


_MockSearcher = MockSearcher

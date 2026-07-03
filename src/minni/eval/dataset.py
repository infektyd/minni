"""Dataset loading, validation, and harvest helpers for recall evaluation."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("sovereign.eval")


def repo_root() -> Path:
    """Return the repository root (two levels up from engine/eval/)."""
    return Path(__file__).resolve().parent.parent.parent


def queries_path() -> Path:
    return repo_root() / "eval" / "queries.jsonl"


def load_queries(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load queries from a JSONL file. Returns list of query dicts."""
    p = path or queries_path()
    if not p.exists():
        logger.warning("queries.jsonl not found at %s - returning empty set", p)
        return []

    queries = []
    with p.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                queries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning("queries.jsonl line %d: JSON parse error: %s", lineno, exc)
    return queries


def validate_queries(
    queries: List[Dict[str, Any]],
    min_reviewed: int = 300,
) -> Dict[str, Any]:
    """Validate adversarial query-set shape before using it for gates."""
    errors = []
    reviewed_count = 0
    classes = {}

    for idx, q in enumerate(queries, start=1):
        label = f"query[{idx}]"
        query_text = str(q.get("query", "")).strip()
        if not query_text:
            errors.append(f"{label}: query is required")
        if not q.get("reviewed"):
            errors.append(f"{label}: reviewed=true is required for gate datasets")
        else:
            reviewed_count += 1
        if not (q.get("expected_refs") or q.get("expected_doc_ids")):
            errors.append(f"{label}: expected_refs or expected_doc_ids is required")
        if q.get("expected_refs") and not isinstance(q["expected_refs"], list):
            errors.append(f"{label}: expected_refs must be a list")
        if q.get("expected_relevance") is None and q.get("relevance") is None:
            errors.append(f"{label}: expected_relevance is required for nDCG")
        if "answer_rubric" not in q:
            errors.append(f"{label}: answer_rubric is required")
        if "privacy_expectation" not in q:
            errors.append(f"{label}: privacy_expectation is required")
        note = str(q.get("notes", "uncategorized") or "uncategorized")
        classes[note] = classes.get(note, 0) + 1

    if reviewed_count < min_reviewed:
        errors.append(
            f"reviewed query count {reviewed_count} is below required minimum {min_reviewed}"
        )

    return {
        "ok": not errors,
        "count": len(queries),
        "reviewed_count": reviewed_count,
        "classes": classes,
        "errors": errors,
    }


def harvest_queries(
    roots: List[Path],
    limit: int = 300,
) -> List[Dict[str, Any]]:
    """
    Harvest review candidates from local markdown/text sources.

    These entries are deliberately marked reviewed=false; humans/agents must
    add relevance grades, answer rubrics, and privacy expectations before gates.
    """
    candidates = []
    suffixes = {".md", ".txt"}
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in paths:
            if path.is_dir() or path.suffix.lower() not in suffixes:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            query = ""
            for line in lines:
                cleaned = line.strip(" #\t")
                if len(cleaned.split()) >= 3:
                    query = cleaned[:120]
                    break
            if not query:
                query = path.stem.replace("-", " ")
            candidates.append({
                "query": query,
                "expected_refs": [str(path)],
                "expected_doc_ids": [],
                "expected_relevance": {},
                "notes": "harvest-candidate",
                "reviewed": False,
                "answer_rubric": "",
                "privacy_expectation": "unknown",
            })
            if len(candidates) >= limit:
                return candidates
    return candidates

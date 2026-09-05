"""Disposable, machine-curated retrieval evaluation; never opens the live vault."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import platform
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .dataset import repo_root
from .metrics import _mrr, _recall_at_k


def load_fixture(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("provenance") not in {"machine-curated-synthetic", "machine-reviewed-public-repository"}:
        raise ValueError("fixture must declare supported machine-curated provenance")
    source_grounded = data["provenance"] == "machine-reviewed-public-repository"
    if source_grounded and (data.get("human_reviewed") is not False
                            or not re.fullmatch(r"[0-9a-f]{40}", data.get("source_revision", ""))):
        raise ValueError("repository fixture requires human_reviewed=false and a pinned source revision")
    documents, queries = data["documents"], data["queries"]
    refs = [d["ref"] for d in documents]
    if not documents or len(set(refs)) != len(refs):
        raise ValueError("fixture documents must have unique refs")
    for doc in documents:
        ref = Path(doc["ref"])
        if ref.is_absolute() or ".." in ref.parts or ref.suffix != ".md":
            raise ValueError("fixture refs must be relative markdown paths without traversal")
        if not doc.get("text", "").strip():
            raise ValueError("fixture document text is required")
        if type(doc.get("expected_eligible")) is not bool:
            raise ValueError("every document requires an explicit expected_eligible boolean")
        if source_grounded:
            _validate_source(doc, data["source_revision"])
    globally_forbidden = {d["ref"] for d in documents if not d["expected_eligible"]}
    if not queries:
        raise ValueError("fixture queries are required")
    for query in queries:
        if not query.get("query", "").strip() or int(query.get("limit", 5)) < 1:
            raise ValueError("query text and positive limit are required")
        expected = set(query["expected_refs"])
        forbidden = set(query["forbidden_refs"]) | globally_forbidden
        if expected & forbidden or not (expected | forbidden) <= set(refs):
            raise ValueError("expected/forbidden refs must be disjoint and resolve to corpus documents")
        if source_grounded:
            negatives = set(query.get("hard_negative_refs", []))
            if negatives & expected or not negatives <= set(refs):
                raise ValueError("hard negatives must resolve and differ from expected refs")
            if not query.get("expected_answer", "").strip() or not query.get("category", "").strip():
                raise ValueError("repository queries require an answer rationale and category")
    return data


def _validate_source(doc: dict, revision: str) -> None:
    """Verify an excerpt against its immutable Git source, not current docs."""
    source = doc.get("source", {})
    path = Path(source.get("path", ""))
    start, end = source.get("start_line"), source.get("end_line")
    if (path.is_absolute() or ".." in path.parts or path.suffix != ".md"
            or type(start) is not int or type(end) is not int or not 1 <= start <= end):
        raise ValueError("invalid repository source path or line range")
    try:
        blob = subprocess.check_output(["git", "show", f"{revision}:{path.as_posix()}"],
                                       cwd=repo_root(), stderr=subprocess.DEVNULL, timeout=10)
        lines = blob.decode("utf-8").splitlines(keepends=True)
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise ValueError("repository fixture source is unavailable") from exc
    excerpt = "".join(lines[start - 1:end])
    if (end > len(lines) or hashlib.sha256(blob).hexdigest() != source.get("sha256")
            or excerpt != doc["text"]
            or hashlib.sha256(excerpt.encode()).hexdigest() != doc.get("text_sha256")):
        raise ValueError("repository fixture source hash or excerpt mismatch")


def _latency_summary(rows: list[dict]) -> dict | None:
    times = sorted(row["latency_s"] for row in rows)
    if not times:
        return None
    return {"p50": times[math.ceil(len(times) * .5) - 1],
            "p95": times[math.ceil(len(times) * .95) - 1], "max": times[-1]}


def run_fixture(path: Path | None = None, *, profile: str = "lexical-deadline", repeats: int = 3) -> dict:
    """Run actual FTS/eligibility/formatting, optionally actual embedding/reranking.

    The lexical profile deliberately supplies an expired model deadline. FTS
    still runs; the engine's own deadline branch bypasses models and reports
    degradation. It is not a proxy for healthy hybrid search performance.
    """
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.principal import EffectivePrincipal
    from minni.retrieval import RetrievalEngine, _trace_ring

    if profile not in {"lexical-deadline", "hybrid"} or repeats < 1:
        raise ValueError("select lexical-deadline/hybrid and positive repeats")
    path = path or repo_root() / "eval/fixtures/retrieval.json"
    data = load_fixture(path)
    corpus_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo_root(), text=True,
        ).strip())
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = "unknown", None

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="minni-eval-") as temporary:
        root = Path(temporary)
        config = SovereignConfig(
            db_path=str(root / "eval.db"), vault_path=str(root / "vault"),
            faiss_index_path=str(root / "index.faiss"), graph_export_dir=str(root / "graphs"),
            writeback_enabled=False, reranker_enabled=profile == "hybrid", hyde_enabled=False,
        )
        db = SovereignDB(config)
        try:
            engine = RetrievalEngine(db, config)
            mapping = {}
            for doc in data["documents"]:
                target = root / "vault" / doc["ref"]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(doc["text"], encoding="utf-8")
                metadata = {key: doc[key] for key in ("agent", "privacy_level", "page_status", "page_type")}
                if profile == "hybrid":
                    indexed = engine.index_durable_document(content=doc["text"], path=str(target), **metadata)
                    if indexed.get("status") != "ok" or not indexed.get("chunks"):
                        raise RuntimeError("hybrid fixture indexing failed or produced no semantic chunks")
                    doc_id = indexed["doc_id"]
                else:
                    # Deliberately unembedded lexical corpus in an isolated DB.
                    with db.transaction() as cursor:
                        cursor.execute(
                            "INSERT INTO documents(path,agent,privacy_level,page_status,page_type,sigil) VALUES(?,?,?,?,?,?)",
                            (str(target), metadata["agent"], metadata["privacy_level"], metadata["page_status"], metadata["page_type"], "T"),
                        )
                        doc_id = cursor.lastrowid
                        cursor.execute("INSERT INTO vault_fts(doc_id,path,content,agent,sigil) VALUES(?,?,?,?,?)",
                                       (doc_id, str(target), doc["text"], doc["agent"], "T"))
                mapping[doc["ref"]] = doc_id
            setup_s = time.perf_counter() - started
            principal = EffectivePrincipal(agent_id="codex", capabilities=["search", "read"],
                                           allowed_vault_roots=[str(root / "vault")])
            reverse = {value: key for key, value in mapping.items()}
            # An independent corpus annotation, not the policy implementation
            # under test, defines eligibility for every query in this profile.
            globally_forbidden = {d["ref"] for d in data["documents"] if not d["expected_eligible"]}
            rows = []
            for repetition in range(repeats):
                for query in data["queries"]:
                    start = time.perf_counter()
                    results = engine.retrieve(
                        query["query"], limit=query.get("limit", 5), principal=principal,
                        update_access=False, expand=False, use_hyde=False, budget_tokens=False,
                        deadline_monotonic=time.monotonic() if profile == "lexical-deadline" else None,
                    )
                    latency = time.perf_counter() - start
                    ids = [r["doc_id"] for r in results]
                    found = [reverse.get(i, "<unknown-document>") for i in ids]
                    missing = sorted(set(query["expected_refs"]) - set(found))
                    forbidden_refs = set(query["forbidden_refs"]) | globally_forbidden
                    forbidden = sorted(forbidden_refs & set(found))
                    unknown_ids = [doc_id for doc_id in ids if doc_id not in reverse]
                    expected_ids = [mapping[ref] for ref in query["expected_refs"]]
                    degradation = {name: getattr(engine, f"last_{name}_degraded")
                                   for name in ("vector", "rerank", "query_expand", "hyde")}
                    trace = _trace_ring().get(engine.last_trace_id, requester="codex") or {}
                    rows.append({
                        "query": query["query"], "case": query["case"], "repetition": repetition,
                        "category": query.get("category", "synthetic-policy"),
                        "limit": query.get("limit", 5),
                        "expected_answer": query.get("expected_answer"),
                        "hard_negative_hits": sorted(set(query.get("hard_negative_refs", [])) & set(found)),
                        "expected_doc_ids": expected_ids, "expected_refs": query["expected_refs"],
                        "forbidden_refs": sorted(forbidden_refs), "unknown_doc_ids": unknown_ids,
                        "result_doc_ids": ids, "result_refs": found, "missing_refs": missing,
                        "forbidden_hits": forbidden, "latency_s": latency, "degradation": degradation,
                        "stage_timing_ms": trace.get("timing", {}),
                        "recall_at_limit": _recall_at_k(expected_ids, ids, query.get("limit", 5)) if expected_ids else None,
                        "mrr": _mrr(expected_ids, ids) if expected_ids else None,
                        # Hybrid search may return unrelated eligible documents
                        # for a denied-only lexical phrase. Do not call that a
                        # privacy leak or require semantic search to abstain.
                        "ok": not missing and not forbidden and not unknown_ids and (
                            bool(expected_ids) or profile == "hybrid" or not ids
                        ),
                    })
        finally:
            db.close()
    scored = [row for row in rows if row["recall_at_limit"] is not None]
    versions = {}
    for package in ("numpy", "faiss-cpu", "sentence-transformers"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return {
        "provenance": data["provenance"], "human_reviewed": False,
        "scope": ("public repository excerpts; machine-reviewed questions; not representative private-memory quality"
                  if data["provenance"] == "machine-reviewed-public-repository" else
                  "synthetic retrieval integration; not representative private-memory quality"),
        "corpus_source_revision": data.get("source_revision"),
        "sources": {d["ref"]: d["source"] for d in data["documents"] if "source" in d},
        "profile": profile, "source_revision": revision, "source_dirty": dirty,
        "fixture_sha256": corpus_digest, "python": platform.python_version(),
        "system": {"os": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "dependencies": versions,
        "principal": {"agent_id": "codex", "capabilities": ["search", "read"],
                      "scope": "entire disposable evaluation corpus; project paths are not authorization boundaries"},
        "options": {"expand": False, "use_hyde": False, "update_access": False,
                    "budget_tokens": False, "repeats": repeats},
        "timing_scope": "setup includes indexing; first pass may load reranker; repeated passes may hit caches; engine stages may overlap",
        "embedding_model": config.embedding_model if profile == "hybrid" else None,
        "reranker_model": config.reranker_model if profile == "hybrid" else None,
        "document_ids": mapping, "setup_s": setup_s, "queries": rows,
        "summary": {"ok": all(row["ok"] for row in rows) and (
                        profile != "hybrid" or not any(any(row["degradation"].values()) for row in rows)
                    ), "runs": len(rows),
                    "mean_recall_at_limit": sum(row["recall_at_limit"] for row in scored) / len(scored) if scored else None,
                    "mrr": sum(row["mrr"] for row in scored) / len(scored) if scored else None,
                    "forbidden_hits": sum(len(row["forbidden_hits"]) for row in rows),
                    "unknown_doc_ids": sum(len(row["unknown_doc_ids"]) for row in rows),
                    "degraded_runs": sum(any(row["degradation"].values()) for row in rows),
                    "latency_s": _latency_summary(rows),
                    "first_pass_latency_s": _latency_summary([r for r in rows if r["repetition"] == 0]),
                    "repeated_pass_latency_s": _latency_summary([r for r in rows if r["repetition"] > 0])},
    }

"""Honest per-run provenance for legacy recall-comparison runs.

The quality comparison validates judgments but cannot say what code, corpus,
or settings produced a report. This module records what the harness can
verify cheaply and marks the rest unknown instead of inventing evidence:

- query-file SHA-256 (bytes on disk; never a live database hash),
- code revision / dirty state via ``git`` (``"unknown"`` when unavailable),
- requested vs effective retrieval options per config,
- actual config/dependency metadata when importable without side effects,
- principal/scope availability (the legacy run supplies no principal),
- run order / timing / cache caveats,
- corpus/database snapshot identity when verifiably available.

The legacy ``minnid``/``sovrd``/``baseline`` path wraps ``RealSearcher`` over
the mutable ``DEFAULT_CONFIG`` live database. That backend is recorded as
live-mutable, never as a frozen or safe snapshot. No digest is computed for
a live database, and this module never opens a database merely for metadata.
Mock runs are labeled mock explicitly. Nothing here attributes human review,
dumps credentials/environment, or certifies a run as passing.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

LIVE_BACKENDS = {"minnid", "sovrd", "baseline"}
FILE_BACKENDS = {"ripgrep", "rg", "raw-context", "raw_context", "raw"}

RUN_CAVEATS = (
    "Reports run sequentially in-process in run_order; the first report may "
    "benefit from or pay for process-level caches, but searcher construction "
    "happens once per retriever before its first report and outside the "
    "per-query timing, so first-report latency does not include constructor cost.",
    "Mean latency is descriptive only, not a production benchmark; engine "
    "stage timings can overlap, so stage times must not be summed.",
    "A live-mutable backend reflects whatever the database holds at query "
    "time; re-running the same command later can score different content.",
)


def sha256_file(path: Optional[Path]) -> Optional[str]:
    """SHA-256 of a file's bytes, or None when missing/unreadable."""
    if path is None:
        return None
    try:
        h = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def canonical_queries_digest(queries: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    """Immutable digest of the exact parsed queries that were scored.

    The query file can change between loading and metadata collection, so a
    re-read of the file cannot describe the scored content. This digest is
    computed from the in-memory rows instead and always corresponds to them.
    """
    if queries is None:
        return None
    try:
        canonical = json.dumps(queries, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def query_file_provenance(
    requested: Optional[Path],
    effective: Optional[Path],
    loaded_queries: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Identify the scored queries plus the separately observed file bytes.

    ``loaded_queries_digest`` covers the exact parsed rows that were scored.
    ``file_sha256`` is a re-read of the file taken after loading, so its
    correspondence with the scored rows is explicitly unverified: a concurrent
    or later modification changes the file digest but not the scored content.
    """
    digest = sha256_file(effective)
    if effective is None or not Path(effective).exists():
        status = "missing" if requested is not None or effective is not None else "no-path"
    else:
        status = "hashed" if digest is not None else "unreadable"
    return {
        "requested_path": str(requested) if requested is not None else None,
        "effective_path": str(effective) if effective is not None else None,
        "loaded_queries_digest": canonical_queries_digest(loaded_queries),
        "file_sha256": digest,
        "file_status": status,
        "correspondence": (
            "unverified: file bytes were observed separately from parsing; "
            "only loaded_queries_digest describes the scored content"
        ),
    }


def code_provenance(root: Optional[Path] = None) -> Dict[str, Any]:
    """Code revision/dirty state, or unknown when git is unavailable."""
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root) if root is not None else None,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
    except Exception:  # noqa: BLE001 - any git failure means unknown
        return {"revision": "unknown", "dirty": None, "method": "git rev-parse HEAD"}
    try:
        dirty_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=str(root) if root is not None else None,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).strip()
        dirty: Optional[bool] = bool(dirty_out)
    except Exception:  # noqa: BLE001 - revision known, dirtiness not established
        dirty = None
    if not revision:
        revision = "unknown"
    return {"revision": revision, "dirty": dirty, "method": "git rev-parse HEAD"}


def retrieval_options_provenance(
    config_name: str,
    config_kwargs: Dict[str, Any],
    known_kwargs: Iterable[str],
    limit: int = 10,
) -> Dict[str, Any]:
    """Record requested config kwargs vs the effective kwargs sent to search()."""
    known = set(known_kwargs)
    requested = dict(config_kwargs)
    effective: Dict[str, Any] = {"limit": limit, "update_access": False}
    ignored = sorted(key for key in requested if key not in known)
    for key, value in requested.items():
        if key in known:
            effective[key] = value
    return {
        "config": config_name,
        "requested": requested,
        "effective": effective,
        "ignored_unknown": ignored,
        "expand_default_note": (
            "When 'expand' is unset the engine default applies "
            "(config.query_expand_default); an unset flag is not evidence "
            "of disabled expansion."
        ),
    }


def environment_provenance() -> Dict[str, Any]:
    """Config defaults and dependency versions when importable without side effects.

    Reads the already-constructed ``DEFAULT_CONFIG`` object only; it never
    opens a database, builds an engine, or loads a model.
    """
    config_info: Dict[str, Any] = {"available": False}
    try:
        from minni.config import DEFAULT_CONFIG

        config_info = {
            "available": True,
            "source": "DEFAULT_CONFIG object (live mutable defaults; not a frozen snapshot)",
            "model_names_note": (
                "Configured model names only; not observed inference from a "
                "loaded model. No model was loaded to collect this metadata."
            ),
            "embedding_model": getattr(DEFAULT_CONFIG, "embedding_model", "unknown"),
            "reranker_model": getattr(DEFAULT_CONFIG, "reranker_model", "unknown"),
            "query_expand_default": getattr(DEFAULT_CONFIG, "query_expand_default", "unknown"),
            "hyde_enabled": getattr(DEFAULT_CONFIG, "hyde_enabled", "unknown"),
        }
    except Exception as exc:  # noqa: BLE001 - metadata is best-effort
        # Type only: exception text may interpolate environment-derived
        # values, so details are deliberately withheld.
        config_info = {"available": False, "reason": f"{type(exc).__name__} (details withheld)"}
    dependencies: Dict[str, Optional[str]] = {}
    for package in ("numpy", "faiss-cpu", "sentence-transformers"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
        except Exception:  # noqa: BLE001 - version lookup is best-effort
            dependencies[package] = "unknown"
    return {
        "python": platform.python_version(),
        "config": config_info,
        "dependencies": dependencies,
    }


def principal_provenance(retriever_name: str, *, is_mock: bool) -> Dict[str, Any]:
    """State principal/scope availability without inventing authorization facts."""
    key = retriever_name.strip().lower()
    if is_mock or key == "mock":
        return {
            "supplied": False,
            "backend": "mock",
            "mock": True,
            "scope": "n/a (mock searcher; no database or principal involved)",
            "note": "Mock-only IDs; scores exercise plumbing only and do not "
                    "establish retrieval quality.",
        }
    if key in LIVE_BACKENDS:
        return {
            "supplied": False,
            "backend": "live-mutable DEFAULT_CONFIG",
            "mock": False,
            "frozen": False,
            "snapshot": "unknown",
            "scope": "unknown; no principal is supplied, so the engine default applies",
            "note": "Mutable live database; not a frozen snapshot and not a "
                    "safe study corpus. No digest is computed for it.",
        }
    if key in FILE_BACKENDS:
        return {
            "supplied": False,
            "backend": key,
            "mock": False,
            "scope": "repository working-tree files (no authorization boundary)",
            "note": "File-text baseline; scores describe lexical overlap, not memory quality.",
        }
    if key in {"snapshot", "study-snapshot", "study_snapshot"}:
        return {
            "supplied": True,
            "backend": "study-snapshot",
            "mock": False,
            "scope": "prepared snapshot vault only (least-privilege study principal)",
            "note": "Study principal is scoped to the frozen snapshot vault; "
                    "packet authorization claims are supplied provenance, not "
                    "independently verified permission.",
        }
    return {
        "supplied": False,
        "backend": key or "unknown",
        "mock": False,
        "scope": "unknown",
        "note": "Unrecognized backend; no authorization or scope claim is made.",
    }


def corpus_provenance(*, is_mock: bool, retriever_name: str = "") -> Dict[str, Any]:
    """Corpus/database identity per backend: only what is verifiably available.

    Only the live-engine backends touch a database at all. File baselines
    and placeholders get their own honest labels instead of inheriting the
    live-database description.
    """
    key = retriever_name.strip().lower()
    if is_mock or key == "mock":
        return {
            "snapshot": "mock",
            "frozen": False,
            "note": "No corpus involved; expected IDs come from the query file.",
        }
    if key in LIVE_BACKENDS:
        return {
            "snapshot": "unknown",
            "frozen": False,
            "note": "Live mutable database; no snapshot digest is computed "
                    "(hashing a live DB is avoided) and no new connection is "
                    "opened merely for metadata.",
        }
    if key in FILE_BACKENDS:
        return {
            "snapshot": "working-tree-files",
            "frozen": False,
            "note": "No database involved; results come from repository "
                    "working-tree files, which are mutable and unversioned "
                    "as a corpus.",
        }
    if key in {"vendor", "vendor-memory", "vendor_memory"}:
        return {
            "snapshot": "unconfigured-placeholder",
            "frozen": False,
            "note": "Vendor-memory baseline is not configured and returns no "
                    "results; no corpus identity applies.",
        }
    if key in {"snapshot", "study-snapshot", "study_snapshot"}:
        return {
            "snapshot": "study-frozen",
            "frozen": True,
            "note": "Disposable study snapshot: all DB/index/vault paths live "
                    "inside the prepared snapshot directory under a manifest "
                    "digest (see snapshot.json). The live corpus is never "
                    "assigned a snapshot ID. Bounded packet study only: not "
                    "representative private-memory quality and not a "
                    "retrieval-performance claim.",
        }
    return {
        "snapshot": "unknown",
        "frozen": False,
        "note": "Unrecognized backend; no corpus identity claim is made.",
    }


def build_report_provenance(
    *,
    query: Dict[str, Any],
    code: Dict[str, Any],
    retrieval: Dict[str, Any],
    principal: Dict[str, Any],
    corpus: Dict[str, Any],
    environment: Dict[str, Any],
    retriever_name: str,
    run_index: int,
    run_order: list,
    started_iso: str,
    mock: bool,
) -> Dict[str, Any]:
    """Assemble the per-report provenance block attached to each JSON report."""
    return {
        "mock": bool(mock),
        "retriever": retriever_name,
        "query_file": query,
        "code": code,
        "requested_effective_options": retrieval,
        "principal": principal,
        "corpus": corpus,
        "environment": environment,
        "run_index": run_index,
        "run_order": list(run_order),
        "run_started_iso": started_iso,
        "timing_caveats": list(RUN_CAVEATS),
        "human_review": "not-established",
        "certification": "none: provenance describes how a report was produced; "
                         "it is not a passing certification.",
    }

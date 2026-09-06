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

# Retrieval kwargs the named adapter swallows without effect. The harness
# passes every known kwarg to every backend, so without this map a ripgrep
# or mock report would claim an `expand` difference that never existed.
# Keys are canonical backend names; aliases resolve through
# ``retrievers.canonical_backend_name`` so no alias set is repeated here.
# Live engine backends honour the known kwargs; file/mock/vendor adapters
# honour only what their search() signature actually consumes.
_BACKEND_SWALLOWED = {
    "ripgrep": frozenset({"expand", "use_hyde", "agent_id", "update_access",
                           "budget_tokens", "depth", "include_superseded",
                           "include_rejected", "include_drafts",
                           "include_expired"}),
    "mock": frozenset({"expand", "use_hyde", "agent_id", "update_access",
                        "budget_tokens", "depth", "include_superseded",
                        "include_rejected", "include_drafts",
                        "include_expired"}),
    "raw-context": frozenset({"expand", "use_hyde", "agent_id",
                               "update_access", "depth",
                               "include_superseded", "include_rejected",
                               "include_drafts", "include_expired"}),
    "vendor": frozenset({"expand", "use_hyde", "agent_id", "update_access",
                          "budget_tokens", "depth", "include_superseded",
                          "include_rejected", "include_drafts",
                          "include_expired"}),
    "vendor-memory": frozenset({"expand", "use_hyde", "agent_id",
                                 "update_access", "budget_tokens", "depth",
                                 "include_superseded", "include_rejected",
                                 "include_drafts", "include_expired"}),
    "snapshot": frozenset({"expand", "use_hyde", "agent_id", "update_access",
                           "budget_tokens", "depth", "include_superseded",
                           "include_rejected", "include_drafts",
                           "include_expired", "deadline_monotonic"}),
}

# Harness envelope defaults (limit / update_access) each adapter consumes.
# Anything the adapter does not consume is omitted from `effective`: e.g.
# only the live engine honours update_access, so reporting it as effective
# for ripgrep would invent a compared difference that never existed.
_BACKEND_ENVELOPE = {
    "ripgrep": frozenset({"limit"}),
    "mock": frozenset({"limit"}),
    "raw-context": frozenset({"limit", "budget_tokens"}),
    "vendor": frozenset(),
    "vendor-memory": frozenset(),
    "snapshot": frozenset({"limit"}),
}

def _canonical_backend(retriever_name: str) -> str:
    from .retrievers import canonical_backend_name

    return canonical_backend_name(retriever_name)


def backend_ignored_options(retriever_name: str) -> frozenset:
    """Known kwargs the named backend swallows without effect (may be empty)."""
    return _BACKEND_SWALLOWED.get(_canonical_backend(retriever_name), frozenset())


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


def backend_envelope_options(retriever_name: str, limit: int = 10) -> Dict[str, Any]:
    """Harness envelope defaults the named backend actually consumes.

    Live engine backends consume both limit and update_access; anything
    unlisted for an adapter is omitted from the reported `effective`
    options instead of being repeated as a default the backend never saw.
    """
    honored = _BACKEND_ENVELOPE.get(_canonical_backend(retriever_name))
    if honored is None:
        return {"limit": limit, "update_access": False}
    envelope: Dict[str, Any] = {}
    if "limit" in honored:
        envelope["limit"] = limit
    if "update_access" in honored:
        envelope["update_access"] = False
    return envelope


def retrieval_options_provenance(
    config_name: str,
    config_kwargs: Dict[str, Any],
    known_kwargs: Iterable[str],
    limit: int = 10,
    backend_ignored: Iterable[str] = (),
    backend_envelope: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Record requested config kwargs vs the effective kwargs sent to search().

    ``backend_ignored`` names known kwargs the constructed adapter swallows
    without effect (e.g. ``expand`` for ripgrep/mock). Those move out of
    ``effective`` into ``ignored_by_backend`` so a report cannot claim a
    compared difference that never existed. ``backend_envelope`` is the
    harness envelope the backend actually consumes (see
    ``backend_envelope_options``); when omitted the legacy limit plus
    update_access default applies, so envelope-agnostic callers are unchanged.
    """
    known = set(known_kwargs)
    swallowed = set(backend_ignored)
    requested = dict(config_kwargs)
    effective: Dict[str, Any] = (
        dict(backend_envelope) if backend_envelope is not None
        else {"limit": limit, "update_access": False}
    )
    ignored = sorted(key for key in requested if key not in known)
    ignored_by_backend = sorted(
        key for key in requested if key in known and key in swallowed
    )
    for key, value in requested.items():
        if key in known and key not in swallowed:
            effective[key] = value
    return {
        "config": config_name,
        "requested": requested,
        "effective": effective,
        "ignored_unknown": ignored,
        "ignored_by_backend": ignored_by_backend,
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


def principal_provenance(
    retriever_name: str, *, is_mock: bool, principal: Any = None
) -> Dict[str, Any]:
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
        if principal is None:
            return {
                "supplied": True,
                "backend": "study-snapshot",
                "mock": False,
                "scope": "prepared snapshot vault only (least-privilege study principal)",
                "note": "Snapshot principal was not initialized; no effective authorization context was recorded. "
                        "Packet authorization claims are supplied provenance, not independently verified permission.",
            }
        return {
            "supplied": True,
            "backend": "study-snapshot",
            "mock": False,
            "agent_id": principal.agent_id,
            "workspace_id": principal.workspace_id,
            "transport": principal.transport,
            "capabilities": list(principal.capabilities),
            "allowed_vault_roots": list(principal.allowed_vault_roots),
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


def corpus_provenance(*, is_mock: bool, retriever_name: str = "",
                      snapshot_id: Optional[str] = None,
                      manifest_digest: Optional[str] = None) -> Dict[str, Any]:
    """Corpus/database identity per backend: only what is verifiably available.

    Only the live-engine backends touch a database at all. File baselines
    and placeholders get their own honest labels instead of inheriting the
    live-database description. The snapshot backend fails closed: without a
    verified snapshot ID it is unknown, never frozen.
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
        if not snapshot_id or snapshot_id == "unknown":
            return {
                "snapshot": "unknown",
                "frozen": False,
                "note": "Snapshot backend without a verified snapshot ID: "
                        "no frozen claim is made. A verified prepared "
                        "snapshot reports its manifest-derived ID instead.",
            }
        return {
            "snapshot": snapshot_id,
            "manifest_digest": manifest_digest,
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
def build_gate_provenance(
    *,
    kind: str,
    query: Dict[str, Any],
    code: Dict[str, Any],
    baseline: str,
    candidate: Optional[str],
    decision: Dict[str, Any],
    corpus_snapshot: str,
    mock: bool,
    live_backend_present: bool,
    started_iso: str,
) -> Dict[str, Any]:
    """Provenance for a gate artifact, which is derived evidence, not a search.

    Carries the same query digest / code revision / corpus identity as the
    per-report blocks plus the gate inputs and the recorded decision, so a
    retained ``*-gate.json`` identifies its query digest, revision, corpus
    state, and effective settings when copied independently.
    """
    return {
        "kind": kind,
        "query_file": query,
        "code": code,
        "baseline": baseline,
        "candidate": candidate,
        "metric": decision.get("metric"),
        "min_relative_improvement": decision.get("min_relative_improvement"),
        "decision": {
            "ok": decision.get("ok"),
            "reason": decision.get("reason"),
            "baseline_score": decision.get("baseline_score"),
            "candidate_score": decision.get("candidate_score"),
            "comparable_queries": decision.get("comparable_queries"),
        },
        "corpus_snapshot": corpus_snapshot,
        "mock": bool(mock),
        "live_backend_present": bool(live_backend_present),
        "run_started_iso": started_iso,
        "human_review": "not-established",
        "certification": "none: provenance describes how an artifact was "
                         "produced; it is not a passing certification.",
    }

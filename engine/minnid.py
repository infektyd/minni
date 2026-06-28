#!/usr/bin/env python3
"""minnid — Minni Memory Daemon (Layer 2 IPC Service).

A lightweight daemon that exposes Minni (FAISS + SQLite) over a
Unix domain socket using JSON-RPC 2.0.  Enables local consumers to execute
search, read, and status requests without spawning a new Python interpreter
or reloading heavy MLX / sentence-transformer weights every call.

Features
--------
* Unix domain socket IPC (JSON-RPC 2.0) for sub-millisecond latency.
* Per-agent scoping — every request carries an optional ``agent_id`` tag.
* Hot-reloadable config via SIGHUP.
* Health / status endpoint.
* Graceful shutdown via SIGTERM / SIGINT.

JSON-RPC Methods
----------------
* ``search(query, agent_id?, limit?, depth?, budget_tokens?)`` — Hybrid FAISS + FTS5 search.
  depth: headline | snippet (default) | chunk | document — progressive disclosure tiers.
  budget_tokens: if set, applies MMR-diverse token-budget packing.
* ``expand(result_id, depth?)``          — Re-fetch a result at a deeper depth tier.
* ``read(agent_id?, limit?)``            — Agent startup context (recall).
* ``learn(content, agent_id?, category?)`` — Write a learning.
* ``log_event(event_type, content, agent_id?)`` — Episodic event.
* ``status()``                           — Daemon + engine health.
* ``ping()``                             — Liveness probe.

Usage
-----
    python minnid.py                    # default socket: ~/.minni/run/minnid.sock (0700/0600, SEC-001)
    python minnid.py --socket /path/s   # custom socket
    python minnid.py --port 9900        # HTTP fallback (optional)

Client Quick-Start (Python)
---------------------------
    import os, socket, json
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(os.path.expanduser("~/.minni/run/minnid.sock"))
    def rpc(method, params=None):
        msg = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                          "params": params or {}}) + "\\n"
        s.sendall(msg.encode())
        resp = json.loads(s.recv(1 << 20))
        return resp.get("result")

    rpc("search", {"query": "websocket architecture"})
    rpc("read",   {"agent_id": "minni"})
    rpc("status")
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import hashlib
import signal
import socket
import sys
import time
import importlib.util
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

# ── Logging ───────────────────────────────────────────────────────────────
# Logging setup and the operational metrics counters are centralized in obs.py
# so every engine entry point shares one configured logger and status counters.

logger = logging.getLogger("minnid")

# ── Sovereign engine imports ─────────────────────────────────────────────
# The daemon lives alongside the Sovereign engine so we can import its
# modules directly.  We add the engine directory to sys.path if needed.

_ENGINE_DIR = Path(__file__).resolve().parent
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

import obs                                                  # noqa: E402  # centralized logging + metrics counters
from config import CANONICAL_SOVEREIGN_HOME, DEFAULT_CONFIG, SovereignConfig          # noqa: E402
from db import SovereignDB                                  # noqa: E402
from principal import (                                     # noqa: E402  # G11 EffectivePrincipal + G14 operator gate
    EffectivePrincipal,
    IdentityMismatchError,
    agent_scope_for,
    can_read_document,  # G19
    is_operator_principal,
    make_capability_denied_error,
    make_mismatch_error,
    allows_cross_agent_recall,
    resolve_effective_principal,
    validate_agent_id,
)
from minnid_runtime.dispatch import DispatchContext, dispatch_request  # noqa: E402
from minnid_runtime.governance import (  # noqa: E402
    GovernanceContext,
    ResolveRejected as _RuntimeResolveRejected,
    explicitly_allowed_operator as _runtime_explicitly_allowed_operator,
    extract_assertion as _runtime_extract_assertion,
    handle_learn as _runtime_handle_learn,
    handle_log_event as _runtime_handle_log_event,
    handle_resolve_contradiction as _runtime_handle_resolve_contradiction,
    handle_subscribe_contradictions as _runtime_handle_subscribe_contradictions,
    list_candidates as _runtime_list_candidates,
    resolve_candidate as _runtime_resolve_candidate,
    stage_candidate as _runtime_stage_candidate,
)
from minnid_runtime.handoff import (  # noqa: E402
    AUDIT_DETAIL_BLOCK_MAX as _AUDIT_DETAIL_BLOCK_MAX,
    AUDIT_DETAIL_LINE_MAX as _AUDIT_DETAIL_LINE_MAX,
    AUDIT_SUMMARY_MAX as _AUDIT_SUMMARY_MAX,
    HANDOFF_ACK_STATUSES as _HANDOFF_ACK_STATUSES,
    HANDOFF_KINDS as _HANDOFF_KINDS,
    HandoffContext,
    agent_env_key as _agent_env_key,
    agent_vault as _agent_vault,
    append_handoff_audit as _append_handoff_audit,
    compile_handoff_page as _compile_handoff_page,
    default_agent_vault as _default_agent_vault,
    ensure_handoff_vault as _ensure_handoff_vault,
    escape_audit_details_block as _escape_audit_details_block,
    escape_audit_field as _escape_audit_field,
    handle_ack_handoff as _runtime_handle_ack_handoff,
    handle_await_handoff as _runtime_handle_await_handoff,
    handle_daemon_handoff as _runtime_handle_daemon_handoff,
    handle_list_pending_handoffs as _runtime_handle_list_pending_handoffs,
    handoff_lease_status as _runtime_handoff_lease_status,
    iso_from_epoch as _iso_from_epoch,
    iter_handoff_files as _runtime_iter_handoff_files,
    known_agent_vaults as _known_agent_vaults,
    lease_to_agent as _runtime_lease_to_agent,
    pending_handoff_leases as _runtime_pending_handoff_leases,
    parse_iso_ts as _parse_iso_ts,
    slugify as _slugify,
    store_handoff_lease as _runtime_store_handoff_lease,
    update_handoff_lease_status as _runtime_update_handoff_lease_status,
    validate_handoff_packet as _validate_handoff_packet,
    write_matching_lease_packets as _runtime_write_matching_lease_packets,
    write_json as _write_json,
)
from minnid_runtime.provenance import (  # noqa: E402
    RECOVERY_ALLOWED_METHODS,
    RPC_CAPABILITY_REQUIREMENTS as _RPC_CAPABILITY_REQUIREMENTS,
    ProvenanceResolution,
    enforce_method_capability as _enforce_method_capability,
    guard_vault_root as _guard_vault_root,
    handler_principal as _handler_principal,
    provenance_claim as _provenance_claim,
    recover,
    resolve_provenance,
)
from minnid_runtime.recall import (  # noqa: E402
    RecallContext,
    anchor_for_result as _runtime_anchor_for_result,
    backend_badge as _runtime_backend_badge,
    expand_reference as _runtime_expand_reference,
    full_provenance as _runtime_full_provenance,
    handle_expand as _runtime_handle_expand,
    handle_feedback as _runtime_handle_feedback,
    handle_read as _runtime_handle_read,
    handle_search as _runtime_handle_search,
    handle_sm_drill as _runtime_handle_sm_drill,
    handle_sm_export_pack as _runtime_handle_sm_export_pack,
    handle_trace as _runtime_handle_trace,
    indexed_at_for_result as _runtime_indexed_at_for_result,
    merge_document_results as _runtime_merge_document_results,
    reference_candidates as _runtime_reference_candidates,
    reference_ids_for_engine as _runtime_reference_ids_for_engine,
    reference_matches as _runtime_reference_matches,
    resolve_backend as _runtime_resolve_backend,
    resolve_document_scope as _runtime_resolve_document_scope,
    score_components as _runtime_score_components,
    tag_document_results as _runtime_tag_document_results,
)
from minnid_runtime.redaction import redact_text as _redact_text, redact_value as _redact_value  # noqa: E402
from minnid_runtime.rpc import make_error as _make_error, make_response as _make_response  # noqa: E402
from minnid_runtime.transport import SOCKET_BODY_LIMIT as _SOCKET_BODY_LIMIT, parse_request as _parse_request  # noqa: E402

# ── Lazy imports (heavy ML deps) ──────────────────────────────────────────
_retrieval = None
_vault_retrieval_cache = {}
_episodic = None
_writeback = None


def _reload_runtime_config(signum=None, frame=None) -> None:
    """Clear identity and per-vault caches after operator config changes."""
    agent_scope_for.cache_clear()
    _vault_retrieval_cache.clear()
    logger.info("SIGHUP received — cleared identity/runtime caches")


def _lazy_retrieval():
    """Lazy-load RetrievalEngine to defer MLX weight loading."""
    global _retrieval
    if _retrieval is None:
        from retrieval import RetrievalEngine
        _retrieval = RetrievalEngine(SovereignDB(DEFAULT_CONFIG), DEFAULT_CONFIG)
    return _retrieval


def _vault_agent_id(vault_path: Path) -> Optional[str]:
    try:
        from afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID

        name = vault_path.name
        if not name.endswith("-vault"):
            return None
        agent_id = _VAULT_SLUG_TO_AGENT_ID.get(name[: -len("-vault")])
        return validate_agent_id(agent_id) if agent_id else None
    except Exception:
        return None


def _vault_index_ready(vault_path: Path) -> bool:
    try:
        from vault_index import vault_index_paths

        return vault_index_paths(vault_path).db_path.exists()
    except Exception:
        return False


def _lazy_vault_retrieval(vault_path: Path):
    """Return (RetrievalEngine, source_agent, db_path) for an indexed vault."""
    try:
        vault = Path(vault_path).expanduser().resolve()
    except Exception:
        vault = Path(vault_path).expanduser()
    if not _vault_index_ready(vault):
        return None
    agent_id = _vault_agent_id(vault)
    if not agent_id:
        return None

    key = str(vault)
    cached = _vault_retrieval_cache.get(key)
    if cached is not None:
        return cached

    from faiss_index import FAISSIndex
    from retrieval import RetrievalEngine
    from vault_index import build_vault_index_config

    cfg = build_vault_index_config(vault, base_config=DEFAULT_CONFIG)
    db = SovereignDB(cfg)
    engine = RetrievalEngine(db, cfg, faiss_index=FAISSIndex(cfg))
    cached = (engine, agent_id, cfg.db_path)
    _vault_retrieval_cache[key] = cached
    return cached


def _agent_vault_retrieval(agent_id: str):
    vault_path, _ = _agent_vault(agent_id)
    return _lazy_vault_retrieval(vault_path)


def _all_vault_retrievals() -> list:
    out = []
    seen = set()
    for vault_path in _known_agent_vaults():
        cached = _lazy_vault_retrieval(vault_path)
        if cached is None:
            continue
        key = cached[2]
        if key in seen:
            continue
        out.append(cached)
        seen.add(key)
    return out


def _tag_document_results(results: list, *, src: str) -> list:
    return _runtime_tag_document_results(results, src=src)


def _result_identity(row: dict) -> tuple:
    from minnid_runtime.recall import result_identity

    return result_identity(row)


def _merge_document_results(result_sets: list, limit: int, *, prefer_personal: bool = False) -> list:
    return _runtime_merge_document_results(
        result_sets,
        limit,
        prefer_personal=prefer_personal,
    )


def _resolve_document_scope(params: dict) -> str:
    return _runtime_resolve_document_scope(params)


def _lazy_writeback():
    """Lazy-load WriteBackMemory."""
    global _writeback
    if _writeback is None:
        from writeback import WriteBackMemory
        _writeback = WriteBackMemory(SovereignDB(), DEFAULT_CONFIG)
    return _writeback


def _durable_doc_path(
    agent_id: str, key: str, vault_path: Optional[str] = None,
    content: Optional[str] = None,
) -> str:
    """Stable synthetic ``documents.path`` for a store-time-indexed learning.

    Keyed on (agent_id, CONTENT) — NOT the per-store ``key``/learning_id — so
    re-storing the SAME content upserts the SAME documents row (idempotent — no
    duplicate chunk_embeddings). This matters because _resolve_candidate enforces
    terminal once-only resolution: a re-store of identical content always goes
    through a NEW candidate -> NEW learning_id, so keying on learning_id would
    mint a fresh path each time and accumulate duplicate semantic-index rows.
    Hashing the content collapses those to one upsert target. When ``content`` is
    not supplied we fall back to the legacy ``key`` digest (callers that already
    pass a content-stable key, or have no content to hash).

    Rooted under the configured vault_path so the path is contained in an
    operator/agent vault root and passes can_read_document's vault-root check
    (G19) for the storing principal. The file is virtual (we never write it to
    disk here — the markdown writeback is handled separately); the path is only
    a stable identity for the documents/chunk_embeddings rows.
    """
    import hashlib
    seed = content if content is not None else key
    digest = hashlib.sha1(f"{agent_id}\x00{seed}".encode("utf-8")).hexdigest()[:16]
    base = vault_path or DEFAULT_CONFIG.vault_path
    return os.path.join(base, "_durable", f"{agent_id}__{digest}.md")


_UNSET = object()


def _index_durable_learning(agent_id: str, content: str, key: str, db=_UNSET) -> None:
    """Semantically index a just-stored durable learning (FAIL-OPEN).

    Hook for BOTH durable-store socket paths (_resolve_candidate(accept) and
    _handle_learn force=true). It chunks+embeds the content into the SAME
    semantic index (documents + chunk_embeddings + vault_fts) the out-of-band
    VaultIndexer writes, and refreshes the live RetrievalEngine's in-memory
    FAISS so a subsequent search in THIS process returns the new chunks without
    an index_all run or restart.

    DB BINDING (data-safety): the semantic index MUST land in the SAME database
    the durable store just committed to — never a separately-resolved one. The
    caller passes the store's ``db`` handle; we index there. CRITICALLY, we only
    reuse the shared _lazy_retrieval() singleton (whose FAISS _handle_search
    reads) when its db_path matches the store's — the normal production case
    where both are DEFAULT_CONFIG. If they differ (e.g. a test that points the
    store at a temp DB but leaves DEFAULT_CONFIG at the live home), we build a
    TRANSIENT engine bound to the store's DB so the write goes to the right
    place and the live singleton/DB is never touched. Without this guard a
    db-divergent caller would write durable rows into DEFAULT_CONFIG's DB —
    i.e. the operator's LIVE ~/.minni — which must never happen.

    Never raises — neither a programmer error (omitted ``db=``) nor an
    availability failure (embedder/FAISS down) may undo or fail a durable store
    that already committed. Both degrade gracefully (log + return).
    """
    try:
        # Data-safety: ``db`` MUST be supplied explicitly. A ``None``/omitted
        # default would silently fall through to the live-DB singleton
        # (DEFAULT_CONFIG.db_path — the operator's ~/.minni), so a future caller
        # forgetting ``db=`` in a live context would write durable
        # semantic-index rows into the operator's real database. We refuse to
        # default to the live DB — but we degrade gracefully rather than raise,
        # because the durable store has ALREADY committed by the time we run:
        # raising here would surface a failure to the RPC client for a memory
        # that was in fact persisted (and could trigger a duplicate-write
        # retry). So we log and return without indexing; recall degrades to
        # lexical-only until the next out-of-band index run.
        if db is _UNSET or db is None:
            logger.warning(
                "durable store: _index_durable_learning called without an "
                "explicit db= (the handle the durable store committed to) — "
                "refusing to default to the live DEFAULT_CONFIG database; "
                "skipping store-time semantic index (recall degraded to "
                "lexical until reindex). agent=%s",
                agent_id,
            )
            return

        from retrieval import RetrievalEngine

        try:
            store_db_path = os.path.abspath(db.config.db_path)
        except Exception:
            store_db_path = None
        default_db_path = os.path.abspath(DEFAULT_CONFIG.db_path)

        if store_db_path is not None and store_db_path == default_db_path:
            # Production path: index via the shared singleton so its in-memory
            # FAISS refreshes for the next _handle_search in this process.
            engine = _lazy_retrieval()
        else:
            # Divergent-DB caller: bind a transient engine to the STORE's DB so
            # the durable rows land there and the live DEFAULT_CONFIG DB is left
            # untouched (data-safety). No shared FAISS to refresh in this case.
            engine = RetrievalEngine(db, db.config)

        # Derive indexing metadata from YAML frontmatter (M-3 privacy bridge).
        # Reuse VaultIndexer._extract_frontmatter so durable-store indexing
        # honors the same privacy floor as out-of-band vault indexing.
        doc_agent = agent_id
        sigil = "❓"
        page_status = "accepted"
        privacy_level = "safe"
        page_type = None
        layer = "knowledge"
        try:
            from indexer import VaultIndexer

            meta = VaultIndexer._extract_frontmatter(content)
            doc_agent = agent_id  # server-stamped ownership; never trust frontmatter agent
            sigil = meta.get("sigil", "❓")
            page_status = (
                meta["page_status"]
                if meta["page_status"] != "candidate"
                else "accepted"
            )
            privacy_level = meta["privacy_level"]
            page_type = meta.get("page_type")
            layer = meta["layer"]
        except Exception:
            pass  # fail-open: keep prior defaults

        engine.index_durable_document(
            content=content,
            path=_durable_doc_path(
                agent_id, key, vault_path=engine.config.vault_path,
                content=content,
            ),
            agent=doc_agent,
            sigil=sigil,
            page_status=page_status,
            privacy_level=privacy_level,
            page_type=page_type,
            layer=layer,
        )
    except Exception as exc:
        logger.warning(
            "durable store: store-time semantic index failed for agent=%s (%s) "
            "— store stands, recall degraded to lexical until reindex",
            agent_id, exc,
        )


def _lazy_episodic():
    """Lazy-load EpisodicMemory."""
    global _episodic
    if _episodic is None:
        from episodic import EpisodicMemory
        _episodic = EpisodicMemory(SovereignDB(), DEFAULT_CONFIG)
    return _episodic


def _trace_ring():
    """Load engine/trace.py without colliding with Python's stdlib trace module."""
    module_name = "_sovereign_trace"
    if module_name in sys.modules:
        return sys.modules[module_name].GLOBAL_TRACE_RING
    trace_path = _ENGINE_DIR / "trace.py"
    spec = importlib.util.spec_from_file_location(module_name, trace_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load trace module from {trace_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.GLOBAL_TRACE_RING

# ── JSON-RPC 2.0 server ──────────────────────────────────────────────────

VERSION = "0.1.0"
_start_time = 0.0
_request_count = 0
_LATENCY_METHODS = ("search", "learn", "read", "embedding", "cross_encoder", "afm")
_latencies = {name: deque(maxlen=100) for name in _LATENCY_METHODS}


def _increment_handoff_request_count() -> None:
    global _request_count
    _request_count += 1


def _handoff_context() -> HandoffContext:
    return HandoffContext(
        make_error=_make_error,
        make_response=_make_response,
        handler_principal=_handler_principal,
        lazy_writeback=_lazy_writeback,
        increment_request_count=_increment_handoff_request_count,
        store_handoff_lease=_store_handoff_lease,
        logger=logger,
    )


def _handle_daemon_handoff(params: dict, request_id: Any) -> dict:
    return _runtime_handle_daemon_handoff(params, request_id, _handoff_context())


def _iter_handoff_files(agent_id: Optional[str] = None):
    yield from _runtime_iter_handoff_files(agent_id, context=_handoff_context())


def _write_matching_lease_packets(lease_id: str, updates: dict) -> list[str]:
    return _runtime_write_matching_lease_packets(lease_id, updates, context=_handoff_context())


def _store_handoff_lease(packet: dict, inbox_path: Path, outbox_path: Path) -> bool:
    return _runtime_store_handoff_lease(packet, inbox_path, outbox_path, _handoff_context())


def _update_handoff_lease_status(
    lease_id: str,
    status: str,
    contradicts_id: Any = None,
) -> bool:
    return _runtime_update_handoff_lease_status(
        lease_id,
        status,
        contradicts_id,
        context=_handoff_context(),
    )


def _pending_handoff_leases(agent_id: str) -> list[dict]:
    return _runtime_pending_handoff_leases(agent_id, context=_handoff_context())


def _handoff_lease_status(lease_id: str) -> Optional[dict]:
    return _runtime_handoff_lease_status(lease_id, context=_handoff_context())


def _lease_to_agent(lease_id: str) -> Optional[str]:
    return _runtime_lease_to_agent(lease_id, context=_handoff_context())


def _handle_ack_handoff(params: dict, request_id: Any) -> dict:
    return _runtime_handle_ack_handoff(params, request_id, _handoff_context())


def _handle_list_pending_handoffs(params: dict, request_id: Any) -> dict:
    return _runtime_handle_list_pending_handoffs(params, request_id, _handoff_context())


async def _handle_await_handoff(params: dict, request_id: Any) -> dict:
    return await _runtime_handle_await_handoff(params, request_id, _handoff_context())


def _record_latency(method: str, duration_seconds: float) -> None:
    """Record a duration in a tiny process-local rolling window."""
    if method not in _latencies:
        _latencies[method] = deque(maxlen=100)
    _latencies[method].append(max(0.0, float(duration_seconds)))


def _percentile(values, percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _latency_snapshot() -> Dict[str, Dict[str, float]]:
    snapshot = {}
    for name in _LATENCY_METHODS:
        values = list(_latencies.get(name, []))
        snapshot[name] = {
            "count": len(values),
            "p50_ms": round(_percentile(values, 0.50) * 1000, 3),
            "p95_ms": round(_percentile(values, 0.95) * 1000, 3),
        }
    return snapshot


def _backend_badge(backends: Any) -> str:
    return _runtime_backend_badge(backends)


def formatRecall(query: str, response: Dict[str, Any]) -> str:
    """Python-side recall formatting with backend provenance badge."""
    backend = response.get("backend") or response.get("backend_badge")
    badge = f" [{backend}]" if backend else ""
    results = response.get("results") or "No recall results."
    if not isinstance(results, str):
        results = json.dumps(results, indent=2)
    return "\n\n".join([
        "# Sovereign Recall",
        f"Query: {query}{badge}",
        "## Daemon Results",
        results,
    ])


def _handle_ping(params: dict, request_id: Any) -> dict:
    return _make_response("pong", request_id)


def _handle_gate_shared(params: dict, request_id: Any) -> dict:
    """Authorize a shared operation through the daemon provenance gate.

    The gate itself is resolved in _dispatch before this handler runs. This
    method gives plugin-local shared flows a daemon checkpoint without moving
    personal vault mirrors into the daemon.
    """
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    operation = str(params.get("operation") or "").strip()
    if not operation:
        return _make_error(-32602, "operation is required", request_id)
    # Fail-loud on an unknown/unauthorized identity: a default-deny principal
    # (no capabilities AND no vault roots) must NOT receive a bare status:ok —
    # that would read as authorization when the gate only attributed. Surface
    # the recovery route instead so the caller re-establishes identity. Capable
    # principals (the plugin always calls as DEFAULT_AGENT_ID, and registered
    # platform agents) are unaffected. (B2 narrow-harden.)
    if not principal.capabilities and not principal.allowed_vault_roots:
        return _make_response(
            recover(
                "unknown_identity",
                {"method": "gate.shared", "supplied_agent_id": params.get("agent_id")},
                render_mode="machine",
            ),
            request_id,
        )
    return _make_response(
        {
            "status": "ok",
            "operation": operation,
            "principal": principal.agent_id,
            "workspace_id": principal.workspace_id,
            "gate": "minnid",
        },
        request_id,
    )


def _resolve_backend(backend_param, config=None):
    return _runtime_resolve_backend(backend_param, config)


def _increment_recall_request_count() -> None:
    global _request_count
    _request_count += 1


def _recall_context() -> RecallContext:
    return RecallContext(
        make_error=_make_error,
        make_response=_make_response,
        handler_principal=_handler_principal,
        lazy_retrieval=_lazy_retrieval,
        agent_vault_retrieval=_agent_vault_retrieval,
        all_vault_retrievals=_all_vault_retrievals,
        trace_ring=_trace_ring,
        record_latency=_record_latency,
        increment_request_count=_increment_recall_request_count,
        sovereign_db=SovereignDB,
        logger=logger,
    )


def _handle_search(params: dict, request_id: Any) -> dict:
    return _runtime_handle_search(params, request_id, _recall_context())


def _handle_feedback(params: dict, request_id: Any) -> dict:
    return _runtime_handle_feedback(params, request_id, _recall_context())


def _handle_trace(params: dict, request_id: Any) -> dict:
    return _runtime_handle_trace(params, request_id, _recall_context())


def _handle_expand(params: dict, request_id: Any) -> dict:
    return _runtime_handle_expand(params, request_id, _recall_context())


def _indexed_at_for_result(retrieval_engine, result: dict) -> Optional[float]:
    return _runtime_indexed_at_for_result(retrieval_engine, result)


def _score_components(reference: dict, result: dict) -> dict:
    return _runtime_score_components(reference, result)


def _full_provenance(
    *,
    retrieval_engine,
    source_agent: str,
    source_vault: str,
    index_db_path: str,
    reference: dict,
    result: dict,
) -> dict:
    return _runtime_full_provenance(
        retrieval_engine=retrieval_engine,
        source_agent=source_agent,
        source_vault=source_vault,
        index_db_path=index_db_path,
        reference=reference,
        result=result,
    )


def _reference_candidates(reference: dict, principal, agent_id: Optional[str], shared_engine) -> list:
    return _runtime_reference_candidates(
        reference,
        principal,
        agent_id,
        shared_engine,
        _recall_context(),
    )


def _reference_matches(result: dict, reference: dict) -> bool:
    return _runtime_reference_matches(result, reference)


def _reference_ids_for_engine(reference: dict, retrieval_engine) -> list[int]:
    return _runtime_reference_ids_for_engine(reference, retrieval_engine)


def _expand_reference(
    reference: dict,
    *,
    depth: str,
    principal,
    agent_id: Optional[str],
    shared_engine,
) -> Optional[dict]:
    return _runtime_expand_reference(
        reference,
        depth=depth,
        principal=principal,
        agent_id=agent_id,
        shared_engine=shared_engine,
        context=_recall_context(),
    )


def _handle_sm_drill(params: dict, request_id: Any) -> dict:
    return _runtime_handle_sm_drill(params, request_id, _recall_context())


def _anchor_for_result(result: dict) -> str:
    return _runtime_anchor_for_result(result)


def _handle_sm_export_pack(params: dict, request_id: Any) -> dict:
    return _runtime_handle_sm_export_pack(params, request_id, _recall_context())


def _handle_read(params: dict, request_id: Any) -> dict:
    return _runtime_handle_read(params, request_id, _recall_context())


def _increment_governance_request_count() -> None:
    global _request_count
    _request_count += 1


def _governance_context() -> GovernanceContext:
    return GovernanceContext(
        make_error=_make_error,
        make_response=_make_response,
        handler_principal=_handler_principal,
        lazy_writeback=_lazy_writeback,
        lazy_episodic=_lazy_episodic,
        record_latency=_record_latency,
        index_durable_learning=_index_durable_learning,
        maybe_archive_inbox_source=_maybe_archive_inbox_source,
        increment_request_count=_increment_governance_request_count,
        sovereign_db=SovereignDB,
        logger=logger,
    )


def _handle_learn(params: dict, request_id: Any) -> dict:
    return _runtime_handle_learn(params, request_id, _governance_context())


def _extract_assertion(content: str) -> str:
    return _runtime_extract_assertion(content)


_ResolveRejected = _RuntimeResolveRejected


def _handle_resolve_contradiction(params: dict, request_id: Any) -> dict:
    return _runtime_handle_resolve_contradiction(params, request_id, _governance_context())


def _handle_subscribe_contradictions(params: dict, request_id: Any) -> dict:
    return _runtime_handle_subscribe_contradictions(params, request_id, _governance_context())


def _handle_log_event(params: dict, request_id: Any) -> dict:
    return _runtime_handle_log_event(params, request_id, _governance_context())


def _handle_status(params: dict, request_id: Any) -> dict:
    """Return daemon and engine status."""
    global _request_count
    _request_count += 1

    vault_path = params.get("vault") or params.get("vault_path") or DEFAULT_CONFIG.vault_path
    err = _guard_vault_root(params, vault_path, request_id, label="status")
    if err:
        return err

    # Calculate audit volume
    audit_vol = 0
    try:
        vp = Path(vault_path)
        if vp.is_dir():
            for p in vp.glob("log*.md"):
                try:
                    audit_vol += p.stat().st_size
                except OSError:
                    pass
            logs_dir = vp / "logs"
            if logs_dir.is_dir():
                for p in logs_dir.glob("*.md"):
                    try:
                        audit_vol += p.stat().st_size
                    except OSError:
                        pass
    except Exception:
        pass

    db_ok = False
    db_stats = {}
    try:
        db = SovereignDB()
        with db.cursor() as c:
            c.execute("SELECT COUNT(*) as n FROM documents")
            db_stats["documents"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM chunk_embeddings")
            db_stats["chunks"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM learnings")
            db_stats["learnings"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM episodic_events")
            db_stats["events"] = c.fetchone()["n"]
        db.close()
        db_ok = True
    except Exception:
        pass

    faiss_path, faiss_ok = _faiss_cache_status(DEFAULT_CONFIG)
    try:
        from afm_provider import afm_runtime_status
        afm_status = afm_runtime_status()
    except Exception as exc:
        afm_status = {
            "mode": "unknown",
            "status": "degraded",
            "native_available": False,
            "error": str(exc),
        }

    uptime = time.time() - _start_time
    return _make_response({
        "daemon": {
            "version": VERSION,
            "uptime_seconds": round(uptime, 1),
            "requests_served": _request_count,
            "socket_path": "[redacted]",
            "latencies": _latency_snapshot(),
            "errors": obs.metrics_snapshot().get("errors", 0),
            "counters": obs.metrics_snapshot(),
        },
        "engine": {
            "db_ok": db_ok,
            "db_path": "[redacted]",
            "faiss_ok": faiss_ok,
            "faiss_path": "[redacted]",
            "stats": db_stats,
            "audit_volume": audit_vol,
        },
        "afm": afm_status,
    }, request_id)


def _faiss_cache_status(config=DEFAULT_CONFIG) -> tuple[Path, bool]:
    legacy_path = Path(config.faiss_index_path)
    if legacy_path.exists():
        return legacy_path, legacy_path.stat().st_size > 0

    try:
        from faiss_persist import _faiss_dir_for_db

        faiss_dir = Path(_faiss_dir_for_db(config.db_path))
        manifest_path = faiss_dir / "index.manifest.json"
        faiss_path = faiss_dir / "index.faiss"
        npz_path = faiss_dir / "index.faiss.npz"
        if manifest_path.exists():
            for candidate in (faiss_path, npz_path):
                if candidate.exists() and candidate.stat().st_size > 0:
                    return candidate, True
            return faiss_path, False
    except Exception:
        pass

    return legacy_path, False


def _faiss_cache_age_seconds(config=DEFAULT_CONFIG) -> Optional[float]:
    path, ok = _faiss_cache_status(config)
    if not ok:
        return None
    return round(max(0.0, time.time() - path.stat().st_mtime), 3)


def _handle_health_report(params: dict, request_id: Any) -> dict:
    """Return deeper read-only memory health diagnostics."""
    now = time.time()
    stale_cutoff = now - (30 * 24 * 60 * 60)
    report = {
        "stale_docs": [],
        "never_recalled": [],
        "contradicting_learnings": [],
        "vector_backend_lag": [],
        "faiss_cache_age_seconds": _faiss_cache_age_seconds(DEFAULT_CONFIG),
        "afm_loop": {
            "last_run_per_pass": {},
            "drafts_pending": 0,
            "drafts_pending_oldest": None,
            "afm_latency_p95": 0.0,
            "status": "disabled" if not _afm_loop_enabled(DEFAULT_CONFIG) else "ok",
        },
    }

    try:
        try:
            from afm_writer import writer_status
            report["afm_loop"] = writer_status(DEFAULT_CONFIG.vault_path)
            if not _afm_loop_enabled(DEFAULT_CONFIG):
                report["afm_loop"]["status"] = "disabled"
        except Exception as exc:
            report["afm_loop"]["status"] = "degraded"
            report["afm_loop"]["error"] = str(exc)

        db = SovereignDB(DEFAULT_CONFIG)
        with db.cursor() as c:
            c.execute(
                """
                SELECT doc_id, path, indexed_at, last_modified
                FROM documents
                WHERE COALESCE(indexed_at, last_modified, 0) < ?
                ORDER BY COALESCE(indexed_at, last_modified, 0) ASC
                LIMIT 25
                """,
                (stale_cutoff,),
            )
            for row in c.fetchall():
                ts = row["indexed_at"] or row["last_modified"] or 0
                report["stale_docs"].append({
                    "doc_id": row["doc_id"],
                    "path": row["path"],
                    "age_days": round((now - ts) / 86400, 1) if ts else None,
                })

            c.execute(
                """
                SELECT doc_id, path
                FROM documents
                WHERE COALESCE(access_count, 0) = 0
                ORDER BY indexed_at DESC NULLS LAST
                LIMIT 25
                """
            )
            report["never_recalled"] = [
                {"doc_id": row["doc_id"], "path": row["path"]}
                for row in c.fetchall()
            ]

            c.execute(
                """
                SELECT learning_id, agent_id, content, contradicts_id, status
                FROM learnings
                WHERE contradicts_id IS NOT NULL OR status = 'contradiction'
                ORDER BY created_at DESC
                LIMIT 25
                """
            )
            report["contradicting_learnings"] = [
                {
                    "learning_id": row["learning_id"],
                    "agent_id": row["agent_id"],
                    "content": (row["content"] or "")[:160],
                    "contradicts_id": row["contradicts_id"],
                    "status": row["status"],
                }
                for row in c.fetchall()
            ]

            try:
                c.execute(
                    "SELECT COALESCE(MAX(chunk_id), 0) AS max_rowid, "
                    "COUNT(*) AS n FROM chunk_embeddings"
                )
                chunk_state = c.fetchone()
                max_rowid = int(chunk_state["max_rowid"] or 0)
                c.execute(
                    """
                    SELECT name, status, last_synced_chunk_rowid, last_synced_at, vector_count
                    FROM vector_backends
                    ORDER BY name
                    """
                )
                for row in c.fetchall():
                    lag = max(0, max_rowid - int(row["last_synced_chunk_rowid"] or 0))
                    if lag or row["status"] not in ("ok", "empty"):
                        report["vector_backend_lag"].append({
                            "name": row["name"],
                            "status": row["status"],
                            "lag_chunks": lag,
                            "last_synced_at": row["last_synced_at"],
                            "vector_count": row["vector_count"],
                        })
            except Exception as exc:
                report["vector_backend_lag"].append({"status": "unknown", "error": str(exc)})
        db.close()
    except Exception as exc:
        logger.warning("health_report degraded: %s", exc)
        report["error"] = str(exc)

    return _make_response(report, request_id)


def _handle_hygiene_report(params: dict, request_id: Any) -> dict:
    """Run read-only vault/wiki hygiene checks and return JSON summary."""
    # G12: enforce stamped principal's allowed_vault_roots on any supplied vault (realpath checked)
    vault_path = params.get("vault") or params.get("vault_path") or DEFAULT_CONFIG.vault_path
    err = _guard_vault_root(params, vault_path, request_id, label="hygiene")
    if err:
        return err
    try:
        from hygiene import run_hygiene_report
        summary = run_hygiene_report(Path(vault_path))
        return _make_response(summary, request_id)
    except Exception as exc:
        logger.warning("hygiene_report degraded: %s", exc)
        return _make_response({
            "status": "degraded",
            "vault": str(vault_path),
            "counts": {"block": 1, "warn": 0, "info": 0},
            "findings": {
                "block": [{
                    "check": "hygiene_report",
                    "path": str(vault_path),
                    "message": str(exc),
                }],
                "warn": [],
                "info": [],
            },
            "report_path": None,
        }, request_id)


def _afm_loop_enabled(config=DEFAULT_CONFIG) -> bool:
    if os.environ.get("MINNI_AFM_LOOP", "").lower() == "off":
        return False
    return bool((getattr(config, "afm_loop_schedule", {}) or {}).get("enabled"))


# ─────────────────────────────────────────────────────────────────────────────
# Consolidation: durable promotion of proposed candidates (AFM loop authority).
# Mirrors the force=true durable-learn path (embed + INSERT learnings) and stamps
# consolidation_actions for audit. The privileged write lives here, not in passes.
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_archive_inbox_source(db, candidate_id: int) -> None:
    """B1 drain-on-resolution (audit C2): once a candidate sourced from a vault
    inbox file reaches a terminal state, move the source file into
    ``<inbox>/.archive/`` so the file channel actually drains instead of
    re-surfacing resolved candidates forever. Best-effort; never deletes
    (rename only); never raises into the resolution path."""
    try:
        from afm_passes.inbox_archive import maybe_archive_for_candidate
        maybe_archive_for_candidate(db, DEFAULT_CONFIG, candidate_id)
    except Exception:
        logger.exception(
            "inbox archive for candidate %s failed (non-fatal)", candidate_id
        )


def _promote_candidate_durable(candidate_id: int, reason: str = "afm-consolidation"):
    """Promote one proposed candidate into a durable, embedded learning.

    Returns the new learning_id, or None if the candidate is missing or no longer
    'proposed' (idempotent — safe to call twice). Embedding is best-effort.
    """
    import time as _time
    import numpy as _np

    wb = _lazy_writeback()
    # Read + embed OUTSIDE the write txn to keep the write lock short.
    with wb.db.cursor() as c:
        c.execute("SELECT * FROM candidate_packets WHERE candidate_id=?", (candidate_id,))
        row = c.fetchone()
    if not row:
        return None
    cand = dict(row)
    if cand.get("status") != "proposed":
        return None
    content = cand.get("content") or ""
    agent_id = cand.get("principal") or "afm-loop"
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        logger.warning(
            "_promote_candidate_durable: invalid agent_id %r (%s) — skipping",
            agent_id,
            exc,
        )
        return None
    from afm_passes.consolidation import content_hash as _content_hash
    chash = _content_hash(content)

    emb_bytes = None
    if getattr(wb, "model", None):
        try:
            emb = wb.model.encode(content).astype(_np.float32)
            emb_bytes = emb.tobytes()
        except Exception as exc:
            logger.warning("consolidation: embedding failed (%s) — storing without", exc)

    now = _time.time()
    with wb.db.transaction() as c:
        # Re-check status inside the txn (closes TOCTOU with a concurrent resolve).
        c.execute("SELECT status FROM candidate_packets WHERE candidate_id=?", (candidate_id,))
        chk = c.fetchone()
        if not chk or chk["status"] != "proposed":
            return None
        c.execute(
            """
            INSERT INTO learnings
            (agent_id, category, content, source_doc_ids, source_query,
             confidence, embedding, created_at,
             assertion, applies_when, evidence_doc_ids, contradicts_id, status,
             content_hash)
            VALUES (?, 'general', ?, NULL, 'afm-consolidation',
                    ?, ?, ?, NULL, NULL, ?, NULL, 'active', ?)
            """,
            (agent_id, content, 0.9, emb_bytes, now, cand.get("evidence_refs"), chash),
        )
        lid = c.lastrowid
        c.execute(
            """
            UPDATE candidate_packets
            SET status='accepted', resolved_at=?, resolved_by='afm-consolidation',
                resolution_reason=?
            WHERE candidate_id=?
            """,
            (now, reason[:500], candidate_id),
        )
        c.execute(
            """
            INSERT INTO consolidation_actions
            (action_type, target_learning_id, claim, category, confidence,
             status, detail, created_at)
            VALUES ('afm_promote', ?, ?, 'general', ?, 'applied', ?, ?)
            """,
            (lid, content[:200], 0.9, reason[:500], now),
        )

    # Best-effort Obsidian markdown parity (never fatal).
    try:
        if wb.config.writeback_enabled:
            wb._write_to_disk(lid, agent_id, "general", content, now)
    except Exception as exc:
        logger.warning("consolidation: write_to_disk failed for learning #%s (%s)", lid, exc)

    logger.info("AFM consolidation promoted candidate #%d -> learning #%d", candidate_id, lid)
    _maybe_archive_inbox_source(wb.db, candidate_id)
    return lid


def _reject_candidate_dedup(candidate_id: int) -> bool:
    """Mark a proposed candidate rejected as a duplicate. Returns True if changed."""
    import time as _time
    wb = _lazy_writeback()
    now = _time.time()
    with wb.db.transaction() as c:
        c.execute("SELECT status FROM candidate_packets WHERE candidate_id=?", (candidate_id,))
        row = c.fetchone()
        if not row or row["status"] != "proposed":
            return False
        c.execute(
            """
            UPDATE candidate_packets
            SET status='rejected', resolved_at=?, resolved_by='afm-consolidation',
                resolution_reason='duplicate of existing learning'
            WHERE candidate_id=?
            """,
            (now, candidate_id),
        )
        c.execute(
            """
            INSERT INTO consolidation_actions
            (action_type, claim, category, status, detail, created_at)
            VALUES ('afm_dedup', ?, 'general', 'applied', 'duplicate of existing learning', ?)
            """,
            (str(candidate_id), now),
        )
    logger.info("AFM consolidation deduped candidate #%d (rejected)", candidate_id)
    _maybe_archive_inbox_source(wb.db, candidate_id)
    return True


def _mark_candidate_review(candidate_id: int, reason: str = "afm-consolidation review") -> bool:
    """Flag a proposed candidate for operator review without changing its status
    (the schema CHECK only allows proposed/accepted/rejected/redacted/expired/
    merged/superseded). The 'afm_review' marker makes the consolidation pass skip
    it on future runs — so the drain loop progresses — while it stays 'proposed'
    and fully resolvable by the operator. Idempotent. Returns True if newly flagged.
    """
    import time as _time
    wb = _lazy_writeback()
    now = _time.time()
    with wb.db.transaction() as c:
        c.execute("SELECT status FROM candidate_packets WHERE candidate_id=?", (candidate_id,))
        row = c.fetchone()
        if not row or row["status"] != "proposed":
            return False
        c.execute(
            "SELECT 1 FROM consolidation_actions WHERE action_type='afm_review' AND claim=? LIMIT 1",
            (str(candidate_id),),
        )
        if c.fetchone():
            return False  # already flagged
        c.execute(
            """
            INSERT INTO consolidation_actions
            (action_type, claim, category, status, detail, created_at)
            VALUES ('afm_review', ?, 'general', 'pending', ?, ?)
            """,
            (str(candidate_id), reason[:500], now),
        )
    return True


def _apply_consolidation_result(result: dict) -> None:
    """Apply a consolidation pass result: durable promotes, dedup rejections,
    and review routing. Every branch moves the candidate OUT of 'proposed'."""
    promoted = deduped = reviewed = 0
    for cid in (result.get("promote_candidate_ids") or []):
        try:
            if _promote_candidate_durable(int(cid)):
                promoted += 1
        except Exception:
            logger.exception("consolidation: promote candidate %s failed", cid)
    for cid in (result.get("dedup_candidate_ids") or []):
        try:
            if _reject_candidate_dedup(int(cid)):
                deduped += 1
        except Exception:
            logger.exception("consolidation: dedup candidate %s failed", cid)
    for cid in (result.get("review_candidate_ids") or []):
        try:
            if _mark_candidate_review(int(cid)):
                reviewed += 1
        except Exception:
            logger.exception("consolidation: review-mark candidate %s failed", cid)
    if promoted or deduped or reviewed:
        logger.info(
            "AFM consolidation applied: promoted=%d deduped=%d reviewed=%d",
            promoted, deduped, reviewed,
        )
    result["applied"] = {"promoted": promoted, "deduped": deduped, "reviewed": reviewed}


def _handle_daemon_compile(params: dict, request_id: Any) -> dict:
    """Run an AFM compile pass. Defaults to dry-run and never auto-accepts."""
    global _request_count
    _request_count += 1
    started_at = time.perf_counter()

    pass_name = str(params.get("pass_name") or "").strip()
    if not pass_name:
        return _make_error(-32602, "pass_name is required", request_id)
    dry_run = bool(params.get("dry_run", True))
    vault_path = str(params.get("vault_path") or DEFAULT_CONFIG.vault_path)

    # G12: vault root binding before any AFM pass or submit_drafts (denies traversal/symlink)
    err = _guard_vault_root(params, vault_path, request_id, label="compile")
    if err:
        return err

    if not _afm_loop_enabled(DEFAULT_CONFIG):
        return _make_response({
            "status": "afm_unavailable",
            "reason": "AFM loop disabled",
            "pass_name": pass_name,
            "dry_run": dry_run,
            "drafts": [],
            "drafts_written": [],
        }, request_id)

    try:
        pass_runners = {
            "session_distillation": "afm_passes.session_distillation",
            "synthesis": "afm_passes.synthesis",
            "procedure_extraction": "afm_passes.procedure_extraction",
            "reorganization": "afm_passes.reorganization",
            "pruning": "afm_passes.pruning",
            "consolidation": "afm_passes.consolidation",
            "vault_ingest": "afm_passes.vault_ingest",
        }
        if pass_name not in pass_runners:
            return _make_error(-32602, f"unsupported pass_name: {pass_name}", request_id)
        trace_id = f"afm-{uuid.uuid4().hex[:12]}"
        db = SovereignDB(DEFAULT_CONFIG)
        pass_module = __import__(pass_runners[pass_name], fromlist=["run"])

        result = pass_module.run(
            db,
            DEFAULT_CONFIG,
            vault_path=vault_path,
            dry_run=dry_run,
            trace_id=trace_id,
        )
        if not dry_run and result.get("drafts"):
            from afm_writer import submit_drafts
            write_result = submit_drafts({
                "pass_name": pass_name,
                "trace_id": trace_id,
                "vault_path": vault_path,
                "drafts": result["drafts"],
                "writeback": _writeback,
            })
            result.update(write_result)
        else:
            result.setdefault("drafts_written", [])

        # Consolidation pass: apply durable promotions + dedup rejections.
        # No-op for passes that don't emit these keys. Skipped on dry_run.
        if not dry_run and (
            result.get("promote_candidate_ids")
            or result.get("dedup_candidate_ids")
            or result.get("review_candidate_ids")
        ):
            _apply_consolidation_result(result)

        try:
            _trace_ring().put(trace_id, {
                "kind": "afm_loop",
                "pass_name": pass_name,
                "inputs": result.get("inputs"),
                "prompt": result.get("prompt"),
                "prompt_version": result.get("prompt_version"),
                "output": result.get("output"),
                "draft_page_ids": [draft.get("page_id") for draft in result.get("drafts", [])],
                "dry_run": dry_run,
            })
        except Exception as exc:
            logger.warning("AFM trace capture degraded: %s", exc)
        return _make_response(result, request_id)
    except Exception as exc:
        logger.exception("daemon.compile failed")
        return _make_response({
            "status": "afm_unavailable",
            "reason": str(exc),
            "pass_name": pass_name,
            "dry_run": dry_run,
            "drafts": [],
            "drafts_written": [],
        }, request_id)
    finally:
        _record_latency("afm", time.perf_counter() - started_at)


async def _afm_loop_runner():
    """Background scheduler for AFM passes (opt-in via MINNI_AFM_LOOP).

    Ticks every idle_seconds; fires each pass when its interval has elapsed.
    Defensive: a pass that raises is logged and skipped — it can NEVER take down
    the socket server task. Last-run times are in-memory (reset on restart, which
    just means each pass runs once shortly after boot — acceptable).
    """
    if not _afm_loop_enabled(DEFAULT_CONFIG):
        logger.info("AFM loop disabled; runner not started.")
        return
    schedule = getattr(DEFAULT_CONFIG, "afm_loop_schedule", {}) or {}
    idle_seconds = int(schedule.get("idle_seconds", 300))
    passes_cfg = schedule.get("passes") or {}
    last_run = {name: 0.0 for name in passes_cfg}
    logger.info(
        "AFM loop runner started: passes=%s idle=%ds",
        list(passes_cfg.keys()), idle_seconds,
    )
    await asyncio.sleep(min(30, idle_seconds))  # let startup settle
    while _running:
        now = time.time()
        for name, cfg in passes_cfg.items():
            interval = int((cfg or {}).get("interval_seconds", 24 * 60 * 60))
            if now - last_run.get(name, 0.0) < interval:
                continue
            try:
                logger.info("AFM loop: running pass '%s'", name)
                params = {
                    "pass_name": name,
                    "dry_run": False,
                    "vault_path": DEFAULT_CONFIG.vault_path,
                }
                # Consolidation drains in a loop until the proposed queue is empty
                # or a per-tick batch budget is hit — clears bursts (swarms) instead
                # of trickling one batch per interval. Every batch resolves all its
                # examined candidates (accept/reject/needs_review), so it strictly
                # makes progress and cannot spin.
                if name == "consolidation":
                    # Drain the inbox stop-candidate channel into candidate_packets
                    # BEFORE the consolidation drain, so freshly-ingested 'proposed'
                    # rows are triaged in the same tick. Idempotent, respects
                    # log_only/do_not_store, never deletes. Failure here must NOT
                    # block consolidation of the existing queue.
                    if (cfg or {}).get("ingest_inbox", True):
                        try:
                            from afm_passes.inbox_ingest import ingest as _ingest_inbox
                            # Reuse the daemon's shared handle: SovereignDB
                            # connections are thread-local and only released
                            # by close(), so a fresh instance per tick leaks
                            # one connection per tick in this loop.
                            _ing_db = _lazy_writeback().db
                            _ing = _ingest_inbox(
                                _ing_db, DEFAULT_CONFIG,
                                fallback_principal=str((cfg or {}).get(
                                    "inbox_fallback_principal", "unknown")),
                                dry_run=False,
                            )
                            if _ing.get("inserted"):
                                logger.info(
                                    "AFM loop: inbox ingest -> %d new candidate(s) "
                                    "(eligible=%d already=%d)",
                                    _ing["inserted"], _ing["eligible"],
                                    _ing["already_present"],
                                )
                            # B4: surface kind-gate drops to the audit channel
                            # instead of silently skipping whole channels.
                            _skips = _ing.get("skipped_by_kind") or {}
                            if any(_skips.values()):
                                logger.info(
                                    "AFM loop: inbox ingest skipped_by_kind=%s",
                                    _skips,
                                )
                        except Exception:
                            logger.exception(
                                "AFM loop: inbox ingest raised (skipped; "
                                "consolidation continues)")
                    max_batches = int((cfg or {}).get("max_batches_per_tick", 40))
                    batches = total_examined = 0
                    last_summ = None
                    while batches < max_batches:
                        res = _handle_daemon_compile(params, request_id=None)
                        last_summ = (res.get("result") or {}).get("summary") if isinstance(res, dict) else None
                        batches += 1
                        examined = (last_summ or {}).get("examined", 0)
                        total_examined += examined
                        if examined == 0:
                            break
                        await asyncio.sleep(0)  # yield to the server task between batches
                    logger.info(
                        "AFM loop: consolidation drained %d batch(es), %d candidate(s); last=%s",
                        batches, total_examined, last_summ,
                    )
                else:
                    res = _handle_daemon_compile(params, request_id=None)
                    summ = (res.get("result") or {}).get("summary") if isinstance(res, dict) else None
                    logger.info("AFM loop: pass '%s' done: %s", name, summ)
            except Exception:
                logger.exception("AFM loop: pass '%s' raised (skipped)", name)
            last_run[name] = time.time()
        await asyncio.sleep(idle_seconds)
    logger.info("AFM loop runner stopped.")


def _handle_daemon_endorse(params: dict, request_id: Any) -> dict:
    """Endorse a draft page by page_id. Accept/reject/edit only."""
    global _request_count
    _request_count += 1
    page_id = str(params.get("page_id") or "").strip()
    decision = str(params.get("decision") or "").strip()
    if not page_id:
        return _make_error(-32602, "page_id is required", request_id)
    if decision not in {"accept", "reject", "edit"}:
        return _make_error(-32602, "decision must be accept, reject, or edit", request_id)
    vault_path = str(params.get("vault_path") or DEFAULT_CONFIG.vault_path)

    # G12: vault root binding (endorse touches the vault wiki); realpath + principal allowlist
    err = _guard_vault_root(params, vault_path, request_id, label="endorse")
    if err:
        return err

    try:
        from afm_writer import endorse_draft
        return _make_response(endorse_draft(vault_path, page_id, decision), request_id)
    except Exception as exc:
        logger.warning("daemon.endorse failed: %s", exc)
        return _make_error(-32000, f"Endorse error: {exc}", request_id)


# ─────────────────────────────────────────────────────────────────────────────
# G14–G18: Candidate approval pipeline (P1 keystone — human governs persistence)
# stage_candidate / list_candidates / resolve_candidate + learn default-to-proposal
# All paths use G11-stamped EffectivePrincipal; operator gate via is_operator_principal
# Rejected/redacted/expired rows preserved for audit; only accepted produce learnings rows.
# ─────────────────────────────────────────────────────────────────────────────

def _stage_candidate(params: dict, request_id: Any) -> dict:
    return _runtime_stage_candidate(params, request_id, _governance_context())


def _list_candidates(params: dict, request_id: Any) -> dict:
    return _runtime_list_candidates(params, request_id, _governance_context())


def _explicitly_allowed_operator(principal: EffectivePrincipal) -> bool:
    return _runtime_explicitly_allowed_operator(principal)


def _resolve_candidate(params: dict, request_id: Any) -> dict:
    return _runtime_resolve_candidate(params, request_id, _governance_context())

def _handle_ax_snapshot_store(params: dict, request_id: Any) -> dict:
    from ax_memory import AXMemory
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    app_name = str(params.get("app_name", "")).strip()
    tree_json = str(params.get("tree_json", ""))
    
    if not app_name or not tree_json:
        return _make_error(-32602, "app_name and tree_json are required", request_id)
        
    try:
        # Shared daemon handle — per-call SovereignDB() instances leak their
        # thread-local connection (only close() releases it).
        db = _lazy_writeback().db
        ax = AXMemory(db)
        snapshot_id = ax.add_snapshot(
            agent_id=agent_id,
            app_name=app_name,
            tree_json=tree_json,
            ttl_seconds=params.get("ttl_seconds", 3600)
        )
        return _make_response({"snapshot_id": snapshot_id}, request_id)
    except Exception as exc:
        logger.exception("ax_snapshot_store failed")
        return _make_error(-32000, f"ax_snapshot_store error: {exc}", request_id)

def _handle_ax_snapshot_get(params: dict, request_id: Any) -> dict:
    from ax_memory import AXMemory
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    app_name = params.get("app_name")
        
    try:
        db = _lazy_writeback().db
        ax = AXMemory(db)
        snapshot = ax.get_latest_snapshot(agent_id=agent_id, app_name=app_name)
        return _make_response({"snapshot": snapshot}, request_id)
    except Exception as exc:
        logger.exception("ax_snapshot_get failed")
        return _make_error(-32000, f"ax_snapshot_get error: {exc}", request_id)


def _handle_vault_index_doc(params: dict, request_id: Any) -> dict:
    """Index a vault page into the semantic recall index without going through
    the learn/candidate governance pipeline.

    M-4 fix: minni_vault_write writes a page to disk but did NOT trigger the
    recall bridge, so vault_write content was NOT semantically recall-able until
    a separate VaultIndexer run. This RPC lets the TypeScript MCP handler call
    ``vault_index_doc`` immediately after writing, matching the instant-recall
    semantics of ``learn`` (which calls index_durable_document on promote).

    Params (all required unless noted):
        content    — full page content (markdown, including frontmatter if any)
        path       — relative path within the vault (e.g. "wiki/sessions/foo.md")
        agent      — agent_id owning this document
        sigil      — optional emoji sigil (default "❓")
        privacy_level — optional, default "safe"
        page_status   — optional, default "accepted"
        layer      — optional, default "knowledge"

    Returns {"status": "ok", "doc_id": <int>, "chunks": <int>} on success.
    FAIL-OPEN: engine errors are caught internally (never raises); returns an
    error envelope only for missing required params.
    """
    global _request_count
    _request_count += 1
    started_at = time.perf_counter()

    principal, err = _handler_principal(params, request_id)
    if err:
        return err

    content = params.get("content", "")
    path = params.get("path", "")
    wire_agent = str(params.get("agent", "")).strip()
    if wire_agent and wire_agent != principal.agent_id:
        return make_mismatch_error(wire_agent, principal.agent_id, request_id)
    agent = principal.agent_id

    if not content:
        return _make_error(-32602, "content is required", request_id)
    if not path:
        return _make_error(-32602, "path is required", request_id)

    rel_path = str(path).replace("\\", "/").lstrip("/")
    if not rel_path or ".." in Path(rel_path).parts:
        return _make_error(-32602, "path must be a relative path within the vault", request_id)

    vault_path = params.get("vault_path")
    if not vault_path:
        vault_path, _ = _agent_vault(principal.agent_id)
    vault_path = str(vault_path)
    full_path = Path(vault_path).resolve() / rel_path
    root_err = _guard_vault_root(params, full_path, request_id, label="vault_index_doc")
    if root_err:
        return root_err

    # SEC-015-style size cap: vault pages capped at 256 KiB (larger than learn
    # which is 64 KiB, because vault pages include rich frontmatter + prose).
    _MAX_VAULT_PAGE_CHARS = 256 * 1024
    if len(content) > _MAX_VAULT_PAGE_CHARS:
        return _make_error(
            -32602,
            f"vault_index_doc content exceeds {_MAX_VAULT_PAGE_CHARS} chars",
            request_id,
        )

    sigil = str(params.get("sigil", "❓"))
    privacy_level = str(params.get("privacy_level", "safe"))
    page_status = str(params.get("page_status", "accepted"))
    layer = str(params.get("layer", "knowledge"))

    try:
        engine = _lazy_retrieval()
        result = engine.index_durable_document(
            content=content,
            path=rel_path,
            agent=agent,
            sigil=sigil,
            privacy_level=privacy_level,
            page_status=page_status,
            layer=layer,
        )
        _record_latency("vault_index_doc", time.perf_counter() - started_at)
        return _make_response(result, request_id)
    except Exception as exc:
        logger.exception("vault_index_doc failed")
        return _make_error(-32000, f"vault_index_doc error: {exc}", request_id)


# Method registry
_METHODS: Dict[str, callable] = {
    "ping":                   _handle_ping,
    "gate.shared":            _handle_gate_shared,
    "search":                 _handle_search,
    "feedback":               _handle_feedback,
    "trace":                  _handle_trace,
    "expand":                 _handle_expand,
    "sm_drill":               _handle_sm_drill,
    "sm_export_pack":         _handle_sm_export_pack,
    "read":                   _handle_read,
    "learn":                  _handle_learn,
    "resolve_contradiction":  _handle_resolve_contradiction,
    "minni_subscribe_contradictions": _handle_subscribe_contradictions,
    "log_event":              _handle_log_event,
    "daemon.handoff":         _handle_daemon_handoff,
    "handoff":                _handle_daemon_handoff,
    "minni_ack_handoff":      _handle_ack_handoff,
    "minni_list_pending_handoffs": _handle_list_pending_handoffs,
    "minni_await_handoff":    _handle_await_handoff,
    "daemon.compile":         _handle_daemon_compile,
    "daemon.endorse":         _handle_daemon_endorse,
    # G14-G18 candidate pipeline (operator-gated governance)
    "stage_candidate":        _stage_candidate,
    "list_candidates":        _list_candidates,
    "resolve_candidate":      _resolve_candidate,
    "ax_snapshot_store":      _handle_ax_snapshot_store,
    "ax_snapshot_get":        _handle_ax_snapshot_get,
    # M-4 fix: vault_write now triggers immediate semantic indexing via this RPC.
    "vault_index_doc":        _handle_vault_index_doc,
    "status":                 _handle_status,
    "health_report":          _handle_health_report,
    "hygiene_report":         _handle_hygiene_report,
}

# ── Unix socket server ───────────────────────────────────────────────────
# SEC-001: canonical secure location (0700 dir, 0600 socket). /tmp is world-writable
# and was the old default; the legacy HTTP variant is deprecated.
SECURE_RUN_DIR: Path = Path.home() / ".minni" / "run"
DEFAULT_SOCKET_PATH: Path = SECURE_RUN_DIR / "minnid.sock"

_unix_socket_path: Path = DEFAULT_SOCKET_PATH
_running = False
_server: Optional[asyncio.AbstractServer] = None


async def _dispatch(request: dict) -> dict:
    """Route a JSON-RPC request to the correct handler."""
    return await dispatch_request(
        request,
        DispatchContext(
            methods=_METHODS,
            recovery_allowed_methods=RECOVERY_ALLOWED_METHODS,
            resolve_provenance=resolve_provenance,
            enforce_method_capability=_enforce_method_capability,
            make_error=_make_error,
            make_response=_make_response,
            obs=obs,
            logger=logger,
        ),
    )


# Sync facade for legacy direct callers in the test suite (post RCM-006/007 async _dispatch refactor).
# ~30 call sites in test_*.py used synchronous minnid._dispatch({...})["result"]; they now use this.
# Real daemon paths (_handle_client, HTTP entry) use await _dispatch or the full async loop.
# This keeps all tests green without requiring pytest-asyncio or rewriting every site to async.
def _dispatch_sync(request: dict) -> dict:
    """Thin sync wrapper: runs the (now async) _dispatch via asyncio.run for test compatibility."""
    return asyncio.run(_dispatch(request))


async def _handle_client(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter):
    """Handle a single client connection."""
    try:
        while True:
            # Read until newline (JSON-RPC over line-delimited protocol).
            # SEC-015: bound a single line to 1 MiB so a runaway sender cannot
            # exhaust memory inside asyncio's StreamReader buffer.
            try:
                data = await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError:
                logger.warning(
                    "client request exceeded %d-byte body limit; closing connection",
                    _SOCKET_BODY_LIMIT,
                )
                response = _make_error(
                    -32600,
                    "request body exceeds 1 MiB limit",
                )
                try:
                    writer.write(json.dumps(response).encode() + b"\n")
                    await writer.drain()
                except Exception:
                    pass
                break
            if not data:
                break

            request = _parse_request(data)
            if request is None:
                response = _make_error(-32700, "Parse error")
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
                continue

            response = await _dispatch(request)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
    except asyncio.IncompleteReadError:
        pass  # Client disconnected
    except Exception:
        logger.exception("Client handler error")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def _serve_unix_socket(path: Path):
    """Start the Unix socket server."""
    global _server, _running

    # SEC-001: ensure parent run/ dir is 0700 (owner-only) before bind
    run_dir = path.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(run_dir), 0o700)
    except OSError as e:
        logger.warning("Failed to chmod run dir %s to 0o700 (OSError: %s) — socket may be accessible to other users (SEC-001)", run_dir, e)

    # Remove stale socket
    if path.exists():
        path.unlink()

    # SEC-015: cap StreamReader buffer at 1 MiB so readuntil raises
    # LimitOverrunError instead of growing unbounded.
    _server = await asyncio.start_unix_server(
        _handle_client,
        path=str(path),
        limit=_SOCKET_BODY_LIMIT,
    )

    # Set socket permissions (owner read/write only)
    try:
        os.chmod(str(path), 0o600)
    except OSError as e:
        logger.warning("Failed to chmod socket %s to 0o600 (OSError: %s) — socket may be accessible to other users (SEC-001)", path, e)

    logger.info("Listening on %s", path)
    _running = True

    try:
        async with _server:
            while _running:
                await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        pass
    finally:
        if _server is not None:
            _server.close()
            await _server.wait_closed()


# ── HTTP fallback server (optional) ──────────────────────────────────────

async def _serve_http(host: str = "127.0.0.1", port: int = 9900):
    """Minimal HTTP server for environments without Unix socket support."""

    async def handler(reader, writer):
        try:
            # Read HTTP request
            request_line = await reader.readline()
            headers = {}
            while True:
                line = await reader.readline()
                if line.strip() == b"":
                    break
                if b":" in line:
                    key, val = line.split(b":", 1)
                    headers[key.decode().strip().lower()] = val.decode().strip()

            content_length = int(headers.get("content-length", 0))
            body = b""
            if content_length:
                body = await reader.readexactly(content_length)

            if not body:
                writer.write(
                    b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"
                )
                await writer.drain()
                return

            request = _parse_request(body)
            if request is None:
                resp = json.dumps(_make_error(-32700, "Parse error"))
            else:
                resp = json.dumps(await _dispatch(request))

            resp_bytes = resp.encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(resp_bytes)}\r\n"
                f"\r\n".encode() + resp_bytes
            )
            await writer.drain()
        except Exception:
            logger.exception("HTTP handler error")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handler, host, port)
    logger.info("HTTP fallback listening on %s:%d", host, port)

    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass


# ── Cloud-sync hygiene ────────────────────────────────────────────────────
# SEC: warn (do not refuse to start) when a daemon-managed path lives under a
# known cloud-sync root. "Local-first" guarantees do not hold on a path that
# Apple/Dropbox/Google/Microsoft is silently replicating offsite.

SYNC_ROOTS = (
    "Library/Mobile Documents",  # iCloud Drive
    "Dropbox",
    "Google Drive",
    "OneDrive",
)


def _warn_if_sync_root(label: str, path: Path) -> None:
    """Emit a warning if ``path`` is under a known cloud-sync root inside $HOME.

    Never raises. Never refuses to start. Resolution failures are silently
    treated as "not under a sync root" — this is best-effort hygiene, not a
    security control.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return
    try:
        home = Path.home().resolve()
    except OSError:
        return
    try:
        rel = resolved.relative_to(home)
    except ValueError:
        return
    rel_str = str(rel)
    for marker in SYNC_ROOTS:
        if rel_str.startswith(marker) or f"/{marker}" in f"/{rel_str}":
            logger.warning(
                "Minni %s appears to be inside a cloud-sync root (%s). "
                "Local-first guarantees do not hold for this path. See README §Local-First Hygiene.",
                label, marker,
            )
            return


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    global _start_time, _unix_socket_path

    _start_time = time.time()

    parser = argparse.ArgumentParser(
        description="minnid — Minni Memory Daemon",
    )
    parser.add_argument(
        "--socket", "-s",
        default=str(DEFAULT_SOCKET_PATH),
        help="Unix socket path (default: ~/.minni/run/minnid.sock with 0600; SEC-001)",
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=0,
        help="HTTP fallback port (0 = disabled, default: 0)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    # Logging: route through the centralized obs setup so format/level honor
    # MINNI_LOG_FORMAT (text|json) and MINNI_LOG_LEVEL.
    obs.configure_logging(verbose=args.verbose)

    _unix_socket_path = Path(args.socket)

    logger.info("minnid v%s starting", VERSION)
    logger.info("Engine dir: %s", _ENGINE_DIR)
    logger.info("Socket:    %s", args.socket)
    if args.port:
        logger.info("HTTP:      %s:%d", args.host, args.port)

    # Cloud-sync hygiene: warn (do not refuse) if any daemon-managed path is
    # inside iCloud / Dropbox / Google Drive / OneDrive. See SEC plan
    # "Cloud-sync hygiene".
    try:
        _warn_if_sync_root("socket", _unix_socket_path)
        _warn_if_sync_root("DB", Path(DEFAULT_CONFIG.db_path))
        _warn_if_sync_root("vault", Path(DEFAULT_CONFIG.vault_path))
    except Exception:  # never block startup on hygiene checks
        logger.exception("cloud-sync hygiene check failed (non-fatal)")

    # Eagerly initialize/migrate database on startup (RCM-028 Phase 0 exit)
    try:
        from db import SovereignDB
        db = SovereignDB(DEFAULT_CONFIG)
        db._get_conn()
        logger.info("Database initialized/migrated on startup.")
    except Exception:
        logger.exception("Eager database initialization failed")

    loop = asyncio.new_event_loop()
    main_task = loop.create_task(_serve_unix_socket(_unix_socket_path))

    http_task = None
    if args.port:
        http_task = loop.create_task(_serve_http(args.host, args.port))

    # Opt-in AFM loop runner (gated by MINNI_AFM_LOOP). Dormant unless enabled.
    afm_task = None
    if _afm_loop_enabled(DEFAULT_CONFIG):
        afm_task = loop.create_task(_afm_loop_runner())

    def _shutdown(signum, frame):
        nonlocal _running
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down…", sig_name)
        _running = False
        if _server is not None:
            _server.close()
        main_task.cancel()
        if http_task is not None:
            http_task.cancel()
        if afm_task is not None:
            afm_task.cancel()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    signal.signal(signal.SIGHUP, _reload_runtime_config)

    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down…")
    finally:
        _running = False
        # Clean up socket
        if _unix_socket_path.exists():
            _unix_socket_path.unlink()
        loop.close()
        logger.info("minnid stopped.")


if __name__ == "__main__":
    main()

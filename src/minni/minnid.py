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
import os
import signal
import sys
import time
import importlib.util
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

import minni.obs as obs                                                  # noqa: E402  # centralized logging + metrics counters
from minni.config import DEFAULT_CONFIG          # noqa: E402
from minni.db import SovereignDB                                  # noqa: E402
from minni.principal import (                                     # noqa: E402  # G11 EffectivePrincipal + G14 operator gate
    EffectivePrincipal,
    agent_scope_for,
    make_mismatch_error,
    resolve_effective_principal,  # re-export: ProvenanceContext test seam + handler calls
    validate_agent_id,
)
_ORIGINAL_RESOLVE_EFFECTIVE_PRINCIPAL = resolve_effective_principal
from minni.minnid_runtime.afm import (  # noqa: E402
    AFMContext,
    afm_loop_enabled as _runtime_afm_loop_enabled,
    afm_loop_runner as _runtime_afm_loop_runner,
    apply_consolidation_result as _runtime_apply_consolidation_result,
    handle_daemon_compile as _runtime_handle_daemon_compile,
    handle_daemon_endorse as _runtime_handle_daemon_endorse,
    mark_candidate_review as _runtime_mark_candidate_review,
    maybe_archive_inbox_source as _runtime_maybe_archive_inbox_source,
    promote_candidate_durable as _runtime_promote_candidate_durable,
    reject_candidate_dedup as _runtime_reject_candidate_dedup,
)
from minni.minnid_runtime.ax import (  # noqa: E402
    AXContext,
    handle_ax_snapshot_get as _runtime_handle_ax_snapshot_get,
    handle_ax_snapshot_store as _runtime_handle_ax_snapshot_store,
)
from minni.minnid_runtime.dispatch import DispatchContext, dispatch_request  # noqa: E402
from minni.minnid_runtime.governance import (  # noqa: E402
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
from minni.minnid_runtime.handoff import (  # noqa: E402
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
from minni.minnid_runtime.health import (  # noqa: E402
    HealthContext,
    handle_health_report as _runtime_handle_health_report,
    handle_hygiene_report as _runtime_handle_hygiene_report,
    handle_status as _runtime_handle_status,
    redact_health_report_for_recovery as _redact_health_report_for_recovery,
)
from minni.minnid_runtime.provenance import (  # noqa: E402
    RECOVERY_ALLOWED_METHODS,
    RPC_CAPABILITY_REQUIREMENTS as _RPC_CAPABILITY_REQUIREMENTS,
    ProvenanceContext,
    ProvenanceResolution,
    enforce_method_capability as _enforce_method_capability,
    guard_vault_root as _guard_vault_root,
    handler_principal as _runtime_handler_principal,
    make_reserved_agent_id_error as _make_reserved_agent_id_error,
    provenance_claim as _provenance_claim,
    recover,
    resolve_provenance as _runtime_resolve_provenance,
)
from minni.minnid_runtime.recall import (  # noqa: E402
    RecallContext,
    handle_expand as _runtime_handle_expand,
    handle_feedback as _runtime_handle_feedback,
    handle_list_events as _runtime_handle_list_events,
    handle_read as _runtime_handle_read,
    handle_search as _runtime_handle_search,
    handle_sm_drill as _runtime_handle_sm_drill,
    handle_sm_export_pack as _runtime_handle_sm_export_pack,
    handle_trace as _runtime_handle_trace,
    resolve_backend as _runtime_resolve_backend,
)
from minni.minnid_runtime.redaction import redact_text as _redact_text, redact_value as _redact_value  # noqa: E402
from minni.minnid_runtime.rpc import make_error as _make_error, make_response as _make_response  # noqa: E402
from minni.minnid_runtime.transport import SOCKET_BODY_LIMIT as _SOCKET_BODY_LIMIT, parse_request as _parse_request  # noqa: E402
from minni.minnid_runtime.vault_index import (  # noqa: E402
    MAX_VAULT_PAGE_CHARS as _MAX_VAULT_PAGE_CHARS,
    VaultIndexContext,
    handle_vault_index_doc as _runtime_handle_vault_index_doc,
)

# ── Lazy imports (heavy ML deps) ──────────────────────────────────────────
_retrieval = None
_vault_retrieval_cache = {}
_episodic = None
_writeback = None


def _provenance_context() -> ProvenanceContext:
    from minni.minnid_runtime import provenance as _provenance_mod

    default = _ORIGINAL_RESOLVE_EFFECTIVE_PRINCIPAL
    minnid_resolver = resolve_effective_principal
    provenance_resolver = _provenance_mod.resolve_effective_principal
    if minnid_resolver is not default:
        resolver = minnid_resolver
    elif provenance_resolver is not default:
        resolver = provenance_resolver
    else:
        resolver = default
    return ProvenanceContext(resolve_effective_principal=resolver)


def resolve_provenance(request: dict, **kwargs) -> ProvenanceResolution:
    return _runtime_resolve_provenance(request, context=_provenance_context(), **kwargs)


def _handler_principal(params, request_id, **kwargs):
    return _runtime_handler_principal(
        params,
        request_id,
        context=_provenance_context(),
        **kwargs,
    )


def _reload_runtime_config(signum=None, frame=None) -> None:
    """Clear identity and per-vault caches after operator config changes."""
    agent_scope_for.cache_clear()
    _vault_retrieval_cache.clear()
    logger.info("SIGHUP received — cleared identity/runtime caches")


def _lazy_retrieval():
    """Lazy-load RetrievalEngine to defer MLX weight loading."""
    global _retrieval
    if _retrieval is None:
        from minni.retrieval import RetrievalEngine
        _retrieval = RetrievalEngine(SovereignDB(DEFAULT_CONFIG), DEFAULT_CONFIG)
    return _retrieval


def _vault_agent_id(vault_path: Path) -> Optional[str]:
    try:
        from minni.afm_passes.inbox_ingest import _VAULT_SLUG_TO_AGENT_ID

        name = vault_path.name
        if not name.endswith("-vault"):
            return None
        agent_id = _VAULT_SLUG_TO_AGENT_ID.get(name[: -len("-vault")])
        return validate_agent_id(agent_id) if agent_id else None
    except Exception:
        return None


def _vault_index_ready(vault_path: Path) -> bool:
    try:
        from minni.vault_index import vault_index_paths

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

    from minni.faiss_index import FAISSIndex
    from minni.retrieval import RetrievalEngine
    from minni.vault_index import build_vault_index_config

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


def _lazy_writeback():
    """Lazy-load WriteBackMemory."""
    global _writeback
    if _writeback is None:
        from minni.writeback import WriteBackMemory
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

        from minni.retrieval import RetrievalEngine

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
            from minni.indexer import VaultIndexer

            meta = VaultIndexer._extract_frontmatter(content)
            doc_agent = agent_id  # server-stamped ownership; never trust frontmatter agent
            sigil = meta.get("sigil", "❓")
            page_status = (
                meta["page_status"]
                if meta["page_status"] != "candidate"
                else "accepted"
            )
            privacy_level = meta["privacy_level"]
            # M2: do NOT copy page_type from model-supplied frontmatter into the
            # durable-learn synthetic doc. can_read_document treats page_type in
            # {wiki,handoff,synthesis,decision,session} as cross-agent-readable, so
            # a `type: wiki` learn would make a private learning cross-visible.
            # A durable learning is always owner-scoped: pin a fixed, non-cross-
            # visible type.
            page_type = "learning"
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


def _purge_durable_learning(agent_id: str, content: str, db=_UNSET) -> None:
    """M4: purge the synthetic doc for a superseded/rejected durable learning.

    Mirrors ``_index_durable_learning``'s db/engine resolution (never touches the
    live DEFAULT_CONFIG DB for a divergent-DB caller) and removes the durable
    document row + FTS + chunks + live FAISS for the content-derived synthetic
    path, so a superseded/rejected learning stops surfacing in semantic/lexical
    document search. FAIL-OPEN: never raises; the learnings-table lifecycle is the
    source of truth and a purge hiccup only leaves recall slightly stale.
    """
    try:
        if db is _UNSET or db is None:
            logger.warning(
                "durable purge: _purge_durable_learning called without an explicit "
                "db= — refusing to default to the live DEFAULT_CONFIG database; "
                "skipping synthetic-doc purge (superseded content may linger in "
                "doc-search until reindex). agent=%s",
                agent_id,
            )
            return

        from minni.retrieval import RetrievalEngine

        try:
            store_db_path = os.path.abspath(db.config.db_path)
        except Exception:
            store_db_path = None
        default_db_path = os.path.abspath(DEFAULT_CONFIG.db_path)

        if store_db_path is not None and store_db_path == default_db_path:
            engine = _lazy_retrieval()
        else:
            engine = RetrievalEngine(db, db.config)

        path = _durable_doc_path(
            agent_id, f"learning:{content}", vault_path=engine.config.vault_path,
            content=content,
        )
        engine.purge_durable_document(path)
    except Exception as exc:
        logger.warning(
            "durable purge: synthetic-doc purge failed for agent=%s (%s) — "
            "learnings lifecycle stands, doc-search stays stale until reindex",
            agent_id, exc,
        )


def _lazy_episodic():
    """Lazy-load EpisodicMemory."""
    global _episodic
    if _episodic is None:
        from minni.episodic import EpisodicMemory
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
    # A wire claim of a reserved operator id gets the distinct reserved-id
    # diagnostic (#121 Root B), not the generic unknown-identity route — the
    # remediation differs (omit agent_id / MINNI_LOCAL_OPERATOR, not authoring
    # a principals/<agent>.json for a reserved name).
    if getattr(principal, "deny_reason", None) == "reserved_agent_id":
        return _make_reserved_agent_id_error(
            principal.agent_id, "gate.shared", request_id
        )
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
        lazy_episodic=_lazy_episodic,
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


def _handle_sm_drill(params: dict, request_id: Any) -> dict:
    return _runtime_handle_sm_drill(params, request_id, _recall_context())


def _handle_sm_export_pack(params: dict, request_id: Any) -> dict:
    return _runtime_handle_sm_export_pack(params, request_id, _recall_context())


def _handle_read(params: dict, request_id: Any) -> dict:
    return _runtime_handle_read(params, request_id, _recall_context())


def _handle_list_events(params: dict, request_id: Any) -> dict:
    return _runtime_handle_list_events(params, request_id, _recall_context())


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
        purge_durable_learning=_purge_durable_learning,
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


def _increment_ops_request_count() -> None:
    global _request_count
    _request_count += 1


def _health_context() -> HealthContext:
    return HealthContext(
        make_error=_make_error,
        make_response=_make_response,
        guard_vault_root=_guard_vault_root,
        latency_snapshot=_latency_snapshot,
        metrics_snapshot=obs.metrics_snapshot,
        afm_loop_enabled=_afm_loop_enabled,
        increment_request_count=_increment_ops_request_count,
        request_count=lambda: _request_count,
        start_time=lambda: _start_time,
        version=VERSION,
        sovereign_db=SovereignDB,
        default_config=DEFAULT_CONFIG,
        logger=logger,
    )


def _handle_status(params: dict, request_id: Any) -> dict:
    return _runtime_handle_status(params, request_id, _health_context())


def _handle_health_report(params: dict, request_id: Any) -> dict:
    return _runtime_handle_health_report(params, request_id, _health_context())


def _handle_hygiene_report(params: dict, request_id: Any) -> dict:
    return _runtime_handle_hygiene_report(params, request_id, _health_context())


def _afm_context() -> AFMContext:
    return AFMContext(
        make_error=_make_error,
        make_response=_make_response,
        guard_vault_root=_guard_vault_root,
        lazy_writeback=_lazy_writeback,
        trace_ring=_trace_ring,
        record_latency=_record_latency,
        maybe_archive_inbox_source=_maybe_archive_inbox_source,
        increment_request_count=_increment_ops_request_count,
        writeback_ref=lambda: _writeback,
        sovereign_db=SovereignDB,
        default_config=DEFAULT_CONFIG,
        is_running=lambda: _running,
        logger=logger,
    )


def _afm_loop_enabled(config=DEFAULT_CONFIG) -> bool:
    return _runtime_afm_loop_enabled(config)


# ─────────────────────────────────────────────────────────────────────────────
# Consolidation: durable promotion of proposed candidates (AFM loop authority).
# Mirrors the force=true durable-learn path (embed + INSERT learnings) and stamps
# consolidation_actions for audit. The privileged write lives here, not in passes.
# ─────────────────────────────────────────────────────────────────────────────

def _maybe_archive_inbox_source(db, candidate_id: int) -> None:
    return _runtime_maybe_archive_inbox_source(db, candidate_id, DEFAULT_CONFIG, logger)


def _promote_candidate_durable(candidate_id: int, reason: str = "afm-consolidation"):
    return _runtime_promote_candidate_durable(candidate_id, reason, _afm_context())


def _reject_candidate_dedup(candidate_id: int) -> bool:
    return _runtime_reject_candidate_dedup(candidate_id, _afm_context())


def _mark_candidate_review(candidate_id: int, reason: str = "afm-consolidation review") -> bool:
    return _runtime_mark_candidate_review(candidate_id, reason, _afm_context())


def _apply_consolidation_result(result: dict) -> None:
    return _runtime_apply_consolidation_result(result, _afm_context())


def _handle_daemon_compile(params: dict, request_id: Any) -> dict:
    return _runtime_handle_daemon_compile(params, request_id, _afm_context())


async def _afm_loop_runner():
    return await _runtime_afm_loop_runner(_afm_context())


def _handle_daemon_endorse(params: dict, request_id: Any) -> dict:
    return _runtime_handle_daemon_endorse(params, request_id, _afm_context())


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


def _ax_context() -> AXContext:
    return AXContext(
        make_error=_make_error,
        make_response=_make_response,
        handler_principal=_handler_principal,
        lazy_writeback=_lazy_writeback,
        logger=logger,
    )


def _handle_ax_snapshot_store(params: dict, request_id: Any) -> dict:
    return _runtime_handle_ax_snapshot_store(params, request_id, _ax_context())


def _handle_ax_snapshot_get(params: dict, request_id: Any) -> dict:
    return _runtime_handle_ax_snapshot_get(params, request_id, _ax_context())


def _vault_index_context() -> VaultIndexContext:
    return VaultIndexContext(
        make_error=_make_error,
        make_response=_make_response,
        make_mismatch_error=make_mismatch_error,
        handler_principal=_handler_principal,
        guard_vault_root=_guard_vault_root,
        lazy_retrieval=_lazy_retrieval,
        agent_vault=_agent_vault,
        record_latency=_record_latency,
        increment_request_count=_increment_ops_request_count,
        logger=logger,
    )


def _handle_vault_index_doc(params: dict, request_id: Any) -> dict:
    return _runtime_handle_vault_index_doc(params, request_id, _vault_index_context())


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
    "list_events":            _handle_list_events,
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
            # Read HTTP request (discard request line; only POST body matters here).
            await reader.readline()
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
        from minni.db import SovereignDB
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

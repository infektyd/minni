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
import concurrent.futures
import json
import logging
import os
import re
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
        _retrieval = RetrievalEngine(SovereignDB.shared(DEFAULT_CONFIG), DEFAULT_CONFIG)
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
    db = SovereignDB.shared(cfg)
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
        _writeback = WriteBackMemory(SovereignDB.shared(DEFAULT_CONFIG), DEFAULT_CONFIG)
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
        _episodic = EpisodicMemory(SovereignDB.shared(DEFAULT_CONFIG), DEFAULT_CONFIG)
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

def _resolve_version() -> str:
    """Resolve the daemon version dynamically; never raise.

    pyproject.toml is authoritative. Prefer the installed distribution's
    metadata (``importlib.metadata.version("minni")``, matches pyproject in the
    repo's editable .venv); fall back to reading pyproject.toml relative to this
    module (from-source / non-installed checkout, resolved off __file__ not cwd
    since a daemon may run from anywhere); finally a non-crashing "unknown".
    """
    try:
        import importlib.metadata as _im

        return _im.version("minni")
    except Exception:
        pass
    try:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        match = re.search(
            r'(?m)^version\s*=\s*["\']([^"\']+)["\']', text
        )
        if match:
            return match.group(1)
    except Exception:
        pass
    return "unknown"


VERSION = _resolve_version()
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
        "# Minni Recall",
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
        metrics_delta_snapshot=obs.metrics_delta_snapshot,
        metrics_last_incremented_at=obs.metrics_last_incremented_at,
        health_flags=obs.health_flags,
        recent_errors=obs.recent_errors,
        afm_loop_enabled=_afm_loop_enabled,
        increment_request_count=_increment_ops_request_count,
        request_count=lambda: _request_count,
        start_time=lambda: _start_time,
        version=VERSION,
        sovereign_db=SovereignDB,
        default_config=DEFAULT_CONFIG,
        logger=logger,
        retrieval_engine=_lazy_retrieval,
        watchdog_state=_read_watchdog_state,
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


# ─────────────────────────────────────────────────────────────────────────────
# Vault watch: keep the per-agent vault indexes current.
#
# Recall gates on a per-vault index (`<vault>/.index/vault.db`, see
# _vault_engine below). Nothing on the write path builds it: vault_write and
# learn drop .md files into a vault and return, and indexer.start_watcher() has
# no caller inside the daemon. The result is that a vault can accumulate
# hundreds of notes that recall can never see -- observed in the field as every
# per-agent vault sitting at zero indexed documents while the vaults held
# ~2100 markdown files.
#
# A periodic incremental sweep is used rather than a filesystem watcher:
# vault_ingest already does its own change detection (it reports
# skipped_unchanged), it covers every discovered vault instead of the single
# configured vault_path, and it needs no watchdog dependency in the daemon.
# ─────────────────────────────────────────────────────────────────────────────

def _vault_watch_enabled() -> bool:
    return (os.environ.get("MINNI_VAULT_WATCH", "on") or "on").strip().lower() != "off"


def _warmup_enabled() -> bool:
    """Preload retrieval models at daemon start (MINNI_WARMUP=off to disable)."""
    return (os.environ.get("MINNI_WARMUP", "on") or "on").strip().lower() != "off"


def _warmup_models() -> None:
    """
    Load the retrieval models the first search would otherwise load lazily.

    Why this exists: every model is a first-call ``functools.cache`` singleton
    (``minni.models.get_embedder`` / ``get_cross_encoder``), and the FAISS index
    is loaded on demand too. That means the FIRST search after a daemon restart
    pays the whole cold cost inside the caller's request — and that caller is
    often a prompt-time hook on a harness deadline it cannot extend (Claude Code
    discards a UserPromptSubmit hook's output at 30s). The load also issues live
    HuggingFace HTTP calls to revalidate the cache, so it is not even bounded by
    local disk speed.

    Moving that cost to daemon start makes it invisible: nobody is waiting.

    Best-effort by contract — a warmup failure must never stop the daemon from
    serving. The lazy path is still there and still correct; warmup only decides
    WHO waits for it.
    """
    start = time.monotonic()
    try:
        from minni.models import get_cross_encoder, get_embedder

        get_embedder()
        if DEFAULT_CONFIG.reranker_enabled:
            get_cross_encoder()
        _lazy_retrieval()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("warmup failed after %.1fs: %s", time.monotonic() - start, exc)
        return
    logger.info("warmup complete in %.1fs (embedder, reranker, retrieval engine)", time.monotonic() - start)


async def _warmup_runner() -> None:
    """
    Run the warmup OFF the event loop, in a worker thread.

    The loads are synchronous and CPU/network-bound for seconds; doing them
    inline would block the accept loop and turn "slow first search" into
    "daemon unreachable", which is strictly worse than the bug being fixed.
    """
    await asyncio.to_thread(_warmup_models)


def _vault_watch_interval() -> int:
    try:
        raw = int(os.environ.get("MINNI_VAULT_WATCH_INTERVAL", "300"))
    except (TypeError, ValueError):
        return 300
    # Below a minute the sweep costs more than the staleness it removes.
    return max(60, raw)


def _expire_shared_vault_drafts() -> int:
    """Expire drafts past their TTL in the shared vault. Returns the count.

    Expiry otherwise runs ONLY inside afm_writer._write_batch, and the AFM loop
    submits a batch only when a pass actually produced drafts — so a loop that
    is healthy but quiet never expires anything, and a backlog can sit past TTL
    indefinitely. Driving it from the sweep makes expiry depend on the clock
    rather than on new work arriving.
    """
    from minni.afm_writer import _expire_stale_drafts

    return _expire_stale_drafts(Path(DEFAULT_CONFIG.vault_path).expanduser())


def _invalidate_shared_faiss() -> None:
    """Make chunks the sweep just wrote visible to the live engine's semantic leg.

    index_shared_vault writes through its OWN short-lived SovereignDB and
    VaultIndexer, so its FAISS rebuild lands on a throwaway instance. The
    daemon's process-wide RetrievalEngine keeps the in-memory index it built at
    startup, and _ensure_faiss_loaded returns early while count > 0 — so new
    chunks stayed invisible to semantic recall until restart even though FTS
    could see them. Invalidating forces the next search to rebuild from the DB.

    Deliberately does NOT call _lazy_retrieval(): if the engine has not been
    constructed yet there is nothing stale to fix, and constructing one here
    would drag model loading onto the sweep thread.
    """
    if _retrieval is None:
        return
    try:
        _retrieval.faiss_index.invalidate()
        logger.info("Vault watch: invalidated live FAISS; next search rebuilds from DB")
    except Exception:
        logger.exception("Vault watch: FAISS invalidation failed")


def _vault_watch_sweep_once() -> dict:
    """Blocking incremental ingest of every discovered vault. Runs off-loop."""
    from minni.index_all import index_agent_vaults, index_shared_vault

    # Isolated like the shared-vault steps below: a sticky agent-vault fault
    # (corrupt sidecar DB, bad path) must not keep the shared vault dark on
    # every tick — going dark on daemon schedule is the defect this whole
    # slice exists to fix.
    stats: dict = {}
    try:
        stats = index_agent_vaults(DEFAULT_CONFIG, dry_run=False, verbose=False)
    except Exception:
        logger.exception("Vault watch: agent-vault sweep failed")
    # The shared vault is the AFM loop's own output directory and is NOT one of
    # the per-agent vaults (see discover_agent_vaults), so without this it was
    # indexed by nothing on a running daemon. Isolated: a failure here must not
    # cost the agent-vault results already in hand.
    #
    # Expire BEFORE indexing so a page that crosses its TTL this tick is indexed
    # with the status it now has, instead of going in as a draft and waiting a
    # whole interval to be corrected.
    try:
        expired = _expire_shared_vault_drafts()
        if expired:
            logger.info("Vault watch: expired %s draft(s) past TTL", expired)
    except Exception:
        logger.exception("Vault watch: draft expiry failed")
    try:
        shared = index_shared_vault(DEFAULT_CONFIG)
        stats.update(shared)
        for s in shared.values():
            if not isinstance(s, dict):
                continue
            # chunks_purged counts as a change: _enforce_embed_policy DELETEs
            # chunk rows on the mtime-skip path, where indexed and pruned are
            # both 0. Without it here, a purge-only sweep leaves the live FAISS
            # index serving chunk_ids whose rows are gone — the join finds
            # nothing and those ghosts punch holes in the fixed candidate
            # window until the process restarts.
            if (s.get("indexed") or 0) or (s.get("pruned") or 0) or (s.get("chunks_purged") or 0):
                logger.info(
                    "Vault watch: shared vault changed (indexed=%s pruned=%s "
                    "chunks_purged=%s)",
                    s.get("indexed") or 0, s.get("pruned") or 0,
                    s.get("chunks_purged") or 0,
                )
                _invalidate_shared_faiss()
                break
    except Exception:
        logger.exception("Vault watch: shared vault sweep failed")
    return stats


def _report_sweep(stats: dict) -> bool:
    """Log per-vault sweep activity; True if any vault actually changed.

    Only speak when something actually changed; an idle sweep is noise.
    """
    changed = False
    for vault, s in (stats or {}).items():
        if not isinstance(s, dict):
            continue
        indexed = s.get("indexed") or 0
        pruned = s.get("pruned") or 0
        errors = s.get("errors") or 0
        # chunks_purged counts as a change here for the same reason it does in
        # the FAISS invalidation gate: a purge-only sweep is real work, and
        # treating it as idle makes it invisible in the logs while the index
        # it invalidated quietly rebuilds.
        chunks_purged = s.get("chunks_purged") or 0
        if indexed or pruned or chunks_purged:
            changed = True
        if indexed or pruned or chunks_purged or errors:
            logger.info(
                "Vault watch: %s indexed=%s pruned=%s chunks_purged=%s "
                "errors=%s",
                vault, indexed, pruned, chunks_purged, errors,
            )
    return changed


async def _vault_watch_runner():
    interval = _vault_watch_interval()
    logger.info("Vault watch enabled: incremental ingest every %ss", interval)
    # Deliberately NOT `while _running`: that global is still False at module
    # scope and only flips inside _serve_unix_socket, so a task created here in
    # main() can reach the loop first and exit silently before ever sweeping.
    # Shutdown cancels this task explicitly, which is the reliable signal.
    while True:
        try:
            stats = await asyncio.to_thread(_vault_watch_sweep_once)
            if _report_sweep(stats):
                # Indexing the file on disk is not enough. _agent_vault_retrieval
                # memoizes a RetrievalEngine per vault, so a live daemon keeps
                # answering from the engine it built at startup and newly indexed
                # notes stay invisible until restart -- verified: a note that
                # recall could not find became FTS rank 1 immediately after a
                # daemon restart, with no change to the index itself.
                _vault_retrieval_cache.clear()
                logger.info("Vault watch: cleared per-vault retrieval cache")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed sweep must never take the daemon down; try again next tick.
            logger.exception("Vault watch sweep failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


def _decay_enabled() -> bool:
    """Run the scheduled decay pass (MINNI_DECAY=off to disable)."""
    return (os.environ.get("MINNI_DECAY", "on") or "on").strip().lower() != "off"


def _decay_interval() -> int:
    try:
        raw = int(os.environ.get("MINNI_DECAY_INTERVAL", "86400"))
    except (TypeError, ValueError):
        return 86400
    # decay.run_decay's own contract is "should be called daily"; below an hour
    # the pass costs more than the freshness it buys, since decay_score moves
    # on a multi-day half-life.
    return max(3600, raw)


def _decay_sweep_once() -> dict:
    """Blocking decay pass over the shared index and every vault. Runs off-loop."""
    from minni.decay import run_decay_all_indexes

    return run_decay_all_indexes(DEFAULT_CONFIG)


async def _decay_runner():
    """Audit #225-R2: run_decay was reachable ONLY from the manual CLI and no
    launchd job ever called it, so every document sat at decay_score=1.0 —
    including documents indexed months earlier, against a declared 7-day
    half-life. Recall was scoring a corpus with decay structurally disabled.

    The pass is idempotent by construction: new_score is recomputed from
    absolute timestamps and access_count, never from the previous score, so a
    restart-heavy machine that sweeps more often than planned converges to the
    same scores rather than compounding them.

    Same shutdown contract as _vault_watch_runner: `while True` plus explicit
    task cancellation, because the `_running` global is still False when main()
    creates this task.
    """
    interval = _decay_interval()
    logger.info("Decay pass enabled: every %ss", interval)
    # First sweep is deferred: daemon start is already paying for socket setup,
    # migrations and model warmup, and decay is never urgent to the second.
    initial_delay = min(300, interval)
    try:
        await asyncio.sleep(initial_delay)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            stats = await asyncio.to_thread(_decay_sweep_once)
            for index_name, s in (stats or {}).items():
                if not isinstance(s, dict):
                    continue
                if s.get("error"):
                    logger.warning("Decay: %s failed: %s", index_name, s["error"])
                elif s.get("updated"):
                    logger.info(
                        "Decay: %s updated=%s reinforced=%s",
                        index_name, s.get("updated"), s.get("reinforced"),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed pass must never take the daemon down; retry next tick.
            logger.exception("Decay pass failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


def _backfill_enabled() -> bool:
    """Drain the embedding backlog (MINNI_BACKFILL=off to disable)."""
    return (os.environ.get("MINNI_BACKFILL", "on") or "on").strip().lower() != "off"


def _backfill_interval() -> int:
    try:
        raw = int(os.environ.get("MINNI_BACKFILL_INTERVAL", "3600"))
    except (TypeError, ValueError):
        return 3600
    return max(300, raw)


def _backfill_sweep_once() -> dict:
    """Blocking bounded backfill pass over every index. Runs off-loop.

    grok-review round 1 (finding 1): committing chunk_embeddings rows is not
    enough to make a document searchable on a WARM daemon —
    retrieval._ensure_faiss_loaded early-returns while faiss_index.count > 0, so
    the live semantic index never sees rows written underneath it and coverage
    would climb while the documents stayed invisible to the semantic leg until a
    restart. Push each backfilled document into the live index through the same
    path store-time indexing uses.
    """
    from minni.backfill import run_backfill_all_indexes

    def _refresh(chunk_ids, vectors):
        engine = _lazy_retrieval()
        engine._refresh_live_faiss(chunk_ids, vectors)

    results = run_backfill_all_indexes(DEFAULT_CONFIG, on_vectors=_refresh)

    # grok-review round 2 (finding 2): on_vectors covers the SHARED index only.
    # Vault engines memoized in _vault_retrieval_cache keep their warm FAISS
    # index (count > 0 → _ensure_faiss_loaded early-returns), so backfilled
    # vault rows would stay invisible to semantic recall until restart — the
    # same blindness the shared-index refresh above exists to prevent. Drop the
    # cache when any vault made progress, exactly as the vault watch does; the
    # next search rebuilds the engine and loads the new rows.
    for name, s in results.items():
        if name == "shared" or not isinstance(s, dict):
            continue
        docs = s.get("documents")
        if isinstance(docs, dict) and (docs.get("documents") or 0) > 0:
            _vault_retrieval_cache.clear()
            logger.info(
                "Backfill: vault %s gained vectors — cleared per-vault "
                "retrieval cache", name,
            )
            break

    # R7: reconcile episodic_fts on every sweep, not only in migration 018.
    # 018 catches its own exceptions so a failed data repair cannot roll back
    # the schema batch — but that also stamps the version, so a transient
    # failure (a locked DB on a contended start) would abandon the repair
    # permanently and the pre-trigger events would stay unsearchable forever.
    # A log line is not a queue; this is the same queue, and the reconcile is
    # idempotent, so a healthy database pays only the count query.
    #
    # The commit belongs HERE, not inside reconcile_episodic_fts: the other
    # caller is migration 018, which runs inside _flush_batch's BEGIN IMMEDIATE,
    # and an inner commit there would prematurely commit the whole migration
    # batch — the partial-batch hazard 018 is written to avoid. _get_conn()
    # opts out of db.cursor()'s auto-commit contract, so without this the INSERT
    # sits in an open transaction: invisible to every other connection (the
    # backfill would report success while recall still found nothing) and
    # holding a write lock that blocks every other daemon writer.
    try:
        from minni.db import SovereignDB
        from minni.episodic import reconcile_episodic_fts

        _conn = SovereignDB.shared(DEFAULT_CONFIG)._get_conn()
        try:
            episodic = reconcile_episodic_fts(_conn)
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise
        if episodic["inserted"]:
            logger.info(
                "Backfill: indexed %d episodic event(s) missing from episodic_fts",
                episodic["inserted"],
            )
        results["episodic_fts"] = episodic
    except Exception as exc:
        logger.warning("Backfill: episodic FTS reconcile failed: %s", exc)
        results["episodic_fts"] = {"error": str(exc)}

    return results


async def _backfill_runner():
    """Audit #225-R6 / GA1-1: two write paths degrade to "no vector" and neither
    ever retried. 381 of 879 shared-index documents had no chunk_embeddings rows
    and 409 learnings had a NULL embedding — both permanently excluded from
    semantic recall, because the degraded status was logged and nothing queued a
    retry. A log line is not a queue; this is the queue.

    Bounded per pass (backfill.DEFAULT_BATCH) so the drain never holds a long
    write lock against live recall — the backlog empties over several passes.
    """
    interval = _backfill_interval()
    logger.info("Embedding backfill enabled: every %ss", interval)
    initial_delay = min(600, interval)
    try:
        await asyncio.sleep(initial_delay)
    except asyncio.CancelledError:
        raise
    while True:
        try:
            stats = await asyncio.to_thread(_backfill_sweep_once)
            for index_name, result in (stats or {}).items():
                if not isinstance(result, dict):
                    continue
                if result.get("error"):
                    logger.warning(
                        "Backfill: %s failed: %s", index_name, result["error"]
                    )
                    continue
                docs = result.get("documents") or {}
                learnings = result.get("learnings") or {}
                if docs.get("documents") or learnings.get("embedded"):
                    logger.info(
                        "Backfill: %s — %s document(s), %s learning(s) embedded",
                        index_name, docs.get("documents", 0),
                        learnings.get("embedded", 0),
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Embedding backfill pass failed")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


# ── Footprint watchdog (#284) ─────────────────────────────────────────────
# PyTorch MPS can accumulate IOAccelerator regions without bound under
# concurrent encode. Cap process maxrss and exit cleanly so launchd restarts;
# persist restart_count so health surfaces the loop (H5).

_DEFAULT_FOOTPRINT_CAP_MB = 4096  # well under the 15.5GB incident; operator override via env
_DEFAULT_FOOTPRINT_WATCH_INTERVAL_S = 30


def _footprint_watchdog_enabled() -> bool:
    """Periodic footprint self-check (MINNI_FOOTPRINT_WATCHDOG=off to disable)."""
    return (os.environ.get("MINNI_FOOTPRINT_WATCHDOG", "on") or "on").strip().lower() != "off"


def _footprint_cap_bytes(default_mb: int = _DEFAULT_FOOTPRINT_CAP_MB) -> int:
    """Parse MINNI_FOOTPRINT_CAP_MB defensively; malformed → default. Never crash."""
    raw = (os.environ.get("MINNI_FOOTPRINT_CAP_MB") or "").strip()
    if not raw:
        return default_mb * 1024 * 1024
    try:
        mb = int(raw)
        if mb > 0:
            return mb * 1024 * 1024
    except ValueError:
        logger.warning(
            "MINNI_FOOTPRINT_CAP_MB=%r is not a positive integer; using default %d",
            raw, default_mb,
        )
    return default_mb * 1024 * 1024


def _footprint_watch_interval() -> float:
    raw = (os.environ.get("MINNI_FOOTPRINT_WATCH_INTERVAL") or "").strip()
    if not raw:
        return float(_DEFAULT_FOOTPRINT_WATCH_INTERVAL_S)
    try:
        value = float(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    return float(_DEFAULT_FOOTPRINT_WATCH_INTERVAL_S)


def _current_footprint_bytes() -> int:
    """Process max RSS in bytes.

    macOS ``ru_maxrss`` is already bytes; Linux reports kilobytes. Match the
    platform, never guess.

    Deliberately per-process (getrusage), never system-wide VM stats: on this
    fleet's macOS 27 beta, ``host_statistics64(HOST_VM_INFO64)`` is a
    confirmed-broken API (Apple Developer Forums thread 796568). Do not
    "simplify" this to host_statistics64/psutil.virtual_memory() — it would
    silently report garbage on this OS build.
    """
    import resource
    import sys

    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return int(usage)
    return int(usage) * 1024


def _footprint_exceeds_cap(current_bytes: int, cap_bytes: int) -> bool:
    return current_bytes > cap_bytes


def _watchdog_state_path() -> Path:
    """Persist under the same secure run/ dir as the Unix socket."""
    override = (os.environ.get("MINNI_WATCHDOG_STATE_PATH") or "").strip()
    if override:
        return Path(override)
    return SECURE_RUN_DIR / "watchdog_state.json"


def _default_watchdog_state() -> dict:
    return {
        "restart_count": 0,
        "last_restart_reason": None,
        "last_restart_at": None,
    }


def _read_watchdog_state(path: Optional[Path] = None) -> dict:
    """Load restart state; missing/corrupt → clean default, never raises."""
    state_path = path if path is not None else _watchdog_state_path()
    try:
        raw = state_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return _default_watchdog_state()
        count = data.get("restart_count", 0)
        try:
            count_i = int(count)
            if count_i < 0:
                count_i = 0
        except (TypeError, ValueError):
            count_i = 0
        return {
            "restart_count": count_i,
            "last_restart_reason": data.get("last_restart_reason"),
            "last_restart_at": data.get("last_restart_at"),
        }
    except FileNotFoundError:
        return _default_watchdog_state()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _default_watchdog_state()


def _record_watchdog_trip(reason: str, path: Optional[Path] = None) -> dict:
    """Increment restart_count and persist; returns the new state dict."""
    from datetime import datetime, timezone

    state_path = path if path is not None else _watchdog_state_path()
    prior = _read_watchdog_state(state_path)
    state = {
        "restart_count": int(prior.get("restart_count") or 0) + 1,
        "last_restart_reason": reason,
        "last_restart_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(state_path)
    except OSError:
        logger.exception("footprint watchdog: failed to persist state at %s", state_path)
    return state


def _footprint_watchdog_tick(
    *,
    measure=_current_footprint_bytes,
    cap_bytes: Optional[int] = None,
    state_path: Optional[Path] = None,
    on_trip=None,
) -> Optional[str]:
    """One check. On trip: persist state, call on_trip(reason), return reason.

    Pure(ish) for tests — inject measure/cap/state_path/on_trip. Returns None
    when under cap.
    """
    current = int(measure())
    cap = int(cap_bytes if cap_bytes is not None else _footprint_cap_bytes())
    if not _footprint_exceeds_cap(current, cap):
        return None
    current_mb = current / (1024 * 1024)
    cap_mb = cap / (1024 * 1024)
    reason = (
        f"footprint_cap_exceeded: {current_mb:.0f}MB > {cap_mb:.0f}MB cap"
    )
    _record_watchdog_trip(reason, path=state_path)
    if on_trip is not None:
        on_trip(reason)
    return reason


def _initiate_graceful_shutdown(reason: str, *, tasks=None, level: str = "warning") -> None:
    """Flip _running, close the socket, cancel background tasks.

    Shared by the SIGTERM/SIGINT handler and the footprint watchdog so a
    watchdog trip takes the same cleanup path as an operator signal — never
    ``sys.exit()`` raw.
    """
    global _running
    log = logger.warning if level == "warning" else logger.info
    log("%s", reason)
    _running = False
    if _server is not None:
        _server.close()
    for task in list(tasks or []):
        if task is not None:
            task.cancel()


async def _footprint_watchdog_runner(
    *,
    measure=_current_footprint_bytes,
    cap_bytes: Optional[int] = None,
    state_path: Optional[Path] = None,
    interval: Optional[float] = None,
    on_trip=None,
    shutdown_tasks=None,
):
    """Periodic footprint self-check. Trips → persist + graceful shutdown."""
    wait = interval if interval is not None else _footprint_watch_interval()
    cap = cap_bytes if cap_bytes is not None else _footprint_cap_bytes()
    logger.info(
        "Footprint watchdog enabled: cap=%dMB interval=%ss",
        cap // (1024 * 1024), wait,
    )

    def _default_on_trip(reason: str) -> None:
        _initiate_graceful_shutdown(
            f"Footprint watchdog: {reason} — shutting down for launchd restart",
            tasks=shutdown_tasks() if callable(shutdown_tasks) else shutdown_tasks,
        )

    trip_cb = on_trip if on_trip is not None else _default_on_trip

    while True:
        try:
            tripped = await asyncio.to_thread(
                _footprint_watchdog_tick,
                measure=measure,
                cap_bytes=cap,
                state_path=state_path,
                on_trip=trip_cb,
            )
            if tripped:
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Footprint watchdog pass failed")
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise


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


def _handle_cache_reload(params: dict, request_id: Any) -> dict:
    """In-band, govern-gated equivalent of the SIGHUP cache flush (W2).

    Agents previously had no in-band way to invalidate identity/runtime caches
    after an operator edited a principals/*.json file — they shelled out to
    launchctl kickstart, which does not even clear the right cache. This routes
    through the SAME _reload_runtime_config() effect the SIGHUP handler uses, so
    the RPC and signal paths can never drift. It requires the 'govern' capability
    (see RPC_CAPABILITY_REQUIREMENTS) and is intentionally absent from
    RECOVERY_ALLOWED_METHODS: a pre-identity caller must not be able to force
    thundering-herd cache re-resolution.
    """
    _reload_runtime_config()
    return _make_response(
        {"cleared": ["agent_scope_for", "vault_retrieval"]}, request_id
    )


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
    # W2: in-band cache flush (govern-gated; NOT recovery-allowed).
    "cache_reload":           _handle_cache_reload,
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


def _rpc_worker_count(default: int = 8) -> int:
    """Parse MINNI_RPC_WORKERS defensively.

    An optional tuning knob must never kill the daemon at startup: empty or
    non-numeric values (a launchd plist typo) fall back to the default, and
    non-positive numbers clamp to 1.
    """
    raw = os.environ.get("MINNI_RPC_WORKERS", "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "MINNI_RPC_WORKERS=%r is not an integer; using default %d",
            raw, default,
        )
        return default


def _raise_fd_ceiling(target: int = 16384) -> int:
    """Raise RLIMIT_NOFILE's soft limit toward ``target`` (capped at the hard
    limit). Returns the resulting soft limit.

    Each pooled worker thread holds SQLite handles (db + wal) per database
    file, so the daemon's fd footprint scales with executor width × database
    count. launchd's default soft limit is low enough that sustained
    multi-agent load exhausts it — accept() then fails with EMFILE, every
    client sees EPIPE, and the job still reports 'running'. Non-fatal on
    failure: the executor bound in main() is the other half of the defense.
    """
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        ceiling = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < ceiling:
            resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
            logger.info("RLIMIT_NOFILE soft limit raised %d -> %d", soft, ceiling)
            return ceiling
        return soft
    except Exception:
        logger.exception("could not raise RLIMIT_NOFILE (non-fatal)")
        return -1


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

    # #284: pin daemon models to CPU by default BEFORE any singleton warmup.
    # setdefault preserves an explicit operator override (e.g. launchd plist
    # MINNI_MODEL_DEVICE=mps). Indexer/backfill never enter main(), so they
    # keep library auto-select (MPS on Apple Silicon).
    os.environ.setdefault("MINNI_MODEL_DEVICE", "cpu")

    # Deploy honesty (GA1-3): snapshot which code this process is running —
    # checkout + HEAD sha at start — so `status` can later report truthfully
    # when the checkout has moved on and the daemon is executing stale code.
    try:
        from minni.minnid_runtime.deploy_honesty import capture_start_state

        capture_start_state()
    except Exception:
        pass  # the lazy fallback in deploy_status() still answers

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
        db = SovereignDB.shared(DEFAULT_CONFIG)
        db._get_conn()
        logger.info("Database initialized/migrated on startup.")
    except Exception:
        logger.exception("Eager database initialization failed")

    _raise_fd_ceiling()

    loop = asyncio.new_event_loop()
    # Bound the pool that asyncio.to_thread dispatches sync RPC handlers onto.
    # Every pooled thread accretes one SQLite connection (db + wal fds) per
    # database file it touches and never releases it, so the executor width is
    # the direct multiplier on the daemon's steady-state fd footprint. The
    # stdlib default (min(32, cpus + 4)) is wide enough to breach the default
    # soft fd limit under sustained multi-agent load; requests beyond the bound
    # queue instead of stacking new fd-holding threads.
    workers = _rpc_worker_count()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="minnid-rpc"
        )
    )
    main_task = loop.create_task(_serve_unix_socket(_unix_socket_path))

    http_task = None
    if args.port:
        http_task = loop.create_task(_serve_http(args.host, args.port))

    # Opt-in AFM loop runner (gated by MINNI_AFM_LOOP). Dormant unless enabled.
    afm_task = None
    if _afm_loop_enabled(DEFAULT_CONFIG):
        afm_task = loop.create_task(_afm_loop_runner())

    # Vault watch runner. On by default (MINNI_VAULT_WATCH=off to disable):
    # unlike the AFM loop this is not an enhancement, it is what keeps recall
    # able to see anything written since the last manual index.
    vault_watch_task = None
    if _vault_watch_enabled():
        vault_watch_task = loop.create_task(_vault_watch_runner())

    # Scheduled decay pass (MINNI_DECAY=off to disable). Nothing else calls
    # run_decay outside the manual CLI, so without this the corpus never decays.
    decay_task = None
    if _decay_enabled():
        decay_task = loop.create_task(_decay_runner())

    # Embedding backfill (MINNI_BACKFILL=off to disable). Without it the
    # document/learning vector gap is permanent — nothing else retries.
    backfill_task = None
    if _backfill_enabled():
        backfill_task = loop.create_task(_backfill_runner())

    # Model warmup. Scheduled AFTER the socket is being served, so the daemon is
    # answerable during the load: an early caller still gets the lazy path, it
    # just no longer has to be the one that pays for it.
    warmup_task = None
    if _warmup_enabled():
        warmup_task = loop.create_task(_warmup_runner())

    # Footprint watchdog (#284). Cap maxrss and restart cleanly via launchd
    # rather than silently eating the machine. On by default.
    footprint_task = None
    background_tasks = lambda: [
        main_task, http_task, afm_task, vault_watch_task,
        decay_task, backfill_task, warmup_task, footprint_task,
    ]
    if _footprint_watchdog_enabled():
        footprint_task = loop.create_task(
            _footprint_watchdog_runner(shutdown_tasks=background_tasks)
        )

    def _shutdown(signum, frame):
        sig_name = signal.Signals(signum).name
        _initiate_graceful_shutdown(
            f"Received {sig_name}, shutting down…",
            tasks=background_tasks(),
            level="info",
        )

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

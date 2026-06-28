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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# ── Logging ───────────────────────────────────────────────────────────────

logger = logging.getLogger("minnid")

# ── Sovereign engine imports ─────────────────────────────────────────────
# The daemon lives alongside the Sovereign engine so we can import its
# modules directly.  We add the engine directory to sys.path if needed.

_ENGINE_DIR = Path(__file__).resolve().parent
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

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

RECOVERY_ALLOWED_METHODS = ("ping", "status", "health_report", "hygiene_report")

# P0 principal enforcement: required stamped capability per DB-backed RPC method.
# Methods with bespoke ownership gates (resolve_candidate, resolve_contradiction,
# minni_ack_handoff) are intentionally omitted — they enforce principal binding
# inside the handler. Recovery/diagnostic methods are also omitted.
_RPC_CAPABILITY_REQUIREMENTS: Dict[str, str] = {
    "search": "search",
    "feedback": "feedback",
    "trace": "read",
    "expand": "read",
    "sm_drill": "read",
    "sm_export_pack": "export",
    "read": "read",
    "learn": "learn",
    "minni_subscribe_contradictions": "read",
    "log_event": "log_event",
    "daemon.handoff": "handoff",
    "handoff": "handoff",
    "minni_ack_handoff": "handoff",
    "minni_list_pending_handoffs": "handoff",
    "minni_await_handoff": "handoff",
    "stage_candidate": "learn",
    "list_candidates": "learn",
    "ax_snapshot_store": "read",
    "ax_snapshot_get": "read",
    "vault_index_doc": "learn",
    "daemon.compile": "read",
    "daemon.endorse": "govern",
}


def _enforce_method_capability(
    method_name: str,
    principal: Optional[EffectivePrincipal],
    request_id: Any,
) -> Optional[dict]:
    """Return a JSON-RPC error when the stamped principal lacks the method cap."""
    if principal is None:
        return None
    required = _RPC_CAPABILITY_REQUIREMENTS.get(method_name)
    if not required or principal.can(required):
        return None
    return make_capability_denied_error(
        required,
        method_name,
        request_id,
        principal_id=principal.agent_id,
    )


@dataclass(frozen=True)
class ProvenanceResolution:
    principal: Optional[EffectivePrincipal]
    recovery: Optional[dict]


def recover(
    reason: str,
    caller_ctx: dict,
    *,
    render_mode: str = "machine",
) -> dict | str:
    """Return the single fail-loud route for unresolved provenance.

    Unknown callers get a diagnostic route, never a synthesized/default identity
    and never silence. ``render_mode="machine"`` is the JSON-RPC payload;
    ``"human"`` is for interactive surfaces.
    """
    method = str(caller_ctx.get("method") or "unknown")
    supplied = caller_ctx.get("supplied_agent_id")
    route = {
        "zone": "pre_identity",
        "surface": "diagnostic",
        "allowed_methods": list(RECOVERY_ALLOWED_METHODS),
        "next": "Call status/health_report, then stamp the runtime with MINNI_AGENT_ID and a principals/<agent>.json entry.",
    }
    remediation = [
        "Stamp this runtime surface with MINNI_AGENT_ID.",
        "Create or fix the matching operator-owned principals/<agent>.json file.",
        "Send SIGHUP to minnid or restart it so identity caches reload.",
        f"Use one of the pre-identity diagnostic methods: {', '.join(RECOVERY_ALLOWED_METHODS)}.",
    ]
    if render_mode == "human":
        supplied_msg = f" supplied={supplied!r}" if supplied is not None else ""
        return (
            f"Minni provenance recovery required for method {method!r}{supplied_msg}: "
            f"{reason}. Stamp this runtime with MINNI_AGENT_ID and a matching "
            f"principals/<agent>.json file; diagnostics available via "
            f"{', '.join(RECOVERY_ALLOWED_METHODS)}."
        )
    if render_mode != "machine":
        raise ValueError("render_mode must be 'machine' or 'human'")
    return {
        "ok": False,
        "status": "recovery_required",
        "reason": reason,
        "identity": None,
        "caller": {
            "method": method,
            "supplied_agent_id": supplied,
        },
        "route": route,
        "remediation": remediation,
    }


def _provenance_claim(method_name: str, params: dict) -> Optional[str]:
    if method_name in {"daemon.handoff", "handoff"}:
        claim = params.get("from_agent")
    else:
        claim = params.get("agent_id")
    if claim is None:
        return None
    claim = str(claim).strip()
    return claim or None


def resolve_provenance(request: dict, *, transport: str = "uds") -> ProvenanceResolution:
    """Resolve the caller at the daemon gate before any handler runs."""
    method_name = str(request.get("method", ""))
    params = request.get("params", {}) or {}
    if not isinstance(params, dict):
        return ProvenanceResolution(
            principal=None,
            recovery=recover(
                "invalid_params",
                {"method": method_name, "supplied_agent_id": None},
                render_mode="machine",
            ),
        )
    supplied = _provenance_claim(method_name, params)
    try:
        principal = resolve_effective_principal(
            supplied_agent_id=supplied,
            transport=transport,
        )
    except IdentityMismatchError as exc:
        return ProvenanceResolution(
            principal=None,
            recovery=recover(
                "identity_mismatch",
                {
                    "method": method_name,
                    "supplied_agent_id": exc.supplied,
                    "stamped_agent_id": exc.stamped,
                },
                render_mode="machine",
            ),
        )
    except Exception as exc:
        return ProvenanceResolution(
            principal=None,
            recovery=recover(
                f"provenance_error: {exc}",
                {"method": method_name, "supplied_agent_id": supplied},
                render_mode="machine",
            ),
        )
    return ProvenanceResolution(principal=principal, recovery=None)


def _handler_principal(
    params: dict,
    request_id: Any,
    *,
    claim_key: str = "agent_id",
) -> tuple[Optional[EffectivePrincipal], Optional[dict]]:
    """Return the dispatch-stamped principal, resolving only for direct calls."""
    stamped = params.get("_principal") if isinstance(params, dict) else None
    if isinstance(stamped, EffectivePrincipal):
        return stamped, None
    supplied = params.get(claim_key) if isinstance(params, dict) else None
    try:
        return (
            resolve_effective_principal(supplied_agent_id=supplied, transport="uds"),
            None,
        )
    except IdentityMismatchError as exc:
        return None, make_mismatch_error(exc.supplied, exc.stamped, request_id)

# G12 (SEC-003): vault root binding guard. Enforces that any vault_path accepted
# from wire (hygiene, compile, endorse, handoff derived) realpath-resolves inside
# the stamped EffectivePrincipal's allowed_vault_roots. Denies .. / symlink escapes.
# Structured error (-32003) + redacted log; no secret leak. Non-strict synthesis
# (G11) preserves prior wide-open behavior until operator ships principals/*.json.
def _guard_vault_root(
    params: dict, vault_path: str | Path, request_id: Any, *, label: str = "vault"
) -> Optional[dict]:
    """Resolve stamped principal (honoring G11) and check allows_vault_root.
    Returns JSON-RPC error dict on denial/mismatch, or None if allowed.
    """
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    if not principal.allows_vault_root(vault_path):
        try:
            resolved = str(Path(vault_path).resolve())
        except Exception:
            resolved = str(vault_path)[:120]
        logger.warning(
            "vault_root_denied label=%s principal=%s resolved=%s",
            label, principal.agent_id, resolved[:120]
        )
        return _make_error(
            -32003,
            f"vault_root_denied: {label} path outside principal.allowed_vault_roots (realpath + relative check; traversal/symlink escape rejected)",
            request_id,
        )
    return None


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
    for row in results:
        row["src"] = src
        # Previous vault-scope experiments exposed ownership/index paths inline.
        # Keep recall tiny; full provenance is available through drill.
        row.pop("source_agent", None)
        row.pop("source_index_db_path", None)
        provenance = row.get("provenance")
        if isinstance(provenance, dict):
            provenance.pop("source_agent", None)
            provenance.pop("source_index_db_path", None)
    return results


def _result_identity(row: dict) -> tuple:
    return (
        str(row.get("source") or row.get("path") or ""),
        row.get("doc_id"),
        row.get("chunk_id"),
    )


def _merge_document_results(result_sets: list, limit: int, *, prefer_personal: bool = False) -> list:
    merged = []
    for rows in result_sets:
        merged.extend(rows)
    if prefer_personal:
        deduped = {}
        for row in merged:
            key = _result_identity(row)
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = row
                continue
            row_score = float(row.get("score") or 0.0)
            existing_score = float(existing.get("score") or 0.0)
            row_priority = 1 if row.get("src") == "p" else 0
            existing_priority = 1 if existing.get("src") == "p" else 0
            if (row_score, row_priority) > (existing_score, existing_priority):
                deduped[key] = row
        merged = list(deduped.values())
    return sorted(
        merged,
        key=lambda row: (
            float(row.get("score") or 0.0),
            1 if prefer_personal and row.get("src") == "p" else 0,
        ),
        reverse=True,
    )[:limit]


def _resolve_document_scope(params: dict) -> str:
    raw_scope = params.get("scope")
    if raw_scope is not None:
        scope = str(raw_scope)
        if scope not in {"personal", "combined", "both"}:
            raise ValueError("scope must be personal, combined, or both")
        return scope
    if bool(params.get("cross_agent", False)):
        return "combined"
    return "both"


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


def _make_response(result: Any, request_id: Any = None) -> dict:
    """Build a JSON-RPC success response."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _make_error(code: int, message: str, request_id: Any = None) -> dict:
    """Build a JSON-RPC error response."""
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


_HANDOFF_KINDS = frozenset({"handoff", "candidate_learning", "request", "answer"})
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|password|secret|credential|private[_ -]?key)\b\s*[:=]\s*([^\s,;<>\"']+)"),
    re.compile(r"(?i)\b(bearer|access[_-]?token|refresh[_-]?token|token)\b\s*[:=]\s*([^\s,;<>\"']+)"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
]
_LOCAL_PATH_PATTERN = re.compile(r"(?<!\w)(?:/Users/[^ \n\r\t\"'<>]+|/Volumes/[^ \n\r\t\"'<>]+|/private/[^ \n\r\t\"'<>]+)")


def _redact_text(text: str) -> tuple[str, bool]:
    redacted = text
    changed = False
    for pattern in _SECRET_PATTERNS:
        if pattern.search(redacted):
            if pattern.groups >= 2:
                redacted = pattern.sub(lambda m: f"{m.group(1)}=[REDACTED]", redacted)
            else:
                redacted = pattern.sub("[REDACTED]", redacted)
            changed = True
    if _LOCAL_PATH_PATTERN.search(redacted):
        redacted = _LOCAL_PATH_PATTERN.sub("[REDACTED_PATH]", redacted)
        changed = True
    return redacted, changed


def _redact_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        items = []
        changed = False
        for item in value:
            redacted, item_changed = _redact_value(item)
            items.append(redacted)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        obj = {}
        changed = False
        for key, item in value.items():
            redacted, item_changed = _redact_value(item)
            obj[key] = redacted
            changed = changed or item_changed
        return obj, changed
    return value, False


def _agent_env_key(agent_id: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", agent_id.upper()).strip("_")


def _default_agent_vault(agent_id: str) -> Path:
    aliases = {
        "claude-code": "claudecode",
        "claudecode": "claudecode",
        "codex": "codex",
        "hermes": "hermes",
        "openclaw": "openclaw",
        # Preserve hyphenated canonical slugs (the strip fallback would map
        # grok-build -> grokbuild-vault, which the Grok overlay never polls).
        "grok-build": "grok-build",
        "grok-beta": "grok-beta",
        "grok": "grok",
    }
    slug = aliases.get(agent_id, re.sub(r"[^a-z0-9]+", "", agent_id.lower()) or "agent")
    return Path(CANONICAL_SOVEREIGN_HOME) / f"{slug}-vault"


def _agent_vault(agent_id: str) -> tuple[Path, bool]:
    mapping_raw = os.environ.get("MINNI_AGENT_VAULTS", "")
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
            if isinstance(mapping, dict) and agent_id in mapping:
                return Path(os.path.expanduser(str(mapping[agent_id]))), True
        except json.JSONDecodeError:
            logger.warning("Invalid MINNI_AGENT_VAULTS JSON; using defaults")

    env_key = f"MINNI_{_agent_env_key(agent_id)}_VAULT_PATH"
    if os.environ.get(env_key):
        return Path(os.path.expanduser(os.environ[env_key])), True
    return _default_agent_vault(agent_id), False


def _ensure_handoff_vault(vault_path: Path) -> None:
    for rel in (
        "raw",
        "wiki",
        "wiki/handoffs",
        "schema",
        "logs",
        "inbox",
        "outbox",
    ):
        (vault_path / rel).mkdir(parents=True, exist_ok=True)
    log_path = vault_path / "log.md"
    if not log_path.exists():
        log_path.write_text("# Minni Log\n\n", encoding="utf-8")
    index_path = vault_path / "index.md"
    if not index_path.exists():
        index_path.write_text("# Minni Index\n\n", encoding="utf-8")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:80] or "handoff"


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_iso_ts(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        from datetime import datetime, timezone
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except Exception:
        return None


def _iso_from_epoch(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _known_agent_vaults() -> list[Path]:
    vaults = []
    mapping_raw = os.environ.get("MINNI_AGENT_VAULTS", "")
    if mapping_raw:
        try:
            mapping = json.loads(mapping_raw)
            if isinstance(mapping, dict):
                vaults.extend(Path(os.path.expanduser(str(v))) for v in mapping.values())
        except json.JSONDecodeError:
            pass
    # Resolve at call time (matches config.py providers/secrets pattern) so
    # MINNI_HOME overrides are honored without hardcoding ~/.minni.
    root = Path(os.environ.get("MINNI_HOME", os.path.expanduser("~/.minni")))
    if root.exists():
        vaults.extend(root.glob("*-vault"))
    return list(dict.fromkeys(vaults))


# SEC-014: caps for audit-log fields. Mirrors the TS helper in
# plugins/minni/src/vault.ts so a forged summary written by one
# daemon cannot be parsed as multiple `## [...]` entries by either reader.
_AUDIT_SUMMARY_MAX = 500
_AUDIT_DETAIL_LINE_MAX = 1000
_AUDIT_DETAIL_BLOCK_MAX = 4000


def _escape_audit_field(value: str, *, mode: str = "inline", max_len: Optional[int] = None) -> str:
    """Escape an audit-log field so injected newlines or leading `#` cannot
    forge a new `## [...]` log entry.

    - mode="inline" (tool, summary): collapse \\ \\r \\n to literal escapes so
      the header line stays single-line. Prefix a leading `#` with `\\`.
    - mode="block" (details): keep real newlines but escape any per-line
      leading `#` so the parser can't mistake them for entry headers.
    """
    v = "" if value is None else str(value)
    if mode == "inline":
        v = v.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        if v.startswith("#"):
            v = "\\" + v
    else:
        v = "\n".join(("\\" + ln) if ln.startswith("#") else ln for ln in v.split("\n"))
    if max_len is not None and len(v) > max_len:
        v = v[: max(0, max_len - 1)] + "…"
    return v


def _escape_audit_details_block(raw: str) -> str:
    out_lines = []
    for ln in raw.split("\n"):
        escaped = ("\\" + ln) if ln.startswith("#") else ln
        if len(escaped) > _AUDIT_DETAIL_LINE_MAX:
            escaped = escaped[: max(0, _AUDIT_DETAIL_LINE_MAX - 1)] + "…"
        out_lines.append(escaped)
    block = "\n".join(out_lines)
    if len(block) > _AUDIT_DETAIL_BLOCK_MAX:
        block = block[: max(0, _AUDIT_DETAIL_BLOCK_MAX - 1)] + "…"
    return block


def _append_handoff_audit(vault_path: Path, tool: str, summary: str, details: dict) -> None:
    _ensure_handoff_vault(vault_path)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    safe_tool = _escape_audit_field(tool, mode="inline", max_len=200)
    safe_summary = _escape_audit_field(summary, mode="inline", max_len=_AUDIT_SUMMARY_MAX)
    raw_details = json.dumps(details, indent=2, sort_keys=True)
    safe_details = _escape_audit_details_block(raw_details)
    line = f"## [{ts}] {safe_tool} | {safe_summary}\n\n```json\n{safe_details}\n```\n\n"
    for path in (vault_path / "log.md", vault_path / "logs" / f"{ts[:10]}.md"):
        if not path.exists():
            path.write_text(f"# {ts[:10]} Minni Audit\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def _validate_handoff_packet(from_agent: str, to_agent: str, packet: Any) -> tuple[Optional[dict], Optional[str]]:
    if not isinstance(packet, dict):
        return None, "packet must be an object"
    normalized = dict(packet)
    normalized.setdefault("from_agent", from_agent)
    normalized.setdefault("to_agent", to_agent)
    normalized.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    normalized.setdefault("trace_id", str(uuid.uuid4()))
    normalized.setdefault("lease_id", f"handoff-{uuid.uuid4().hex[:16]}")
    normalized.setdefault("requires_ack", True)
    normalized.setdefault("expires_at", _iso_from_epoch(time.time() + 24 * 3600))

    required = {
        "from_agent": str,
        "to_agent": str,
        "kind": str,
        "task": str,
        "envelope": str,
        "wikilink_refs": list,
        "trace_id": str,
        "created_at": str,
        "lease_id": str,
        "requires_ack": bool,
        "expires_at": str,
    }
    for key, typ in required.items():
        if key not in normalized:
            return None, f"{key} is required"
        if not isinstance(normalized[key], typ):
            return None, f"{key} must be {typ.__name__}"
        if typ is str and not normalized[key].strip():
            return None, f"{key} must not be empty"
    if normalized["from_agent"] != from_agent:
        return None, "from_agent must match params.from_agent"
    if normalized["to_agent"] != to_agent:
        return None, "to_agent must match params.to_agent"
    if normalized["kind"] not in _HANDOFF_KINDS:
        return None, f"kind must be one of {sorted(_HANDOFF_KINDS)}"
    if not all(isinstance(ref, str) and ref.strip() for ref in normalized["wikilink_refs"]):
        return None, "wikilink_refs must be a list of strings"
    if "expires_at" in normalized and normalized["expires_at"] is not None and not isinstance(normalized["expires_at"], str):
        return None, "expires_at must be a string"
    return normalized, None


def _compile_handoff_page(sender_vault: Path, packet: dict, stamp: str) -> Optional[Path]:
    if packet.get("kind") != "handoff":
        return None
    title = packet["task"].strip()
    slug = _slugify(f"{packet['from_agent']}-to-{packet['to_agent']}-{title}")
    page_path = sender_vault / "wiki" / "handoffs" / f"{stamp[:8]}-{slug}.md"
    refs = "\n".join(f"- [[{ref.removesuffix('.md')}]]" for ref in packet.get("wikilink_refs", []))
    page = (
        "---\n"
        f"title: {title}\n"
        "type: handoff\n"
        "status: accepted\n"
        "privacy: safe\n"
        f"from_agent: {packet['from_agent']}\n"
        f"to_agent: {packet['to_agent']}\n"
        f"trace_id: {packet['trace_id']}\n"
        f"created: {packet['created_at']}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"From: `{packet['from_agent']}`\n\n"
        f"To: `{packet['to_agent']}`\n\n"
        "## Envelope\n\n"
        f"```xml\n{packet['envelope']}\n```\n\n"
        "## Wikilink References\n\n"
        f"{refs or '- None'}\n"
    )
    page_path.write_text(page, encoding="utf-8")
    return page_path


def _handle_daemon_handoff(params: dict, request_id: Any) -> dict:
    """Validate, redact, and mediate an agent-to-agent inbox handoff."""
    global _request_count
    _request_count += 1

    from_agent = str(params.get("from_agent", "")).strip()
    to_agent = str(params.get("to_agent", "")).strip()
    if not from_agent or not to_agent:
        return _make_error(-32602, "from_agent and to_agent are required", request_id)

    # G11: EffectivePrincipal stamp for the handoff path (from_agent is the identity
    # claim for the packet originator). Mismatch is rejected before any vault creation,
    # redaction, or cross-agent delivery. This closes the last public RPC that carried
    # an unauthenticated agent identity claim. (to_agent is a destination, not a
    # self-claim; it is validated by the consent contract in later G items.)
    principal, err = _handler_principal(params, request_id, claim_key="from_agent")
    if err:
        return err
    # Use the stamped value for any attribution inside this handler (future G items
    # will thread the full EffectivePrincipal object).
    from_agent = principal.agent_id

    packet, error = _validate_handoff_packet(from_agent, to_agent, params.get("packet"))
    if error:
        return _make_error(-32602, error, request_id)

    redacted_packet, redacted = _redact_value(packet)
    sender_vault, sender_explicit = _agent_vault(from_agent)
    recipient_vault, recipient_explicit = _agent_vault(to_agent)

    # G12: after G11 principal stamp, verify both derived vaults are inside allowed_vault_roots
    # (covers handoff across agents; realpath denies .. and symlink escapes to outside roots)
    for vlabel, vpath in (("sender", sender_vault), ("recipient", recipient_vault)):
        if not principal.allows_vault_root(vpath):
            logger.warning(
                "vault_root_denied handoff label=%s principal=%s", vlabel, principal.agent_id
            )
            return _make_error(
                -32003,
                f"vault_root_denied: {vlabel} vault outside principal.allowed_vault_roots",
                request_id,
            )

    # G23: harden wikilink_refs containment — no traversal out of sender's allowed vault/wiki
    # (realpath + is_relative_to + allows_vault_root; symlink-aware)
    wiki_root = sender_vault / "wiki"
    for ref in packet.get("wikilink_refs", []) or []:
        try:
            ref_str = str(ref).strip()
            if not ref_str:
                continue
            # candidate target inside wiki/ (or vault root); reject ../ or absolute escapes
            cand = (wiki_root / ref_str.lstrip("/")).resolve()
            if not cand.is_relative_to(wiki_root.resolve()) or not principal.allows_vault_root(cand):
                logger.warning("wikilink_traversal_denied ref=%s principal=%s", ref_str, principal.agent_id)
                return _make_error(
                    -32003,
                    "wikilink_traversal_denied: wikilink escapes allowed vault roots (G23)",
                    request_id,
                )
        except Exception:
            # treat resolution failure as deny
            return _make_error(-32003, "wikilink_traversal_denied: unresolvable ref", request_id)

    create_missing = os.environ.get("MINNI_HANDOFF_CREATE_MISSING_VAULTS", "1").lower() not in {"0", "false", "off"}

    if not recipient_explicit and not recipient_vault.exists() and not create_missing:
        if create_missing or sender_explicit:
            _ensure_handoff_vault(sender_vault)
        return _make_response({
            "status": "degraded",
            "delivered": False,
            "reason": f"destination vault not found for {to_agent}",
            "to_agent": to_agent,
            "recipient_vault": str(recipient_vault),
        }, request_id)

    try:
        _ensure_handoff_vault(sender_vault)
        _ensure_handoff_vault(recipient_vault)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        suffix = f"{stamp}-{_slugify(redacted_packet['task'])}-{redacted_packet['trace_id'][:8]}.json"
        outbox_path = sender_vault / "outbox" / suffix
        inbox_path = recipient_vault / "inbox" / suffix
        _write_json(outbox_path, redacted_packet)
        _write_json(inbox_path, redacted_packet)
        lease_persisted = _store_handoff_lease(redacted_packet, inbox_path, outbox_path)
        page_path = _compile_handoff_page(sender_vault, redacted_packet, stamp)
        audit_details = {
            "trace_id": redacted_packet["trace_id"],
            "from_agent": from_agent,
            "to_agent": to_agent,
            "kind": redacted_packet["kind"],
            "task": redacted_packet["task"],
            "inbox_path": str(inbox_path),
            "outbox_path": str(outbox_path),
            "handoff_page": str(page_path) if page_path else None,
            "redacted": redacted,
            "lease_id": redacted_packet["lease_id"],
            "expires_at": redacted_packet.get("expires_at"),
            "lease_persisted": lease_persisted,
        }
        _append_handoff_audit(sender_vault, "handoff_sent", redacted_packet["task"], audit_details)
        _append_handoff_audit(recipient_vault, "handoff_received", redacted_packet["task"], audit_details)
        status = "ok" if lease_persisted else "degraded"
        return _make_response({
            "status": status,
            "delivered": True,
            "lease_persisted": lease_persisted,
            "reason": None if lease_persisted else "handoff delivered to JSON packets but SQLite lease persistence failed",
            "redacted": redacted,
            "inbox_path": str(inbox_path),
            "outbox_path": str(outbox_path),
            "handoff_page": str(page_path) if page_path else None,
            "trace_id": redacted_packet["trace_id"],
            "lease_id": redacted_packet["lease_id"],
            "expires_at": redacted_packet.get("expires_at"),
        }, request_id)
    except Exception as exc:
        logger.exception("daemon.handoff failed")
        return _make_response({
            "status": "degraded",
            "delivered": False,
            "reason": str(exc),
            "to_agent": to_agent,
        }, request_id)


_HANDOFF_ACK_STATUSES = {"accepted", "rejected_stale", "rejected_contradicts", "rejected_scope"}


def _iter_handoff_files(agent_id: Optional[str] = None):
    if agent_id:
        vault, _ = _agent_vault(agent_id)
        vaults = [vault]
    else:
        vaults = _known_agent_vaults()
    for vault in vaults:
        for rel in ("inbox", "outbox"):
            directory = vault / rel
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    packet = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(packet, dict) and packet.get("lease_id"):
                    yield vault, path, packet


def _write_matching_lease_packets(lease_id: str, updates: dict) -> list[str]:
    changed = []
    for _vault, path, packet in _iter_handoff_files():
        if packet.get("lease_id") != lease_id:
            continue
        packet.update(updates)
        _write_json(path, packet)
        changed.append(str(path))
    return changed


def _store_handoff_lease(packet: dict, inbox_path: Path, outbox_path: Path) -> bool:
    """Persist the authoritative handoff lease row while keeping JSON packets."""
    try:
        created_at = _parse_iso_ts(packet.get("created_at")) or time.time()
        expires_at = _parse_iso_ts(packet.get("expires_at"))
        status = "pending" if packet.get("requires_ack", True) else "not_required"
        db = _lazy_writeback().db
        with db.cursor() as c:
            c.execute(
                """INSERT INTO handoff_leases
                   (lease_id, from_agent, to_agent, task, status, contradicts_id,
                    inbox_path, outbox_path, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(lease_id) DO UPDATE SET
                     from_agent = excluded.from_agent,
                     to_agent = excluded.to_agent,
                     task = excluded.task,
                     inbox_path = excluded.inbox_path,
                     outbox_path = excluded.outbox_path,
                     expires_at = excluded.expires_at""",
                (
                    packet["lease_id"],
                    packet["from_agent"],
                    packet["to_agent"],
                    packet.get("task"),
                    status,
                    packet.get("contradicts_id"),
                    str(inbox_path),
                    str(outbox_path),
                    created_at,
                    expires_at,
                ),
            )
        return True
    except Exception as exc:
        logger.warning("Could not persist handoff lease %r: %s", packet.get("lease_id"), exc)
        return False


def _update_handoff_lease_status(
    lease_id: str,
    status: str,
    contradicts_id: Any = None,
) -> bool:
    try:
        db = _lazy_writeback().db
        with db.cursor() as c:
            c.execute(
                """UPDATE handoff_leases
                   SET status = ?,
                       contradicts_id = COALESCE(?, contradicts_id)
                   WHERE lease_id = ?""",
                (status, contradicts_id, lease_id),
            )
            return c.rowcount > 0
    except Exception as exc:
        logger.warning("Could not update handoff lease %r: %s", lease_id, exc)
        return False


def _pending_handoff_leases(agent_id: str) -> list[dict]:
    try:
        now = time.time()
        db = _lazy_writeback().db
        with db.cursor() as c:
            rows = c.execute(
                """SELECT lease_id, from_agent, to_agent, task, expires_at, inbox_path
                   FROM handoff_leases
                   WHERE to_agent = ?
                     AND status = 'pending'
                     AND (expires_at IS NULL OR expires_at > ?)
                   ORDER BY created_at ASC, lease_id ASC""",
                (agent_id, now),
            ).fetchall()
        return [
            {
                "lease_id": row["lease_id"],
                "from_agent": row["from_agent"],
                "to_agent": row["to_agent"],
                "task": row["task"],
                "expires_at": _iso_from_epoch(row["expires_at"]) if row["expires_at"] is not None else None,
                "path": row["inbox_path"],
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning("Could not list SQLite handoff leases for %r: %s", agent_id, exc)
        return []


def _handoff_lease_status(lease_id: str) -> Optional[dict]:
    try:
        db = _lazy_writeback().db
        with db.cursor() as c:
            row = c.execute(
                "SELECT lease_id, status FROM handoff_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        if row is None:
            return None
        return {"lease_id": row["lease_id"], "status": row["status"]}
    except Exception as exc:
        logger.warning("Could not read handoff lease %r: %s", lease_id, exc)
        return None


def _lease_to_agent(lease_id: str) -> Optional[str]:
    """Recipient (``to_agent``) of a handoff lease: authoritative SQLite row
    first, JSON packets as fallback (leases can exist file-only when SQLite
    persistence degraded). None when the lease is unknown on both channels."""
    try:
        db = _lazy_writeback().db
        with db.cursor() as c:
            row = c.execute(
                "SELECT to_agent FROM handoff_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        if row is not None and row["to_agent"]:
            return str(row["to_agent"])
    except Exception as exc:
        logger.warning("Could not read lease %r for authz: %s", lease_id, exc)
    for _vault, _path, packet in _iter_handoff_files():
        if packet.get("lease_id") == lease_id and packet.get("to_agent"):
            return str(packet.get("to_agent"))
    return None


def _handle_ack_handoff(params: dict, request_id: Any) -> dict:
    lease_id = str(params.get("lease_id", "")).strip()
    status = str(params.get("status", "")).strip()
    if not lease_id:
        return _make_error(-32602, "lease_id is required", request_id)
    if status not in _HANDOFF_ACK_STATUSES:
        return _make_error(-32602, f"status must be one of {sorted(_HANDOFF_ACK_STATUSES)}", request_id)
    # A3 authz (fixes a live incident: co-located session subagents acked and
    # archived other agents' live handoffs): only the lease's RECIPIENT may ack
    # it. The principal is server-stamped (G11) — caller-supplied agent_id is
    # only honoured through the platform_agent_ids / alias machinery, never raw.
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    to_agent = _lease_to_agent(lease_id)
    if to_agent is None:
        return _make_error(-32000, f"No handoff lease found for {lease_id}", request_id)
    if to_agent != principal.agent_id:
        return _make_error(
            -32004,
            f"principal_mismatch: handoff lease {lease_id} is addressed to "
            f"'{to_agent}'; caller principal '{principal.agent_id}' may not ack it",
            request_id,
        )
    contradicts_id = params.get("contradicts_id")
    updates = {
        "ack_status": status,
        "acked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if contradicts_id is not None:
        updates["contradicts_id"] = contradicts_id
    changed = _write_matching_lease_packets(lease_id, updates)
    db_changed = _update_handoff_lease_status(lease_id, status, contradicts_id)
    if not changed and not db_changed:
        return _make_error(-32000, f"No handoff lease found for {lease_id}", request_id)
    # B1: an explicit ack is terminal for the file channel — archive the
    # recipient-side inbox copies so they stop re-surfacing at SessionStart.
    # The outbox copy (and the authoritative lease row) keep the ack_status
    # for await_handoff. Rename only, never unlink.
    archived = []
    for changed_path in changed:
        p = Path(changed_path)
        if p.parent.name != "inbox":
            continue
        try:
            from afm_passes.inbox_archive import archive_inbox_file
            new_path = archive_inbox_file(p)
            if new_path:
                archived.append(new_path)
        except Exception as exc:
            logger.warning("ack_handoff: archive of %s failed: %s", p, exc)
    return _make_response({
        "lease_id": lease_id,
        "status": status,
        "updated_paths": changed,
        "archived_paths": archived,
    }, request_id)


def _handle_list_pending_handoffs(params: dict, request_id: Any) -> dict:
    # G11 / RCM-003/009: EffectivePrincipal stamp + mismatch guard (closes last model-facing agent_id surfaces)
    # Model-supplied agent_id is NEVER trusted; use stamped principal.agent_id for leases/queries.
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    if not agent_id:
        return _make_error(-32602, "agent_id is required", request_id)
    sqlite_handoffs = _pending_handoff_leases(agent_id)
    if sqlite_handoffs:
        return _make_response({"agent_id": agent_id, "handoffs": sqlite_handoffs}, request_id)

    now = time.time()
    handoffs = []
    for _vault, path, packet in _iter_handoff_files(agent_id):
        if packet.get("to_agent") != agent_id:
            continue
        if not packet.get("requires_ack", False):
            continue
        if packet.get("ack_status"):
            continue
        expires_at = _parse_iso_ts(packet.get("expires_at"))
        if expires_at is not None and expires_at <= now:
            continue
        handoffs.append({
            "lease_id": packet.get("lease_id"),
            "from_agent": packet.get("from_agent"),
            "to_agent": packet.get("to_agent"),
            "task": packet.get("task"),
            "expires_at": packet.get("expires_at"),
            "path": str(path),
        })
    return _make_response({"agent_id": agent_id, "handoffs": handoffs}, request_id)


async def _handle_await_handoff(params: dict, request_id: Any) -> dict:
    lease_id = str(params.get("lease_id", "")).strip()
    if not lease_id:
        return _make_error(-32602, "lease_id is required", request_id)
    timeout_ms = max(0, min(int(params.get("timeout_ms", 30000)), 300000))
    deadline = time.time() + (timeout_ms / 1000.0)
    while True:
        lease = _handoff_lease_status(lease_id)
        if lease is not None and lease.get("status") not in {None, "pending"}:
            return _make_response({
                "lease_id": lease_id,
                "status": lease.get("status"),
            }, request_id)
        for _vault, _path, packet in _iter_handoff_files():
            if packet.get("lease_id") == lease_id and packet.get("ack_status"):
                return _make_response({
                    "lease_id": lease_id,
                    "status": packet.get("ack_status"),
                    "acked_at": packet.get("acked_at"),
                }, request_id)
        if time.time() >= deadline:
            return _make_response({"lease_id": lease_id, "status": "timeout"}, request_id)
        await asyncio.sleep(0.05)


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
    if backends is None:
        names = ["faiss-disk"]
    elif isinstance(backends, str):
        names = [backends]
    elif isinstance(backends, (list, tuple)):
        names = [str(item) for item in backends if item]
    else:
        names = [str(backends)]
    return "+".join(names)


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
    """
    Resolve the backend parameter for a search request.

    backend_param may be:
      None / "auto"      → use config.vector_backends (default: ["faiss-disk"])
      "faiss-disk"       → single backend by name
      "faiss-mem"        → single backend by name
      ["faiss-disk", X]  → fan-out via MultiBackend

    Returns a resolved backend string or list for passing to retrieval.
    For single-backend default mode, returns None (retrieval uses its own FAISS).
    """
    from config import DEFAULT_CONFIG
    cfg = config or DEFAULT_CONFIG

    if backend_param is None or backend_param == "auto":
        # Auto-resolution: try each configured backend in order
        # For now return the first backend in config (full cascade in §2.3)
        backends = cfg.vector_backends
        if not backends or backends == ["faiss-disk"]:
            return None  # Default path — use existing FAISSIndex in retrieval
        return backends
    return backend_param


def _handle_search(params: dict, request_id: Any) -> dict:
    """Search Minni via hybrid retrieval.

    Accepts optional ``depth`` parameter for progressive disclosure:
      headline  — wikilink, title, score, confidence, age_days (~30 tokens/result)
      snippet   — + text (≤280 chars) (~120 tokens/result) [DEFAULT]  (M-2 fix)
      chunk     — + full chunk text, heading context, provenance (~500 tokens)
      document  — + full source document (whole_document=1 rows only)
    Omitting depth returns "snippet". Previous default was "headline" (no text)
    which was a documentation/implementation mismatch — fixed.

    Accepts optional ``budget_tokens`` for MMR-diverse token-budgeted packing.
    When provided, selects a diverse subset fitting within the token budget.
    ``depth="auto"`` with ``budget_tokens`` uses "snippet" as the base tier.

    Accepts optional ``backend`` parameter:
      "auto" (default)   — use config.vector_backends priority cascade
      "faiss-disk"       — force specific backend
      ["faiss-disk", X]  — fan-out via multi-backend
    """
    global _request_count
    _request_count += 1
    started_at = time.perf_counter()

    query = params.get("query", "")
    if not query:
        return _make_error(-32602, "query is required", request_id)

    # G11: EffectivePrincipal is the single server-stamped source (never trust caller).
    # Some legacy/mixed clients can still arrive without a usable principal; that
    # path keeps learning recall unscoped for back-compat instead of silently
    # dropping other agents' rows.
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    if bool(params.get("cross_agent", False)) and principal is not None:
        if not allows_cross_agent_recall(principal):
            return make_capability_denied_error(
                "cross_agent",
                "search",
                request_id,
                principal_id=principal.agent_id,
            )
    agent_id = principal.agent_id if principal is not None else None
    learnings_cross_agent = bool(params.get("cross_agent", False)) or principal is None
    try:
        document_scope = _resolve_document_scope(params)
    except ValueError as exc:
        return _make_error(-32602, str(exc), request_id)
    limit = min(int(params.get("limit", 5)), 20)
    # M-2 fix: default was "headline" (no text) despite the docstring claiming
    # "snippet". Callers that omit depth (e.g. recallMemory() in sovereign.ts)
    # received wikilink+score only — no evidence text for the agent to read.
    # Changed to "snippet" to match the documented intent (~120 tokens/result).
    depth = str(params.get("depth", "snippet"))
    budget_tokens_param = params.get("budget_tokens")
    backend_param = params.get("backend", "auto")
    layers = params.get("layers")
    sort = str(params.get("sort", "semantic"))
    start_date = params.get("start_date")
    end_date = params.get("end_date")
    expand = params.get("expand", True)
    summarize_neighborhood = bool(params.get("summarize_neighborhood", False))

    # depth="auto" means "pick snippet and apply MMR budget packing"
    if depth == "auto":
        depth = "snippet"

    # Resolve backend — None means use the default FAISSIndex path (bit-identical)
    _resolved_backend = _resolve_backend(backend_param)

    try:
        engine = _lazy_retrieval()

        def _retrieve_from(
            retrieval_engine,
            *,
            src: str,
            principal_for_documents,
        ) -> list:
            rows = retrieval_engine.retrieve(
                query=query,
                agent_id=agent_id,
                limit=limit,
                depth=depth,
                backend=_resolved_backend,
                layers=layers,
                sort=sort,
                start_date=start_date,
                end_date=end_date,
                expand=expand,
                summarize_neighborhood=summarize_neighborhood,
                cross_agent=learnings_cross_agent,
                # G19/G20/G22: the stamped principal gates every document
                # stream. Combined/shared paths still gather all vault indexes,
                # but each hit must pass can_read_document for the caller.
                principal=principal_for_documents,
                workspace=(
                    principal_for_documents.workspace_id
                    if principal_for_documents is not None
                    else "default"
                ),
            )
            return _tag_document_results(
                rows,
                src=src,
            )

        def _retrieve_shared() -> list:
            return _retrieve_from(
                engine,
                src="c",
                principal_for_documents=principal,
            )

        def _retrieve_personal() -> list:
            vault_retrieval = _agent_vault_retrieval(agent_id) if agent_id else None
            if vault_retrieval is not None:
                vault_engine, _source_agent, _source_db_path = vault_retrieval
                try:
                    return _retrieve_from(
                        vault_engine,
                        src="p",
                        principal_for_documents=principal,
                    )
                except Exception as exc:
                    logger.warning(
                        "search: personal vault index failed for %s (%s); falling back to shared",
                        agent_id,
                        exc,
                    )
            return _retrieve_shared()

        def _retrieve_combined() -> list:
            result_sets = []
            for vault_engine, _source_agent, _source_db_path in _all_vault_retrievals():
                result_sets.append(
                    _retrieve_from(
                        vault_engine,
                        src="c",
                        principal_for_documents=principal,
                    )
                )
            result_sets.append(_retrieve_shared())
            return _merge_document_results(result_sets, limit)

        if principal is None:
            # Back-compat: callers with no stamped principal use the shared
            # legacy document layer only.
            results = _retrieve_shared()
        elif document_scope == "personal":
            results = _retrieve_personal()
        elif document_scope == "combined":
            # Combined is the pooled/fleet view: all existing per-vault indexes
            # including the caller when present, plus shared legacy documents.
            results = _retrieve_combined()
        else:
            result_sets = [_retrieve_personal(), _retrieve_combined()]
            results = _merge_document_results(result_sets, limit, prefer_personal=True)

        # Apply MMR token-budget packing if requested
        if budget_tokens_param is not None:
            try:
                budget = int(budget_tokens_param)
                from tokens import pack_results
                results = pack_results(results, budget_tokens=budget, depth=depth)
            except Exception as pack_exc:
                logger.warning("pack_results failed: %s — returning unbudgeted results", pack_exc)

        # hooks-PL-2 leg (a): searching is reading. The doc-retrieval stream
        # above can never carry learnings — ``learning://<id>`` rows exist only
        # for evidence-backed learnings and are never indexed in vault_fts or
        # FAISS — so match the learnings table directly. The matched learnings
        # are surfaced to the caller in the response below, and
        # search_learnings records a learning_reads row for each (the row
        # subscribe_contradictions joins on, so stale_beliefs can fire when a
        # searched belief is later superseded).
        learnings: list = []
        try:
            learnings = engine.search_learnings(
                query,
                agent_id=agent_id,
                cross_agent=learnings_cross_agent,
                limit=limit,
                source="minnid.search",
            )
        except Exception as exc:
            # hooks-PL-5: visible, never silently green
            logger.warning("search: learnings surfacing/tracking failed: %s", exc)

        return _make_response({
            "query": query,
            "agent_id": agent_id,
            "depth": depth,
            "count": len(results),
            "backend": _backend_badge(_resolved_backend),
            "trace_id": getattr(engine, "last_trace_id", None),
            "query_variants": (
                results[0].get("query_variants", [query])
                if results else [query]
            ),
            "results": results,
            # Active learnings matching the query (read-tracked above).
            "learnings": learnings,
        }, request_id)
    except Exception as exc:
        logger.exception("search failed")
        return _make_error(-32000, f"Search error: {exc}", request_id)
    finally:
        _record_latency("search", time.perf_counter() - started_at)


def _handle_feedback(params: dict, request_id: Any) -> dict:
    """Store useful/not-useful feedback for a prior search result."""
    global _request_count
    _request_count += 1

    query = params.get("query", "")
    result_id = params.get("result_id")
    if not query:
        return _make_error(-32602, "query is required", request_id)
    if result_id is None:
        return _make_error(-32602, "result_id is required", request_id)

    try:
        result_id = int(result_id)
    except (TypeError, ValueError):
        return _make_error(-32602, "result_id must be an integer", request_id)

    useful = bool(params.get("useful", False))
    # G11: EffectivePrincipal is the single server-stamped source (never trust caller)
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id

    try:
        result = _lazy_retrieval().record_feedback(
            query=query,
            result_id=result_id,
            useful=useful,
            agent_id=agent_id,
        )
        return _make_response(result, request_id)
    except Exception as exc:
        logger.exception("feedback failed")
        return _make_error(-32000, f"Feedback error: {exc}", request_id)


def _handle_trace(params: dict, request_id: Any) -> dict:
    """Return a process-local trace entry by id."""
    global _request_count
    _request_count += 1

    # RCM-009: principal gate (redaction applied unconditionally via _redact_value for paths/secrets;
    # traces are ephemeral/observability and many lack stamped agent_id, so no additional owner-match filter here).
    principal, err = _handler_principal(params, request_id)
    if err:
        return err

    trace_id = params.get("trace_id")
    if not trace_id:
        return _make_error(-32602, "trace_id is required", request_id)

    try:
        trace = _trace_ring().get(str(trace_id))
        if trace is None:
            return _make_response({
                "trace_id": trace_id,
                "trace": None,
                "status": "not_found",
                "ephemeral": True,
            }, request_id)
        # RCM-009: redact paths/secrets from trace payload before return (principal resolved for gate;
        # _redact_value used for consistency with handoff; status uses explicit literals for its 3 known fields).
        redacted_trace, _ = _redact_value(trace)
        return _make_response({
            "trace_id": trace_id,
            "trace": redacted_trace,
            "status": "ok",
            "ephemeral": True,
        }, request_id)
    except Exception as exc:
        logger.warning("trace lookup failed: %s", exc)
        degraded = {
            "trace_id": trace_id,
            "degraded": True,
            "reason": str(exc),
        }
        redacted_degraded, _ = _redact_value(degraded)
        return _make_response({
            "trace_id": trace_id,
            "trace": redacted_degraded,
            "status": "degraded",
            "ephemeral": True,
        }, request_id)


def _handle_expand(params: dict, request_id: Any) -> dict:
    """Re-fetch a specific result at a deeper depth tier.

    Accepts:
      result_id  — chunk_id or doc_id from a prior search result (required)
      depth      — target depth tier: 'chunk' or 'document' (default: 'chunk')
    """
    global _request_count
    _request_count += 1

    result_id = params.get("result_id")
    if result_id is None:
        return _make_error(-32602, "result_id is required", request_id)

    try:
        result_id = int(result_id)
    except (TypeError, ValueError):
        return _make_error(-32602, "result_id must be an integer", request_id)

    depth = str(params.get("depth", "chunk"))

    # G11 + G19: stamp principal for expand (read surface) and pass to gate
    principal, err = _handler_principal(params, request_id)
    if err:
        return err

    try:
        engine = _lazy_retrieval()
        result = engine.expand_result(result_id=result_id, depth=depth, principal=principal, workspace=principal.workspace_id)
        if result is None:
            return _make_error(-32000, f"No result found for result_id={result_id}", request_id)
        return _make_response({
            "result_id": result_id,
            "depth": depth,
            "result": result,
        }, request_id)
    except Exception as exc:
        logger.exception("expand failed")
        return _make_error(-32000, f"Expand error: {exc}", request_id)


def _indexed_at_for_result(retrieval_engine, result: dict) -> Optional[float]:
    doc_id = result.get("doc_id")
    if doc_id is None:
        return None
    try:
        with retrieval_engine.db.cursor() as c:
            c.execute("SELECT indexed_at FROM documents WHERE doc_id = ?", (int(doc_id),))
            row = c.fetchone()
        if row is None:
            return None
        value = row["indexed_at"]
        return float(value) if value is not None else None
    except Exception:
        return None


def _score_components(reference: dict, result: dict) -> dict:
    ref_prov = reference.get("provenance") if isinstance(reference.get("provenance"), dict) else {}
    result_prov = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}

    def pick(*names):
        for name in names:
            if name in reference and reference.get(name) is not None:
                return reference.get(name)
            if name in ref_prov and ref_prov.get(name) is not None:
                return ref_prov.get(name)
            if name in result and result.get(name) is not None:
                return result.get(name)
            if name in result_prov and result_prov.get(name) is not None:
                return result_prov.get(name)
        return None

    return {
        "score": pick("score"),
        "fts_rank": pick("fts_rank"),
        "semantic_rank": pick("semantic_rank", "sem_rank"),
        "rrf_score": pick("rrf_score"),
        "cross_encoder_score": pick("cross_encoder_score", "rerank_score"),
        "decay_factor": pick("decay_factor", "decay_score"),
        "backend": pick("backend"),
    }


def _full_provenance(
    *,
    retrieval_engine,
    source_agent: str,
    source_vault: str,
    index_db_path: str,
    reference: dict,
    result: dict,
) -> dict:
    return {
        "owning_agent_id": source_agent,
        "document_agent": result.get("agent"),
        "source_vault": source_vault,
        "index_db_path": index_db_path,
        "indexed_at": _indexed_at_for_result(retrieval_engine, result),
        "score_components": _score_components(reference, result),
    }


def _reference_candidates(reference: dict, principal, agent_id: Optional[str], shared_engine) -> list:
    marker = reference.get("src")
    candidates = []

    def add(candidate):
        if candidate is None:
            return
        retrieval_engine, source_agent, index_db_path, principal_for_documents = candidate
        key = str(index_db_path)
        if any(str(existing[3]) == key for existing in candidates):
            return
        source_vault = str(Path(getattr(retrieval_engine.config, "vault_path", "")).expanduser().resolve())
        candidates.append(
            (
                retrieval_engine,
                source_agent,
                source_vault,
                index_db_path,
                principal_for_documents,
            )
        )

    # G19: the stamped principal gates every candidate index (shared and
    # per-vault alike); expand_result applies can_read_document per hit.
    shared_candidate = (
        shared_engine,
        "shared",
        DEFAULT_CONFIG.db_path,
        principal,
    )

    if principal is None:
        add(shared_candidate)
        return candidates

    personal = _agent_vault_retrieval(agent_id) if agent_id else None
    if marker == "p":
        if personal is not None:
            vault_engine, source_agent, index_db_path = personal
            add((vault_engine, source_agent, index_db_path, principal))
        add(shared_candidate)
        return candidates

    if marker == "c":
        for vault_engine, source_agent, index_db_path in _all_vault_retrievals():
            add((vault_engine, source_agent, index_db_path, principal))
        add(shared_candidate)
        return candidates

    if personal is not None:
        vault_engine, source_agent, index_db_path = personal
        add((vault_engine, source_agent, index_db_path, principal))
    for vault_engine, source_agent, index_db_path in _all_vault_retrievals():
        add((vault_engine, source_agent, index_db_path, principal))
    add(shared_candidate)
    return candidates


def _reference_matches(result: dict, reference: dict) -> bool:
    ref_source = reference.get("source") or reference.get("path")
    if ref_source:
        try:
            return Path(str(result.get("source") or "")).resolve() == Path(str(ref_source)).resolve()
        except Exception:
            return str(result.get("source") or "") == str(ref_source)
    ref_wikilink = reference.get("wikilink")
    if ref_wikilink:
        return str(result.get("wikilink") or "") == str(ref_wikilink)
    return True


def _reference_ids_for_engine(reference: dict, retrieval_engine) -> list[int]:
    raw_id = reference.get("chunk_id") or reference.get("doc_id") or reference.get("result_id")
    if raw_id is not None:
        try:
            return [int(raw_id)]
        except (TypeError, ValueError):
            return []

    ref_source = reference.get("source") or reference.get("path")
    ref_wikilink = reference.get("wikilink")
    normalized_wikilink = str(ref_wikilink).strip() if ref_wikilink else ""
    if normalized_wikilink and not normalized_wikilink.startswith("[["):
        normalized_wikilink = f"[[{normalized_wikilink.removesuffix('.md')}]]"

    ids = []
    try:
        from retrieval import _path_to_wikilink  # type: ignore

        with retrieval_engine.db.cursor() as c:
            c.execute("SELECT doc_id, path FROM documents")
            rows = c.fetchall()
        for row in rows:
            path_value = str(row["path"])
            if ref_source:
                try:
                    if Path(path_value).resolve() == Path(str(ref_source)).resolve():
                        ids.append(int(row["doc_id"]))
                        continue
                except Exception:
                    if path_value == str(ref_source):
                        ids.append(int(row["doc_id"]))
                        continue
            if normalized_wikilink and _path_to_wikilink(path_value) == normalized_wikilink:
                ids.append(int(row["doc_id"]))
    except Exception:
        return ids
    return ids


def _expand_reference(
    reference: dict,
    *,
    depth: str,
    principal,
    agent_id: Optional[str],
    shared_engine,
) -> Optional[dict]:
    for retrieval_engine, source_agent, source_vault, index_db_path, principal_for_documents in _reference_candidates(
        reference,
        principal,
        agent_id,
        shared_engine,
    ):
        for result_id in _reference_ids_for_engine(reference, retrieval_engine):
            result = retrieval_engine.expand_result(
                result_id=result_id,
                depth=depth,
                principal=principal_for_documents,
                workspace=(
                    principal_for_documents.workspace_id
                    if principal_for_documents is not None
                    else "default"
                ),
            )
            if result is None or not _reference_matches(result, reference):
                continue
            marker = "p" if reference.get("src") == "p" else "c"
            full = _full_provenance(
                retrieval_engine=retrieval_engine,
                source_agent=source_agent,
                source_vault=source_vault,
                index_db_path=str(Path(index_db_path).expanduser().resolve()),
                reference=reference,
                result=result,
            )
            result["src"] = marker
            result["full_provenance"] = full
            provenance = result.get("provenance")
            if not isinstance(provenance, dict):
                provenance = {}
            provenance.update(full)
            result["provenance"] = provenance
            return result
    return None


def _handle_sm_drill(params: dict, request_id: Any) -> dict:
    """Batch drill prior headline results to snippet/chunk/document depth."""
    global _request_count
    _request_count += 1

    raw_ids = params.get("chunk_ids", params.get("result_ids"))
    raw_references = params.get("references", params.get("refs"))
    if raw_ids is None and raw_references is None:
        return _make_error(-32602, "chunk_ids, result_ids, references, or refs is required", request_id)
    if raw_ids is not None and not isinstance(raw_ids, list):
        return _make_error(-32602, "chunk_ids/result_ids must be a list", request_id)
    if raw_references is not None and not isinstance(raw_references, list):
        return _make_error(-32602, "references/refs must be a list", request_id)
    raw_ids = raw_ids or []
    raw_references = raw_references or []
    if len(raw_ids) + len(raw_references) > 20:
        return _make_error(-32602, "sm_drill accepts at most 20 ids/references", request_id)

    depth = str(params.get("depth", "snippet"))
    if depth not in {"snippet", "chunk", "document"}:
        return _make_error(-32602, "depth must be snippet, chunk, or document", request_id)

    principal, err = _handler_principal(params, request_id)
    if err:
        return err

    try:
        ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError):
        return _make_error(-32602, "all ids must be integers", request_id)

    references = []
    for ref in raw_references:
        if isinstance(ref, dict):
            references.append(ref)
        elif isinstance(ref, str):
            stripped = ref.strip()
            references.append({"wikilink": stripped} if stripped.startswith("[[") else {"source": stripped})
        else:
            return _make_error(-32602, "references must be objects or strings", request_id)

    try:
        principal, err = _handler_principal(params, request_id)
        if err:
            return err
        agent_id = principal.agent_id if principal is not None else None

        engine = _lazy_retrieval()
        results = []
        missing = []
        for result_id in ids:
            # G19: same gate as reference drills — direct numeric ids must not
            # bypass can_read_document on the shared index.
            result = engine.expand_result(
                result_id=result_id,
                depth=depth,
                principal=principal,
                workspace=(
                    principal.workspace_id if principal is not None else "default"
                ),
            )
            if result is None:
                missing.append(result_id)
            else:
                results.append(result)
        for index, reference in enumerate(references):
            result = _expand_reference(
                reference,
                depth=depth,
                principal=principal,
                agent_id=agent_id,
                shared_engine=engine,
            )
            if result is None:
                missing.append(reference.get("doc_id") or reference.get("chunk_id") or reference.get("source") or index)
            else:
                results.append(result)
        return _make_response({
            "depth": depth,
            "count": len(results),
            "missing": missing,
            "results": results,
        }, request_id)
    except Exception as exc:
        logger.exception("sm_drill failed")
        return _make_error(-32000, f"Drill error: {exc}", request_id)


def _anchor_for_result(result: dict) -> str:
    doc_id = result.get("doc_id")
    chunk_id = result.get("chunk_id")
    if doc_id is not None and chunk_id is not None:
        return f"sm://doc/{doc_id}/chunk/{chunk_id}"
    if doc_id is not None:
        return f"sm://doc/{doc_id}"
    source = str(result.get("source") or result.get("filename") or "unknown")
    return f"sm://source/{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}"


def _handle_sm_export_pack(params: dict, request_id: Any) -> dict:
    """Export deterministic, cache-prefix-stable context pack."""
    global _request_count
    _request_count += 1

    # G11 / RCM-003/009: EffectivePrincipal stamp + mismatch guard (closes last model-facing agent_id surface for export)
    # agent_id in pack and retrieve visibility now always from stamped principal; model cannot spoof attribution/exfil view.
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    workspace_id = params.get("workspace_id")

    query = str(params.get("query", "")).strip()
    if not query:
        return _make_error(-32602, "query is required", request_id)
    budget_tokens = int(params.get("budget_tokens", 4096))
    cache_key = str(params.get("cache_key", "default"))
    limit = min(int(params.get("limit", 12)), 50)

    try:
        engine = _lazy_retrieval()
        results = engine.retrieve(
            query=query,
            agent_id=agent_id,
            limit=limit,
            depth="snippet",
            update_access=False,
            # G19/G20/G22: thread stamped principal (from resolve_effective_principal) + workspace
            # so can_read_document filter + evidence envelope (instruction_like/visibility/reasoning)
            # are applied uniformly, matching _handle_search (and read/expand) exactly.
            principal=principal,
            workspace=principal.workspace_id if principal is not None else (workspace_id or "default"),
        )
        prefix_anchors = []
        for result in sorted(
            results,
            key=lambda r: (
                str(r.get("source", "")),
                int(r.get("doc_id") or 0),
                int(r.get("chunk_id") or 0),
            ),
        ):
            prefix_anchors.append({
                "anchor": _anchor_for_result(result),
                "doc_id": result.get("doc_id"),
                "chunk_id": result.get("chunk_id"),
                "source": result.get("source", ""),
                "wikilink": result.get("wikilink"),
            })

        suffix_snippets = []
        used_tokens = 0
        for result in results:
            tokens = int(result.get("token_count") or max(1, len(str(result.get("text", ""))) // 4))
            if suffix_snippets and used_tokens + tokens > budget_tokens:
                break
            suffix_snippets.append({
                "anchor": _anchor_for_result(result),
                "text": result.get("text", ""),
                "score": result.get("score"),
                "token_count": tokens,
            })
            used_tokens += tokens

        pack = {
            "cache_key": cache_key,
            "query": query,
            "budget_tokens": budget_tokens,
            "prefix": {
                "identity": {
                    "agent_id": agent_id,
                    "workspace_id": workspace_id,
                    "format": "sovereign-context-pack-v1",
                },
                "anchors": prefix_anchors,
            },
            "suffix": {
                "query": query,
                "snippets": suffix_snippets,
                "token_count": used_tokens,
            },
        }
        canonical = json.dumps(pack, sort_keys=True, separators=(",", ":"))
        manifest_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        pack["manifest_hash"] = manifest_hash
        return _make_response(pack, request_id)
    except Exception as exc:
        logger.exception("sm_export_pack failed")
        return _make_error(-32000, f"Export pack error: {exc}", request_id)


def _handle_read(params: dict, request_id: Any) -> dict:
    """Read agent startup context (identity + knowledge + learnings)."""
    global _request_count
    _request_count += 1
    started_at = time.perf_counter()

    # G11: EffectivePrincipal is the single server-stamped source (never trust caller)
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    limit = min(int(params.get("limit", 5)), 20)

    try:
        db = SovereignDB()
        lines = []

        # Layer 1: whole-document identity/envelope. This is delivered before
        # retrieved knowledge so the agent receives its stable local context
        # first. For hosted agents such as Codex this may be a map, not a soul.
        with db.cursor() as c:
            c.execute("""
                SELECT d.path, ce.chunk_text
                FROM documents d
                JOIN chunk_embeddings ce
                  ON ce.doc_id = d.doc_id AND ce.chunk_index = 0
                WHERE d.agent = ?
                  AND d.whole_document = 1
                ORDER BY d.path  -- intentional: deterministic alpha order; primary envelope sorts first by naming convention
            """, (f"identity:{agent_id}",))
            rows = c.fetchall()
            if rows:
                lines.append(f"## Agent Identity: {agent_id.title()}")
                lines.append("Loaded whole (not chunked). This is Layer 1.")
                for row in rows:
                    fname = os.path.basename(row["path"]).replace(".md", "").upper()
                    lines.append(f"\n### {fname}")
                    lines.append(row["chunk_text"] or "")

        # Prior context
        with db.cursor() as c:
            c.execute("""
                SELECT d.doc_id, d.path, d.agent, d.sigil,
                       d.access_count, d.decay_score
                FROM documents d
                WHERE (d.agent = ? OR d.agent = 'unknown'
                       OR d.agent LIKE 'wiki:%')
                  AND d.whole_document = 0
                ORDER BY d.decay_score * d.access_count DESC,
                         d.last_accessed DESC NULLS LAST
                LIMIT ?
            """, (agent_id, limit))
            rows = c.fetchall()
            if rows:
                lines.append(f"## Prior Context ({agent_id})")
                for row in rows:
                    # G19/G20: post-fetch gate on the legacy wiki/unknown Prior Context slice
                    # (prevents foreign wiki/handoff leakage on the read surface)
                    meta = {
                        "path": row["path"],
                        "agent": row["agent"],
                        "page_type": "wiki" if "wiki" in str(row["agent"] or "") else "knowledge",
                        "privacy_level": "safe",
                    }
                    if not can_read_document(principal, principal.workspace_id, meta):
                        continue
                    fname = os.path.basename(row["path"])
                    line = (
                        f"  - **{fname}** ({row['sigil']}) "
                        f"[{row['agent']}] "
                        f"accessed {row['access_count']}x, "
                        f"decay={row['decay_score']:.2f}"
                    )
                    lines.append(line)

        # Recent learnings
        with db.cursor() as c:
            c.execute("""
                SELECT learning_id, category, content, confidence, created_at
                FROM learnings
                WHERE agent_id = ? AND superseded_by IS NULL
                ORDER BY created_at DESC
                LIMIT 10
            """, (agent_id,))
            rows = c.fetchall()
            if rows:
                lines.append(f"\n## Learnings ({agent_id})")
                for row in rows:
                    lines.append(
                        f"  - [{row['category']}] {row['content'][:150]} "
                        f"(conf={row['confidence']:.1f})"
                    )
                    try:
                        # OR IGNORE: a same-tick re-read collides on the
                        # (learning_id, agent_id, read_at) PK — the read is
                        # already recorded, so dropping the duplicate is
                        # correct (a raise here would be swallowed below as
                        # "dropped tracking").
                        c.execute(
                            """INSERT OR IGNORE INTO learning_reads
                               (learning_id, agent_id, read_at, source)
                               VALUES (?, ?, ?, ?)""",
                            (row["learning_id"], agent_id, time.time(), "minnid.read"),
                        )
                    except Exception as exc:
                        # hooks-PL-5: a silently dropped read row starves
                        # subscribe_contradictions (stale_beliefs) of matches.
                        logger.warning(
                            "read: learning_reads insert failed for learning #%s: %s",
                            row["learning_id"], exc,
                        )

        # Recent episodic events
        with db.cursor() as c:
            c.execute("""
                SELECT event_type, content, created_at
                FROM episodic_events
                WHERE agent_id = ?
                ORDER BY created_at DESC
                LIMIT 5
            """, (agent_id,))
            rows = c.fetchall()
            if rows:
                lines.append(f"\n## Recent Activity ({agent_id})")
                for row in rows:
                    ts = time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(row["created_at"])
                    )
                    lines.append(
                        f"  - [{row['event_type']}] "
                        f"{row['content'][:120]} ({ts})"
                    )
        db.close()

        return _make_response({
            "agent_id": agent_id,
            "context": "\n".join(lines) if lines else f"No context for '{agent_id}'.",
        }, request_id)
    except Exception as exc:
        logger.exception("read failed")
        return _make_error(-32000, f"Read error: {exc}", request_id)
    finally:
        _record_latency("read", time.perf_counter() - started_at)


def _handle_learn(params: dict, request_id: Any) -> dict:
    """Store a learning.

    PR-6 behavior change: if contradictions are detected and ``force`` is not
    True (default False), returns::

        {"status": "contradiction", "candidates": [...]}

    without writing anything.  The caller must either resubmit with
    ``force=true`` to bypass detection, or supply a ``contradicts_id`` to
    explicitly record which prior learning is contradicted.

    On success the return shape is unchanged::

        {"status": "ok", "learning_id": <int>, "agent_id": ..., "category": ...}

    Backward compatibility: existing calls without the new fields (assertion,
    applies_when, evidence_doc_ids, contradicts_id, force) continue to work
    exactly as before — the default of force=False is the only new behavior.
    """
    global _request_count
    _request_count += 1
    started_at = time.perf_counter()

    content = params.get("content", "")
    if not content:
        return _make_error(-32602, "content is required", request_id)

    # SEC-015: bound caller-supplied learn payload sizes to keep the embedder,
    # writeback, and contradiction-detection paths from being abused as a DoS
    # vector. Content cap is generous (64 KiB ≈ a long markdown note); auxiliary
    # text fields are capped at 4 KiB each.
    MAX_LEARN_CHARS = 64 * 1024  # 64 KiB; SEC-015
    MAX_LEARN_FIELD_CHARS = 4 * 1024  # 4 KiB; SEC-015

    if len(content) > MAX_LEARN_CHARS:
        return _make_error(
            -32602,
            f"learn content exceeds {MAX_LEARN_CHARS} chars",
            request_id,
        )

    for _field in ("title", "summary"):
        _val = params.get(_field)
        if isinstance(_val, str) and len(_val) > MAX_LEARN_FIELD_CHARS:
            return _make_error(
                -32602,
                f"learn {_field} exceeds {MAX_LEARN_FIELD_CHARS} chars",
                request_id,
            )

    # G11: EffectivePrincipal is the single server-stamped source (never trust caller)
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        return _make_error(-32602, str(exc), request_id)
    category = params.get("category", "general")
    force = bool(params.get("force", False))

    # PR-6 structured fields (all optional)
    assertion = params.get("assertion")        # explicit structured assertion
    applies_when = params.get("applies_when")  # scoping condition
    evidence_doc_ids = params.get("evidence_doc_ids")  # list of doc ids
    contradicts_id = params.get("contradicts_id")      # explicit contradiction

    try:
        wb = _lazy_writeback()

        # ── PR-6: Contradiction Detection ────────────────────────────────────
        # Skip detection when the caller explicitly forces the write or already
        # acknowledges a specific contradicts_id.
        if not force and contradicts_id is None:
            # Use the explicit assertion if provided, otherwise extract the
            # first sentence of content (or content itself if short).
            check_text = assertion or _extract_assertion(content)
            try:
                candidates = wb.detect_contradictions(
                    content_or_assertion=check_text,
                    agent_id=None,  # cross-agent — global check
                )
            except Exception as exc:
                logger.warning(
                    "learn: contradiction detection failed (%s) — proceeding with write",
                    exc,
                )
                candidates = []

            if candidates:
                return _make_response({
                    "status": "contradiction",
                    "candidates": candidates,
                }, request_id)

        # G16: default learn() is proposal-first (stage to candidate_packets).
        # Only force=true takes the durable INSERT path (explicit escape hatch).
        if not force:
            return _stage_candidate(params, request_id)

        # G16 operator gate on the escape hatch (mirrors _resolve_candidate exactly)
        if not is_operator_principal(principal):
            return _make_error(
                -32004,
                "operator_only: force=true durable learn requires operator principal (principals/*.json capabilities or trusted name)",
                request_id,
            )

        # force=true path — prominent audit stamp only after the gate passes
        logger.warning(
            "FORCE_DURABLE_LEARN principal=%s (explicit escape; G16 audit stamp)",
            principal.agent_id,
        )

        # ── Store the learning ───────────────────────────────────────────────
        import sqlite3 as _sqlite3, time as _time, json as _json
        import numpy as _np

        now = _time.time()
        doc_ids_json = _json.dumps(evidence_doc_ids) if evidence_doc_ids else None

        # Embed for semantic search (and future contradiction detection)
        emb_bytes = None
        if wb.model:
            try:
                embedding_started_at = time.perf_counter()
                emb = wb.model.encode(content).astype(_np.float32)
                emb_bytes = emb.tobytes()
                _record_latency("embedding", time.perf_counter() - embedding_started_at)
            except Exception as exc:
                logger.warning("learn: embedding failed (%s) — storing without embedding", exc)

        # Resolve category
        from writeback import CATEGORIES
        if category not in CATEGORIES:
            category = "general"

        with wb.db.cursor() as c:
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, source_doc_ids, source_query,
                 confidence, embedding, created_at,
                 assertion, applies_when, evidence_doc_ids, contradicts_id, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, category, content,
                None,              # source_doc_ids (legacy field, not the new evidence_doc_ids)
                params.get("source_query"),
                params.get("confidence", 1.0),
                emb_bytes, now,
                assertion, applies_when, doc_ids_json,
                contradicts_id,
                "active",
            ))
            lid = c.lastrowid

            # If caller supplies supersedes, mark the old learning
            supersedes = params.get("supersedes")
            if supersedes:
                c.execute(
                    "UPDATE learnings SET superseded_by = ? WHERE learning_id = ?",
                    (lid, supersedes),
                )

        wb.add_derived_from_edges(
            learning_id=lid,
            agent_id=agent_id,
            category=category,
            content=content,
            evidence_doc_ids=evidence_doc_ids,
            created_at=now,
        )

        logger.info(
            "Stored learning #%d [%s/%s]: %.60s…",
            lid, agent_id, category, content,
        )

        # Write to disk as markdown (Obsidian integration — existing behaviour)
        if wb.config.writeback_enabled:
            wb._write_to_disk(lid, agent_id, category, content, now)

        # Store-time semantic indexing: make this durable learning recall-able
        # via the semantic `results` stream immediately, in-process (FAIL-OPEN).
        # Keyed on the learning_id so a re-store of the same learning upserts
        # rather than duplicating chunk_embeddings. Bound to wb.db so the index
        # lands in the SAME DB this learning was committed to.
        _index_durable_learning(agent_id, content, key=f"learning:{lid}", db=wb.db)

        return _make_response({
            "status": "ok",
            "learning_id": lid,
            "agent_id": agent_id,
            "category": category,
        }, request_id)
    except Exception as exc:
        logger.exception("learn failed")
        return _make_error(-32000, f"Learn error: {exc}", request_id)
    finally:
        _record_latency("learn", time.perf_counter() - started_at)


def _extract_assertion(content: str) -> str:
    """
    Extract a short assertion from content for contradiction checking.

    Uses the first sentence if content is longer than 120 characters,
    otherwise uses content as-is.
    """
    content = content.strip()
    if len(content) <= 120:
        return content
    # Split on common sentence terminators followed by whitespace
    import re
    m = re.search(r'[.!?]\s', content)
    if m:
        return content[:m.start() + 1].strip()
    return content[:120].strip()


class _ResolveRejected(Exception):
    """Carries a pre-built JSON-RPC error response out of an OPEN write
    transaction. Raising (instead of returning) makes the transaction context
    manager ROLL BACK, so an early rejection releases the BEGIN IMMEDIATE
    write lock without committing a no-op transaction."""

    def __init__(self, response: dict):
        super().__init__(str(response.get("error", {}).get("message", "rejected")))
        self.response = response


def _handle_resolve_contradiction(params: dict, request_id: Any) -> dict:
    """Write a new learning and atomically supersede a list of prior learnings.

    Params:
        new_content (str, required): Content of the new learning.
        supersede_ids (list[int], required): IDs of learnings to supersede.
        agent_id (str, optional): Agent writing this resolution.
        category (str, optional): Category for the new learning.
        assertion (str, optional): Structured assertion for the new learning.
        applies_when (str, optional): Scoping condition.
        evidence_doc_ids (list, optional): Supporting document IDs.

    Returns on success::

        {"status": "ok", "new_learning_id": <int>, "superseded": [<int>, ...]}

    The operation is a single SQLite transaction: either the new learning is
    written AND all supersede_ids are updated, or nothing changes.
    """
    global _request_count
    _request_count += 1

    new_content = params.get("new_content", "")
    if not new_content:
        return _make_error(-32602, "new_content is required", request_id)

    supersede_ids = params.get("supersede_ids", [])
    if not isinstance(supersede_ids, list):
        return _make_error(-32602, "supersede_ids must be a list", request_id)
    # Validate element types BEFORE any SQL binding: a None/float/dict element
    # would otherwise raise an InterfaceError inside the transaction and leak
    # internal exception text via the -32000 catch-all below.
    if not all(isinstance(sid, int) and not isinstance(sid, bool) for sid in supersede_ids):
        return _make_error(-32602, "supersede_ids must be a list of integers", request_id)
    # An EMPTY list would pass validation, skip the per-id ownership loop
    # entirely, and insert an unsupervised learning that bypasses the staging
    # workflow — resolution must actually resolve something.
    if not supersede_ids:
        return _make_error(
            -32602, "supersede_ids must be a non-empty list of integers", request_id
        )

    # G11: EffectivePrincipal is the single server-stamped source (never trust caller)
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        return _make_error(-32602, str(exc), request_id)
    category = params.get("category", "general")
    assertion = params.get("assertion")
    applies_when = params.get("applies_when")
    evidence_doc_ids = params.get("evidence_doc_ids")

    try:
        import time as _time, json as _json
        import numpy as _np

        wb = _lazy_writeback()

        from writeback import CATEGORIES
        if category not in CATEGORIES:
            category = "general"

        now = _time.time()
        doc_ids_json = _json.dumps(evidence_doc_ids) if evidence_doc_ids else None

        emb_bytes = None
        if wb.model:
            try:
                emb = wb.model.encode(new_content).astype(_np.float32)
                emb_bytes = emb.tobytes()
            except Exception as exc:
                logger.warning(
                    "resolve_contradiction: embedding failed (%s) — storing without embedding",
                    exc,
                )

        # Single transaction: write new + supersede old
        with wb.db.transaction() as c:
            # A3 authz (mirrors _resolve_candidate's owner check at the same
            # trust boundary): a caller may only supersede learnings it OWNS;
            # cross-principal supersession requires an EXPLICITLY allowed
            # operator. Validated BEFORE the INSERT, and rejections RAISE
            # (_ResolveRejected -> rollback) so no orphan new learning can be
            # left behind and no lock-holding no-op transaction is committed.
            for sid in supersede_ids:
                c.execute(
                    "SELECT agent_id FROM learnings WHERE learning_id = ?",
                    (sid,),
                )
                srow = c.fetchone()
                if not srow:
                    raise _ResolveRejected(_make_error(
                        -32001, f"learning_not_found: {sid}", request_id
                    ))
                owner = str(srow["agent_id"] or "")
                if owner != agent_id and not _explicitly_allowed_operator(principal):
                    raise _ResolveRejected(_make_error(
                        -32004,
                        f"principal_mismatch: learning #{sid} is owned by '{owner}'; "
                        f"caller principal '{agent_id}' may only supersede its own "
                        "learnings (cross-principal supersession requires an "
                        "explicitly allowed operator)",
                        request_id,
                    ))
            c.execute("""
                INSERT INTO learnings
                (agent_id, category, content, source_doc_ids, source_query,
                 confidence, embedding, created_at,
                 assertion, applies_when, evidence_doc_ids, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                agent_id, category, new_content,
                None, None,
                params.get("confidence", 1.0),
                emb_bytes, now,
                assertion, applies_when, doc_ids_json,
                "active",
            ))
            new_lid = c.lastrowid

            for sid in supersede_ids:
                c.execute(
                    """UPDATE learnings
                       SET superseded_by = ?, status = 'superseded'
                       WHERE learning_id = ?""",
                    (new_lid, sid),
                )
                c.execute(
                    """INSERT INTO contradiction_events
                       (superseded_learning_id, new_learning_id, originating_agent, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (sid, new_lid, agent_id, now),
                )

        logger.info(
            "Resolved contradiction: new learning #%d supersedes %s",
            new_lid, supersede_ids,
        )

        # Write to disk as markdown
        if wb.config.writeback_enabled:
            wb._write_to_disk(new_lid, agent_id, category, new_content, now)

        return _make_response({
            "status": "ok",
            "new_learning_id": new_lid,
            "superseded": supersede_ids,
        }, request_id)
    except _ResolveRejected as exc:
        return exc.response
    except Exception as exc:
        logger.exception("resolve_contradiction failed")
        return _make_error(-32000, f"Resolve contradiction error: {exc}", request_id)


def _handle_subscribe_contradictions(params: dict, request_id: Any) -> dict:
    """Return contradiction events for learnings recently read by an agent.

    Params:
      since_ts            Cursor for incremental polling. NOTE: event_window_days
                          is a hard cap — since_ts=0 ("everything") still only
                          returns the last event_window_days of events. The
                          effective lower bound is echoed back as
                          checked.event_since so callers can detect clamping.
      event_window_days   Boot-noise bound, default 30, clamped to [0, 365].
                          An explicit 0 is honored (no historic events), not
                          coerced to the default.
      read_window_hours   Optional read-recency filter; 0 means "no reads
                          qualify" (an explicit zero is honored, not coerced
                          to the default). Clamped to [0, 8760].

    Non-finite values (NaN/Inf) for any of the three are rejected and replaced
    with the parameter's default: NaN poisons min/max clamping (min(nan, x) is
    nan in CPython), which would either bypass the event-window DoS cap
    (event_since collapsing to 0 → full-history scan) or make the SQL
    comparison silently match nothing — defeating the hooks-PL-1
    checked/no-match discriminator.
    """
    # G11 / RCM-003/009: EffectivePrincipal stamp + mismatch guard (closes last model-facing agent_id surfaces)
    # Model-supplied agent_id is NEVER trusted; use stamped principal.agent_id for queries.
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    if not agent_id:
        return _make_error(-32602, "agent_id is required", request_id)
    # Non-numeric params are a caller error (-32602), never an unhandled
    # exception: a raise here would propagate out of the handler and drop the
    # whole connection with no JSON-RPC error.
    try:
        since_ts = float(params.get("since_ts", 0) or 0)
    except (TypeError, ValueError):
        return _make_error(-32602, "since_ts must be a number", request_id)
    if not math.isfinite(since_ts):
        # NaN survives the falsy-or (it is truthy) and turns every SQL
        # comparison false — events:[] would masquerade as checked_no_match.
        since_ts = 0.0
    now = time.time()
    # Clamp caller-supplied bounds (local-DoS guard): a huge event window or a
    # far-future since_ts must not force a full-history scan / nonsense JOIN
    # on the daemon's single-threaded query loop.
    since_ts = min(since_ts, now + 60)

    # hooks-PL-2 leg (b): the old default required a learning_reads row within
    # the last 24h for the SUPERSEDED learning. The sole read writer skips
    # superseded learnings (correctly — stale beliefs must not be re-injected),
    # so a corrected belief could never get a fresh read row and stale_beliefs
    # fired events:[] forever for exactly the corrections that matter. The fix:
    # ANY historical read of the superseded learning now qualifies (the agent
    # held the belief at some point); recency bounds apply to the *event*, not
    # the read. Callers can still opt back into read-recency filtering by
    # passing read_window_hours explicitly.
    read_window_hours_param = params.get("read_window_hours")
    read_window_hours = None
    read_since = None
    if read_window_hours_param is not None:
        # No falsy-or fallback: an explicit 0 means "no reads qualify" and
        # must not be silently coerced to the 24h default.
        try:
            read_window_hours = float(read_window_hours_param)
        except (TypeError, ValueError):
            return _make_error(
                -32602, "read_window_hours must be a number", request_id
            )
        if not math.isfinite(read_window_hours):
            # NaN bypasses min/max clamping; fall back to the default
            # (no read-recency filter) instead of a poisoned read_since.
            read_window_hours = None
        else:
            read_window_hours = min(max(read_window_hours, 0.0), 8760.0)
            read_since = now - read_window_hours * 3600

    # Bound boot-time noise: with since_ts unset, only surface events from the
    # last event_window_days (default 30) instead of the full history. This is
    # a HARD CAP: since_ts=0 does not mean "all history" (see docstring).
    # No falsy-or fallback (mirrors read_window_hours): an explicit 0 means
    # "no historic events" and must not be silently coerced to 30.
    event_window_days_param = params.get("event_window_days")
    try:
        event_window_days = (
            30.0 if event_window_days_param is None else float(event_window_days_param)
        )
    except (TypeError, ValueError):
        return _make_error(
            -32602, "event_window_days must be a number", request_id
        )
    if not math.isfinite(event_window_days):
        # NaN bypasses min/max clamping and would collapse event_since to 0
        # (full-history scan) — exactly the local DoS this cap blocks.
        event_window_days = 30.0
    event_window_days = min(max(event_window_days, 0.0), 365.0)
    event_since = max(since_ts, now - event_window_days * 86400)

    try:
        db = _lazy_writeback().db
        clauses = ["lr.agent_id = ?", "ce.created_at >= ?"]
        query_params: list = [agent_id, event_since]
        if read_since is not None:
            clauses.append("lr.read_at >= ?")
            query_params.append(read_since)
        with db.cursor() as c:
            rows = c.execute(f"""
                SELECT DISTINCT
                       ce.event_id,
                       ce.superseded_learning_id,
                       ce.new_learning_id,
                       ce.originating_agent,
                       ce.created_at
                FROM contradiction_events ce
                JOIN learning_reads lr
                  ON lr.learning_id = ce.superseded_learning_id
                WHERE {" AND ".join(clauses)}
                ORDER BY ce.created_at ASC, ce.event_id ASC
            """, query_params).fetchall()
            # hooks-PL-1: empty events must be distinguishable from a match
            # failure — count what was actually checked so silence is honest.
            total_events = c.execute(
                "SELECT COUNT(*) AS n FROM contradiction_events WHERE created_at >= ?",
                (event_since,),
            ).fetchone()["n"]
            # Agent-scoped count (the JOIN predicate, minus the read-recency
            # filter): without it, "N events in window, 0 matched" reads like
            # a matching bug when the events simply belong to other agents.
            agent_events = c.execute(
                """SELECT COUNT(*) AS n FROM contradiction_events
                   WHERE created_at >= ?
                     AND superseded_learning_id IN
                         (SELECT learning_id FROM learning_reads WHERE agent_id = ?)""",
                (event_since, agent_id),
            ).fetchone()["n"]
            reads_for_agent = c.execute(
                "SELECT COUNT(*) AS n FROM learning_reads WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()["n"]
        events = [
            {
                "event_id": row["event_id"],
                "superseded_learning_id": row["superseded_learning_id"],
                "new_learning_id": row["new_learning_id"],
                "originating_agent": row["originating_agent"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        return _make_response({
            "agent_id": agent_id,
            "events": events,
            # hooks-PL-1 discriminator: "matched" = stale beliefs found;
            # "checked_no_match" = the join ran and legitimately found nothing.
            # Errors take the JSON-RPC error path and never look like silence.
            "status": "matched" if events else "checked_no_match",
            "checked": {
                "contradiction_events_in_window": total_events,
                "contradiction_events_for_agent_reads": agent_events,
                "learning_reads_for_agent": reads_for_agent,
                "event_window_days": event_window_days,
                "read_window_hours": read_window_hours,
                "since_ts": since_ts,
                # Effective lower bound after the event_window_days hard cap,
                # so callers can detect that since_ts was clamped.
                "event_since": event_since,
            },
        }, request_id)
    except Exception as exc:
        logger.exception("subscribe_contradictions failed")
        return _make_error(-32000, f"Subscribe contradictions error: {exc}", request_id)


def _handle_log_event(params: dict, request_id: Any) -> dict:
    """Log an episodic event."""
    global _request_count
    _request_count += 1

    event_type = params.get("event_type", "")
    content = params.get("content", "")
    if not event_type or not content:
        return _make_error(-32602, "event_type and content are required",
                           request_id)

    # G11: EffectivePrincipal is the single server-stamped source (never trust caller)
    principal, err = _handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    task_id = params.get("task_id")
    thread_id = params.get("thread_id")

    try:
        ep = _lazy_episodic()
        eid = ep.log_event(
            agent_id=agent_id,
            event_type=event_type,
            content=content,
            task_id=task_id,
            thread_id=thread_id,
        )
        return _make_response({"event_id": eid, "agent_id": agent_id},
                              request_id)
    except Exception as exc:
        logger.exception("log_event failed")
        return _make_error(-32000, f"Log event error: {exc}", request_id)


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
                c.execute("SELECT COALESCE(MAX(rowid), 0) AS max_rowid, COUNT(*) AS n FROM chunk_embeddings")
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
    """G14/G16: Stage a candidate packet (default learn path). No durable write."""
    principal, err = _handler_principal(params, request_id)
    if err:
        return err

    content = (params.get("content") or params.get("text") or "").strip()
    if not content:
        return _make_error(-32602, "content is required", request_id)

    import time as _time
    import json as _json

    now = _time.time()
    ws = params.get("workspace_id") or "default"
    ev = _json.dumps(params.get("evidence_refs") or params.get("evidence_doc_ids") or [])
    df = _json.dumps(params.get("derived_from") or {})
    instr = 1 if params.get("instruction_like") else 0

    try:
        db = _lazy_writeback().db
        with db.cursor() as c:
            c.execute(
                """
                INSERT INTO candidate_packets
                (principal, workspace_id, layer, privacy_level, content,
                 evidence_refs, derived_from, instruction_like, status, proposed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    principal.agent_id,
                    ws,
                    params.get("layer"),
                    params.get("privacy_level"),
                    content[:200000],  # bound for safety (P1)
                    ev,
                    df,
                    instr,
                    now,
                ),
            )
            cid = c.lastrowid
        logger.info(
            "G16 stage_candidate #%d principal=%s (proposal-first, no durable learn yet)",
            cid, principal.agent_id
        )
        return _make_response(
            {
                "status": "proposed",
                "candidate_id": cid,
                "principal": principal.agent_id,
                "workspace_id": ws,
            },
            request_id,
        )
    except Exception as exc:
        logger.exception("stage_candidate failed")
        return _make_error(-32000, f"stage_candidate error: {exc}", request_id)


def _list_candidates(params: dict, request_id: Any) -> dict:
    """G14/G17: List candidates for the stamped principal (for console)."""
    principal, err = _handler_principal(params, request_id)
    if err:
        return err

    status_f = params.get("status")
    limit = min(int(params.get("limit", 100)), 500)
    try:
        from db import SovereignDB as _SovDB
        import json as _json
        db = _SovDB()
        with db.cursor() as c:
            if status_f:
                c.execute(
                    "SELECT * FROM candidate_packets WHERE principal=? AND status=? ORDER BY proposed_at DESC LIMIT ?",
                    (principal.agent_id, status_f, limit),
                )
            else:
                c.execute(
                    "SELECT * FROM candidate_packets WHERE principal=? ORDER BY proposed_at DESC LIMIT ?",
                    (principal.agent_id, limit),
                )
            rows = []
            for r in c.fetchall():
                d = dict(r)
                for k in ("evidence_refs", "derived_from"):
                    if d.get(k):
                        try:
                            d[k] = _json.loads(d[k])
                        except Exception:
                            pass
                rows.append(d)
        return _make_response(
            {"candidates": rows, "principal": principal.agent_id, "count": len(rows)},
            request_id,
        )
    except Exception as exc:
        logger.exception("list_candidates failed")
        return _make_error(-32000, f"list_candidates error: {exc}", request_id)


def _explicitly_allowed_operator(principal: EffectivePrincipal) -> bool:
    """A3 authz: TRUE only for an operator EXPLICITLY granted cross-principal
    governance — never the synthesized wide-open local default. Grants:

      * a LITERAL ``resolve_candidate`` / ``govern`` capability in the stamped
        principal's capability list (an operator-authored principals/*.json
        grant; the synthesized default only carries ``"*"``, which is NOT
        accepted here), or
      * the explicit ``operator`` principal (only exists when the operator
        ships principals/operator.json), or
      * an agent_id listed in the daemon-env allowlist
        ``MINNI_RESOLVE_OPERATORS`` (comma-separated; operator-controlled,
        never wire-controlled).

    Rationale (live incident): every co-located session subagent is stamped
    'main' on the shared UDS surface, so the blanket is_operator_principal
    default let workflow subagents repeatedly re-resolve another principal's
    candidate and archive a live inbox file. Cross-principal resolution now
    requires one of the explicit grants above.
    """
    caps = principal.capabilities or []
    if "resolve_candidate" in caps or "govern" in caps:
        return True
    if principal.agent_id == "operator":
        return True
    allow = {
        a.strip()
        for a in os.environ.get("MINNI_RESOLVE_OPERATORS", "").split(",")
        if a.strip()
    }
    return principal.agent_id in allow


def _resolve_candidate(params: dict, request_id: Any) -> dict:
    """G15: Resolve a candidate (accept→durable learn, reject/redact etc).

    Authorization is owner-or-explicit-operator and lives INSIDE the
    transaction (after the candidate row is read), because ownership cannot be
    known before the read. There is deliberately NO up-front
    ``is_operator_principal`` gate: it would reject a restricted-capability
    platform agent (caps without ``*``/``resolve_candidate``/``govern``)
    resolving its OWN candidate before the owner check is ever reached, while
    adding nothing the owner check does not already enforce.
    """
    principal, err = _handler_principal(params, request_id)
    if err:
        return err

    try:
        validate_agent_id(principal.agent_id)
    except ValueError as exc:
        return _make_error(-32602, str(exc), request_id)

    cid = params.get("candidate_id")
    if cid is None:
        return _make_error(-32602, "candidate_id is required", request_id)
    try:
        cid = int(cid)
    except Exception:
        return _make_error(-32602, "candidate_id must be integer", request_id)

    decision = str(params.get("decision") or "").strip().lower()
    reason = str(params.get("reason") or params.get("resolution_reason") or "")[:500]
    allowed = {"accept", "learn", "reject", "redact", "do_not_store", "log_only", "merge", "supersede", "mark_sensitive", "mark_temporary", "mark_project_scoped"}
    if decision not in allowed:
        return _make_error(-32602, f"decision must be one of {sorted(allowed)}", request_id)

    status_map = {
        "accept": "accepted",
        "learn": "accepted",
        "reject": "rejected",
        "redact": "redacted",
        "do_not_store": "rejected",
        "log_only": "rejected",
        "merge": "merged",
        "supersede": "superseded",
        "mark_sensitive": "accepted",  # still accepted but tagged via reason
        "mark_temporary": "accepted",
        "mark_project_scoped": "accepted",
    }
    new_status = status_map.get(decision, "rejected")

    import time as _time
    import json as _json
    now = _time.time()

    try:
        from db import SovereignDB as _SovDB
        db = _SovDB()
        lid = None
        # Explicit transaction (BEGIN IMMEDIATE) for atomic SELECT + conditional promote + UPDATE.
        # Closes TOCTOU between read and write (mirrors the pattern in
        # _handle_resolve_contradiction, including _ResolveRejected so early
        # rejections roll back instead of committing a lock-holding no-op).
        with db.transaction() as c:
            c.execute("SELECT * FROM candidate_packets WHERE candidate_id=?", (cid,))
            row = c.fetchone()
            if not row:
                raise _ResolveRejected(
                    _make_error(-32001, f"candidate_not_found: {cid}", request_id)
                )
            rowd = dict(row)
            # A3 authz (live incident: subagents stamped 'main' re-resolved
            # candidate #999 and archived a live inbox file): a caller may only
            # resolve its OWN candidates; cross-principal resolution requires an
            # EXPLICITLY allowed operator (see _explicitly_allowed_operator).
            owner = str(rowd.get("principal") or "")
            if owner != principal.agent_id and not _explicitly_allowed_operator(principal):
                raise _ResolveRejected(_make_error(
                    -32004,
                    f"principal_mismatch: candidate #{cid} is owned by '{owner}'; "
                    f"caller principal '{principal.agent_id}' may only resolve its own "
                    "candidates (cross-principal resolution requires an explicitly "
                    "allowed operator: 'operator', a literal resolve_candidate/govern "
                    "capability, or MINNI_RESOLVE_OPERATORS)",
                    request_id,
                ))
            # Resolution is terminal and once-only (same incident: repeated
            # re-resolution re-ran the inbox archive against a LIVE file).
            # Mirrors the idempotent 'proposed'-only pattern in
            # _promote_candidate_durable / _reject_candidate_dedup.
            if rowd.get("status") != "proposed":
                raise _ResolveRejected(_make_error(
                    -32009,
                    f"already_resolved: candidate #{cid} is '{rowd.get('status')}' "
                    f"(resolved_by={rowd.get('resolved_by')!r}); resolution is terminal "
                    "and cannot be repeated",
                    request_id,
                ))

            if new_status == "accepted":
                # Promote to durable learning (the only path that writes to learnings)
                # Use minimal column set guaranteed by baseline schema (PR-6 columns added by 005 migration;
                # safe subset avoids "no column" in fresh test DBs where ALTER timing varies).
                c.execute(
                    """
                    INSERT INTO learnings
                    (agent_id, category, content, created_at)
                    VALUES (?, 'general', ?, ?)
                    """,
                    (principal.agent_id, rowd.get("content"), now),
                )
                lid = c.lastrowid

            c.execute(
                """
                UPDATE candidate_packets
                SET status=?, resolved_at=?, resolved_by=?, resolution_reason=?
                WHERE candidate_id=?
                """,
                (new_status, now, principal.agent_id, reason, cid),
            )

        # Store-time semantic indexing for the governed promote path: a candidate
        # accepted into a durable learning is now chunked+embedded into the
        # semantic index and the live FAISS is refreshed, so an agent recalling
        # what it just promoted gets semantic hits (the `results` stream), not
        # lexical-only. Runs AFTER the transaction commits (the learning is
        # already durable) and FAILS-OPEN — a semantic-index hiccup never undoes
        # the resolution. Keyed on the learning_id for idempotent re-accept.
        if lid is not None:
            _index_durable_learning(
                principal.agent_id, str(rowd.get("content") or ""),
                key=f"learning:{lid}", db=db,
            )

        # Prominent audit log for the governance action (owner or explicit operator)
        logger.warning(
            "RESOLVE_CANDIDATE operator=%s candidate=%s decision=%s new_status=%s reason=%s (G15 audit)",
            principal.agent_id, cid, decision, new_status, reason[:80]
        )

        # B1 drain-on-resolution: every status in status_map is terminal, so an
        # inbox-sourced candidate's file can now leave the live inbox.
        _maybe_archive_inbox_source(db, cid)

        return _make_response(
            {
                "status": "resolved",
                "candidate_id": cid,
                "new_status": new_status,
                "learning_id": lid,
                "principal": principal.agent_id,
            },
            request_id,
        )
    except _ResolveRejected as exc:
        return exc.response
    except Exception as exc:
        logger.exception("resolve_candidate failed")
        return _make_error(-32000, f"resolve_candidate error: {exc}", request_id)

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


def _parse_request(data: bytes) -> Optional[dict]:
    """Parse a JSON-RPC request from raw bytes."""
    try:
        text = data.decode("utf-8").strip()
        if not text:
            return None
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Invalid request: %s", exc)
        return None


async def _dispatch(request: dict) -> dict:
    """Route a JSON-RPC request to the correct handler."""
    method_name = request.get("method", "")
    request_id = request.get("id")
    params = request.get("params", {}) or {}

    handler = _METHODS.get(method_name)
    if handler is None:
        return _make_error(-32601, f"Method not found: {method_name}",
                           request_id)

    # Handlers index params with .get(); a positional-array params (JSON-RPC
    # allows `"params": [...]`) would crash them with AttributeError, and
    # recovery-allowed methods (ping/status/...) reach the handler even before
    # identity resolves. Reject non-dict params loudly here so no handler — gated
    # or recovery-allowed — ever sees a list.
    if not isinstance(params, dict):
        return _make_error(-32602, "Invalid params: expected a JSON object",
                           request_id)

    provenance = resolve_provenance(request)
    if provenance.recovery is not None and method_name not in RECOVERY_ALLOWED_METHODS:
        return _make_response(provenance.recovery, request_id)
    if provenance.principal is not None:
        params.setdefault("_principal", provenance.principal)

    cap_err = _enforce_method_capability(method_name, provenance.principal, request_id)
    if cap_err is not None:
        return cap_err

    if asyncio.iscoroutinefunction(handler):
        result = await handler(params, request_id)
    else:
        result = await asyncio.to_thread(handler, params, request_id)
    return result


# Sync facade for legacy direct callers in the test suite (post RCM-006/007 async _dispatch refactor).
# ~30 call sites in test_*.py used synchronous minnid._dispatch({...})["result"]; they now use this.
# Real daemon paths (_handle_client, HTTP entry) use await _dispatch or the full async loop.
# This keeps all tests green without requiring pytest-asyncio or rewriting every site to async.
def _dispatch_sync(request: dict) -> dict:
    """Thin sync wrapper: runs the (now async) _dispatch via asyncio.run for test compatibility."""
    return asyncio.run(_dispatch(request))


# SEC-015: cap a single JSON-RPC request body at 1 MiB to bound peak memory
# and protect the embedder from caller-controlled mega-payloads.
_SOCKET_BODY_LIMIT = 1_048_576  # 1 MiB


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

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [minnid] %(levelname)s: %(message)s",
    )

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

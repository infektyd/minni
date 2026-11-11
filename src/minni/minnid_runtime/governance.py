import json
import logging
import math
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from minni.afm_review_markers import supersede_afm_review
from minni.config import normalize_workspace_scope
from minni.db import MinniDB
from minni.durability import durability_of
from minni.durable_projection import (
    evict_learning_to_cold_archive,
    find_near_duplicate_active_learning,
    projection_row_closed,
)


def find_near_duplicate_learning_ids(
    conn_or_cursor: Any,
    agent_id: str,
    category: str,
    content: str,
    embedding: bytes | None = None,
) -> list[int]:
    """Positional/keyword wrapper for find_near_duplicate_active_learning."""
    return find_near_duplicate_active_learning(
        conn_or_cursor,
        agent_id=agent_id,
        category=category,
        content=content,
        embedding=embedding,
    )
from minni.graph_commit import ensure_canonical_learning_node
from minni.graph_coordinator import (
    GraphCommitAborted,
    LearningFields,
    commit_prepared_learning,
)

_HARD_GRAPH_ABORT_CODES = frozenset(
    {
        "canonical_restricted",
        "principal_mismatch",
        "store_binding_mismatch",
        "store_binding_invalid",
        "payload_tampered",
        "bad_payload",
        "bad_principal",
        "learning_field_invalid",
        "learning_field_unavailable",
        "supersedes_missing",
    }
)


class _LearnRejected(Exception):
    def __init__(self, response: dict) -> None:
        super().__init__("learn rejected")
        self.response = response
from minni.migrations import _candidate_status_check_allows_dns_log_only
from minni.minnid_runtime.graph_adapters import (
    apply_fallback_graph_edges,
    collect_superseded_purge_targets,
    finalize_governed_promotion,
    linked_learning_ids_for_edges,
    prepare_governed_promotion,
)

_ORIG_ENSURE_CANONICAL_LEARNING_NODE = ensure_canonical_learning_node
from minni.principal import (
    EffectivePrincipal,
    can_read_document,
    is_operator_principal,
    validate_agent_id,
)
from minni.safety import is_instruction_like

from .redaction import redact_value

# Migration-015 probe. db.py treats a failed migrations run as non-fatal
# (warns and keeps serving), so a live daemon can run against a DB whose
# candidate_packets CHECK still predates 015 and rejects the do_not_store /
# log_only statuses. Probe the CHECK lazily — only when those statuses are
# requested. No memo: resolve_candidate opens a fresh connection per call,
# so a per-connection cache never hits and only retains closed connections.
def _check_allows_dns_log_only(conn: Any) -> bool:
    return _candidate_status_check_allows_dns_log_only(conn)


def _learnings_has_workspace_col(c: Any) -> bool:
    try:
        rows = c.execute("PRAGMA table_info(learnings)").fetchall()
        return any(
            (r["name"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]) == "workspace_id"
            for r in rows
        )
    except Exception:
        return False


def _call_index_durable_learning(
    context: Any,
    agent_id: str,
    content: str,
    *,
    key: str,
    db: Any,
    workspace_id: str = "default",
) -> Any:
    try:
        return context.index_durable_learning(
            agent_id,
            content,
            key=key,
            db=db,
            workspace_id=workspace_id,
        )
    except TypeError:
        return context.index_durable_learning(agent_id, content, key=key, db=db)


logger = logging.getLogger("minnid")

MAX_LEARN_CHARS = 64 * 1024
MAX_LEARN_FIELD_CHARS = 4 * 1024
ACCEPT_FLAGGED_CAPABILITY = "accept_flagged"


@dataclass(frozen=True)
class GovernanceContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    handler_principal: Callable[..., tuple[Any, Optional[dict]]]
    lazy_writeback: Callable[[], Any]
    lazy_episodic: Callable[[], Any]
    record_latency: Callable[[str, float], None]
    index_durable_learning: Callable[..., bool | None]
    maybe_archive_inbox_source: Callable[[Any, int], None]
    # M4: purge the synthetic durable doc when a learning is superseded/rejected
    # so its stale chunks stop surfacing in doc search. Default no-op keeps
    # existing constructions/tests working without threading a real purger.
    purge_durable_learning: Callable[..., None] = lambda *a, **k: None
    increment_request_count: Callable[[], None] | None = None
    sovereign_db: Callable[..., Any] = MinniDB
    notify_candidate_staged: Optional[Callable[[], None]] = None
    resolve_retrieval_engine: Optional[Callable[[Any], Any]] = None
    on_refresh_retry: Optional[Callable[[Any], None]] = None
    logger: logging.Logger = logger


class ResolveRejected(Exception):
    """Carries a pre-built JSON-RPC error response out of a write transaction."""

    def __init__(self, response: dict):
        super().__init__(str(response.get("error", {}).get("message", "rejected")))
        self.response = response


class _CanonicalCommitFailed(Exception):
    """Canonical node/join write failed inside an acceptance transaction.

    Caught by the accept path that raised it: the enclosing transaction has
    already rolled back (zero partial durable writes), so the handler stages
    the proposal for review instead of losing the learn.
    """

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


# #290: ``auto_accept_own`` is OPERATOR CONFIGURATION, read from
# principals/<id>.json. It is never read from wire params, and a caller that
# supplies it is refused rather than ignored.
#
# Refused for EVERY principal, operator included. A principal check would be
# weaker for no benefit: config that can travel over the wire is config an agent
# can reach the moment it obtains — or is mistaken for — a privileged stamp, and
# this particular knob turns the -32004 self-approval gate off. Governance
# config moves by filesystem/console only.
#
# Refusing loudly rather than silently dropping the key keeps the attempt
# visible: a silent ignore looks identical to a working bypass from the caller's
# side, and leaves nothing in the log for an audit to find.
AUTO_ACCEPT_PARAM_KEYS = ("auto_accept_own", "learn.auto_accept_own")

# One request must not be able to hold the daemon's write lock open writing an
# event row per entry. See handle_resolve_contradiction.
MAX_SUPERSEDE_IDS = 64
MAX_PENDING_CANDIDATES_PER_PRINCIPAL = 2000


def reject_wire_supplied_auto_accept(
    params: dict, request_id: Any, context: "GovernanceContext"
) -> Optional[dict]:
    """Return an error response if a caller tried to set the #290 knob."""
    for key in AUTO_ACCEPT_PARAM_KEYS:
        if key in params:
            context.logger.warning(
                "AUTO_ACCEPT_KNOB_REFUSED key=%s (wire-supplied governance config; "
                "the knob is operator-set in principals/<id>.json) (G15 audit)",
                key,
            )
            return context.make_error(
                -32004,
                f"operator_only: '{key}' is operator configuration and cannot be "
                "supplied by a caller; set auto_accept_own in "
                "principals/<id>.json (filesystem/console only)",
                request_id,
            )
    return None


def extract_assertion(content: str) -> str:
    content = content.strip()
    if len(content) <= 120:
        return content
    match = re.search(r"[.!?]\s", content)
    if match:
        return content[: match.start() + 1].strip()
    return content[:120].strip()


def handle_learn(params: dict, request_id: Any, context: GovernanceContext) -> dict:
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
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()

    refusal = reject_wire_supplied_auto_accept(params, request_id, context)
    if refusal is not None:
        return refusal

    content = params.get("content", "")
    if not content:
        return context.make_error(-32602, "content is required", request_id)

    if len(content) > MAX_LEARN_CHARS:
        return context.make_error(
            -32602,
            f"learn content exceeds {MAX_LEARN_CHARS} chars",
            request_id,
        )

    for field_name in ("title", "summary"):
        value = params.get(field_name)
        if isinstance(value, str) and len(value) > MAX_LEARN_FIELD_CHARS:
            return context.make_error(
                -32602,
                f"learn {field_name} exceeds {MAX_LEARN_FIELD_CHARS} chars",
                request_id,
            )

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        return context.make_error(-32602, str(exc), request_id)
    category = str(params.get("category") or "general").strip().lower()
    force = bool(params.get("force", False))

    assertion = params.get("assertion")
    applies_when = params.get("applies_when")
    evidence_doc_ids = params.get("evidence_doc_ids")
    contradicts_id = params.get("contradicts_id")

    try:
        wb = context.lazy_writeback()

        if not force:
            return stage_candidate(params, request_id, context)

        from minni.afm_passes.inbox_ingest import has_wrapper_leak

        if has_wrapper_leak(content):
            return context.make_error(
                -32602,
                "learn content rejected (wrapper_leak)",
                request_id,
            )

        if not is_operator_principal(principal):
            return context.make_error(
                -32004,
                "operator_only: force=true durable learn requires operator principal (principals/*.json capabilities or trusted name)",
                request_id,
            )

        context.logger.warning(
            "FORCE_DURABLE_LEARN principal=%s (explicit escape; G16 audit stamp)",
            principal.agent_id,
        )

        import numpy as np
        from minni.afm_passes.consolidation import (
            content_hash as _content_hash,
            find_active_learning_by_hash,
        )
        from minni.writeback import CATEGORIES

        now = time.time()
        doc_ids_json = json.dumps(evidence_doc_ids) if evidence_doc_ids else None
        supersedes = params.get("supersedes")
        _have_hash_col = ensure_content_hash_column(wb.db)
        _chash = _content_hash(content)

        if category not in CATEGORIES:
            category = "general"

        ws = normalize_workspace_scope(params.get("workspace_id"), default="default")

        with wb.db.cursor() as c_pre:
            if not supersedes and contradicts_id is None:
                existing = find_active_learning_by_hash(c_pre, _chash, agent_id)
                if existing is not None:
                    existing_lid = int(existing["learning_id"])
                    if _have_hash_col:
                        c_pre.execute(
                            "UPDATE learnings SET content_hash = ? "
                            "WHERE learning_id = ? AND content_hash IS NULL",
                            (_chash, existing_lid),
                        )
                    return context.make_response(
                        {
                            "status": "ok",
                            "learning_id": existing_lid,
                            "agent_id": agent_id,
                            "category": existing.get("category") or category,
                            "deduplicated": True,
                        },
                        request_id,
                    )
            has_ws_col = _learnings_has_workspace_col(c_pre)

        plan = prepare_governed_promotion(
            wb.db,
            author_agent=agent_id,
            workspace_id=ws,
            caller_principal=principal,
            content=content,
            category=category,
            learning_fields=LearningFields(
                source_query=params.get("source_query"),
                evidence_doc_ids=doc_ids_json,
                confidence=params.get("confidence", 1.0),
                assertion=assertion,
                applies_when=applies_when,
                contradicts_id=contradicts_id,
                supersedes=supersedes,
                content_hash=_chash if _have_hash_col else None,
                status="active",
                workspace_id=ws if has_ws_col else None,
            ),
            vault_path=getattr(wb.config, "vault_path", None),
            resolve_retrieval_engine=context.resolve_retrieval_engine,
            on_refresh_retry=context.on_refresh_retry,
            allow_adapter=(ensure_canonical_learning_node is _ORIG_ENSURE_CANONICAL_LEARNING_NODE),
        )

        emb_bytes = None
        if plan.prepared is not None and plan.prepared.learning_vector is not None:
            emb_bytes = plan.prepared.learning_vector
        elif wb.model:
            try:
                from minni.models import get_embedder_lock

                embedding_started_at = time.perf_counter()
                with get_embedder_lock():
                    emb = wb.model.encode(content, show_progress_bar=False).astype(np.float32)
                emb_bytes = emb.tobytes()
                context.record_latency("embedding", time.perf_counter() - embedding_started_at)
            except Exception as exc:
                context.logger.warning("learn: embedding failed (%s) - storing without embedding", exc)

        commit_res = None
        superseded_docs: list[tuple[str, str]] = []
        try:
            with wb.db.transaction() as c:
                if supersedes:
                    srow = c.execute(
                        "SELECT 1 FROM learnings WHERE learning_id = ?",
                        (supersedes,),
                    ).fetchone()
                    if srow is None:
                        raise _LearnRejected(
                            context.make_error(
                                -32001, f"learning_not_found: {supersedes}", request_id
                            )
                        )
                if not supersedes and contradicts_id is None:
                    existing = find_active_learning_by_hash(c, _chash, agent_id)
                    if existing is not None:
                        existing_lid = int(existing["learning_id"])
                        if _have_hash_col:
                            c.execute(
                                "UPDATE learnings SET content_hash = ? "
                                "WHERE learning_id = ? AND content_hash IS NULL",
                                (_chash, existing_lid),
                            )
                        return context.make_response(
                            {
                                "status": "ok",
                                "learning_id": existing_lid,
                                "agent_id": agent_id,
                                "category": existing.get("category") or category,
                                "deduplicated": True,
                            },
                            request_id,
                        )

                committed_edges: list[Any] = []
                graph_superseded_lids: list[int] = []
                if (
                    plan.prepared is not None
                    and ensure_canonical_learning_node is _ORIG_ENSURE_CANONICAL_LEARNING_NODE
                ):
                    c.execute("SAVEPOINT minni_graph_commit")
                    try:
                        commit_res = commit_prepared_learning(
                            c, plan.prepared, db=wb.db, principal=plan.graph_principal
                        )
                        c.execute("RELEASE SAVEPOINT minni_graph_commit")
                    except GraphCommitAborted as abort:
                        c.execute("ROLLBACK TO SAVEPOINT minni_graph_commit")
                        c.execute("RELEASE SAVEPOINT minni_graph_commit")
                        if abort.code in _HARD_GRAPH_ABORT_CODES:
                            err_code = (
                                -32001
                                if abort.code == "supersedes_missing"
                                else (
                                    -32004
                                    if abort.code in ("canonical_restricted", "principal_mismatch")
                                    else -32602
                                )
                            )
                            raise _LearnRejected(
                                context.make_error(
                                    err_code, f"{abort.code}: {abort}", request_id
                                )
                            )
                        context.logger.warning(
                            "learn: graph commit aborted (%s: %s); falling back to direct canonical commit",
                            abort.code,
                            abort,
                        )
                        commit_res = None

                if commit_res is not None:
                    lid = commit_res.learning_id
                    canonical_doc_id = commit_res.doc_id
                    committed_edges.extend(commit_res.edges)
                    graph_superseded_lids.extend(commit_res.superseded_learning_ids)
                    if plan.fallback_edges is not None and not committed_edges:
                        fb_edges, fb_sup = apply_fallback_graph_edges(
                            c,
                            plan.fallback_edges,
                            graph_principal=plan.graph_principal,
                            workspace_id=ws,
                            new_learning_id=lid,
                            new_doc_id=canonical_doc_id,
                            now=now,
                        )
                        committed_edges.extend(fb_edges)
                        graph_superseded_lids.extend(fb_sup)
                else:
                    _cols = (
                        "agent_id, category, content, source_doc_ids, source_query, "
                        "confidence, embedding, created_at, "
                        "assertion, applies_when, evidence_doc_ids, contradicts_id, status"
                    )
                    _vals = [
                        agent_id,
                        category,
                        content,
                        None,
                        params.get("source_query"),
                        params.get("confidence", 1.0),
                        emb_bytes,
                        now,
                        assertion,
                        applies_when,
                        doc_ids_json,
                        contradicts_id,
                        "active",
                    ]
                    if _have_hash_col:
                        _cols += ", content_hash"
                        _vals.append(_chash)
                    if _learnings_has_workspace_col(c):
                        _cols += ", workspace_id"
                        _vals.append(ws)
                    c.execute(
                        f"INSERT INTO learnings ({_cols}) VALUES ({', '.join('?' * len(_vals))})",
                        _vals,
                    )
                    lid = c.lastrowid

                    canonical_doc_id = ensure_canonical_learning_node(
                        c,
                        learning_id=lid,
                        agent_id=agent_id,
                        content=content,
                        vault_path=wb.config.vault_path,
                        created_at=now,
                        workspace_id=ws,
                    )
                    if canonical_doc_id is not None:
                        post_doc = c.execute(
                            "SELECT page_status, privacy_level FROM documents WHERE doc_id = ?",
                            (canonical_doc_id,),
                        ).fetchone()
                        if post_doc is not None and projection_row_closed(
                            post_doc["page_status"], post_doc["privacy_level"]
                        ):
                            raise _LearnRejected(
                                context.make_error(
                                    -32004,
                                    "canonical_restricted: canonical node is lifecycle-closed/restricted",
                                    request_id,
                                )
                            )
                    fb_edges, fb_sup = apply_fallback_graph_edges(
                        c,
                        plan.fallback_edges,
                        graph_principal=plan.graph_principal,
                        workspace_id=ws,
                        new_learning_id=lid,
                        new_doc_id=canonical_doc_id,
                        now=now,
                    )
                    committed_edges.extend(fb_edges)
                    graph_superseded_lids.extend(fb_sup)

                    if supersedes:
                        c.execute(
                            "UPDATE learnings SET superseded_by = ? WHERE learning_id = ?",
                            (lid, supersedes),
                        )

                if supersedes and canonical_doc_id is not None:
                    try:
                        sup_doc = c.execute(
                            "SELECT doc_id FROM learning_documents WHERE learning_id = ? ORDER BY doc_id LIMIT 1",
                            (int(supersedes),),
                        ).fetchone()
                        if sup_doc is not None:
                            sup_doc_id = int(
                                sup_doc["doc_id"] if hasattr(sup_doc, "keys") else sup_doc[0]
                            )
                            if sup_doc_id != int(canonical_doc_id):
                                ml_cols = {
                                    r["name"] if hasattr(r, "keys") else r[1]
                                    for r in c.execute("PRAGMA table_info(memory_links)").fetchall()
                                }
                                if "edge_status" in ml_cols:
                                    c.execute(
                                        """INSERT INTO memory_links
                                           (source_doc_id, target_doc_id, link_type, weight, created_at,
                                            confidence, inference_method, model_id, prompt_version,
                                            inference_run_id, evidence_json, inferred_at, edge_status)
                                           VALUES (?, ?, 'updates', 1.0, ?, 1.0, 'explicit_supersedes',
                                                   'explicit', 'v1', 'explicit', '{"explicit":true}', ?, 'active')
                                           ON CONFLICT(source_doc_id, target_doc_id, link_type) DO NOTHING""",
                                        (int(canonical_doc_id), sup_doc_id, now, now),
                                    )
                    except Exception:
                        pass

                wb.add_derived_from_edges(
                    learning_id=lid,
                    agent_id=agent_id,
                    category=category,
                    content=content,
                    evidence_doc_ids=evidence_doc_ids,
                    created_at=now,
                    _cursor=c,
                    canonical_doc_id=canonical_doc_id,
                )

                linked_lids = linked_learning_ids_for_edges(
                    c, committed_edges, exclude_learning_id=lid
                )
                sup_targets = list(graph_superseded_lids)
                if supersedes and int(supersedes) not in sup_targets:
                    sup_targets.append(int(supersedes))
                superseded_docs.extend(
                    collect_superseded_purge_targets(c, sup_targets, agent_id)
                )

                near_dups = find_near_duplicate_active_learning(
                    c,
                    agent_id=agent_id,
                    category=category,
                    content=content,
                    exclude_learning_id=lid,
                    embedding=emb_bytes,
                )
                for dup_lid in near_dups:
                    if supersedes and int(dup_lid) == int(supersedes):
                        continue
                    if int(dup_lid) in linked_lids or int(dup_lid) in graph_superseded_lids:
                        continue
                    dup_row = c.execute(
                        "SELECT agent_id, content FROM learnings WHERE learning_id = ?",
                        (dup_lid,),
                    ).fetchone()
                    dup_agent = str(dup_row["agent_id"]) if dup_row and "agent_id" in dup_row.keys() else agent_id
                    dup_content = str(dup_row["content"]) if dup_row and "content" in dup_row.keys() else f"learning:{dup_lid}"
                    evict_learning_to_cold_archive(
                        c,
                        None,
                        dup_lid,
                        reason=f"superseded_near_duplicate_by:{lid}",
                        db_path=getattr(wb.config, "db_path", None),
                        vault_path=getattr(wb.config, "vault_path", None),
                    )
                    superseded_docs.append((dup_agent, dup_content))
        except _LearnRejected as rejected:
            return rejected.response

        for sup_agent, sup_content in superseded_docs:
            if sup_content:
                try:
                    context.purge_durable_learning(sup_agent, sup_content, db=wb.db)
                except Exception:
                    pass

        context.logger.info(
            "Stored learning #%d [%s/%s]: %.60s...",
            lid, agent_id, category, content,
        )

        if wb.config.writeback_enabled:
            wb._write_to_disk(lid, agent_id, category, content, now, workspace_id=ws)

        if commit_res is not None:
            indexed = finalize_governed_promotion(
                plan, commit_res, content=content, workspace_id=ws
            )
            if not indexed:
                indexed = _call_index_durable_learning(
                    context, agent_id, content, key=f"learning:{lid}", db=wb.db, workspace_id=ws
                )
        else:
            indexed = _call_index_durable_learning(
                context, agent_id, content, key=f"learning:{lid}", db=wb.db, workspace_id=ws
            )

        result = {
            "status": "ok",
            "learning_id": lid,
            "agent_id": agent_id,
            "category": category,
            "workspace_id": ws,
        }
        if indexed is False:
            result["indexed"] = False
        return context.make_response(result, request_id)
    except Exception as exc:
        context.logger.exception("learn failed")
        return context.make_error(-32000, f"Learn error: {exc}", request_id)
    finally:
        context.record_latency("learn", time.perf_counter() - started_at)


def handle_resolve_contradiction(params: dict, request_id: Any, context: GovernanceContext) -> dict:
    """Write a new learning and atomically supersede prior learnings."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    new_content = params.get("new_content", "")
    if not new_content:
        return context.make_error(-32602, "new_content is required", request_id)

    supersede_ids = params.get("supersede_ids", [])
    if not isinstance(supersede_ids, list):
        return context.make_error(-32602, "supersede_ids must be a list", request_id)
    if not all(isinstance(sid, int) and not isinstance(sid, bool) for sid in supersede_ids):
        return context.make_error(-32602, "supersede_ids must be a list of integers", request_id)
    if not supersede_ids:
        return context.make_error(
            -32602,
            "supersede_ids must be a non-empty list of integers",
            request_id,
        )

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        return context.make_error(-32602, str(exc), request_id)

    # This writes an ARBITRARY durable learning — embedded, active, FTS-indexed
    # — and its only precondition is owning the learning being superseded.
    #
    # Measured on this branch: with #290's auto-accept enabled, a principal
    # staged one benign learning (auto-accepted, so now OWNED), then superseded
    # it with content the auto-accept floor had just refused — landing the
    # refused text as 'active'. Owning a learning used to require a human accept,
    # and that accept records the RESOLVER as owner, so a learn-only principal
    # could not previously own one at all. Auto-acceptance is the first path that
    # hands it ownership unattended, which makes this the same kind of
    # unattended durable write and it needs the same floor.
    #
    # A first pass ported only the instruction_like rule. That was a fence with
    # one plank: every other floor rule (too short, oversized, filler, low
    # alphabetic ratio, author-declared privacy) was still one call away from
    # irrelevant for exactly the principal docs/concepts.md tells operators to
    # create. The whole floor applies now.
    #
    # Operators keep today's unrestricted correction machinery — they can
    # already write durable memory directly via force=true. instruction_like is
    # stricter than operator on purpose, mirroring resolve_candidate: it lifts
    # only for the LITERAL 'accept_flagged' capability, so correcting a poisoned
    # learning stays possible but never accidental.
    if len(new_content) > MAX_LEARN_CHARS:
        return context.make_error(
            -32602,
            f"new_content exceeds {MAX_LEARN_CHARS} chars",
            request_id,
        )

    if is_instruction_like(new_content) and not explicitly_allowed_accept_flagged(
        principal
    ):
        context.logger.warning(
            "RESOLVE_CONTRADICTION_REFUSED principal=%s reason=instruction_like "
            "(G15 audit)",
            agent_id,
        )
        return context.make_error(
            -32004,
            "accept_flagged_required: new_content is instruction_like; writing it "
            "as a durable learning requires the literal 'accept_flagged' capability",
            request_id,
        )

    # DELIBERATELY only the safety rule, not the whole auto-accept floor.
    #
    # A previous round applied all five floor rules here and that was an
    # overcorrection, measured against origin/main: it refused corrections that
    # had always worked, from principals with no relationship to this feature —
    # "Port: 8080" (too short), a JSON config line (low alphabetic ratio), a
    # 10k-char runbook (oversized), "rollback rollback rollback" (too few
    # distinct words). It also gated on `privacy_level`, a field this handler
    # never stores, so the rule blocked an honest caller and was evaded by
    # deleting the field.
    #
    # The right standard is the one the MANUAL accept path already sets:
    # resolve_candidate enforces instruction_like and nothing else. The floor is
    # an auto-PROMOTION quality gate — it decides whether newly authored content
    # is good enough to store with nobody looking — and content failing it is
    # low-quality, not dangerous. A correction names what it supersedes and is
    # the agent's own tier, which is the whole point of the #290 ruling.
    #
    # RESIDUAL, stated rather than implied: an opted-in agent can therefore land
    # content here that auto-accept would have withheld on quality. That is
    # bounded to its own memory and to content a human accepting the same
    # candidate would also have allowed. The class that is NOT allowed is the
    # one that matters — prompt injection — and it is gated unconditionally
    # below, lifting only for the literal accept_flagged capability.

    # Unbounded and unde-duplicated, this wrote one contradiction_events row per
    # (id, new learning) pair inside BEGIN IMMEDIATE: measured 400k rows and 2.1s
    # of exclusive daemon write-lock from a single request under the 1 MiB body
    # cap, with a 1.2 MB response echoing the list back.
    supersede_ids = list(dict.fromkeys(supersede_ids))
    if len(supersede_ids) > MAX_SUPERSEDE_IDS:
        return context.make_error(
            -32602,
            f"supersede_ids exceeds {MAX_SUPERSEDE_IDS} entries",
            request_id,
        )

    category = str(params.get("category") or "general").strip().lower()
    assertion = params.get("assertion")
    applies_when = params.get("applies_when")
    evidence_doc_ids = params.get("evidence_doc_ids")

    try:
        import numpy as np
        from minni.writeback import CATEGORIES

        wb = context.lazy_writeback()

        if category not in CATEGORIES:
            category = "general"

        now = time.time()
        doc_ids_json = json.dumps(evidence_doc_ids) if evidence_doc_ids else None

        # Hoisted ABOVE the transaction on purpose. This helper opens a cursor,
        # and MinniDB.cursor() commits on exit on the same thread-local
        # connection — calling it inside `with db.transaction()` ended the
        # enclosing BEGIN IMMEDIATE, so the ownership checks committed early and
        # the INSERT/UPDATE/event block silently ran in a separate deferred
        # transaction. Measured: a rollback after that point left rows behind,
        # and the docstring's "atomically supersede" had stopped being true.
        # Fail-SOFT, matching stage_candidate: the INSERT below names
        # content_hash unconditionally, so a failed column ensure (a transient
        # SQLITE_BUSY on the DDL is enough) would otherwise make the whole
        # CORRECTION fail. A correction is the mechanism for fixing poisoned
        # memory; it must not be the thing that breaks when the DB is busy.
        _have_hash_col = ensure_content_hash_column(wb.db)
        _chash = None
        if _have_hash_col:
            from minni.afm_passes.consolidation import content_hash as _content_hash

            _chash = _content_hash(new_content)

        emb_bytes = None
        if wb.model:
            try:
                from minni.models import get_embedder_lock

                with get_embedder_lock():
                    emb = wb.model.encode(new_content, show_progress_bar=False).astype(np.float32)
                emb_bytes = emb.tobytes()
            except Exception as exc:
                context.logger.warning(
                    "resolve_contradiction: embedding failed (%s) - storing without embedding",
                    exc,
                )

        # M4: (agent_id, content) of each superseded learning, captured inside the
        # txn so we can purge their now-stale synthetic docs after commit.
        superseded_docs: list[tuple[str, str]] = []
        resolved_ws = normalize_workspace_scope(params.get("workspace_id"), default="")
        with wb.db.transaction() as c:
            _has_ws_col = _learnings_has_workspace_col(c)
            for sid in supersede_ids:
                c.execute(
                    "SELECT agent_id, content, workspace_id FROM learnings WHERE learning_id = ?"
                    if _has_ws_col
                    else "SELECT agent_id, content FROM learnings WHERE learning_id = ?",
                    (sid,),
                )
                srow = c.fetchone()
                if not srow:
                    raise ResolveRejected(context.make_error(
                        -32001, f"learning_not_found: {sid}", request_id
                    ))
                owner = str(srow["agent_id"] or "")
                superseded_docs.append((owner, str(srow["content"] or "")))
                if not resolved_ws and _has_ws_col and "workspace_id" in srow.keys():
                    cand_sup_ws = str(srow["workspace_id"] or "").strip()
                    if cand_sup_ws and cand_sup_ws != "default":
                        resolved_ws = cand_sup_ws
                if owner != agent_id and not explicitly_allowed_operator(principal):
                    raise ResolveRejected(context.make_error(
                        -32004,
                        f"principal_mismatch: learning #{sid} is owned by '{owner}'; "
                        f"caller principal '{agent_id}' may only supersede its own "
                        "learnings (cross-principal supersession requires an "
                        "explicitly allowed operator)",
                        request_id,
                    ))
            resolved_ws = resolved_ws or "default"
            _cols = ("agent_id, category, content, source_doc_ids, source_query, "
                     "confidence, embedding, created_at, assertion, applies_when, "
                     "evidence_doc_ids, status")
            _vals = [
                agent_id, category, new_content, None, None,
                params.get("confidence", 1.0), emb_bytes, now,
                assertion, applies_when, doc_ids_json, "active",
            ]
            if _have_hash_col:
                _cols += ", content_hash"
                _vals.append(_chash)
            if _has_ws_col:
                _cols += ", workspace_id"
                _vals.append(resolved_ws)
            c.execute(
                f"INSERT INTO learnings ({_cols}) VALUES "
                f"({', '.join('?' * len(_vals))})",
                _vals,
            )
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

        context.logger.info(
            "Resolved contradiction: new learning #%d supersedes %s",
            new_lid, supersede_ids,
        )

        # M4: superseded learnings must stop surfacing in doc search — purge each
        # one's synthetic durable doc (FTS/chunks/FAISS). Best-effort; never
        # undoes the committed supersession.
        for sup_agent, sup_content in superseded_docs:
            if sup_content:
                context.purge_durable_learning(sup_agent, sup_content, db=wb.db)

        if wb.config.writeback_enabled:
            wb._write_to_disk(
                new_lid, agent_id, category, new_content, now, workspace_id=resolved_ws
            )

        return context.make_response({
            "status": "ok",
            "new_learning_id": new_lid,
            "superseded": supersede_ids,
        }, request_id)
    except ResolveRejected as exc:
        return exc.response
    except Exception as exc:
        context.logger.exception("resolve_contradiction failed")
        return context.make_error(-32000, f"Resolve contradiction error: {exc}", request_id)


_GRAPH_SUBSCRIBE_PAGE = 32
_GRAPH_SUBSCRIBE_MAX_EVENTS = 32


def _graph_contradiction_events_for_reads(
    cursor: Any,
    *,
    agent_id: str,
    event_since: float,
    read_since: Optional[float],
    principal: EffectivePrincipal,
    workspace: Optional[str] = None,
) -> tuple[list[dict], dict]:
    """Page unresolved graph contradiction_log rows via learning_reads.

    Bounded keyset pages, bounded metadata IN lists, cached authorization.
    Duplicate pairs are skipped. Denied counterparts do not consume the
    result cap. Exhausted page/time budget sets incomplete rather than
    dropping already-authorized events.
    """
    from minni.request_deadline import RequestDeadlineExceeded, check_deadline

    work = {
        "log_rows_seen": 0,
        "auth_checks": 0,
        "pages": 0,
        "incomplete": False,
    }
    clauses = [
        "cl.resolution_status = 'unresolved'",
        "cl.detected_at >= ?",
        "lr.agent_id = ?",
        "cl.source_doc_id IS NOT NULL",
        "cl.target_doc_id IS NOT NULL",
    ]
    base_params: list = [event_since, agent_id]
    if read_since is not None:
        clauses.append("lr.read_at >= ?")
        base_params.append(read_since)
    where_sql = " AND ".join(clauses)
    principal_ws = str(getattr(principal, "workspace_id", "default") or "default").strip() or "default"
    if principal_ws not in ("default", "*"):
        effective_ws = principal_ws
    elif workspace:
        effective_ws = normalize_workspace_scope(workspace, default="default")
    else:
        effective_ws = principal_ws
    events: list[dict] = []
    meta_by_id: dict[int, dict] = {}
    auth_cache: dict[int, bool] = {}
    pair_seen: set = set()
    seen_logs: set = set()
    cursor_ts = float(event_since)
    cursor_id = 0
    has_doc_ws_col: Optional[bool] = None

    def _expired() -> bool:
        try:
            check_deadline()
        except RequestDeadlineExceeded:
            return True
        except Exception:
            return False
        return False

    def _readable(doc_id: Any) -> Optional[bool]:
        try:
            key = int(doc_id)
        except (TypeError, ValueError):
            return False
        cached = auth_cache.get(key)
        if cached is not None:
            return cached
        if _expired():
            work["incomplete"] = True
            return None
        meta = meta_by_id.get(key)
        work["auth_checks"] += 1
        allowed = False
        if meta is not None:
            try:
                allowed = bool(can_read_document(principal, effective_ws, meta))
            except Exception:
                allowed = False
        auth_cache[key] = allowed
        if _expired():
            work["incomplete"] = True
        return allowed

    while len(events) < _GRAPH_SUBSCRIBE_MAX_EVENTS:
        if _expired():
            work["incomplete"] = True
            break
        params = list(base_params) + [cursor_ts, cursor_ts, cursor_id, _GRAPH_SUBSCRIBE_PAGE]
        try:
            page = cursor.execute(
                f"""
                SELECT DISTINCT
                       cl.id AS log_id,
                       cl.memory_a_id,
                       cl.source_doc_id,
                       cl.target_doc_id,
                       cl.confidence,
                       cl.detection_method,
                       cl.edge_run_id,
                       cl.detected_at
                FROM contradiction_log cl
                JOIN learning_documents ld
                  ON ld.doc_id IN (cl.source_doc_id, cl.target_doc_id)
                JOIN learning_reads lr
                  ON lr.learning_id = ld.learning_id
                WHERE {where_sql}
                  AND (cl.detected_at > ?
                       OR (cl.detected_at = ? AND cl.id > ?))
                ORDER BY cl.detected_at ASC, cl.id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        except RequestDeadlineExceeded:
            work["incomplete"] = True
            break
        except sqlite3.OperationalError:
            work["incomplete"] = True
            break
        except Exception:
            work["incomplete"] = True
            break
        work["pages"] += 1
        if not page:
            break
        needed: list[int] = []
        parsed = []
        for row in page:
            work["log_rows_seen"] += 1
            try:
                source_id = int(row["source_doc_id"])
                target_id = int(row["target_doc_id"])
                log_id = int(row["log_id"])
            except (TypeError, ValueError):
                continue
            if log_id in seen_logs:
                continue
            pair_key = (
                (source_id, target_id) if source_id <= target_id
                else (target_id, source_id)
            )
            if pair_key in pair_seen:
                continue
            parsed.append((row, source_id, target_id, pair_key, log_id))
            if source_id not in meta_by_id:
                needed.append(source_id)
            if target_id not in meta_by_id:
                needed.append(target_id)
        last = page[-1]
        cursor_ts = float(last["detected_at"] or cursor_ts)
        cursor_id = int(last["log_id"])
        fresh = []
        seen_need = set()
        for doc_id in needed:
            if doc_id in meta_by_id or doc_id in seen_need:
                continue
            seen_need.add(doc_id)
            fresh.append(doc_id)
        for offset in range(0, len(fresh), _GRAPH_SUBSCRIBE_PAGE):
            if _expired():
                work["incomplete"] = True
                break
            chunk = fresh[offset:offset + _GRAPH_SUBSCRIBE_PAGE]
            marks = ",".join("?" * len(chunk))
            try:
                if has_doc_ws_col is None:
                    doc_cols = {
                        r["name"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
                        for r in cursor.execute("PRAGMA table_info(documents)").fetchall()
                    }
                    has_doc_ws_col = "workspace_id" in doc_cols
                ws_col_sql = ", workspace_id" if has_doc_ws_col else ""
                for doc in cursor.execute(
                    f"""SELECT doc_id, path, agent, privacy_level, page_status,
                               page_type, memory_kind{ws_col_sql}
                        FROM documents WHERE doc_id IN ({marks})""",
                    chunk,
                ).fetchall():
                    meta_entry = {
                        "doc_id": doc["doc_id"],
                        "path": doc["path"],
                        "agent": doc["agent"],
                        "privacy_level": doc["privacy_level"],
                        "page_status": doc["page_status"],
                        "page_type": doc["page_type"],
                        "memory_kind": doc["memory_kind"],
                    }
                    if has_doc_ws_col:
                        meta_entry["workspace_id"] = doc["workspace_id"]
                    meta_by_id[int(doc["doc_id"])] = meta_entry
            except RequestDeadlineExceeded:
                work["incomplete"] = True
                break
            except sqlite3.OperationalError:
                work["incomplete"] = True
                break
            except Exception:
                work["incomplete"] = True
                break
        if work["incomplete"] and not parsed:
            break
        stop = False
        for row, source_id, target_id, pair_key, log_id in parsed:
            if len(events) >= _GRAPH_SUBSCRIBE_MAX_EVENTS:
                work["incomplete"] = True
                stop = True
                break
            pair_seen.add(pair_key)
            seen_logs.add(log_id)
            source_ok = _readable(source_id)
            if source_ok is None:
                stop = True
                break
            target_ok = _readable(target_id)
            if target_ok is None:
                stop = True
                break
            if not source_ok or not target_ok:
                continue
            confidence = row["confidence"]
            try:
                confidence = float(confidence) if confidence is not None else None
            except (TypeError, ValueError):
                confidence = None
            if confidence is not None and not math.isfinite(confidence):
                confidence = None
            events.append({
                "event_id": f"graph:{log_id}",
                "kind": "graph",
                "source_doc_id": source_id,
                "target_doc_id": target_id,
                "memory_a_id": row["memory_a_id"],
                "confidence": confidence,
                "detection_method": row["detection_method"],
                "created_at": row["detected_at"],
            })
        if stop:
            break
        if len(page) < _GRAPH_SUBSCRIBE_PAGE:
            break
        if len(events) >= _GRAPH_SUBSCRIBE_MAX_EVENTS:
            work["incomplete"] = True
            break
    _graph_contradiction_events_for_reads.last_work = work  # type: ignore[attr-defined]
    return events, work


def handle_subscribe_contradictions(params: dict, request_id: Any, context: GovernanceContext) -> dict:
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
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    if not agent_id:
        return context.make_error(-32602, "agent_id is required", request_id)
    try:
        since_ts = float(params.get("since_ts", 0) or 0)
    except (TypeError, ValueError):
        return context.make_error(-32602, "since_ts must be a number", request_id)
    if not math.isfinite(since_ts):
        since_ts = 0.0
    now = time.time()
    since_ts = min(since_ts, now + 60)

    read_window_hours_param = params.get("read_window_hours")
    read_window_hours = None
    read_since = None
    if read_window_hours_param is not None:
        try:
            read_window_hours = float(read_window_hours_param)
        except (TypeError, ValueError):
            return context.make_error(
                -32602, "read_window_hours must be a number", request_id
            )
        if not math.isfinite(read_window_hours):
            read_window_hours = None
        else:
            read_window_hours = min(max(read_window_hours, 0.0), 8760.0)
            read_since = now - read_window_hours * 3600

    event_window_days_param = params.get("event_window_days")
    try:
        event_window_days = (
            30.0 if event_window_days_param is None else float(event_window_days_param)
        )
    except (TypeError, ValueError):
        return context.make_error(
            -32602, "event_window_days must be a number", request_id
        )
    if not math.isfinite(event_window_days):
        event_window_days = 30.0
    event_window_days = min(max(event_window_days, 0.0), 365.0)
    event_since = max(since_ts, now - event_window_days * 86400)

    try:
        db = context.lazy_writeback().db
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
            # R10: the global "events in window" count leaked cross-agent
            # activity (an unscoped SELECT COUNT(*) over all contradiction_events)
            # to any caller. Dropped — only agent-scoped counts are returned.
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
            graph_events, graph_work = _graph_contradiction_events_for_reads(
                c,
                agent_id=agent_id,
                event_since=event_since,
                read_since=read_since,
                principal=principal,
                workspace=params.get("workspace_id") or params.get("workspace"),
            )
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
        events.extend(graph_events)
        events.sort(key=lambda item: (item.get("created_at") or 0, str(item.get("event_id"))))
        graph_incomplete = bool(graph_work.get("incomplete"))
        if events:
            status = "matched"
        elif graph_incomplete:
            status = "checked_incomplete"
        else:
            status = "checked_no_match"
        checked = {
            "contradiction_events_for_agent_reads": agent_events,
            "learning_reads_for_agent": reads_for_agent,
            "event_window_days": event_window_days,
            "read_window_hours": read_window_hours,
            "since_ts": since_ts,
            "event_since": event_since,
            "graph_scan_incomplete": graph_incomplete,
        }
        return context.make_response({
            "agent_id": agent_id,
            "events": events,
            "status": status,
            "checked": checked,
        }, request_id)
    except Exception as exc:
        context.logger.exception("subscribe_contradictions failed")
        return context.make_error(-32000, f"Subscribe contradictions error: {exc}", request_id)


def handle_log_event(params: dict, request_id: Any, context: GovernanceContext) -> dict:
    """Log an episodic event."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    event_type = params.get("event_type", "")
    content = params.get("content", "")
    if not event_type or not content:
        return context.make_error(-32602, "event_type and content are required", request_id)

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    task_id = params.get("task_id")
    thread_id = params.get("thread_id")

    try:
        ep = context.lazy_episodic()
        eid = ep.add_event(
            agent_id=agent_id,
            event_type=event_type,
            content=content,
            task_id=task_id,
            thread_id=thread_id,
        )
        return context.make_response({"event_id": eid, "agent_id": agent_id}, request_id)
    except Exception as exc:
        context.logger.exception("log_event failed")
        return context.make_error(-32000, f"Log event error: {exc}", request_id)


def apply_accepted_candidate_effects(cursor: Any, candidate_id: int) -> None:
    """In-transaction side effects of accepting a candidate. ONE definition.

    Every accept path must run this: resolve_candidate (manual), the AFM
    promotion, and #290's auto-accept. It exists as a shared function rather
    than three copies because the copies drift — the auto-accept branch
    originally skipped `supersede_afm_review` on the reasoning that a candidate
    staged microseconds earlier cannot carry a review marker. That reasoning is
    true today and is exactly the kind of thing that silently stops being true
    (a future staging path that fences on write, or inbox ingest reaching this
    code). #305's M5 fixed the orphaned-marker class once; parity should not
    depend on anyone re-deriving that argument.

    M5: the review fence must not outlive the candidate it fences. Same cursor,
    same transaction — a rolled-back accept takes the marker change with it.
    """
    supersede_afm_review(cursor, candidate_id)


def settle_accepted_candidate(
    context: "GovernanceContext",
    db: Any,
    candidate_id: int,
    agent_id: str,
    content: str,
    learning_id: int,
    workspace_id: str = "default",
) -> bool:
    """Post-COMMIT side effects. Returns whether indexing succeeded.

    Never raises: the row is already committed by the time this runs, so an
    exception here reports a failure for a learn that in fact succeeded — the
    caller then retries and duplicates it while the first copy sits durable and
    unindexed.
    """
    indexed_ok = True
    try:
        indexed = _call_index_durable_learning(
            context,
            agent_id,
            content,
            key=f"learning:{learning_id}",
            db=db,
            workspace_id=workspace_id,
        )
        # Production returns an explicit bool, including its fail-open paths.
        # Older injected adapters returned None after a successful call; keep
        # that contract without losing the production failure signal.
        indexed_ok = indexed is True or indexed is None
    except Exception as exc:
        indexed_ok = False
        context.logger.warning(
            "candidate=%s learning=%s indexed=FAILED (%s); durable but not "
            "recallable until reindexed (G15 audit)",
            candidate_id, learning_id, exc,
        )
    try:
        context.maybe_archive_inbox_source(db, candidate_id)
    except Exception as exc:
        context.logger.warning(
            "candidate=%s inbox archive failed (%s; non-fatal)", candidate_id, exc,
        )
    return indexed_ok


_HARVEST_SOURCES = frozenset(
    {
        "jev_harvest",
        "stop_candidates",
        "compact_distillation",
        "session_distillation",
        "hook",
        "auto_harvest",
        "harvest",
        "subagent_harvest",
        "session_harvest",
        "stop_harvest",
    }
)

_SPRINT_CHATTER_RE = re.compile(
    r"(?im)(?:"
    r"\b(?:pushed|committed|merged|landed)\s+(?:commit\s+|sha\s+|pr\s+#?\d+\b|[0-9a-f]{7,40}\b)"
    r"|\b\d+\s*/\s*\d+\s+(?:tests?\s+)?(?:passed|green)\b"
    r"|\b\d+\s+(?:tests?\s+)?passed,\s*0\s+failed\b"
    r"|\b(?:phase|slice|wave|workstream|sprint)\s+(?:\d+[a-z0-9_.-]*|[a-z]\d+[a-z0-9_.-]*|[a-z0-9]+[-_][a-z0-9_.-]+)\s+(?:is\s+|are\s+)?(?:complete|completed|done|green|passed|status|shipped|landed)\b"
    r"|^\s*Compaction summary\s*[—–-]"
    r"|Test failure recovered after edit:\s*Created At:\s*\d{4}-\d{2}-\d{2}"
    r"|\bGIT_BYPASS\s*=\s*1\b(?![^\n]*(?:byte-identical|never\s+bypass))"
    r")"
)

_STRONG_INVARIANT_MARKER_RE = re.compile(
    r"(?i)\b(?:"
    r"invariant|contract|must\s+never|never\s+run|root\s+cause|operator\s+rule"
    r"|never\s+(?:merge|push|commit|bypass|loosen|skip|delete)"
    r")\b"
)

_WEAK_INVARIANT_MARKER_RE = re.compile(
    r"(?i)\b(?:"
    r"only\s+(?:legitimate|valid|allowed|permitted|safe)\s+(?:with|when|if|after)"
    r")\b"
)

_INVARIANT_MARKER_RE = re.compile(
    r"(?i)\b(?:"
    r"invariant|contract|must\s+never|never\s+run|root\s+cause|operator\s+rule"
    r"|only\s+(?:legitimate|valid|allowed|permitted|safe)\s+(?:with|when|if|after)"
    r"|never\s+(?:merge|push|commit|bypass|loosen|skip|delete)"
    r")\b"
)

_EPHEMERAL_TIME_OR_STATUS_RE = re.compile(
    r"(?i)(?:\b(?:today|tonight|this\s+week|before\s+release)\b|(?<![-\w])now(?![-\w])|\b\d{1,2}(?::\d\d)?\s*(?:am|pm)\b)"
)


def non_durable_promotion_blocker(
    *,
    content: str,
    category: str = "general",
    source: Optional[str] = None,
    origin: Optional[str] = None,
) -> Optional[str]:
    """Return a blocker reason if content fails the Rev 6 durability gate for promotion."""
    cat = (category or "general").strip().lower()
    src = (source or "").strip().lower() if source is not None else ""
    orig = (origin or "").strip().lower() if origin is not None else ""
    is_sprint_chatter = bool(_SPRINT_CHATTER_RE.search(content or ""))
    if is_sprint_chatter:
        return "non_durable:noise"
    dur = durability_of(
        {
            "category": cat,
            "content": content or "",
            "source": source,
            "origin": origin,
        }
    )
    if dur == "noise":
        return "non_durable:noise"
    is_harvest_src = (
        bool(src)
        and (
            src in _HARVEST_SOURCES
            or "harvest" in src
            or any(src.startswith(prefix) for prefix in _HARVEST_SOURCES)
        )
    ) or (
        bool(orig)
        and (orig in _HARVEST_SOURCES or "harvest" in orig)
    )
    has_strong_invariant = bool(_STRONG_INVARIANT_MARKER_RE.search(content or ""))
    has_weak_invariant = bool(_WEAK_INVARIANT_MARKER_RE.search(content or ""))
    has_ephemeral_time = bool(_EPHEMERAL_TIME_OR_STATUS_RE.search(content or ""))
    has_invariant = has_strong_invariant or (has_weak_invariant and not has_ephemeral_time)
    if cat == "preference" and not has_invariant:
        return f"non_durable:{dur if dur != 'durable' else 'ordinary'}"
    if is_harvest_src:
        if dur != "durable" or not has_invariant:
            return f"non_durable:{dur if dur != 'durable' else 'ordinary'}"
    elif orig == "agent" and dur != "durable" and not (dur == "ordinary" and has_invariant):
        return f"non_durable:{dur}"
    return None



def auto_accept_blockers(
    content: str,
    instruction_like: bool,
    requested_privacy: str = "safe",
    *,
    category: str = "general",
    source: Optional[str] = None,
    origin: Optional[str] = None,
) -> list[str]:
    """Reasons to WITHHOLD #290 auto-acceptance. Empty list == eligible."""
    # Cheap import: consolidation pulls only stdlib + minni.safety.
    from minni.afm_passes.consolidation import (
        _MIN_CONTENT_LEN,
        _SAFE_PRIVACY,
        _quality_blockers,
    )

    reasons: list[str] = []
    if instruction_like:
        reasons.append("instruction_like")
    if len((content or "").strip()) < _MIN_CONTENT_LEN:
        reasons.append("below minimum content length")
    if str(requested_privacy or "").strip().lower() not in _SAFE_PRIVACY:
        reasons.append(f"privacy={requested_privacy}")
    reasons.extend(_quality_blockers(content))
    dur_blocker = non_durable_promotion_blocker(
        content=content or "",
        category=category,
        source=source,
        origin=origin,
    )
    if dur_blocker is not None:
        reasons.append(dur_blocker)
    return reasons


def ensure_content_hash_column(db: Any) -> bool:
    """Ensure learnings.content_hash + its index exist. No backfill."""
    try:
        with db.cursor() as c:
            c.execute("PRAGMA table_info(learnings)")
            if "content_hash" not in [r["name"] for r in c.fetchall()]:
                try:
                    c.execute("ALTER TABLE learnings ADD COLUMN content_hash TEXT")
                except Exception:
                    pass  # concurrent writer won the race; the column exists now
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_learnings_content_hash "
                "ON learnings(content_hash)"
            )
        return True
    except Exception:
        return False


def stage_candidate(params: dict, request_id: Any, context: GovernanceContext) -> dict:
    """Stage a candidate packet.

    Normally no durable learning write. #290: when the OPERATOR has opted this
    principal in via principals/<id>.json, the candidate the caller just staged
    is promoted to accepted in the same transaction, stamped so an audit can
    tell auto-acceptance from a human resolve.
    """
    refusal = reject_wire_supplied_auto_accept(params, request_id, context)
    if refusal is not None:
        return refusal

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    try:
        wb = context.lazy_writeback()
        with wb.db.cursor() as c:
            c.execute(
                "SELECT COUNT(*) AS n FROM candidate_packets WHERE principal=? AND status='proposed'",
                (principal.agent_id,),
            )
            pending_count = int(c.fetchone()["n"])
            if pending_count >= MAX_PENDING_CANDIDATES_PER_PRINCIPAL:
                return context.make_error(
                    -32005,
                    f"Backpressure: pending candidate queue limit reached ({pending_count} >= {MAX_PENDING_CANDIDATES_PER_PRINCIPAL}) for principal {principal.agent_id}",
                    request_id,
                )
    except Exception as exc:
        context.logger.warning("stage_candidate: backpressure check failed (%s); proceeding", exc)

    content = (params.get("content") or params.get("text") or "").strip()
    if not content:
        return context.make_error(-32602, "content is required", request_id)
    stored_content = content[:200000]
    from minni.afm_passes.inbox_ingest import intake_junk_reason

    junk_reason = intake_junk_reason(stored_content)
    if junk_reason is not None:
        return context.make_error(
            -32602,
            f"candidate rejected at intake ({junk_reason})",
            request_id,
        )

    now = time.time()
    ws = normalize_workspace_scope(params.get("workspace_id"), default="default")
    ev = json.dumps(params.get("evidence_refs") or params.get("evidence_doc_ids") or [])
    raw_df = params.get("derived_from")
    if isinstance(raw_df, dict):
        df_dict = dict(raw_df)
    elif isinstance(raw_df, str) and raw_df.strip():
        try:
            parsed_df = json.loads(raw_df)
            df_dict = dict(parsed_df) if isinstance(parsed_df, dict) else {"raw": raw_df}
        except Exception:
            df_dict = {"raw": raw_df}
    else:
        df_dict = {}
    source_param = params.get("source")
    if source_param is None and isinstance(raw_df, dict):
        source_param = raw_df.get("source")
    origin_param = params.get("origin")
    if origin_param is None and isinstance(raw_df, dict):
        origin_param = raw_df.get("origin")
    if source_param is not None and "source" not in df_dict:
        df_dict["source"] = str(source_param)
    if origin_param is not None and "origin" not in df_dict:
        df_dict["origin"] = str(origin_param)
    if str(df_dict.get("source") or "").strip().lower() == "inbox":
        df_dict.pop("source", None)
    df_dict.pop("inbox_file", None)
    df = json.dumps(df_dict)
    instr = 1 if (params.get("instruction_like") or is_instruction_like(stored_content)) else 0
    from minni.durability import normalize_category
    from minni.writeback import CATEGORIES

    raw_category = str(params.get("category") or "general").strip().lower()
    norm_category = normalize_category(raw_category)
    category = (
        raw_category
        if raw_category in CATEGORIES
        else (norm_category if norm_category in CATEGORIES else "general")
    )

    if "privacy_level" in params:
        requested_privacy = params["privacy_level"]
    else:
        try:
            from minni.indexer import VaultIndexer

            requested_privacy = VaultIndexer._extract_frontmatter(
                stored_content
            )["privacy_level"]
        except Exception:
            requested_privacy = "safe"

    # Never persist SQL NULL / blank: that reopens the permanent-park bug.
    if requested_privacy is None or (
        isinstance(requested_privacy, str) and not str(requested_privacy).strip()
    ):
        requested_privacy = "safe"
    else:
        requested_privacy = str(requested_privacy).strip()

    if explicitly_allowed_operator(principal):
        stored_privacy = requested_privacy
    else:
        stored_privacy = "review"

    withheld = auto_accept_blockers(
        stored_content,
        bool(instr),
        requested_privacy,
        category=category,
        source=str(source_param) if source_param is not None else None,
        origin=str(origin_param) if origin_param is not None else None,
    )
    auto_accept = bool(getattr(principal, "auto_accept_own", False)) and not withheld
    resolved_by = f"auto_accept_own({principal.agent_id})"

    try:
        wb = context.lazy_writeback()
        db = wb.db
        lid = None
        chash = None
        emb_bytes = None
        plan = None
        commit_res = None
        superseded_docs: list[tuple[str, str]] = []
        if auto_accept:
            try:
                from minni.afm_passes.consolidation import (
                    content_hash as _content_hash,
                    has_active_learning_hash,
                )

                if not ensure_content_hash_column(db):
                    raise RuntimeError("content_hash column unavailable")
                chash = _content_hash(stored_content)
                model = getattr(wb, "model", None)
                if model is None:
                    raise RuntimeError("embedder unavailable")
                import numpy as np

                from minni.models import get_embedder_lock

                with get_embedder_lock():
                    emb = model.encode(
                        stored_content, show_progress_bar=False
                    ).astype(np.float32)
                emb_bytes = emb.tobytes()
                with db.cursor() as c_pre:
                    has_ws_col = _learnings_has_workspace_col(c_pre)
                plan = prepare_governed_promotion(
                    db,
                    author_agent=principal.agent_id,
                    workspace_id=ws,
                    caller_principal=principal,
                    content=stored_content,
                    category=category,
                    learning_fields=LearningFields(
                        content_hash=chash,
                        status="active",
                        workspace_id=ws if has_ws_col else None,
                    ),
                    vault_path=getattr(wb.config, "vault_path", None),
                    resolve_retrieval_engine=context.resolve_retrieval_engine,
                    on_refresh_retry=context.on_refresh_retry,
                    allow_adapter=(
                        ensure_canonical_learning_node is _ORIG_ENSURE_CANONICAL_LEARNING_NODE
                    ),
                )
            except Exception as exc:
                context.logger.warning(
                    "auto_accept_own: could not prepare the durable write (%s); "
                    "staging as proposed instead (G15 audit)", exc,
                )
                auto_accept = False
                chash = None
                emb_bytes = None
                plan = None
                withheld = list(withheld) + ["durable-write preparation failed"]

        def _stage_proposal(c):
            pragma = c.execute("PRAGMA table_info(candidate_packets)").fetchall()
            cols = {row["name"] if isinstance(row, dict) else row[1] for row in pragma}
            if "category" in cols and "requested_privacy" in cols:
                c.execute(
                    """
                    INSERT INTO candidate_packets
                    (principal, workspace_id, layer, privacy_level, requested_privacy, category, content,
                     evidence_refs, derived_from, instruction_like, status, proposed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)
                    """,
                    (
                        principal.agent_id,
                        ws,
                        params.get("layer"),
                        stored_privacy,
                        requested_privacy,
                        category,
                        stored_content,
                        ev,
                        df,
                        instr,
                        now,
                    ),
                )
            elif "requested_privacy" in cols:
                c.execute(
                    """
                    INSERT INTO candidate_packets
                    (principal, workspace_id, layer, privacy_level, requested_privacy, content,
                     evidence_refs, derived_from, instruction_like, status, proposed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)
                    """,
                    (
                        principal.agent_id,
                        ws,
                        params.get("layer"),
                        stored_privacy,
                        requested_privacy,
                        stored_content,
                        ev,
                        df,
                        instr,
                        now,
                    ),
                )
            else:
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
                        stored_privacy,
                        stored_content,
                        ev,
                        df,
                        instr,
                        now,
                    ),
                )
            return c.lastrowid

        try:
            with db.transaction() as c:
                cid = _stage_proposal(c)
                if auto_accept and chash is not None:
                    if has_active_learning_hash(c, chash, principal.agent_id):
                        auto_accept = False
                        withheld = ["duplicate of an existing learning"]
                if auto_accept:
                    commit_res = None
                    committed_edges: list[Any] = []
                    graph_superseded_lids: list[int] = []
                    if (
                        plan is not None
                        and plan.prepared is not None
                        and ensure_canonical_learning_node is _ORIG_ENSURE_CANONICAL_LEARNING_NODE
                    ):
                        c.execute("SAVEPOINT minni_graph_commit")
                        try:
                            commit_res = commit_prepared_learning(
                                c, plan.prepared, db=db, principal=plan.graph_principal
                            )
                            c.execute("RELEASE SAVEPOINT minni_graph_commit")
                        except GraphCommitAborted as abort:
                            c.execute("ROLLBACK TO SAVEPOINT minni_graph_commit")
                            c.execute("RELEASE SAVEPOINT minni_graph_commit")
                            if abort.code in _HARD_GRAPH_ABORT_CODES:
                                raise _CanonicalCommitFailed(abort)
                            context.logger.warning(
                                "auto_accept_own: graph commit aborted (%s: %s); falling back to direct canonical commit",
                                abort.code,
                                abort,
                            )
                            commit_res = None
                        except Exception as exc:
                            c.execute("ROLLBACK TO SAVEPOINT minni_graph_commit")
                            c.execute("RELEASE SAVEPOINT minni_graph_commit")
                            raise _CanonicalCommitFailed(exc)

                    if commit_res is not None:
                        lid = commit_res.learning_id
                        canonical_doc_id = commit_res.doc_id
                        committed_edges.extend(commit_res.edges)
                        graph_superseded_lids.extend(commit_res.superseded_learning_ids)
                        if plan.fallback_edges is not None and not committed_edges:
                            fb_edges, fb_sup = apply_fallback_graph_edges(
                                c,
                                plan.fallback_edges,
                                graph_principal=plan.graph_principal,
                                workspace_id=ws,
                                new_learning_id=lid,
                                new_doc_id=canonical_doc_id,
                                now=now,
                            )
                            committed_edges.extend(fb_edges)
                            graph_superseded_lids.extend(fb_sup)
                    else:
                        if _learnings_has_workspace_col(c):
                            c.execute(
                                """
                                INSERT INTO learnings
                                (agent_id, category, content, created_at, embedding,
                                 content_hash, status, workspace_id)
                                VALUES (?, ?, ?, ?, ?, ?, 'active', ?)
                                """,
                                (
                                    principal.agent_id, category, stored_content, now,
                                    emb_bytes, chash, ws,
                                ),
                            )
                        else:
                            c.execute(
                                """
                                INSERT INTO learnings
                                (agent_id, category, content, created_at, embedding,
                                 content_hash, status)
                                VALUES (?, ?, ?, ?, ?, ?, 'active')
                                """,
                                (
                                    principal.agent_id, category, stored_content, now,
                                    emb_bytes, chash,
                                ),
                            )
                        lid = c.lastrowid
                        try:
                            canonical_doc_id = ensure_canonical_learning_node(
                                c,
                                learning_id=lid,
                                agent_id=principal.agent_id,
                                content=stored_content,
                                vault_path=wb.config.vault_path,
                                created_at=now,
                                workspace_id=ws,
                            )
                        except Exception as exc:
                            raise _CanonicalCommitFailed(exc)
                        if canonical_doc_id is not None:
                            post_doc = c.execute(
                                "SELECT page_status, privacy_level FROM documents WHERE doc_id = ?",
                                (canonical_doc_id,),
                            ).fetchone()
                            if post_doc is not None and projection_row_closed(
                                post_doc["page_status"], post_doc["privacy_level"]
                            ):
                                raise _CanonicalCommitFailed(
                                    RuntimeError("canonical node is lifecycle-closed/restricted")
                                )
                        if plan is not None and plan.fallback_edges is not None:
                            fb_edges, fb_sup = apply_fallback_graph_edges(
                                c,
                                plan.fallback_edges,
                                graph_principal=plan.graph_principal,
                                workspace_id=ws,
                                new_learning_id=lid,
                                new_doc_id=canonical_doc_id,
                                now=now,
                            )
                            committed_edges.extend(fb_edges)
                            graph_superseded_lids.extend(fb_sup)

                    linked_lids = linked_learning_ids_for_edges(
                        c, committed_edges, exclude_learning_id=lid
                    )
                    superseded_docs.extend(
                        collect_superseded_purge_targets(c, graph_superseded_lids, principal.agent_id)
                    )
                    near_dups = find_near_duplicate_active_learning(
                        c,
                        agent_id=principal.agent_id,
                        category=category,
                        content=stored_content,
                        exclude_learning_id=lid,
                        embedding=emb_bytes,
                    )
                    for dup_lid in near_dups:
                        if int(dup_lid) in linked_lids or int(dup_lid) in graph_superseded_lids:
                            continue
                        dup_row = c.execute(
                            "SELECT agent_id, content FROM learnings WHERE learning_id = ?",
                            (dup_lid,),
                        ).fetchone()
                        dup_agent = (
                            str(dup_row["agent_id"])
                            if dup_row and "agent_id" in dup_row.keys()
                            else principal.agent_id
                        )
                        dup_content = (
                            str(dup_row["content"])
                            if dup_row and "content" in dup_row.keys()
                            else f"learning:{dup_lid}"
                        )
                        evict_learning_to_cold_archive(
                            c,
                            None,
                            dup_lid,
                            reason=f"superseded_near_duplicate_by:{lid}",
                            db_path=getattr(wb.config, "db_path", None),
                            vault_path=getattr(wb.config, "vault_path", None),
                        )
                        superseded_docs.append((dup_agent, dup_content))
                    c.execute(
                        """
                        UPDATE candidate_packets
                        SET status='accepted', resolved_at=?, resolved_by=?,
                            resolution_reason=?
                        WHERE candidate_id=?
                        """,
                        (
                            now,
                            resolved_by,
                            "auto_accept_own: operator-enabled auto-acceptance of the "
                            "principal's own candidate (#290)",
                            cid,
                        ),
                    )
                    apply_accepted_candidate_effects(c, cid)
        except _CanonicalCommitFailed as exc:
            # Zero partial durable writes (the txn above rolled back); stage
            # the proposal so the learn is routed to review, not lost.
            context.logger.warning(
                "auto_accept_own: canonical commit failed (%s); "
                "staging as proposed instead (G15 audit)", exc.cause,
            )
            auto_accept = False
            lid = None
            commit_res = None
            superseded_docs = []
            withheld = list(withheld) + ["canonical commit failed"]
            with db.transaction() as c2:
                cid = _stage_proposal(c2)

        for sup_agent, sup_content in superseded_docs:
            if sup_content:
                try:
                    context.purge_durable_learning(sup_agent, sup_content, db=db)
                except Exception:
                    pass

        indexed_ok = True
        if lid is not None:
            try:
                if getattr(getattr(wb, "config", None), "writeback_enabled", False) and hasattr(
                    wb, "_write_to_disk"
                ):
                    wb._write_to_disk(
                        lid, principal.agent_id, category, stored_content, now, workspace_id=ws
                    )
            except Exception:
                pass
            if commit_res is not None and plan is not None:
                indexed_ok = finalize_governed_promotion(
                    plan, commit_res, content=stored_content, workspace_id=ws
                )
                if not indexed_ok:
                    indexed_ok = settle_accepted_candidate(
                        context, db, cid, principal.agent_id, stored_content, lid,
                        workspace_id=ws,
                    )
                else:
                    try:
                        context.maybe_archive_inbox_source(db, cid)
                    except Exception as exc:
                        context.logger.warning(
                            "candidate=%s inbox archive failed (%s; non-fatal)", cid, exc,
                        )
            else:
                indexed_ok = settle_accepted_candidate(
                    context, db, cid, principal.agent_id, stored_content, lid,
                    workspace_id=ws,
                )
            context.logger.warning(
                "AUTO_ACCEPT_OWN candidate=%s learning=%s principal=%s "
                "(operator-enabled; resolved_by=%s) (G15 audit)",
                cid, lid, principal.agent_id, resolved_by,
            )
        else:
            if getattr(principal, "auto_accept_own", False):
                # Observable, not silent: the operator enabled the knob and this
                # candidate still needs a human. Saying why is the difference
                # between "withheld" and "quietly broken".
                context.logger.warning(
                    "AUTO_ACCEPT_OWN_WITHHELD candidate=%s principal=%s reasons=%s "
                    "(routed to review, not dropped) (G15 audit)",
                    cid, principal.agent_id, ",".join(withheld),
                )
            context.logger.info(
                "G16 stage_candidate #%d principal=%s privacy=%s (proposal-first, no durable learn yet)",
                cid, principal.agent_id, stored_privacy,
            )
            if context.notify_candidate_staged is not None:
                try:
                    context.notify_candidate_staged()
                except Exception as exc:
                    context.logger.warning("notify_candidate_staged failed: %s", exc)

        result = {
            "status": "accepted" if lid is not None else "proposed",
            "candidate_id": cid,
            "principal": principal.agent_id,
            "workspace_id": ws,
            "privacy_level": stored_privacy,
            "category": category,
        }
        if lid is not None:
            result["learning_id"] = lid
            result["resolved_by"] = resolved_by
            if not indexed_ok:
                result["indexed"] = False
        elif withheld and getattr(principal, "auto_accept_own", False):
            result["auto_accept_withheld"] = withheld
        return context.make_response(result, request_id)
    except Exception as exc:
        context.logger.exception("stage_candidate failed")
        return context.make_error(-32000, f"stage_candidate error: {exc}", request_id)


def list_candidates(params: dict, request_id: Any, context: GovernanceContext) -> dict:
    """List candidates for the stamped principal.

    Default status is ``proposed`` (the drain queue). Omitting status used
    to SELECT every status, so a later list after redact/reject returned
    hidden packet content. Explicit status still works for console zones.
    Envelopes that cross the process boundary are POLICY §2 redacted, and
    ``total`` / ``has_more`` make a truncated page visible. Both come from
    a single ``LIMIT n+1`` read — sqlite3 does not start a transaction on
    SELECT, so a COUNT(*) after the page is a second WAL snapshot. When
    has_more, total is len(page)+1 (the n+1 lower bound), not a later
    COUNT(*).
    """
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    raw_status = params.get("status")
    if isinstance(raw_status, str):
        status_f = raw_status.strip() or "proposed"
    elif raw_status in (None, ""):
        status_f = "proposed"
    else:
        status_f = str(raw_status).strip() or "proposed"

    try:
        limit = int(params.get("limit", 100))
    except (TypeError, ValueError):
        return context.make_error(-32602, "limit must be an integer", request_id)
    limit = min(max(limit, 1), 500)

    db = None
    try:
        db = context.sovereign_db()
        from minni.afm_passes.inbox_ingest import _principal_family

        family = _principal_family(principal.agent_id)
        placeholders = ",".join("?" for _ in family)
        with db.cursor() as c:
            # One statement for the page + has_more probe. A prior COUNT(*)
            # is a different WAL snapshot; extras that land between the two
            # would fill LIMIT and still report has_more false.
            # Family-scoped (#44): co-located sessions share a principal
            # family, so scope by family, not the single stamped agent_id.
            # Top-level operator ('main' / 'operator') without an explicit
            # principal filter inspects the full drain queue across principals.
            review_cols = (
                "cp.*, "
                "(SELECT ca.status FROM consolidation_actions ca "
                "WHERE ca.claim = CAST(cp.candidate_id AS TEXT) "
                "ORDER BY ca.created_at DESC, ca.action_id DESC LIMIT 1) AS review_status, "
                "(SELECT ca.detail FROM consolidation_actions ca "
                "WHERE ca.claim = CAST(cp.candidate_id AS TEXT) "
                "ORDER BY ca.created_at DESC, ca.action_id DESC LIMIT 1) AS review_detail, "
                "(SELECT ca.action_type FROM consolidation_actions ca "
                "WHERE ca.claim = CAST(cp.candidate_id AS TEXT) "
                "ORDER BY ca.created_at DESC, ca.action_id DESC LIMIT 1) AS review_action_type "
            )
            explicit_agent_filter = bool(
                isinstance(params, dict) and str(params.get("agent_id") or "").strip()
            )
            if is_operator_principal(principal) and not explicit_agent_filter:
                c.execute(
                    f"SELECT {review_cols} FROM candidate_packets cp "
                    "WHERE cp.status=? "
                    "ORDER BY cp.proposed_at DESC LIMIT ?",
                    (status_f, limit + 1),
                )
            else:
                c.execute(
                    f"SELECT {review_cols} FROM candidate_packets cp "
                    f"WHERE cp.principal IN ({placeholders}) AND cp.status=? "
                    "ORDER BY cp.proposed_at DESC LIMIT ?",
                    (*family, status_f, limit + 1),
                )
            rows = []
            for row in c.fetchall():
                item = dict(row)
                for key in ("evidence_refs", "derived_from"):
                    if item.get(key):
                        try:
                            item[key] = json.loads(item[key])
                        except Exception:
                            pass
                redacted, _ = redact_value(item)
                rows.append(redacted)
            has_more = len(rows) > limit
            rows = rows[:limit]
            total = len(rows) + 1 if has_more else len(rows)
        return context.make_response(
            {
                "candidates": rows,
                "principal": principal.agent_id,
                "status": status_f,
                "count": len(rows),
                "total": total,
                "limit": limit,
                "has_more": has_more,
            },
            request_id,
        )
    except Exception as exc:
        context.logger.exception("list_candidates failed")
        envelope = context.make_error(
            -32000, f"list_candidates error: {exc}", request_id
        )
        redacted, _ = redact_value(envelope)
        return redacted
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass


def explicitly_allowed_operator(principal: EffectivePrincipal) -> bool:
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
        agent.strip()
        for agent in os.environ.get("MINNI_RESOLVE_OPERATORS", "").split(",")
        if agent.strip()
    }
    return principal.agent_id in allow


def explicitly_allowed_accept_flagged(principal: EffectivePrincipal) -> bool:
    """TRUE only for a literal operator-authored poisoned-candidate override."""
    return ACCEPT_FLAGGED_CAPABILITY in (principal.capabilities or [])


def resolve_candidate(params: dict, request_id: Any, context: GovernanceContext) -> dict:
    """G15: Resolve a candidate (accept→durable learn, reject/redact etc).

    Authorization is owner-or-explicit-operator and lives INSIDE the
    transaction (after the candidate row is read), because ownership cannot be
    known before the read. There is deliberately NO up-front
    ``is_operator_principal`` gate: it would reject a restricted-capability
    platform agent (caps without ``*``/``resolve_candidate``/``govern``)
    resolving its OWN candidate before the owner check is ever reached, while
    adding nothing the owner check does not already enforce.
    """
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    try:
        validate_agent_id(principal.agent_id)
    except ValueError as exc:
        return context.make_error(-32602, str(exc), request_id)

    cid = params.get("candidate_id")
    if cid is None:
        return context.make_error(-32602, "candidate_id is required", request_id)
    try:
        cid = int(cid)
    except Exception:
        return context.make_error(-32602, "candidate_id must be integer", request_id)

    decision = str(params.get("decision") or "").strip().lower()
    reason = str(params.get("reason") or params.get("resolution_reason") or "")[:500]
    allowed = {
        "accept",
        "learn",
        "reject",
        "redact",
        "do_not_store",
        "log_only",
        "merge",
        "supersede",
    }
    # Issue #123: mark_sensitive/mark_temporary/mark_project_scoped were
    # unenforced no-ops that mapped to a plain "accepted" — no privacy flag,
    # no expiry, no scope was ever stored or honored. Rejected explicitly
    # until real privacy/TTL/scope semantics land (schema + retrieval).
    unimplemented = {"mark_sensitive", "mark_temporary", "mark_project_scoped"}
    if decision in unimplemented:
        return context.make_error(
            -32602,
            f"decision '{decision}' is not yet implemented: it previously mapped to a "
            f"plain accept with no privacy/expiry/scope enforcement (see issue #123); "
            f"decision must be one of {sorted(allowed)}",
            request_id,
        )
    if decision not in allowed:
        return context.make_error(-32602, f"decision must be one of {sorted(allowed)}", request_id)

    status_map = {
        "accept": "accepted",
        "learn": "accepted",
        "reject": "rejected",
        "redact": "redacted",
        # Distinct statuses so quarantine / log-only zones can list them
        # (list_candidates filters by status string). Pre-change rows stay
        # "rejected" — no migration; zones populate going forward.
        "do_not_store": "do_not_store",
        "log_only": "log_only",
        "merge": "merged",
        "supersede": "superseded",
    }
    new_status = status_map.get(decision, "rejected")
    now = time.time()

    db = None
    try:
        db = context.sovereign_db()
        lid = None
        deduplicated_accept = False
        author_agent = principal.agent_id
        superseded_docs: list[tuple[str, str]] = []
        plan = None
        commit_res = None
        pre_snapshot: tuple[str, str, str, str] | None = None
        if new_status == "accepted":
            try:
                from minni.afm_passes.consolidation import content_hash as _pre_content_hash
                from minni.afm_passes.inbox_ingest import _canonical_principal as _pre_canon
                from minni.writeback import CATEGORIES as _PRE_CATEGORIES

                with db.cursor() as c_pre:
                    c_pre.execute(
                        "SELECT * FROM candidate_packets WHERE candidate_id=?", (cid,)
                    )
                    pre_row = c_pre.fetchone()
                    if pre_row:
                        pre_d = dict(pre_row)
                        pre_owner = str(pre_d.get("principal") or "")
                        pre_author = pre_owner or principal.agent_id
                        pre_content = str(pre_d.get("content") or "")
                        pre_instr = bool(int(pre_d.get("instruction_like") or 0)) or is_instruction_like(
                            pre_content
                        )
                        pre_raw_cat = str(
                            pre_d.get("category") or params.get("category") or "general"
                        ).strip().lower()
                        pre_cat = (
                            pre_raw_cat if pre_raw_cat in _PRE_CATEGORIES else "general"
                        )
                        pre_ws = (
                            str(pre_d.get("workspace_id") or "default").strip()
                            or "default"
                        )
                        pre_cols = {
                            r["name"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
                            for r in c_pre.execute("PRAGMA table_info(learnings)").fetchall()
                        }
                        pre_ok = (
                            pre_d.get("status") == "proposed"
                            and (
                                _pre_canon(pre_owner) == _pre_canon(principal.agent_id)
                                or explicitly_allowed_operator(principal)
                            )
                            and (
                                not pre_instr
                                or explicitly_allowed_accept_flagged(principal)
                            )
                            and (
                                is_operator_principal(principal)
                                or explicitly_allowed_operator(principal)
                            )
                        )
                    else:
                        pre_ok = False
                if pre_ok and pre_content.strip():
                    pre_has_hash = "content_hash" in pre_cols
                    pre_has_status = "status" in pre_cols
                    pre_has_ws = "workspace_id" in pre_cols
                    plan = prepare_governed_promotion(
                        db,
                        author_agent=pre_author,
                        workspace_id=pre_ws,
                        caller_principal=principal,
                        content=pre_content,
                        category=pre_cat,
                        learning_fields=LearningFields(
                            content_hash=_pre_content_hash(pre_content) if pre_has_hash else None,
                            status="active" if pre_has_status else None,
                            workspace_id=pre_ws if pre_has_ws else None,
                        ),
                        vault_path=getattr(getattr(db, "config", None), "vault_path", None),
                        resolve_retrieval_engine=context.resolve_retrieval_engine,
                        on_refresh_retry=context.on_refresh_retry,
                        allow_adapter=(
                            ensure_canonical_learning_node
                            is _ORIG_ENSURE_CANONICAL_LEARNING_NODE
                        ),
                    )
                    pre_snapshot = (pre_content, pre_author, pre_ws, pre_cat)
            except Exception as exc:
                context.logger.debug("resolve_candidate: Phase-A prepare skipped (%s)", exc)
                plan = None
                pre_snapshot = None

        with db.transaction() as c:
            c.execute("SELECT * FROM candidate_packets WHERE candidate_id=?", (cid,))
            row = c.fetchone()
            if not row:
                raise ResolveRejected(
                    context.make_error(-32001, f"candidate_not_found: {cid}", request_id)
                )
            rowd = dict(row)
            owner = str(rowd.get("principal") or "")
            author_agent = owner or principal.agent_id
            from minni.afm_passes.inbox_ingest import _canonical_principal

            if (
                _canonical_principal(owner)
                != _canonical_principal(principal.agent_id)
                and not explicitly_allowed_operator(principal)
            ):
                raise ResolveRejected(context.make_error(
                    -32004,
                    f"principal_mismatch: candidate #{cid} is owned by '{owner}'; "
                    f"caller principal '{principal.agent_id}' may only resolve its own "
                    "candidates (cross-principal resolution requires an explicitly "
                    "allowed operator: 'operator', a literal resolve_candidate/govern "
                    "capability, or MINNI_RESOLVE_OPERATORS)",
                    request_id,
                ))
            if rowd.get("status") != "proposed":
                raise ResolveRejected(context.make_error(
                    -32009,
                    f"already_resolved: candidate #{cid} is '{rowd.get('status')}' "
                    f"(resolved_by={rowd.get('resolved_by')!r}); resolution is terminal "
                    "and cannot be repeated",
                    request_id,
                ))

            content = str(rowd["content"] or "")
            stored_instruction_like = bool(int(rowd["instruction_like"] or 0))
            computed_instruction_like = stored_instruction_like or is_instruction_like(content)
            if computed_instruction_like and not stored_instruction_like:
                c.execute(
                    "UPDATE candidate_packets SET instruction_like=1 WHERE candidate_id=?",
                    (cid,),
                )

            if (
                new_status == "accepted"
                and computed_instruction_like
                and not explicitly_allowed_accept_flagged(principal)
            ):
                return context.make_error(
                    -32004,
                    "accept_flagged_required: candidate is instruction_like; "
                    "accepting it requires literal 'accept_flagged' capability",
                    request_id,
                )

            # P2/P5: a decision that durably promotes the candidate to a learning
            # (accept/learn and the mark_* accepts) requires operator/govern
            # authority even for a SELF-OWNED candidate. Owner-only principals may
            # still reject/redact/do_not_store/log_only/merge/supersede their own
            # candidate, but must not stage-then-self-approve into durable memory.
            # (The owner check above still governs the reject/redact branch; the
            # instruction_like gate above still runs first so poisoned content is
            # rejected uniformly regardless of operator status.)
            if (
                new_status == "accepted"
                and not is_operator_principal(principal)
                and not explicitly_allowed_operator(principal)
            ):
                raise ResolveRejected(context.make_error(
                    -32004,
                    f"operator_only: accepting candidate #{cid} into a durable learning "
                    f"requires an operator/govern principal (caller '{principal.agent_id}' "
                    "owns the candidate but may only reject/redact it, not self-approve "
                    "it into durable memory)",
                    request_id,
                ))

            if new_status == "accepted":
                from minni.afm_passes.consolidation import (
                    content_hash as _content_hash,
                    find_active_learning_ids_by_hash,
                )
                from minni.writeback import CATEGORIES

                raw_cat = str(rowd.get("category") or params.get("category") or "general").strip().lower()
                cat = raw_cat if raw_cat in CATEGORIES else "general"
                _chash = _content_hash(content)
                pragma_l = c.execute("PRAGMA table_info(learnings)").fetchall()
                l_cols = {
                    r["name"] if isinstance(r, dict) or hasattr(r, "keys") else r[1]
                    for r in pragma_l
                }
                _have_hash_col = "content_hash" in l_cols
                _have_status_col = "status" in l_cols
                existing_lids: list[int] = []
                raw_sup = (
                    params.get("supersede_ids")
                    if "supersede_ids" in params
                    else params.get("supersedes")
                )
                if raw_sup is not None:
                    if isinstance(raw_sup, int) and not isinstance(raw_sup, bool):
                        raw_sup_list: list[Any] = [raw_sup]
                    elif isinstance(raw_sup, (list, tuple)):
                        raw_sup_list = list(raw_sup)
                    else:
                        raise ResolveRejected(
                            context.make_error(
                                -32602,
                                "supersede_ids must be a list of integers",
                                request_id,
                            )
                        )
                    if len(raw_sup_list) > MAX_SUPERSEDE_IDS:
                        raise ResolveRejected(
                            context.make_error(
                                -32602,
                                f"supersede_ids exceeds maximum batch size ({MAX_SUPERSEDE_IDS})",
                                request_id,
                            )
                        )
                    for sid in raw_sup_list:
                        if isinstance(sid, bool) or not isinstance(sid, int):
                            try:
                                if isinstance(sid, bool) or not isinstance(sid, (int, str)):
                                    raise ValueError("not an int")
                                sid_int = int(sid)
                            except (TypeError, ValueError):
                                raise ResolveRejected(
                                    context.make_error(
                                        -32602,
                                        "supersede_ids must be a list of integers",
                                        request_id,
                                    )
                                )
                        else:
                            sid_int = sid
                        srow = c.execute(
                            "SELECT agent_id, content FROM learnings WHERE learning_id = ?",
                            (sid_int,),
                        ).fetchone()
                        if not srow:
                            raise ResolveRejected(
                                context.make_error(
                                    -32001, f"learning_not_found: {sid_int}", request_id
                                )
                            )
                        sup_owner = str(srow["agent_id"] or "")
                        if (
                            _canonical_principal(sup_owner)
                            != _canonical_principal(author_agent)
                            and not explicitly_allowed_operator(principal)
                        ):
                            raise ResolveRejected(
                                context.make_error(
                                    -32004,
                                    f"principal_mismatch: learning #{sid_int} is owned by '{sup_owner}'; "
                                    f"caller principal '{principal.agent_id}' may only supersede its own "
                                    "learnings (cross-principal supersession requires an "
                                    "explicitly allowed operator)",
                                    request_id,
                                )
                            )
                        if sid_int not in existing_lids:
                            existing_lids.append(sid_int)
                # Collect pre-existing active hash-matching rows WITHOUT mutating
                # their status yet so `_revalidate_target` inside
                # `commit_prepared_learning` continues to see active targets;
                # they are superseded/evicted after `lid` is committed below.
                for ex_lid in find_active_learning_ids_by_hash(c, _chash, author_agent):
                    if ex_lid not in existing_lids:
                        existing_lids.append(ex_lid)
                cand_ws = str(rowd.get("workspace_id") or "default").strip() or "default"
                commit_res = None
                committed_edges: list[Any] = []
                graph_superseded_lids: list[int] = []
                plan_matches = (
                    plan is not None
                    and pre_snapshot == (content, author_agent, cand_ws, cat)
                )
                if (
                    plan_matches
                    and plan is not None
                    and plan.prepared is not None
                    and ensure_canonical_learning_node is _ORIG_ENSURE_CANONICAL_LEARNING_NODE
                ):
                    c.execute("SAVEPOINT minni_graph_commit")
                    try:
                        commit_res = commit_prepared_learning(
                            c, plan.prepared, db=db, principal=plan.graph_principal
                        )
                        c.execute("RELEASE SAVEPOINT minni_graph_commit")
                    except GraphCommitAborted as abort:
                        c.execute("ROLLBACK TO SAVEPOINT minni_graph_commit")
                        c.execute("RELEASE SAVEPOINT minni_graph_commit")
                        if abort.code in _HARD_GRAPH_ABORT_CODES:
                            err_code = (
                                -32004
                                if abort.code in ("canonical_restricted", "principal_mismatch")
                                else -32602
                            )
                            raise ResolveRejected(
                                context.make_error(
                                    err_code, f"{abort.code}: {abort}", request_id
                                )
                            )
                        context.logger.warning(
                            "resolve_candidate: graph commit aborted (%s: %s); falling back to direct canonical commit",
                            abort.code,
                            abort,
                        )
                        commit_res = None

                if commit_res is not None and plan is not None:
                    lid = commit_res.learning_id
                    canonical_doc_id = commit_res.doc_id
                    committed_edges.extend(commit_res.edges)
                    graph_superseded_lids.extend(commit_res.superseded_learning_ids)
                    if plan.fallback_edges is not None and not committed_edges:
                        fb_edges, fb_sup = apply_fallback_graph_edges(
                            c,
                            plan.fallback_edges,
                            graph_principal=plan.graph_principal,
                            workspace_id=cand_ws,
                            new_learning_id=lid,
                            new_doc_id=canonical_doc_id,
                            now=now,
                        )
                        committed_edges.extend(fb_edges)
                        graph_superseded_lids.extend(fb_sup)
                else:
                    ins_cols = ["agent_id", "category", "content", "created_at"]
                    ins_vals: list[Any] = [author_agent, cat, content, now]
                    if _have_status_col:
                        ins_cols.append("status")
                        ins_vals.append("active")
                    if _have_hash_col:
                        ins_cols.append("content_hash")
                        ins_vals.append(_chash)
                    if "workspace_id" in l_cols:
                        ins_cols.append("workspace_id")
                        ins_vals.append(cand_ws)
                    c.execute(
                        f"INSERT INTO learnings ({', '.join(ins_cols)}) "
                        f"VALUES ({', '.join('?' * len(ins_vals))})",
                        ins_vals,
                    )
                    lid = c.lastrowid
                    canonical_doc_id = ensure_canonical_learning_node(
                        c,
                        learning_id=lid,
                        agent_id=author_agent,
                        content=content,
                        vault_path=db.config.vault_path,
                        created_at=now,
                        workspace_id=cand_ws,
                    )
                    if canonical_doc_id is not None:
                        post_doc = c.execute(
                            "SELECT page_status, privacy_level FROM documents WHERE doc_id = ?",
                            (canonical_doc_id,),
                        ).fetchone()
                        if post_doc is not None and projection_row_closed(
                            post_doc["page_status"], post_doc["privacy_level"]
                        ):
                            raise ResolveRejected(
                                context.make_error(
                                    -32004,
                                    "canonical_restricted: canonical node is lifecycle-closed/restricted",
                                    request_id,
                                )
                            )
                    if plan_matches and plan is not None and plan.fallback_edges is not None:
                        fb_edges, fb_sup = apply_fallback_graph_edges(
                            c,
                            plan.fallback_edges,
                            graph_principal=plan.graph_principal,
                            workspace_id=cand_ws,
                            new_learning_id=lid,
                            new_doc_id=canonical_doc_id,
                            now=now,
                        )
                        committed_edges.extend(fb_edges)
                        graph_superseded_lids.extend(fb_sup)

                linked_lids = linked_learning_ids_for_edges(
                    c, committed_edges, exclude_learning_id=lid
                )
                superseded_docs.extend(
                    collect_superseded_purge_targets(c, graph_superseded_lids, author_agent)
                )
                near_dups = find_near_duplicate_active_learning(
                    c,
                    agent_id=author_agent,
                    category=cat,
                    content=content,
                    exclude_learning_id=lid,
                )
                for dup_lid in near_dups:
                    if (
                        int(dup_lid) not in existing_lids
                        and int(dup_lid) not in linked_lids
                        and int(dup_lid) not in graph_superseded_lids
                    ):
                        existing_lids.append(dup_lid)
                for ex_lid in existing_lids:
                    if ex_lid == lid or int(ex_lid) in graph_superseded_lids:
                        continue
                    ex_row = c.execute(
                        "SELECT agent_id, content FROM learnings WHERE learning_id = ?",
                        (ex_lid,),
                    ).fetchone()
                    ex_agent = (
                        str(ex_row["agent_id"])
                        if ex_row and "agent_id" in ex_row.keys()
                        else author_agent
                    )
                    ex_content = (
                        str(ex_row["content"])
                        if ex_row and "content" in ex_row.keys()
                        else f"learning:{ex_lid}"
                    )
                    if getattr(db.config, "writeback_enabled", True):
                        evict_learning_to_cold_archive(
                            c,
                            None,
                            ex_lid,
                            reason=f"superseded_by:{lid}",
                            db_path=getattr(db.config, "db_path", None),
                            vault_path=getattr(db.config, "vault_path", None),
                        )
                    elif _have_status_col and "superseded_by" in l_cols:
                        c.execute(
                            "UPDATE learnings SET superseded_by = ?, status = 'superseded' "
                            "WHERE learning_id = ?",
                            (lid, ex_lid),
                        )
                    elif "superseded_by" in l_cols:
                        c.execute(
                            "UPDATE learnings SET superseded_by = ? "
                            "WHERE learning_id = ?",
                            (lid, ex_lid),
                        )
                    superseded_docs.append((ex_agent, ex_content))

            # Migration-015 guard: on a pre-015 schema the candidate_packets
            # CHECK does not admit do_not_store / log_only, so the UPDATE would
            # raise IntegrityError and strand the candidate in 'proposed'.
            # Degrade to the legacy 'rejected' status (pre-015 behavior collapsed
            # both decisions to 'rejected') while recording the operator's true
            # intent in resolution_reason.
            resolution_reason = reason
            if new_status in ("do_not_store", "log_only") and not _check_allows_dns_log_only(
                c.connection
            ):
                context.logger.warning(
                    "RESOLVE_CANDIDATE candidate=%s intended_status=%s not admitted by "
                    "pre-015 candidate_packets CHECK; writing 'rejected' instead (G15 audit)",
                    cid, new_status,
                )
                resolution_reason = f"{reason} [intended_status={new_status}; schema pre-015]"
                new_status = "rejected"

            c.execute(
                """
                UPDATE candidate_packets
                SET status=?, resolved_at=?, resolved_by=?, resolution_reason=?
                WHERE candidate_id=?
                """,
                (new_status, now, principal.agent_id, resolution_reason, cid),
            )
            apply_accepted_candidate_effects(c, cid)

        for sup_agent, sup_content in superseded_docs:
            if sup_content:
                try:
                    context.purge_durable_learning(sup_agent, sup_content, db=db)
                except Exception:
                    pass

        indexed_ok = True
        cand_ws_resolved = str(rowd.get("workspace_id") or "default").strip() or "default"
        if lid is not None:
            try:
                wb_opt = context.lazy_writeback() if callable(context.lazy_writeback) else None
                if (
                    wb_opt is not None
                    and getattr(getattr(wb_opt, "config", None), "writeback_enabled", False)
                    and hasattr(wb_opt, "_write_to_disk")
                ):
                    raw_c = str(rowd.get("category") or params.get("category") or "general").strip().lower()
                    wb_opt._write_to_disk(
                        lid,
                        author_agent,
                        raw_c or "general",
                        str(rowd.get("content") or ""),
                        now,
                        workspace_id=cand_ws_resolved,
                    )
            except Exception:
                pass
            if commit_res is not None and plan is not None:
                indexed_ok = finalize_governed_promotion(
                    plan, commit_res,
                    content=str(rowd.get("content") or ""),
                    workspace_id=cand_ws_resolved,
                )
                if not indexed_ok:
                    indexed_ok = settle_accepted_candidate(
                        context, db, cid, author_agent,
                        str(rowd.get("content") or ""), lid,
                        workspace_id=cand_ws_resolved,
                    )
                else:
                    try:
                        context.maybe_archive_inbox_source(db, cid)
                    except Exception as exc:
                        context.logger.warning(
                            "candidate=%s inbox archive failed (%s; non-fatal)", cid, exc,
                        )
            else:
                indexed_ok = settle_accepted_candidate(
                    context, db, cid, author_agent,
                    str(rowd.get("content") or ""), lid,
                    workspace_id=cand_ws_resolved,
                )
        else:
            # A non-accept resolution still archives its inbox source.
            context.maybe_archive_inbox_source(db, cid)

        context.logger.warning(
            "RESOLVE_CANDIDATE operator=%s candidate=%s decision=%s new_status=%s reason=%s (G15 audit)",
            principal.agent_id, cid, decision, new_status, reason[:80],
        )

        result = {
            "status": "resolved",
            "candidate_id": cid,
            "new_status": new_status,
            "learning_id": lid,
            "principal": principal.agent_id,
        }
        if not indexed_ok:
            result["indexed"] = False
        return context.make_response(result, request_id)
    except ResolveRejected as exc:
        return exc.response
    except Exception as exc:
        context.logger.exception("resolve_candidate failed")
        envelope = context.make_error(
            -32000, f"resolve_candidate error: {exc}", request_id
        )
        redacted, _ = redact_value(envelope)
        return redacted
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass

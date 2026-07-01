import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from db import SovereignDB
from principal import EffectivePrincipal, is_operator_principal, validate_agent_id
from safety import is_instruction_like


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
    index_durable_learning: Callable[..., None]
    maybe_archive_inbox_source: Callable[[Any, int], None]
    increment_request_count: Callable[[], None] | None = None
    sovereign_db: Callable[..., Any] = SovereignDB
    logger: logging.Logger = logger


class ResolveRejected(Exception):
    """Carries a pre-built JSON-RPC error response out of a write transaction."""

    def __init__(self, response: dict):
        super().__init__(str(response.get("error", {}).get("message", "rejected")))
        self.response = response


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
    category = params.get("category", "general")
    force = bool(params.get("force", False))

    assertion = params.get("assertion")
    applies_when = params.get("applies_when")
    evidence_doc_ids = params.get("evidence_doc_ids")
    contradicts_id = params.get("contradicts_id")

    try:
        wb = context.lazy_writeback()

        if not force and contradicts_id is None:
            check_text = assertion or extract_assertion(content)
            try:
                candidates = wb.detect_contradictions(
                    content_or_assertion=check_text,
                    agent_id=None,
                )
            except Exception as exc:
                context.logger.warning(
                    "learn: contradiction detection failed (%s) - proceeding with write",
                    exc,
                )
                candidates = []

            if candidates:
                return context.make_response({
                    "status": "contradiction",
                    "candidates": candidates,
                }, request_id)

        if not force:
            return stage_candidate(params, request_id, context)

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
        from writeback import CATEGORIES

        now = time.time()
        doc_ids_json = json.dumps(evidence_doc_ids) if evidence_doc_ids else None

        emb_bytes = None
        if wb.model:
            try:
                embedding_started_at = time.perf_counter()
                emb = wb.model.encode(content).astype(np.float32)
                emb_bytes = emb.tobytes()
                context.record_latency("embedding", time.perf_counter() - embedding_started_at)
            except Exception as exc:
                context.logger.warning("learn: embedding failed (%s) - storing without embedding", exc)

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
                None,
                params.get("source_query"),
                params.get("confidence", 1.0),
                emb_bytes, now,
                assertion, applies_when, doc_ids_json,
                contradicts_id,
                "active",
            ))
            lid = c.lastrowid

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

        context.logger.info(
            "Stored learning #%d [%s/%s]: %.60s...",
            lid, agent_id, category, content,
        )

        if wb.config.writeback_enabled:
            wb._write_to_disk(lid, agent_id, category, content, now)

        context.index_durable_learning(agent_id, content, key=f"learning:{lid}", db=wb.db)

        return context.make_response({
            "status": "ok",
            "learning_id": lid,
            "agent_id": agent_id,
            "category": category,
        }, request_id)
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
    category = params.get("category", "general")
    assertion = params.get("assertion")
    applies_when = params.get("applies_when")
    evidence_doc_ids = params.get("evidence_doc_ids")

    try:
        import numpy as np
        from writeback import CATEGORIES

        wb = context.lazy_writeback()

        if category not in CATEGORIES:
            category = "general"

        now = time.time()
        doc_ids_json = json.dumps(evidence_doc_ids) if evidence_doc_ids else None

        emb_bytes = None
        if wb.model:
            try:
                emb = wb.model.encode(new_content).astype(np.float32)
                emb_bytes = emb.tobytes()
            except Exception as exc:
                context.logger.warning(
                    "resolve_contradiction: embedding failed (%s) - storing without embedding",
                    exc,
                )

        with wb.db.transaction() as c:
            for sid in supersede_ids:
                c.execute(
                    "SELECT agent_id FROM learnings WHERE learning_id = ?",
                    (sid,),
                )
                srow = c.fetchone()
                if not srow:
                    raise ResolveRejected(context.make_error(
                        -32001, f"learning_not_found: {sid}", request_id
                    ))
                owner = str(srow["agent_id"] or "")
                if owner != agent_id and not explicitly_allowed_operator(principal):
                    raise ResolveRejected(context.make_error(
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

        context.logger.info(
            "Resolved contradiction: new learning #%d supersedes %s",
            new_lid, supersede_ids,
        )

        if wb.config.writeback_enabled:
            wb._write_to_disk(new_lid, agent_id, category, new_content, now)

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
            total_events = c.execute(
                "SELECT COUNT(*) AS n FROM contradiction_events WHERE created_at >= ?",
                (event_since,),
            ).fetchone()["n"]
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
        return context.make_response({
            "agent_id": agent_id,
            "events": events,
            "status": "matched" if events else "checked_no_match",
            "checked": {
                "contradiction_events_in_window": total_events,
                "contradiction_events_for_agent_reads": agent_events,
                "learning_reads_for_agent": reads_for_agent,
                "event_window_days": event_window_days,
                "read_window_hours": read_window_hours,
                "since_ts": since_ts,
                "event_since": event_since,
            },
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
        eid = ep.log_event(
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


def stage_candidate(params: dict, request_id: Any, context: GovernanceContext) -> dict:
    """Stage a candidate packet. No durable learning write."""
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    content = (params.get("content") or params.get("text") or "").strip()
    if not content:
        return context.make_error(-32602, "content is required", request_id)
    stored_content = content[:200000]

    now = time.time()
    ws = params.get("workspace_id") or "default"
    ev = json.dumps(params.get("evidence_refs") or params.get("evidence_doc_ids") or [])
    df = json.dumps(params.get("derived_from") or {})
    instr = 1 if (params.get("instruction_like") or is_instruction_like(stored_content)) else 0

    try:
        db = context.lazy_writeback().db
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
                    stored_content,
                    ev,
                    df,
                    instr,
                    now,
                ),
            )
            cid = c.lastrowid
        context.logger.info(
            "G16 stage_candidate #%d principal=%s (proposal-first, no durable learn yet)",
            cid, principal.agent_id
        )
        return context.make_response(
            {
                "status": "proposed",
                "candidate_id": cid,
                "principal": principal.agent_id,
                "workspace_id": ws,
            },
            request_id,
        )
    except Exception as exc:
        context.logger.exception("stage_candidate failed")
        return context.make_error(-32000, f"stage_candidate error: {exc}", request_id)


def list_candidates(params: dict, request_id: Any, context: GovernanceContext) -> dict:
    """List candidates for the stamped principal."""
    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    status_f = params.get("status")
    limit = min(int(params.get("limit", 100)), 500)
    db = None
    try:
        db = context.sovereign_db()
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
            for row in c.fetchall():
                item = dict(row)
                for key in ("evidence_refs", "derived_from"):
                    if item.get(key):
                        try:
                            item[key] = json.loads(item[key])
                        except Exception:
                            pass
                rows.append(item)
        return context.make_response(
            {"candidates": rows, "principal": principal.agent_id, "count": len(rows)},
            request_id,
        )
    except Exception as exc:
        context.logger.exception("list_candidates failed")
        return context.make_error(-32000, f"list_candidates error: {exc}", request_id)
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
        "mark_sensitive",
        "mark_temporary",
        "mark_project_scoped",
    }
    if decision not in allowed:
        return context.make_error(-32602, f"decision must be one of {sorted(allowed)}", request_id)

    status_map = {
        "accept": "accepted",
        "learn": "accepted",
        "reject": "rejected",
        "redact": "redacted",
        "do_not_store": "rejected",
        "log_only": "rejected",
        "merge": "merged",
        "supersede": "superseded",
        "mark_sensitive": "accepted",
        "mark_temporary": "accepted",
        "mark_project_scoped": "accepted",
    }
    new_status = status_map.get(decision, "rejected")
    now = time.time()

    db = None
    try:
        db = context.sovereign_db()
        lid = None
        with db.transaction() as c:
            c.execute("SELECT * FROM candidate_packets WHERE candidate_id=?", (cid,))
            row = c.fetchone()
            if not row:
                raise ResolveRejected(
                    context.make_error(-32001, f"candidate_not_found: {cid}", request_id)
                )
            rowd = dict(row)
            owner = str(rowd.get("principal") or "")
            if owner != principal.agent_id and not explicitly_allowed_operator(principal):
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

            if new_status == "accepted":
                c.execute(
                    """
                    INSERT INTO learnings
                    (agent_id, category, content, created_at)
                    VALUES (?, 'general', ?, ?)
                    """,
                    (principal.agent_id, content, now),
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

        if lid is not None:
            context.index_durable_learning(
                principal.agent_id,
                str(rowd.get("content") or ""),
                key=f"learning:{lid}",
                db=db,
            )

        context.logger.warning(
            "RESOLVE_CANDIDATE operator=%s candidate=%s decision=%s new_status=%s reason=%s (G15 audit)",
            principal.agent_id, cid, decision, new_status, reason[:80],
        )

        context.maybe_archive_inbox_source(db, cid)

        return context.make_response(
            {
                "status": "resolved",
                "candidate_id": cid,
                "new_status": new_status,
                "learning_id": lid,
                "principal": principal.agent_id,
            },
            request_id,
        )
    except ResolveRejected as exc:
        return exc.response
    except Exception as exc:
        context.logger.exception("resolve_candidate failed")
        return context.make_error(-32000, f"resolve_candidate error: {exc}", request_id)
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass

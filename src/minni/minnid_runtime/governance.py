import json
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from minni.afm_review_markers import supersede_afm_review
from minni.db import SovereignDB
from minni.migrations import _candidate_status_check_allows_dns_log_only
from minni.principal import EffectivePrincipal, is_operator_principal, validate_agent_id
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
    # M4: purge the synthetic durable doc when a learning is superseded/rejected
    # so its stale chunks stop surfacing in doc search. Default no-op keeps
    # existing constructions/tests working without threading a real purger.
    purge_durable_learning: Callable[..., None] = lambda *a, **k: None
    increment_request_count: Callable[[], None] | None = None
    sovereign_db: Callable[..., Any] = SovereignDB
    logger: logging.Logger = logger


class ResolveRejected(Exception):
    """Carries a pre-built JSON-RPC error response out of a write transaction."""

    def __init__(self, response: dict):
        super().__init__(str(response.get("error", {}).get("message", "rejected")))
        self.response = response


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
                # R5: scope contradiction detection to the caller's own agent.
                # A global (agent_id=None) scan returned full cross-agent
                # content/assertion/agent_id, letting a caller probe and
                # exfiltrate other agents' durable learnings by crafting an
                # assertion. The learn write is same-agent, so same-agent scoping
                # loses no legitimate signal.
                candidates = wb.detect_contradictions(
                    content_or_assertion=check_text,
                    agent_id=agent_id,
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
        from minni.writeback import CATEGORIES

        now = time.time()
        doc_ids_json = json.dumps(evidence_doc_ids) if evidence_doc_ids else None


        emb_bytes = None
        if wb.model:
            try:
                from minni.models import get_embedder_lock

                embedding_started_at = time.perf_counter()
                with get_embedder_lock():
                    emb = wb.model.encode(content, show_progress_bar=False).astype(np.float32)
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

    category = params.get("category", "general")
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
        # and SovereignDB.cursor() commits on exit on the same thread-local
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
        with wb.db.transaction() as c:
            for sid in supersede_ids:
                c.execute(
                    "SELECT agent_id, content FROM learnings WHERE learning_id = ?",
                    (sid,),
                )
                srow = c.fetchone()
                if not srow:
                    raise ResolveRejected(context.make_error(
                        -32001, f"learning_not_found: {sid}", request_id
                    ))
                owner = str(srow["agent_id"] or "")
                superseded_docs.append((owner, str(srow["content"] or "")))
                if owner != agent_id and not explicitly_allowed_operator(principal):
                    raise ResolveRejected(context.make_error(
                        -32004,
                        f"principal_mismatch: learning #{sid} is owned by '{owner}'; "
                        f"caller principal '{agent_id}' may only supersede its own "
                        "learnings (cross-principal supersession requires an "
                        "explicitly allowed operator)",
                        request_id,
                    ))
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
) -> bool:
    """Post-COMMIT side effects. Returns whether indexing succeeded.

    Never raises: the row is already committed by the time this runs, so an
    exception here reports a failure for a learn that in fact succeeded — the
    caller then retries and duplicates it while the first copy sits durable and
    unindexed.
    """
    indexed_ok = True
    try:
        context.index_durable_learning(
            agent_id, content, key=f"learning:{learning_id}", db=db
        )
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


def auto_accept_blockers(
    content: str, instruction_like: bool, requested_privacy: str = "safe"
) -> list[str]:
    """Reasons to WITHHOLD #290 auto-acceptance. Empty list == eligible.

    The floor reuses the repo's existing structural gate rather than inventing a
    score: ``consolidation._quality_blockers`` already describes itself as "reasons
    to WITHHOLD auto-promotion (the candidate is routed to review, never silently
    dropped)", which is exactly this decision. Two safety rules from the manual
    accept path join it:

      * instruction_like — ``resolve_candidate`` refuses to accept flagged content
        without an explicit ``accept_flagged`` capability. Auto-accept must not
        become the way around that gate, so flagged content falls back to
        proposed and a human still sees it.
      * minimum length — consolidation's own auto-promotion floor; a three-word
        fragment is not a durable learning.
      * declared privacy — consolidation refuses to auto-promote anything outside
        {safe, public, low}. Note this reads the AUTHOR'S declaration, not the
        clamped value stored on the row: the clamp exists because "learn-only
        callers are not trusted to create auto-promotable rows", and this knob is
        the operator lifting THAT distrust. It is not the operator overruling a
        writer that said "this is sensitive" about its own content — under the
        old flow a human saw that declaration, and nothing else in the row
        preserves it.

    Withheld means the candidate stays ``proposed`` for a human. It is never
    dropped, so the operator loses nothing by opting in.
    """
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
    return reasons


def ensure_content_hash_column(db: Any) -> bool:
    """Ensure learnings.content_hash + its index exist. No backfill.

    Deliberately NOT consolidation._ensure_dedup_index: that one also backfills
    every NULL hash in a per-row Python loop inside BEGIN IMMEDIATE. Measured at
    ~2.9s of exclusive daemon write-lock on a 20k-row table — acceptable in a
    background pass, not on a user-facing learn. This path only needs to write
    and query its own hashes; legacy NULL rows simply do not dedup, which is
    exactly where they already were.

    The PRAGMA -> ALTER window is a TOCTOU across the daemon's thread-local
    connections, so a losing racer is caught rather than surfaced as -32000.
    """
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

    content = (params.get("content") or params.get("text") or "").strip()
    if not content:
        return context.make_error(-32602, "content is required", request_id)
    stored_content = content[:200000]

    now = time.time()
    ws = params.get("workspace_id") or "default"
    ev = json.dumps(params.get("evidence_refs") or params.get("evidence_doc_ids") or [])
    df = json.dumps(params.get("derived_from") or {})
    instr = 1 if (params.get("instruction_like") or is_instruction_like(stored_content)) else 0

    # The governed proposal writer makes privacy explicit. Preserve a caller's
    # explicit privacy_level; otherwise reuse the M-3 frontmatter bridge used by
    # durable-document indexing (which conservatively defaults to safe when no
    # frontmatter is present). The separate Finding 1 clamp below remains in
    # force: learn-only callers are not trusted to create auto-promotable rows.
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

    # Only an *explicit* govern / resolve_candidate grant may keep the writer's
    # privacy decision. Do NOT use is_operator_principal here — that treats
    # main/operator + any nonempty caps as operator, letting learn-only main
    # bypass the clamp.
    if explicitly_allowed_operator(principal):
        stored_privacy = requested_privacy
    else:
        stored_privacy = "review"

    # #290: decided BEFORE the write so the promotion lands in the SAME
    # transaction as the insert. A half-applied promotion — learning written,
    # candidate still 'proposed' — would be accepted a second time by a later
    # manual resolve and double-write the learning.
    #
    # Read from the STAMPED principal only. `params` was already refused above if
    # it carried the knob, so there is no path from the wire to this decision.
    #
    # Note the privacy clamp above is deliberately NOT an additional gate here.
    # It stamps 'review' because "learn-only callers are not trusted to create
    # auto-promotable rows" — and this knob is precisely the operator lifting
    # that distrust for one principal's own candidates. Gating on it would make
    # the knob a no-op for exactly the principals it exists to serve.
    withheld = auto_accept_blockers(stored_content, bool(instr), requested_privacy)
    auto_accept = bool(getattr(principal, "auto_accept_own", False)) and not withheld
    resolved_by = f"auto_accept_own({principal.agent_id})"

    try:
        wb = context.lazy_writeback()
        db = wb.db
        lid = None
        chash = None
        emb_bytes = None
        if auto_accept:
            # detect_contradictions filters `embedding IS NOT NULL`, so a
            # learning stored without one is invisible to the only guard standing
            # in front of this channel — and the more the knob writes, the
            # blinder that guard gets. Consolidation, the other unattended
            # writer, hashes and embeds for exactly this reason.
            #
            # ALL of it is fail-soft. Staging a candidate was pure SQL before
            # this feature and never touched the embedder; a first pass left the
            # column check and the `wb.model` property access outside the
            # try, so a locked DB or an offline model turned a working learn
            # into -32000 WITH NOTHING STAGED — the learning lost outright
            # rather than routed to review. An optimisation must never take down
            # the base path: on any failure the candidate still stages as
            # proposed, which is exactly where it would have gone anyway.
            try:
                from minni.afm_passes.consolidation import content_hash as _content_hash

                if not ensure_content_hash_column(db):
                    # The INSERT below names content_hash unconditionally, so a
                    # missing column is fatal to the durable write. Raise into
                    # the handler right below rather than proceeding: a
                    # transient SQLITE_BUSY on the DDL otherwise reached the
                    # INSERT and lost the learn outright ("no column named
                    # content_hash"), which is the exact regression this
                    # fail-soft block exists to prevent.
                    raise RuntimeError("content_hash column unavailable")
                chash = _content_hash(stored_content)
                model = getattr(wb, "model", None)
                if model is None:
                    # No embedder means no embedding, and detect_contradictions
                    # filters `embedding IS NOT NULL` — an active learning
                    # without one is invisible to the guard in front of this
                    # channel, which is the F2 defect in a different costume.
                    # Every other preparation failure stages as proposed; a
                    # missing model must not be the one exception that writes
                    # durable memory anyway.
                    raise RuntimeError("embedder unavailable")
                import numpy as np

                from minni.models import get_embedder_lock

                with get_embedder_lock():
                    emb = model.encode(
                        stored_content, show_progress_bar=False
                    ).astype(np.float32)
                emb_bytes = emb.tobytes()
            except Exception as exc:
                context.logger.warning(
                    "auto_accept_own: could not prepare the durable write (%s); "
                    "staging as proposed instead (G15 audit)", exc,
                )
                auto_accept = False
                chash = None
                emb_bytes = None
                withheld = list(withheld) + ["durable-write preparation failed"]

        with db.transaction() as c:
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
            cid = c.lastrowid
            if auto_accept and chash is not None:
                # Inside the txn: a read-then-insert outside it races another
                # auto-accept of the same content.
                # Scoped to the caller, matching where the row is written. A
                # global probe is an exact-match existence oracle over every
                # agent's durable content, and the echoed "duplicate" reason
                # confirms the hit — the same class this repo already closed on
                # handle_learn's contradiction scan (R5, 2026-07-02 triage).
                # status='active' matters: a superseded or rejected row is not
                # a live duplicate, and matching it blocked re-learning content
                # that no active learning carries any more — the correction
                # machinery would have permanently poisoned the hash.
                c.execute(
                    "SELECT 1 FROM learnings WHERE content_hash=? AND agent_id=? "
                    "AND status='active' LIMIT 1",
                    (chash, principal.agent_id),
                )
                if c.fetchone() is not None:
                    auto_accept = False
                    withheld = ["duplicate of an existing learning"]
            if auto_accept:
                # 'general' matches both resolve_candidate and afm.py's
                # promote, so the category cannot diverge by acceptance route.
                #
                # The OWNER does diverge, and it is worth being exact: a manual
                # accept records the RESOLVER as agent_id, so a human accepting
                # agent X's candidate produces a learning owned by the operator.
                # This path records X. That is the intended reading of "the
                # learnings are the agent's own" — and it is also why X can now
                # own a learning at all, which is what made resolve_contradiction
                # reachable and gave it the floor above.
                c.execute(
                    """
                    INSERT INTO learnings
                    (agent_id, category, content, created_at, embedding,
                     content_hash, status)
                    VALUES (?, 'general', ?, ?, ?, ?, 'active')
                    """,
                    (
                        principal.agent_id, stored_content, now,
                        emb_bytes, chash,
                    ),
                )
                lid = c.lastrowid
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

        indexed_ok = True
        if lid is not None:
            indexed_ok = settle_accepted_candidate(
                context, db, cid, principal.agent_id, stored_content, lid,
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

        result = {
            "status": "accepted" if lid is not None else "proposed",
            "candidate_id": cid,
            "principal": principal.agent_id,
            "workspace_id": ws,
            "privacy_level": stored_privacy,
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
    ``total`` / ``has_more`` make a truncated page visible. ``has_more``
    comes from a single ``LIMIT n+1`` read — sqlite3 does not start a
    transaction on SELECT, so a COUNT(*) then LIMIT pair can hide a live
    proposed row when WAL commits between the two.
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
        with db.cursor() as c:
            # One statement for the page + has_more probe. A prior COUNT(*)
            # is a different WAL snapshot; extras that land between the two
            # would fill LIMIT and still report has_more false.
            c.execute(
                "SELECT * FROM candidate_packets WHERE principal=? AND status=? "
                "ORDER BY proposed_at DESC LIMIT ?",
                (principal.agent_id, status_f, limit + 1),
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
            if has_more:
                c.execute(
                    "SELECT COUNT(*) AS n FROM candidate_packets WHERE principal=? AND status=?",
                    (principal.agent_id, status_f),
                )
                total = max(int(c.fetchone()["n"]), len(rows) + 1)
            else:
                total = len(rows)
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
                c.execute(
                    """
                    INSERT INTO learnings
                    (agent_id, category, content, created_at)
                    VALUES (?, 'general', ?, ?)
                    """,
                    (principal.agent_id, content, now),
                )
                lid = c.lastrowid

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

        if lid is not None:
            settle_accepted_candidate(
                context, db, cid, principal.agent_id,
                str(rowd.get("content") or ""), lid,
            )
        else:
            # A non-accept resolution still archives its inbox source.
            context.maybe_archive_inbox_source(db, cid)

        context.logger.warning(
            "RESOLVE_CANDIDATE operator=%s candidate=%s decision=%s new_status=%s reason=%s (G15 audit)",
            principal.agent_id, cid, decision, new_status, reason[:80],
        )

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

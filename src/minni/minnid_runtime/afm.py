import asyncio
import inspect
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import minni.obs as obs
from minni.config import DEFAULT_CONFIG
from minni.db import SovereignDB
from minni.principal import (
    OPERATOR_RESERVED_AGENT_IDS,
    EffectivePrincipal,
    is_operator_principal,
    validate_agent_id,
)
from minni.safety import is_instruction_like


logger = logging.getLogger("minnid")


@dataclass(frozen=True)
class AFMContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    guard_vault_root: Callable[..., Optional[dict]]
    lazy_writeback: Callable[[], Any]
    trace_ring: Callable[[], Any]
    record_latency: Callable[[str, float], None]
    maybe_archive_inbox_source: Callable[[Any, int], None]
    increment_request_count: Callable[[], None] | None = None
    writeback_ref: Callable[[], Any] = lambda: None
    sovereign_db: Callable[..., Any] = SovereignDB
    default_config: Any = field(default_factory=lambda: DEFAULT_CONFIG)
    is_running: Callable[[], bool] = lambda: False
    logger: logging.Logger = logger


def loop_principal() -> EffectivePrincipal:
    """Synthesized operator stamp for the background AFM loop's wet runs.

    Issue #119 (defect 1): handle_daemon_compile gates dry_run=false on an
    operator params["_principal"], but afm_loop_runner built params without
    one — so every background wet run was rejected and the loop was a silent
    no-op. The loop is an in-process trusted caller (it never crosses the
    wire; wire callers get their principal stamped by dispatch and cannot
    inject "_principal"), so stamp a daemon-internal operator here rather
    than weakening the wire-side gate. agent_id "afm-loop" keeps the audit
    trail (trace-ring owner, logs) attributed to the loop, not an operator.
    """
    return EffectivePrincipal(
        agent_id="afm-loop",
        workspace_id="default",
        transport="internal",
        capabilities=["govern"],
        allowed_vault_roots=[],
    )


# AFM-8 (#230): how soon a pass that FAILED is retried. Short enough that a
# transient fault is not punished with the full interval, long enough that a
# pass failing instantly cannot spin the loop.
_FAILURE_RETRY_SECONDS = 300

# handle_daemon_compile does NOT re-raise: it catches every exception and
# RETURNS one of these statuses. Review round 1 on PR #260 caught that a
# backoff placed only in the loop's `except` was therefore dead code for the
# exact failure mode AFM-8 is about — the loop's try never sees the exception,
# so a pass failing instantly still burned the full 24h interval while status
# read "failing". The tick decision has to read the RESPONSE, not rely on an
# exception that never arrives.
#
# Review round 2 on PR #260: "write_timeout" belongs here too. A hung writer
# does not raise — submit_drafts waits out its 30s and RETURNS that status, so
# handle_daemon_compile merges it into a non-exception response. Leaving it
# out of this set scheduled the next attempt a full interval later: the same
# AFM-8 hole, one status name over.
# Round 5: "write_in_flight" is the writer refusing a re-submit while the
# previous job for the same pass is still queued (see afm_writer.submit_drafts)
# — a failed tick for scheduling, and proof the earlier batch has not landed.
# Round 17: "lifecycle_recovered" re-applies a PRIOR deferred lifecycle and
# deliberately does NOT enqueue THIS batch's drafts. Treating it as success
# burned the full interval after a tick that threw away wet work (especially
# under max_batches_per_tick == 1). Soft failure → short backoff; multi-batch
# drain continues so a subsequent batch can still write review pages.
_COMPILE_FAILURE_STATUSES = frozenset(
    {
        "afm_unavailable",
        "write_failed",
        "write_timeout",
        "write_in_flight",
        "lifecycle_recovered",
    }
)

# Review round 5 on PR #260: write_timeout / write_in_flight are NOT the same
# failure mode as afm_unavailable / write_failed. The latter mean nothing
# durable is in flight, so the 300s retry is pure recovery; the former mean
# the previous batch is still queued and may yet land — retrying those every
# 300s re-ran the whole pass (LLM compute) 288x more often than the old
# schedule while the writer was stuck. The writer's in-flight guard is what
# prevents duplicate drafts; this longer backoff (well past the 30s wait plus
# drain margin) keeps the loop from burning compute against a blocked queue.
_WRITE_STALL_STATUSES = frozenset({"write_timeout", "write_in_flight"})
_WRITE_STALL_RETRY_SECONDS = 1800


def compile_failure_status(res: Any) -> Optional[str]:
    """The failure status carried by a handle_daemon_compile response, if any.

    Returns None for a successful (or unrecognizable) response, so a caller can
    treat "not a known failure" as success without guessing.
    """
    if not isinstance(res, dict):
        return None
    # Review round 5 on PR #260: handle_daemon_compile's make_error paths
    # (unsupported pass_name, vault-guard denial) return a top-level JSON-RPC
    # `error` with no `result` at all. Reading only result.status made those
    # look like SUCCESSFUL ticks — a misconfigured pass burned the full
    # interval silently and recorded neither attempt nor failure.
    if "error" in res:
        return "rpc_error"
    payload = res.get("result")
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    return status if status in _COMPILE_FAILURE_STATUSES else None


def record_rpc_error(pass_name: str, res: Any) -> None:
    """GA4-3 for the make_error paths (round 5, PR #260).

    A top-level JSON-RPC error returns BEFORE handle_daemon_compile's try
    body, so neither record_pass_attempt nor record_pass_failure ran — a pass
    failing its argument/guard checks on every tick left no trace on any
    health surface. The loop records both here for its wet runs.
    """
    from minni.afm_writer import record_pass_attempt, record_pass_failure

    err = res.get("error") if isinstance(res, dict) else None
    message = str((err or {}).get("message") or "JSON-RPC error")
    record_pass_attempt(pass_name)
    record_pass_failure(pass_name, message)


def next_last_run(
    interval: float,
    now: float,
    failed: bool,
    retry_seconds: Optional[float] = None,
) -> float:
    """The ``last_run`` stamp a just-finished tick should record.

    A failed tick backs off to ``retry_seconds`` (default
    :data:`_FAILURE_RETRY_SECONDS`) instead of consuming the whole interval; a
    successful one records now. Pulled out as a pure function so the
    scheduling decision is testable without driving the async loop — the
    behavior, not the source text, is what needs pinning.
    """
    if not failed:
        return now
    retry = _FAILURE_RETRY_SECONDS if retry_seconds is None else retry_seconds
    return now - max(0, interval - retry)


def afm_loop_enabled(config=DEFAULT_CONFIG) -> bool:
    if os.environ.get("MINNI_AFM_LOOP", "").lower() == "off":
        return False
    return bool((getattr(config, "afm_loop_schedule", {}) or {}).get("enabled"))


def maybe_archive_inbox_source(db, candidate_id: int, config=DEFAULT_CONFIG, log: logging.Logger = logger) -> None:
    """Move terminal candidate source files into inbox/.archive best-effort."""
    try:
        from minni.afm_passes.inbox_archive import maybe_archive_for_candidate

        maybe_archive_for_candidate(db, config, candidate_id)
    except Exception:
        log.exception(
            "inbox archive for candidate %s failed (non-fatal)", candidate_id
        )


def promote_candidate_durable(candidate_id: int, reason: str, context: AFMContext):
    """Promote one proposed candidate into a durable, embedded learning.

    Returns the new learning_id, or None if the candidate is missing or no longer
    'proposed' (idempotent — safe to call twice). Embedding is best-effort.
    """
    import numpy as np

    wb = context.lazy_writeback()
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
    if int(cand["instruction_like"] or 0) == 1 or is_instruction_like(content):
        now = time.time()
        with wb.db.transaction() as c:
            c.execute("SELECT status FROM candidate_packets WHERE candidate_id=?", (candidate_id,))
            chk = c.fetchone()
            if not chk or chk["status"] != "proposed":
                return None
            c.execute(
                "UPDATE candidate_packets SET instruction_like=1 WHERE candidate_id=?",
                (candidate_id,),
            )
            c.execute(
                """SELECT 1 FROM consolidation_actions
                   WHERE action_type='afm_review' AND claim=?
                     AND COALESCE(status, '') != 'superseded' LIMIT 1""",
                (str(candidate_id),),
            )
            if not c.fetchone():
                c.execute(
                    """
                    INSERT INTO consolidation_actions
                    (action_type, claim, category, status, detail, created_at)
                    VALUES ('afm_review', ?, 'general', 'pending', ?, ?)
                    """,
                    (str(candidate_id), f"{reason[:450]}: instruction_like", now),
                )
        context.logger.warning(
            "AFM consolidation routed instruction_like candidate #%d to review",
            candidate_id,
        )
        return None
    agent_id = cand.get("principal") or "afm-loop"
    try:
        validate_agent_id(agent_id)
    except ValueError as exc:
        context.logger.warning(
            "_promote_candidate_durable: invalid agent_id %r (%s) - skipping",
            agent_id,
            exc,
        )
        return None
    from minni.afm_passes.consolidation import content_hash as _content_hash

    chash = _content_hash(content)

    emb_bytes = None
    if getattr(wb, "model", None):
        try:
            emb = wb.model.encode(content).astype(np.float32)
            emb_bytes = emb.tobytes()
        except Exception as exc:
            context.logger.warning("consolidation: embedding failed (%s) - storing without", exc)

    now = time.time()
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

    try:
        if wb.config.writeback_enabled:
            wb._write_to_disk(lid, agent_id, "general", content, now)
    except Exception as exc:
        context.logger.warning("consolidation: write_to_disk failed for learning #%s (%s)", lid, exc)

    context.logger.info("AFM consolidation promoted candidate #%d -> learning #%d", candidate_id, lid)
    context.maybe_archive_inbox_source(wb.db, candidate_id)
    return lid


def reject_candidate_dedup(candidate_id: int, context: AFMContext) -> bool:
    """Mark a proposed candidate rejected as a duplicate."""
    wb = context.lazy_writeback()
    now = time.time()
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
    context.logger.info("AFM consolidation deduped candidate #%d (rejected)", candidate_id)
    context.maybe_archive_inbox_source(wb.db, candidate_id)
    return True


def mark_candidate_review(candidate_id: int, reason: str, context: AFMContext) -> bool:
    """Flag a proposed candidate for operator review without changing status."""
    wb = context.lazy_writeback()
    now = time.time()
    with wb.db.transaction() as c:
        c.execute("SELECT status FROM candidate_packets WHERE candidate_id=?", (candidate_id,))
        row = c.fetchone()
        if not row or row["status"] != "proposed":
            return False
        c.execute(
            """SELECT 1 FROM consolidation_actions
               WHERE action_type='afm_review' AND claim=?
                 AND COALESCE(status, '') != 'superseded' LIMIT 1""",
            (str(candidate_id),),
        )
        if c.fetchone():
            return False
        c.execute(
            """
            INSERT INTO consolidation_actions
            (action_type, claim, category, status, detail, created_at)
            VALUES ('afm_review', ?, 'general', 'pending', ?, ?)
            """,
            (str(candidate_id), reason[:500], now),
        )
    return True


def apply_consolidation_result(result: dict, context: AFMContext) -> None:
    """Apply durable promotes, dedup rejections, and review routing."""
    promoted = deduped = reviewed = 0
    for cid in (result.get("promote_candidate_ids") or []):
        try:
            if promote_candidate_durable(int(cid), "afm-consolidation", context):
                promoted += 1
        except Exception:
            context.logger.exception("consolidation: promote candidate %s failed", cid)
    for cid in (result.get("dedup_candidate_ids") or []):
        try:
            if reject_candidate_dedup(int(cid), context):
                deduped += 1
        except Exception:
            context.logger.exception("consolidation: dedup candidate %s failed", cid)
    for cid in (result.get("review_candidate_ids") or []):
        try:
            if mark_candidate_review(int(cid), "afm-consolidation review", context):
                reviewed += 1
        except Exception:
            context.logger.exception("consolidation: review-mark candidate %s failed", cid)
    if promoted or deduped or reviewed:
        context.logger.info(
            "AFM consolidation applied: promoted=%d deduped=%d reviewed=%d",
            promoted, deduped, reviewed,
        )
    result["applied"] = {"promoted": promoted, "deduped": deduped, "reviewed": reviewed}


def handle_daemon_compile(params: dict, request_id: Any, context: AFMContext) -> dict:
    """Run an AFM compile pass. Defaults to dry-run and never auto-accepts."""
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()

    pass_name = str(params.get("pass_name") or "").strip()
    if not pass_name:
        return context.make_error(-32602, "pass_name is required", request_id)
    dry_run = bool(params.get("dry_run", True))
    vault_path = str(params.get("vault_path") or context.default_config.vault_path)

    # P1: dry_run=false runs submit_drafts/apply_consolidation_result, promoting
    # candidates to durable learnings — a write op. The method-capability table
    # only requires 'read' (so read principals may still dry-run/preview). Gate
    # the durable path on operator/govern status here. dry_run=true is untouched.
    if not dry_run:
        stamped = params.get("_principal")
        if not isinstance(stamped, EffectivePrincipal) or not is_operator_principal(stamped):
            principal_id = (
                stamped.agent_id if isinstance(stamped, EffectivePrincipal) else None
            )
            return context.make_error(
                -32004,
                "operator_only: daemon.compile with dry_run=false promotes candidates "
                "to durable learnings and requires an operator/govern principal "
                f"(caller principal '{principal_id}' is not authorized); "
                "run with dry_run=true to preview without writing",
                request_id,
            )

    err = context.guard_vault_root(params, vault_path, request_id, label="compile")
    if err:
        return err

    if not afm_loop_enabled(context.default_config):
        return context.make_response({
            "status": "afm_unavailable",
            "reason": "AFM loop disabled",
            "pass_name": pass_name,
            "dry_run": dry_run,
            "drafts": [],
            "drafts_written": [],
        }, request_id)

    db = None
    try:
        pass_runners = {
            "session_distillation": "minni.afm_passes.session_distillation",
            "synthesis": "minni.afm_passes.synthesis",
            "procedure_extraction": "minni.afm_passes.procedure_extraction",
            "reorganization": "minni.afm_passes.reorganization",
            "pruning": "minni.afm_passes.pruning",
            "consolidation": "minni.afm_passes.consolidation",
            "vault_ingest": "minni.afm_passes.vault_ingest",
        }
        if pass_name not in pass_runners:
            return context.make_error(-32602, f"unsupported pass_name: {pass_name}", request_id)
        trace_id = f"afm-{uuid.uuid4().hex[:12]}"
        db = context.sovereign_db(context.default_config)
        pass_module = __import__(pass_runners[pass_name], fromlist=["run"])

        # Finding 9: bind AFM passes to the stamped caller principal when present
        # (not only MINNI_AGENT_ID / config.agent_id), so dry-run compile cannot
        # pull another agent's episodic events into model-visible inputs.
        # Regression guard: the background loop stamps agent_id="afm-loop" for
        # the wet-run operator gate, and unstamped local callers synthesize an
        # operator-reserved id ("main"/"operator"). None of these identities is
        # a data owner — binding a pass to them scopes it to zero events and the
        # compile silently distills nothing. Fall through to env/config/unknown
        # inside the pass for every non-owner identity; only real agent
        # principals (codex, gemini, ...) bind the pass to themselves.
        _NON_OWNER_BIND_IDS = frozenset(OPERATOR_RESERVED_AGENT_IDS) | {"afm-loop"}
        stamped_for_bind = params.get("_principal")
        compile_agent_id = None
        if isinstance(stamped_for_bind, EffectivePrincipal):
            if stamped_for_bind.agent_id not in _NON_OWNER_BIND_IDS:
                compile_agent_id = stamped_for_bind.agent_id

        run_kwargs = {
            "vault_path": vault_path,
            "dry_run": dry_run,
            "trace_id": trace_id,
        }
        # Passes that accept agent_id get it. inspect can fail on exotic wrappers —
        # log and leave unbound (pass falls back to env/config/unknown) rather
        # than crashing the compile RPC.
        try:
            run_params = inspect.signature(pass_module.run).parameters
        except (TypeError, ValueError):
            context.logger.warning(
                "daemon.compile: could not inspect %s.run for agent_id; "
                "leaving unbound (pass falls back to env/config/unknown)",
                pass_runners[pass_name],
            )
        else:
            if "agent_id" in run_params:
                run_kwargs["agent_id"] = compile_agent_id

        result = pass_module.run(
            db,
            context.default_config,
            **run_kwargs,
        )
        if not dry_run:
            # Liveness, recorded whether or not the pass produced drafts. Without
            # it the only evidence a pass ever ran is _LAST_RUN_PER_PASS, which
            # a healthy no-op pass never sets — so health could not tell "ran,
            # found nothing" from "has been failing for days".
            from minni.afm_writer import record_pass_attempt

            record_pass_attempt(pass_name)
        write_refused = False
        write_status = None
        if not dry_run and result.get("drafts"):
            from minni.afm_writer import submit_drafts

            # Round 10/11: per-job lifecycle + handler so a timed-out waiter
            # still applies once if the write lands (no process-global slot).
            lifecycle = {
                "promote_candidate_ids": list(
                    result.get("promote_candidate_ids") or []
                ),
                "dedup_candidate_ids": list(result.get("dedup_candidate_ids") or []),
                "review_candidate_ids": list(
                    result.get("review_candidate_ids") or []
                ),
            }
            write_result = submit_drafts({
                "pass_name": pass_name,
                "trace_id": trace_id,
                "vault_path": vault_path,
                "drafts": result["drafts"],
                "writeback": context.writeback_ref(),
                "lifecycle": lifecycle,
                # Close over this request's context; job-scoped so concurrent
                # compiles cannot steal each other's applier (round 11).
                "lifecycle_handler": (
                    lambda life, _ctx=context: apply_consolidation_result(life, _ctx)
                ),
            })
            result.update(write_result)
            # Review round 8 on PR #260: write_in_flight means THIS batch's
            # drafts were REFUSED — never enqueued, never going to land. The
            # raising failures (DraftWriteError/DraftQueueFull) already skip
            # the lifecycle apply below because they jump to the except path;
            # the non-raising refusal must skip it too, or candidates get
            # promoted/dedup'd/marked-reviewed while the review drafts that
            # explain those mutations do not exist anywhere.
            #
            # Round 9/10: write_timeout skips apply HERE (drafts unobserved).
            # Ownership transfers to the worker via defer_lifecycle_to_worker
            # so a late success applies once without a full re-run minting
            # duplicate drafts.
            #
            # Round 16: lifecycle_recovered means the writer re-applied a
            # *prior* deferred decision set and deliberately did NOT enqueue
            # THIS batch's drafts (AFM-8 anti-duplicate). Applying the NEW
            # result's promote/dedup/review ids here would terminalize a
            # decision set whose review drafts were never written.
            write_status = write_result.get("status")
            write_refused = write_status in {
                "write_in_flight",
                "write_timeout",
                "lifecycle_recovered",
            }
        else:
            result.setdefault("drafts_written", [])

        if not dry_run and (
            result.get("promote_candidate_ids")
            or result.get("dedup_candidate_ids")
            or result.get("review_candidate_ids")
        ):
            if write_refused:
                if write_status == "write_timeout":
                    context.logger.warning(
                        "daemon.compile: consolidation lifecycle DEFERRED to "
                        "writer — write_timeout; worker applies once if drafts "
                        "land (no duplicate re-run for this batch)"
                    )
                else:
                    context.logger.warning(
                        "daemon.compile: consolidation lifecycle apply SKIPPED — "
                        "this batch's drafts were refused (%s); candidates stay "
                        "proposed and re-run after the writer drains",
                        write_status or "write_refused",
                    )
            else:
                apply_consolidation_result(result, context)

        try:
            # R8: bind the compile trace to the calling principal (present in
            # params for both the operator-gated wet path and the read-only
            # dry-run path above) so it is not readable by an arbitrary caller.
            _stamped = params.get("_principal")
            _owner = _stamped.agent_id if isinstance(_stamped, EffectivePrincipal) else None
            context.trace_ring().put(trace_id, {
                "kind": "afm_loop",
                "pass_name": pass_name,
                "inputs": result.get("inputs"),
                "prompt": result.get("prompt"),
                "prompt_version": result.get("prompt_version"),
                "output": result.get("output"),
                "draft_page_ids": [draft.get("page_id") for draft in result.get("drafts", [])],
                "dry_run": dry_run,
            }, owner=_owner)
        except Exception as exc:
            context.logger.warning("AFM trace capture degraded: %s", exc)
        return context.make_response(result, request_id)
    except Exception as exc:
        context.logger.exception("daemon.compile failed")
        # GA4-3: a pass that raises used to skip record_pass_attempt entirely,
        # so a pass failing on every single tick read as "never invoked" or
        # went stale — indistinguishable from an idle one, and no failure
        # counter existed anywhere. Record the attempt (it DID run) and the
        # failure (it DID fault), so health can say which.
        if not dry_run:
            try:
                from minni.afm_writer import record_pass_attempt, record_pass_failure

                record_pass_attempt(pass_name)
                record_pass_failure(pass_name, str(exc))
            except Exception:  # noqa: BLE001 - bookkeeping must not mask the fault
                context.logger.exception(
                    "daemon.compile: failed to record the pass failure"
                )
        obs.incr("afm_pass_failures_total")
        obs.incr(f"afm_pass_failures.{pass_name}")
        obs.record_error(f"daemon.compile.{pass_name}", exc)
        # AFM-8: a draft-WRITE failure was reported as "afm_unavailable", which
        # blames the model provider for a durable-storage fault. They need
        # different remediation, so they get different statuses.
        from minni.afm_writer import DraftQueueFull, DraftWriteError

        if isinstance(exc, (DraftWriteError, DraftQueueFull)):
            status = "write_failed"
        else:
            status = "afm_unavailable"
        return context.make_response({
            "status": status,
            "reason": str(exc),
            "pass_name": pass_name,
            "dry_run": dry_run,
            "drafts": [],
            "drafts_written": [],
        }, request_id)
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass
        context.record_latency("afm", time.perf_counter() - started_at)


async def afm_loop_runner(context: AFMContext):
    """Background scheduler for AFM passes (opt-in via MINNI_AFM_LOOP).

    Ticks every idle_seconds; fires each pass when its interval has elapsed.
    Defensive: a pass that raises is logged and skipped — it can NEVER take down
    the socket server task. Last-run times are in-memory (reset on restart, which
    just means each pass runs once shortly after boot — acceptable).
    """
    if not afm_loop_enabled(context.default_config):
        context.logger.info("AFM loop disabled; runner not started.")
        return
    schedule = getattr(context.default_config, "afm_loop_schedule", {}) or {}
    idle_seconds = int(schedule.get("idle_seconds", 300))
    passes_cfg = schedule.get("passes") or {}
    last_run = {name: 0.0 for name in passes_cfg}
    context.logger.info(
        "AFM loop runner started: passes=%s idle=%ds",
        list(passes_cfg.keys()), idle_seconds,
    )
    await asyncio.sleep(min(30, idle_seconds))  # let startup settle
    while context.is_running():
        now = time.time()
        for name, cfg in passes_cfg.items():
            interval = int((cfg or {}).get("interval_seconds", 24 * 60 * 60))
            if now - last_run.get(name, 0.0) < interval:
                continue
            # AFM-8: set by ANY compile in this tick that came back with a
            # failure status. handle_daemon_compile swallows the exception and
            # returns, so this is the only place the failure is observable.
            tick_failed = False
            # Round 5: WHICH failure decides the backoff class — a write stall
            # (previous batch still in flight) backs off much longer than a
            # recoverable fault (see _WRITE_STALL_STATUSES).
            tick_failure: Optional[str] = None
            try:
                context.logger.info("AFM loop: running pass '%s'", name)
                params = {
                    "pass_name": name,
                    "dry_run": False,
                    "vault_path": context.default_config.vault_path,
                    # Daemon-internal operator stamp: the wet-run gate in
                    # handle_daemon_compile otherwise rejects the loop's own
                    # dry_run=false calls (issue #119).
                    "_principal": loop_principal(),
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
                        # Defined ahead of the try so a failure inside it (e.g.
                        # the ingest call itself raising) leaves this an empty
                        # dict rather than undefined — the quarantine check
                        # right below reads it unconditionally.
                        _skips: dict = {}
                        try:
                            from minni.afm_passes.inbox_ingest import ingest as _ingest_inbox
                            # Reuse the daemon's shared handle: SovereignDB
                            # connections are thread-local and only released
                            # by close(), so a fresh instance per tick leaks
                            # one connection per tick in this loop.
                            _ing_db = context.lazy_writeback().db
                            _ing = _ingest_inbox(
                                _ing_db,
                                context.default_config,
                                fallback_principal=str((cfg or {}).get(
                                    "inbox_fallback_principal", "unknown")),
                                dry_run=False,
                            )
                            if _ing.get("inserted"):
                                context.logger.info(
                                    "AFM loop: inbox ingest -> %d new candidate(s) "
                                    "(eligible=%d already=%d)",
                                    _ing["inserted"],
                                    _ing["eligible"],
                                    _ing["already_present"],
                                )
                            # B4: surface kind-gate drops to the audit channel
                            # instead of silently skipping whole channels.
                            _skips = _ing.get("skipped_by_kind") or {}
                            if any(_skips.values()):
                                context.logger.info(
                                    "AFM loop: inbox ingest skipped_by_kind=%s",
                                    _skips,
                                )
                        except Exception as exc:
                            context.logger.exception(
                                "AFM loop: inbox ingest raised (skipped; "
                                "consolidation continues)"
                            )
                            # GA6-2: these four sub-ops counted only their
                            # successes, so a sub-op failing on every tick was
                            # indistinguishable from one that had no work — the
                            # counter simply stopped climbing either way. Each
                            # failure now has its own counter and reaches the
                            # error ring, which record_error was previously
                            # only ever fed from dispatch.
                            obs.incr("inbox_ingest_failures_total")
                            obs.record_error("afm_loop.inbox_ingest", exc)
                        # W3 (audit §4 / consolidation inbox residue): an
                        # _agent_mismatch skip is PERMANENT — the same file
                        # produces the identical skip on every future tick, so
                        # left alone it just piles up invisibly forever (the
                        # 59-file unknown-vault/inbox cohort). Quarantine it
                        # once stale instead. Scoped to _agent_mismatch ONLY;
                        # _malformed_kind/_unrecognized are different bugs and
                        # are not drained here. Failure here must NOT block
                        # consolidation of the existing queue.
                        if (cfg or {}).get("ingest_inbox", True) and _skips.get("_agent_mismatch"):
                            try:
                                from minni.afm_passes.inbox_quarantine import (
                                    quarantine_stale_agent_mismatch,
                                )
                                _q = quarantine_stale_agent_mismatch(
                                    context.default_config,
                                    fallback_principal=str((cfg or {}).get(
                                        "inbox_fallback_principal", "unknown")),
                                    ttl_days=(cfg or {}).get("inbox_quarantine_ttl_days"),
                                )
                                if _q.get("quarantined"):
                                    # Fail-LOUD: a climbing global counter
                                    # (surfaced on status.daemon.counters,
                                    # obs.py's existing metrics-snapshot path)
                                    # plus a WARNING log line, so stale residue
                                    # is visible on every status call instead
                                    # of silently accumulating.
                                    obs.incr("inbox_quarantined_total", _q["quarantined"])
                                    context.logger.warning(
                                        "AFM loop: quarantined %d stale "
                                        "_agent_mismatch inbox file(s): %s",
                                        _q["quarantined"], _q["quarantined_files"],
                                    )
                            except Exception as exc:
                                context.logger.exception(
                                    "AFM loop: inbox quarantine raised (skipped; "
                                    "consolidation continues)"
                                )
                                obs.incr("inbox_quarantine_failures_total")
                                obs.record_error("afm_loop.inbox_quarantine", exc)
                        # Inert-file sweep (2026-08 pile-up): a stop-candidate
                        # file whose EVERY candidate ingest rejects (audit
                        # echo / log_only / do_not_store / blank) can never
                        # earn a DB row, so archive-on-resolution can never
                        # reclaim it — the same permanent-residue shape as
                        # _agent_mismatch, drained to `.archive/` instead of
                        # quarantine because ingest's rejection IS its terminal
                        # resolution (nothing for an operator to remediate).
                        # Runs every tick: the scan costs what ingest's own
                        # scan already costs. Failure must NOT block
                        # consolidation of the existing queue.
                        if (cfg or {}).get("archive_inert_inbox", True):
                            try:
                                from minni.afm_passes.inbox_archive import (
                                    archive_inert_files,
                                )
                                _ia = archive_inert_files(
                                    context.default_config,
                                    fallback_principal=str((cfg or {}).get(
                                        "inbox_fallback_principal", "unknown")),
                                )
                                if _ia.get("archived"):
                                    obs.incr(
                                        "inbox_inert_archived_total",
                                        _ia["archived"],
                                    )
                                    context.logger.info(
                                        "AFM loop: archived %d inert inbox "
                                        "file(s) (%s)",
                                        _ia["archived"], _ia.get("reasons"),
                                    )
                            except Exception as exc:
                                context.logger.exception(
                                    "AFM loop: inert inbox archive raised "
                                    "(skipped; consolidation continues)"
                                )
                                obs.incr("inbox_archive_failures_total")
                                obs.record_error("afm_loop.inbox_archive", exc)
                    # Distill harvested raw compaction summaries (inbox kind
                    # 'compact_summary', written by the platform hooks'
                    # compact harvest) into proposed candidates on the same
                    # tick, so they are triaged alongside the stop-candidate
                    # channel. AFM session_distill per section when available,
                    # deterministic section flatten otherwise. Failure here
                    # must NOT block consolidation of the existing queue.
                    if (cfg or {}).get("distill_compact_summaries", True):
                        try:
                            from minni.afm_passes.compact_distillation import (
                                distill as _distill_compact,
                            )
                            _dc = _distill_compact(
                                context.lazy_writeback().db,
                                context.default_config,
                                fallback_principal=str((cfg or {}).get(
                                    "inbox_fallback_principal", "unknown")),
                                dry_run=False,
                            )
                            if _dc.get("inserted") or _dc.get("vault_notes_written"):
                                context.logger.info(
                                    "AFM loop: compact distillation -> %d new "
                                    "shared candidate(s) (files=%d already_done=%d "
                                    "afm_mode=%s afm_sections=%d personal=%d "
                                    "notes=%d archived=%d)",
                                    _dc["inserted"],
                                    _dc["files_scanned"],
                                    _dc["files_already_done"],
                                    _dc["afm_mode"],
                                    _dc["afm_sections"],
                                    _dc["personal_sections"],
                                    _dc["vault_notes_written"],
                                    _dc["archived_zero_shared"] + _dc["archived_with_shared"],
                                )
                        except Exception as exc:
                            context.logger.exception(
                                "AFM loop: compact distillation raised "
                                "(skipped; consolidation continues)"
                            )
                            obs.incr("compact_distillation_failures_total")
                            obs.record_error("afm_loop.compact_distillation", exc)
                    max_batches = int((cfg or {}).get("max_batches_per_tick", 40))
                    batches = total_examined = 0
                    last_summ = None
                    while batches < max_batches:
                        res = handle_daemon_compile(params, request_id=None, context=context)
                        failure = compile_failure_status(res)
                        if failure:
                            tick_failed = True
                            tick_failure = failure
                            if failure == "rpc_error":
                                record_rpc_error(name, res)
                            # Round 7: an rpc_error carries its message in the
                            # top-level `error`, not result.reason — reading
                            # only the latter logged "rpc_error: None".
                            # Round 17: lifecycle_recovered surfaces via status
                            # (no reason); log drafts_deferred for honesty.
                            payload = (res.get("result") or {}) if isinstance(res, dict) else {}
                            detail = (
                                (res.get("error") or {}).get("message")
                                if failure == "rpc_error"
                                else payload.get("reason")
                                or (
                                    f"drafts_deferred={payload.get('drafts_deferred')}"
                                    if failure == "lifecycle_recovered"
                                    else None
                                )
                            )
                            context.logger.warning(
                                "AFM loop: consolidation batch %d returned %s: %s",
                                batches + 1, failure, detail,
                            )
                            # Soft: sticky lifecycle was cleared; keep draining
                            # so this tick can still produce review pages.
                            if failure == "lifecycle_recovered":
                                last_summ = payload.get("summary") if isinstance(payload, dict) else None
                                batches += 1
                                examined = (last_summ or {}).get("examined", 0)
                                total_examined += examined
                                if examined == 0:
                                    break
                                await asyncio.sleep(0)
                                continue
                            break
                        last_summ = (res.get("result") or {}).get("summary") if isinstance(res, dict) else None
                        batches += 1
                        examined = (last_summ or {}).get("examined", 0)
                        total_examined += examined
                        if examined == 0:
                            break
                        await asyncio.sleep(0)  # yield to the server task between batches
                    context.logger.info(
                        "AFM loop: consolidation drained %d batch(es), %d candidate(s); last=%s",
                        batches,
                        total_examined,
                        last_summ,
                    )
                else:
                    res = handle_daemon_compile(params, request_id=None, context=context)
                    failure = compile_failure_status(res)
                    if failure:
                        tick_failed = True
                        tick_failure = failure
                        if failure == "rpc_error":
                            record_rpc_error(name, res)
                        context.logger.warning(
                            "AFM loop: pass '%s' returned %s: %s",
                            name, failure,
                            (res.get("error") or {}).get("message")
                            if failure == "rpc_error"
                            else (res.get("result") or {}).get("reason"),
                        )
                    else:
                        summ = (res.get("result") or {}).get("summary") if isinstance(res, dict) else None
                        context.logger.info("AFM loop: pass '%s' done: %s", name, summ)
            except Exception as exc:
                context.logger.exception("AFM loop: pass '%s' raised (skipped)", name)
                obs.incr("afm_loop_tick_failures_total")
                obs.incr(f"afm_loop_tick_failures.{name}")
                obs.record_error(f"afm_loop.{name}", exc)
                from minni.afm_writer import record_pass_attempt, record_pass_failure

                record_pass_attempt(name)
                record_pass_failure(name, str(exc))
                tick_failed = True
            # AFM-8 (#230): last_run used to advance unconditionally, so a tick
            # that accomplished nothing consumed the whole interval — a pass
            # failing instantly retried only once every 24h and looked, from
            # outside, exactly like a pass that ran and had nothing to do.
            # `tick_failed` covers BOTH routes a failure can take: an exception
            # the loop sees, and (the common one) a failure status returned by
            # handle_daemon_compile, which never raises. A write stall backs
            # off longer (round 5) — the previous batch is still queued, and a
            # 300s re-fire re-ran the whole pass against a blocked writer.
            last_run[name] = next_last_run(
                interval,
                time.time(),
                tick_failed,
                retry_seconds=(
                    _WRITE_STALL_RETRY_SECONDS
                    if tick_failure in _WRITE_STALL_STATUSES
                    else None
                ),
            )
        await asyncio.sleep(idle_seconds)
    context.logger.info("AFM loop runner stopped.")


def handle_daemon_endorse(params: dict, request_id: Any, context: AFMContext) -> dict:
    """Endorse a draft page by page_id. Accept/reject/edit only."""
    if context.increment_request_count is not None:
        context.increment_request_count()
    page_id = str(params.get("page_id") or "").strip()
    decision = str(params.get("decision") or "").strip()
    if not page_id:
        return context.make_error(-32602, "page_id is required", request_id)
    if decision not in {"accept", "reject", "edit"}:
        return context.make_error(-32602, "decision must be accept, reject, or edit", request_id)
    vault_path = str(params.get("vault_path") or context.default_config.vault_path)

    err = context.guard_vault_root(params, vault_path, request_id, label="endorse")
    if err:
        return err

    try:
        from minni.afm_writer import endorse_draft

        return context.make_response(endorse_draft(vault_path, page_id, decision), request_id)
    except Exception as exc:
        context.logger.warning("daemon.endorse failed: %s", exc)
        return context.make_error(-32000, f"Endorse error: {exc}", request_id)

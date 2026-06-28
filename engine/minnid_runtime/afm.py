import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config import DEFAULT_CONFIG
from db import SovereignDB
from principal import validate_agent_id


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


def afm_loop_enabled(config=DEFAULT_CONFIG) -> bool:
    if os.environ.get("MINNI_AFM_LOOP", "").lower() == "off":
        return False
    return bool((getattr(config, "afm_loop_schedule", {}) or {}).get("enabled"))


def maybe_archive_inbox_source(db, candidate_id: int, config=DEFAULT_CONFIG, log: logging.Logger = logger) -> None:
    """Move terminal candidate source files into inbox/.archive best-effort."""
    try:
        from afm_passes.inbox_archive import maybe_archive_for_candidate

        maybe_archive_for_candidate(db, config, candidate_id)
    except Exception:
        log.exception(
            "inbox archive for candidate %s failed (non-fatal)", candidate_id
        )


def promote_candidate_durable(candidate_id: int, reason: str, context: AFMContext):
    """Promote one proposed candidate into a durable, embedded learning."""
    import numpy as np

    wb = context.lazy_writeback()
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
        context.logger.warning(
            "_promote_candidate_durable: invalid agent_id %r (%s) - skipping",
            agent_id,
            exc,
        )
        return None
    from afm_passes.consolidation import content_hash as _content_hash

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
            "SELECT 1 FROM consolidation_actions WHERE action_type='afm_review' AND claim=? LIMIT 1",
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
            return context.make_error(-32602, f"unsupported pass_name: {pass_name}", request_id)
        trace_id = f"afm-{uuid.uuid4().hex[:12]}"
        db = context.sovereign_db(context.default_config)
        pass_module = __import__(pass_runners[pass_name], fromlist=["run"])

        result = pass_module.run(
            db,
            context.default_config,
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
                "writeback": context.writeback_ref(),
            })
            result.update(write_result)
        else:
            result.setdefault("drafts_written", [])

        if not dry_run and (
            result.get("promote_candidate_ids")
            or result.get("dedup_candidate_ids")
            or result.get("review_candidate_ids")
        ):
            apply_consolidation_result(result, context)

        try:
            context.trace_ring().put(trace_id, {
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
            context.logger.warning("AFM trace capture degraded: %s", exc)
        return context.make_response(result, request_id)
    except Exception as exc:
        context.logger.exception("daemon.compile failed")
        return context.make_response({
            "status": "afm_unavailable",
            "reason": str(exc),
            "pass_name": pass_name,
            "dry_run": dry_run,
            "drafts": [],
            "drafts_written": [],
        }, request_id)
    finally:
        context.record_latency("afm", time.perf_counter() - started_at)


async def afm_loop_runner(context: AFMContext):
    """Background scheduler for AFM passes."""
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
    await asyncio.sleep(min(30, idle_seconds))
    while context.is_running():
        now = time.time()
        for name, cfg in passes_cfg.items():
            interval = int((cfg or {}).get("interval_seconds", 24 * 60 * 60))
            if now - last_run.get(name, 0.0) < interval:
                continue
            try:
                context.logger.info("AFM loop: running pass '%s'", name)
                params = {
                    "pass_name": name,
                    "dry_run": False,
                    "vault_path": context.default_config.vault_path,
                }
                if name == "consolidation":
                    if (cfg or {}).get("ingest_inbox", True):
                        try:
                            from afm_passes.inbox_ingest import ingest as _ingest_inbox

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
                            _skips = _ing.get("skipped_by_kind") or {}
                            if any(_skips.values()):
                                context.logger.info(
                                    "AFM loop: inbox ingest skipped_by_kind=%s",
                                    _skips,
                                )
                        except Exception:
                            context.logger.exception(
                                "AFM loop: inbox ingest raised (skipped; "
                                "consolidation continues)"
                            )
                    max_batches = int((cfg or {}).get("max_batches_per_tick", 40))
                    batches = total_examined = 0
                    last_summ = None
                    while batches < max_batches:
                        res = handle_daemon_compile(params, request_id=None, context=context)
                        last_summ = (res.get("result") or {}).get("summary") if isinstance(res, dict) else None
                        batches += 1
                        examined = (last_summ or {}).get("examined", 0)
                        total_examined += examined
                        if examined == 0:
                            break
                        await asyncio.sleep(0)
                    context.logger.info(
                        "AFM loop: consolidation drained %d batch(es), %d candidate(s); last=%s",
                        batches,
                        total_examined,
                        last_summ,
                    )
                else:
                    res = handle_daemon_compile(params, request_id=None, context=context)
                    summ = (res.get("result") or {}).get("summary") if isinstance(res, dict) else None
                    context.logger.info("AFM loop: pass '%s' done: %s", name, summ)
            except Exception:
                context.logger.exception("AFM loop: pass '%s' raised (skipped)", name)
            last_run[name] = time.time()
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
        from afm_writer import endorse_draft

        return context.make_response(endorse_draft(vault_path, page_id, decision), request_id)
    except Exception as exc:
        context.logger.warning("daemon.endorse failed: %s", exc)
        return context.make_error(-32000, f"Endorse error: {exc}", request_id)

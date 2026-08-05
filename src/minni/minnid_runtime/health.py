import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from minni.config import DEFAULT_CONFIG
from minni.db import SovereignDB
from minni.timestamps import (
    malformed_timestamp_report,
    parse_epoch_or_report,
    stored_malformed_timestamp_count,
)


logger = logging.getLogger("minnid")

# NEW-01: health_report is reachable pre-identity (in RECOVERY_ALLOWED_METHODS),
# so its per-record fields — document paths and learning contents — must be
# withheld from an unstamped recovery-mode caller. Liveness/aggregate signals stay.
# W2: recent_errors (exception messages) joins the list — a traceback message can
# embed paths/payloads, so it rides the identical operator-gate/redaction path.
_HEALTH_REPORT_SENSITIVE_KEYS = (
    "stale_docs",
    "never_recalled",
    "contradicting_learnings",
    "recent_errors",
)


# GA6-2: the four consolidation-tick sub-ops, named explicitly so this field
# stays scoped to the subsystem it describes.
CONSOLIDATION_FAILURE_COUNTERS = (
    "inbox_ingest_failures_total",
    "inbox_quarantine_failures_total",
    "inbox_archive_failures_total",
    "compact_distillation_failures_total",
)
# Review round 3 on PR #260: the ingest status was derived from the cumulative
# totals, which nothing ages out — one boot-time failure read "failing" until
# daemon restart, the same one-way latch round 2 removed from derive_loop_status
# and writes_dropped. Only a failure inside this window is a live condition;
# the lifetime totals stay reported as data.
CONSOLIDATION_FAILURE_RECENT_SECONDS = 3600.0


def redact_health_report_for_recovery(report: dict) -> dict:
    """Strip document paths and learning contents from a pre-identity health_report.

    Per-record detail is replaced with a count so an unauthenticated caller
    cannot enumerate filesystem paths or learning text; non-sensitive liveness
    fields (afm_loop, faiss_cache_age_seconds, vector_backend_lag,
    inbox_quarantine — aggregate counts only, no file paths) are retained.
    Returns a new dict; the input is not mutated.
    """
    redacted = dict(report)
    for key in _HEALTH_REPORT_SENSITIVE_KEYS:
        items = report.get(key) or []
        redacted[f"{key}_count"] = len(items)
        redacted[key] = []
    # grok-review (PR #242): malformed_timestamps.examples carries per-doc
    # doc_id + value repr. It is a nested dict, not a flat list, so the
    # _HEALTH_REPORT_SENSITIVE_KEYS loop above (which counts list length)
    # cannot cover it — do it explicitly. Aggregate fields (stored_rows,
    # read_skips, by_field, remediation) are counts/labels only and stay.
    malformed = report.get("malformed_timestamps")
    if isinstance(malformed, dict) and malformed.get("examples"):
        redacted_malformed = dict(malformed)
        redacted_malformed["examples"] = {}
        redacted["malformed_timestamps"] = redacted_malformed
    redacted["redacted"] = (
        "pre-identity diagnostic: per-record detail withheld until a principal is stamped"
    )
    return redacted


@dataclass(frozen=True)
class HealthContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    guard_vault_root: Callable[..., Optional[dict]]
    latency_snapshot: Callable[[], dict]
    metrics_snapshot: Callable[[], dict]
    afm_loop_enabled: Callable[[Any], bool]
    # W2 (health opacity): delta-aware counters + derived flags + the exception
    # ring buffer. Defaulted so tests/legacy wiring that construct a HealthContext
    # without them keep working (status just omits the self-diagnosing extras).
    metrics_delta_snapshot: Callable[[], dict] = lambda: {}
    # Review round 3 (PR #260): recency source for latch-free status verdicts.
    # Defaulted to "unknown" (None), which callers must treat as NOT recovered —
    # a context wired without recency keeps the alarm rather than clearing it.
    metrics_last_incremented_at: Callable[[str], Optional[float]] = lambda name: None
    health_flags: Callable[[dict], list] = lambda deltas: []
    recent_errors: Callable[[], list] = lambda: []
    increment_request_count: Callable[[], None] | None = None
    request_count: Callable[[], int] = lambda: 0
    start_time: Callable[[], float] = lambda: time.time()
    version: str = "unknown"
    sovereign_db: Callable[..., Any] = SovereignDB
    default_config: Any = field(default_factory=lambda: DEFAULT_CONFIG)
    logger: logging.Logger = logger
    # P0-B (2026-07-19 blackout): lets status surface the live engine's
    # vector_model_down flag. Optional so tests/legacy wiring keep working.
    retrieval_engine: Callable[[], Any] | None = None
    # #284 footprint watchdog: restart_count survives process restart via a
    # JSON state file. Default returns a clean never-tripped shape so tests
    # and legacy HealthContext constructors stay valid without wiring.
    watchdog_state: Callable[[], dict] = lambda: {
        "restart_count": 0,
        "last_restart_reason": None,
        "last_restart_at": None,
    }


def faiss_cache_status(config=DEFAULT_CONFIG) -> tuple[Path, bool]:
    legacy_path = Path(config.faiss_index_path)
    if legacy_path.exists():
        return legacy_path, legacy_path.stat().st_size > 0

    try:
        from minni.faiss_persist import _faiss_dir_for_db

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


def faiss_cache_age_seconds(config=DEFAULT_CONFIG) -> Optional[float]:
    path, ok = faiss_cache_status(config)
    if not ok:
        return None
    return round(max(0.0, time.time() - path.stat().st_mtime), 3)


def handle_status(params: dict, request_id: Any, context: HealthContext) -> dict:
    """Return daemon and engine status."""
    if context.increment_request_count is not None:
        context.increment_request_count()

    vault_path = params.get("vault") or params.get("vault_path") or context.default_config.vault_path
    err = context.guard_vault_root(params, vault_path, request_id, label="status")
    if err:
        return err

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
    db = None
    try:
        db = context.sovereign_db()
        with db.cursor() as c:
            c.execute("SELECT COUNT(*) as n FROM documents")
            db_stats["documents"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM chunk_embeddings")
            db_stats["chunks"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM learnings")
            db_stats["learnings"] = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM episodic_events")
            db_stats["events"] = c.fetchone()["n"]
        db_ok = True
    except Exception:
        pass
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass

    _, faiss_ok = faiss_cache_status(context.default_config)
    # P0-B: faiss_ok only proves the index FILE exists. The query encoder can
    # still be down (recall silently FTS-only for 14.8h in the 2026-07-18
    # session) — surface the engine's flag so the two states are separable.
    # "ok" here means "no failed encode attempt yet", not a live probe (a
    # probe would force a multi-second model load inside status).
    vector_model = "unknown"
    if context.retrieval_engine is not None:
        try:
            _eng = context.retrieval_engine()
            vector_model = (
                "DOWN" if getattr(_eng, "vector_model_down", False) else "ok"
            )
        except Exception:
            vector_model = "unknown"
    try:
        from minni.afm_provider import afm_runtime_status

        afm_status = afm_runtime_status()
    except Exception as exc:
        afm_status = {
            "mode": "unknown",
            "status": "degraded",
            "native_available": False,
            "error": str(exc),
        }

    uptime = time.time() - context.start_time()
    metrics = context.metrics_snapshot()
    # W2: delta-aware view (compute once so the baseline advances exactly once)
    # + derived flags, so status is self-diagnosing instead of a bare int.
    # Review r2 (P2) + r3 (P2): consuming the single global delta baseline is
    # EXPLICIT-OPT-IN, not implicit in identity. The shipped SessionStart/hook
    # path calls status over the local UDS as a stamped (non-recovery, often
    # operator) principal, so "identified ⇒ consume" still let routine
    # background polls swallow an errors.search burst and clear
    # errors_search_rising before a human ever looked. Now a caller must (a) be
    # dispatch-stamped non-recovery (`_recovery` is False — trusted flag, not
    # spoofable), (b) be an operator/govern principal (same bar as the
    # un-redacted health_report), and (c) explicitly ask via
    # `consume_deltas: true`. Everything else — hooks, recovery, pre-identity,
    # plain status — peeks, so deltas read "since last explicit operator
    # consume" (or since daemon start). Legacy zero-arg context wiring keeps
    # the old always-consume behavior via the TypeError fallback.
    from minni.principal import EffectivePrincipal, is_operator_principal

    stamped_principal = params.get("_principal")
    consume = (
        params.get("_recovery") is False
        and params.get("consume_deltas") is True
        and isinstance(stamped_principal, EffectivePrincipal)
        and is_operator_principal(stamped_principal)
    )
    try:
        deltas = context.metrics_delta_snapshot(consume=consume)
    except TypeError:
        deltas = context.metrics_delta_snapshot()
    flags = context.health_flags(deltas)
    started_at = datetime.fromtimestamp(
        context.start_time(), tz=timezone.utc
    ).isoformat()
    # Deploy honesty (GA1-3/GA5-1): report when the RUNNING code is stale
    # relative to the checkout it was installed from. Local comparison only;
    # deploy_status() never raises.
    from minni.minnid_runtime.deploy_honesty import deploy_status

    deploy = deploy_status()
    return context.make_response({
        "daemon": {
            "version": context.version,
            "pid": os.getpid(),
            "started_at": started_at,
            "uptime_seconds": round(uptime, 1),
            "requests_served": context.request_count(),
            "socket_path": "[redacted]",
            "latencies": context.latency_snapshot(),
            "errors": metrics.get("errors", 0),
            "counters": metrics,
            "counter_deltas": deltas,
            "health_flags": flags,
            "deploy": deploy,
            # #284 H5 guard: restart count must be visible in status, not silent.
            "footprint_watchdog": context.watchdog_state(),
        },
        "engine": {
            "db_ok": db_ok,
            "db_path": "[redacted]",
            "faiss_ok": faiss_ok,
            "vector_model": vector_model,
            "faiss_path": "[redacted]",
            "stats": db_stats,
            "audit_volume": audit_vol,
        },
        "afm": afm_status,
    }, request_id)


def handle_health_report(params: dict, request_id: Any, context: HealthContext) -> dict:
    """Return deeper read-only memory health diagnostics."""
    now = time.time()
    stale_cutoff = now - (30 * 24 * 60 * 60)
    report = {
        "stale_docs": [],
        "never_recalled": [],
        "contradicting_learnings": [],
        "vector_backend_lag": [],
        # #284 H5: footprint restarts must surface here too, not only on status.
        "footprint_watchdog": context.watchdog_state(),
        # Audit R0: non-numeric values sitting in the REAL-affinity timestamp
        # columns. The readers are defensive now, but a skipped row must stay
        # VISIBLE here rather than being silently tolerated.
        "malformed_timestamps": {
            "stored_rows": 0, "read_skips": 0, "by_field": {},
            "examples": {}, "remediation": None,
        },
        # W3 (audit §4 / consolidation inbox residue): operator-visible
        # recovery surface for files parked in <inbox>/quarantine/ (see
        # afm_passes.inbox_quarantine). Aggregate-only (count + reason
        # breakdown + oldest timestamp) — no file paths, so this stays
        # outside _HEALTH_REPORT_SENSITIVE_KEYS like vector_backend_lag.
        "inbox_quarantine": {"count": 0, "oldest_quarantined_at": None, "by_reason": {}},
        # M4/M5 (#229): the three memory-lifecycle queues that accumulated
        # without a reader, without aging, or without any count on a health
        # surface. Aggregate-only (depths and ages — no paths, no candidate
        # content), so this stays outside _HEALTH_REPORT_SENSITIVE_KEYS.
        "memory_lifecycle": {
            "afm_dead_letter": {"files": 0, "oldest_age_days": None},
            "afm_review_orphans": 0,
            "proposed_queue": {"depth": 0, "oldest_age_days": None, "stale": 0},
            # #307: how many inbox files the distillation pass currently
            # cannot read. A LIVE count, not the cumulative drop counter — a
            # dropped file is never archived, so it is re-dropped every tick
            # and a cumulative total is files x ticks-since-boot, which would
            # overstate corruption by ~96x/day per file. This clears when the
            # file is removed.
            "compact_distillation_unusable": {"files": 0},
        },
        # #225-R6 / GA1-1: health never compared document count against vector
        # count, so a 43% document-vector gap (381/879, all knowledge layer) and
        # 409 NULL-embedding learnings were invisible to every status surface —
        # only a manual query could find them. Aggregate counts and ratios only,
        # no paths and no learning text, so it stays outside
        # _HEALTH_REPORT_SENSITIVE_KEYS like vector_backend_lag.
        "embedding_coverage": {},
        # GA4-1 guardrail: wiring record_score means score_distribution fills
        # during normal retrieval, and crossing the activation threshold changes
        # what `confidence` MEANS for every caller (raw blend -> percentile
        # rank). A feature that switches semantics on a row count with nothing
        # observable is the silent-degrade class this audit removes, so the
        # transition is reported. Counts and labels only — no scores — so it
        # stays outside _HEALTH_REPORT_SENSITIVE_KEYS.
        "score_calibration": {},
        # GA6-2: the four consolidation-tick sub-ops (inbox ingest, quarantine,
        # inert archive, compact distillation) incremented a counter ONLY on
        # their success paths and swallowed every exception, so a sub-op broken
        # for days was indistinguishable from one that simply had no work —
        # health had no ingest-failure field and no inbox-backlog field at all.
        # Both live here. Aggregate-only, so it stays outside
        # _HEALTH_REPORT_SENSITIVE_KEYS alongside inbox_quarantine.
        "consolidation_ingest": {
            "failures": {},
            "inbox_backlog": 0,
            "status": "unknown",
        },
        # W2: last-N dispatch exceptions so a climbing errors.<method> counter is
        # attributable. Sensitive (messages can embed paths/payloads) — redacted
        # to a count for any non-operator caller via _HEALTH_REPORT_SENSITIVE_KEYS.
        "recent_errors": context.recent_errors(),
        "faiss_cache_age_seconds": faiss_cache_age_seconds(context.default_config),
        # Placeholder only: overwritten below by afm_writer.writer_status(), which
        # derives `status` from the loop's own numbers. Until then the honest
        # value for an enabled loop is "unknown", not "ok" — nothing has been
        # inspected yet at this point.
        "afm_loop": {
            "last_run_per_pass": {},
            "last_attempt_per_pass": {},
            "drafts_pending": 0,
            "drafts_pending_oldest": None,
            "afm_latency_p95": 0.0,
            "status": "disabled" if not context.afm_loop_enabled(context.default_config) else "unknown",
            "status_reasons": [],
        },
    }

    db = None
    try:
        try:
            from minni.afm_writer import writer_status

            report["afm_loop"] = writer_status(
                context.default_config.vault_path,
                # The intervals status is judged against live in config, not in
                # the writer; pass them rather than letting the writer guess.
                schedule=getattr(context.default_config, "afm_loop_schedule", {}) or {},
            )
            if not context.afm_loop_enabled(context.default_config):
                report["afm_loop"]["status"] = "disabled"
        except Exception as exc:
            report["afm_loop"]["status"] = "degraded"
            report["afm_loop"]["error"] = str(exc)

        db = context.sovereign_db(context.default_config)

        # #225-R6 / GA1-1: the document-to-vector and learning-to-embedding
        # ratios. Best-effort — a coverage query must never cost the operator
        # the rest of the health report.
        try:
            from minni.backfill import embedding_coverage, vault_embedding_coverage

            coverage = embedding_coverage(db)
            # grok-review round 3 (finding 4): the drain covers every index
            # (run_backfill_all_indexes) but this surface sampled only the
            # shared DB, so "coverage fine" could mask a still-gapped vault.
            # Counts-only per-vault rollup, same best-effort contract.
            try:
                coverage["vaults"] = vault_embedding_coverage(
                    base_config=context.default_config
                )
            except Exception as exc:
                coverage["vaults"] = {"error": str(exc)}
            report["embedding_coverage"] = coverage
        except Exception as exc:
            report["embedding_coverage"] = {"error": str(exc)}

        try:
            from minni.scoring import calibration_status

            report["score_calibration"] = calibration_status(db)
        except Exception as exc:
            report["score_calibration"] = {"error": str(exc)}

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
                # Audit R0: parse-or-report. A TEXT timestamp here used to
                # TypeError on the subtraction and take the whole health report
                # down with it.
                #
                # grok-review (PR #242): `row["indexed_at"] or row["last_modified"]
                # or 0` picks indexed_at whenever it is truthy — including a
                # non-numeric TEXT value, which is truthy too — so a poisoned
                # indexed_at shadowed a perfectly good last_modified and this
                # row's age_days went to None instead of a real number. Parse
                # indexed_at first and only fall back to last_modified if that
                # parse fails, matching how decay.py treats the same two columns.
                ts = parse_epoch_or_report(
                    row["indexed_at"], field="indexed_at",
                    source="health.stale_docs", doc_id=row["doc_id"],
                )
                if ts is None:
                    ts = parse_epoch_or_report(
                        row["last_modified"], field="last_modified",
                        source="health.stale_docs", doc_id=row["doc_id"],
                    )
                report["stale_docs"].append({
                    "doc_id": row["doc_id"],
                    "path": row["path"],
                    # grok-review (PR #242): `if ts` treats the migration's own
                    # 0.0 sentinel (a deliberate "needs attention" marker for
                    # unparseable rows) as "no timestamp", since 0.0 is falsy —
                    # so a repaired-to-sentinel row's very large, very visible
                    # age gets hidden as None instead of shown. `is not None`
                    # keeps 0.0 visible.
                    "age_days": round((now - ts) / 86400, 1) if ts is not None else None,
                })

            # Audit R0 visibility: a poisoned timestamp must be *reported*, not
            # merely tolerated by the defensive readers above. Count the rows
            # SQLite still holds as non-numeric in a REAL-affinity column, and
            # fold in whatever this process has already had to skip at read time.
            stored_bad = stored_malformed_timestamp_count(c)
            skips = malformed_timestamp_report()
            report["malformed_timestamps"] = {
                "stored_rows": stored_bad,
                "read_skips": skips["total"],
                "by_field": skips["by_field"],
                "examples": skips["examples"],
                "remediation": (
                    "migration 016 normalizes stored timestamps"
                    if stored_bad else None
                ),
            }
            if stored_bad or skips["total"]:
                logger.warning(
                    "Health: %d document row(s) hold a non-numeric timestamp and "
                    "%d read(s) were skipped this process. Run migration 016.",
                    stored_bad, skips["total"],
                )

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

        try:
            # Filesystem-native like the rest of this subsystem (no DB round
            # trip needed — quarantined files by definition never got a
            # candidate_packets row). Own try/except so a scan failure cannot
            # take down the rest of the report.
            from minni.afm_passes.inbox_ingest import discover_inboxes

            q_count = 0
            oldest: Optional[str] = None
            by_reason: dict[str, int] = {}
            for inbox in discover_inboxes(context.default_config):
                q_dir = inbox / "quarantine"
                if not q_dir.is_dir():
                    continue
                for reason_path in q_dir.glob("*.reason.json"):
                    q_count += 1
                    try:
                        payload = json.loads(reason_path.read_text(encoding="utf-8"))
                    except Exception:
                        payload = {}
                    reason = str(payload.get("reason") or "unknown")
                    by_reason[reason] = by_reason.get(reason, 0) + 1
                    qat = payload.get("quarantined_at")
                    if isinstance(qat, str) and (oldest is None or qat < oldest):
                        oldest = qat
            report["inbox_quarantine"] = {
                "count": q_count,
                "oldest_quarantined_at": oldest,
                "by_reason": by_reason,
            }
        except Exception as exc:
            # Review r3 (P2): exception class only — OSError messages embed the
            # quarantine/sidecar filesystem path, and this block deliberately
            # sits OUTSIDE _HEALTH_REPORT_SENSITIVE_KEYS (aggregate-only
            # contract), so a raw str(exc) would survive recovery/non-operator
            # redaction and leak local paths.
            report["inbox_quarantine"] = {
                "status": "unknown",
                "error": type(exc).__name__,
            }

        # M4/M5 (#229). Each of these queues grew unbounded or drifted while
        # every health surface stayed quiet: the dead letter had no reader and
        # no count, review markers outlived their candidates with nothing
        # reporting the orphan total, and the proposed queue sat at a constant
        # depth with no staleness signal. A depth alone cannot tell a healthy
        # queue from a parked one, so ages are reported alongside counts.
        #
        # The filesystem scan and the DB scan get their OWN try/except: they
        # fail independently, and a DB fault must not blank out a dead-letter
        # count that was read successfully (nor the reverse). Exception class
        # only — same path-leak reasoning as the inbox_quarantine block.
        lifecycle = dict(report["memory_lifecycle"])
        try:
            from minni.afm_passes.inbox_quarantine import count_afm_dead_letter

            lifecycle["afm_dead_letter"] = count_afm_dead_letter(
                config=context.default_config,
            )
        except Exception as exc:
            # Replace the sub-dict rather than adding a sibling key: leaving
            # the zero-valued default in place meant a scan that never ran
            # read as "0 files", which is the health overstatement this block
            # exists to remove. Mirrors the inbox_quarantine precedent above.
            lifecycle["afm_dead_letter"] = {
                "files": None,
                "oldest_age_days": None,
                "unreadable": None,
                "status": "unknown",
                "error": type(exc).__name__,
            }
        try:
            from minni.afm_passes.compact_distillation import (
                count_unusable_compact_files,
            )

            lifecycle["compact_distillation_unusable"] = count_unusable_compact_files(
                config=context.default_config,
            )
        except Exception as exc:
            # Replace the sub-dict, matching the sibling blocks: leaving the
            # zero default would report a scan that never ran as "0 files".
            lifecycle["compact_distillation_unusable"] = {
                "files": None,
                "status": "unknown",
                "error": type(exc).__name__,
            }
        try:
            from minni.afm_review_markers import (
                count_orphaned_afm_review,
                proposed_queue_stats,
            )

            # `db` is already open in this scope (line ~383) and closed in
            # the finally below — opening a second SovereignDB here re-ran
            # PRAGMA journal_mode=WAL on every health call for nothing.
            with db.cursor() as c:
                lifecycle["afm_review_orphans"] = count_orphaned_afm_review(c)
                lifecycle["proposed_queue"] = proposed_queue_stats(c)
        except Exception as exc:
            lifecycle["afm_review_orphans"] = None
            lifecycle["proposed_queue"] = {
                "depth": None,
                "oldest_age_days": None,
                "stale": None,
                "unparseable_proposed_at": None,
                "status": "unknown",
                "error": type(exc).__name__,
            }
        report["memory_lifecycle"] = lifecycle

        try:
            # GA6-2: failures come from the global counters the sub-ops now
            # increment on their exception paths; backlog is the count of
            # undrained inbox files. A climbing backlog with zero failures and
            # a climbing failure count are different faults, so both are
            # reported rather than collapsed into one "ingest is unhappy".
            from minni.afm_passes.inbox_ingest import discover_inboxes

            # metrics_snapshot(), not metrics_delta_snapshot(): this is a plain
            # copy of the counters. The delta variant advances a baseline, and
            # a report must never consume the deltas a status poll is reading.
            counters = context.metrics_snapshot()
            # Whitelisted, not suffix-matched. A bare `_failures_total` filter
            # also swept in afm_pass_failures_total and
            # afm_loop_tick_failures_total, so a synthesis-pass fault flipped
            # THIS field to "failing" — a different subsystem's problem
            # reported as an ingest problem, which is exactly the kind of
            # mis-attribution this whole slice exists to end. (Review round 1
            # on PR #260.)
            failures = {
                name: counters[name]
                for name in CONSOLIDATION_FAILURE_COUNTERS
                if counters.get(name)
            }
            # Review round 3 (PR #260): status from RECENCY, not lifetime
            # totals — a counter bumped once at boot must not read "failing"
            # forever (the latch class round 2 removed from derive_loop_status).
            # Unknown recency (no stamp available) keeps the alarm: better a
            # stale reason than a suppressed fault. The totals stay in
            # `failures` as data either way.
            now = time.time()
            recent_failures = {}
            for name, total in failures.items():
                last_at = context.metrics_last_incremented_at(name)
                if last_at is None or now - last_at <= CONSOLIDATION_FAILURE_RECENT_SECONDS:
                    recent_failures[name] = total
            backlog = 0
            for inbox in discover_inboxes(context.default_config):
                if inbox.is_dir():
                    backlog += sum(1 for _ in inbox.glob("*.json"))
            report["consolidation_ingest"] = {
                "failures": failures,
                "recent_failures": recent_failures,
                "inbox_backlog": backlog,
                "status": "failing" if recent_failures else "ok",
            }
        except Exception as exc:
            # Exception class only, for the same reason as inbox_quarantine
            # above: this block is aggregate-only and outside the redaction set,
            # so an OSError message would leak an inbox filesystem path.
            report["consolidation_ingest"] = {
                "status": "unknown",
                "error": type(exc).__name__,
            }
    except Exception as exc:
        context.logger.warning("health_report degraded: %s", exc)
        report["error"] = str(exc)
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass

    # Fail-closed: redact unless the dispatcher's trusted flag says this is a
    # fully-identified (non-recovery) caller. `_recovery` is set by dispatch and
    # cannot be spoofed by the client.
    #
    # R6: the un-redacted report enumerates cross-agent document paths and
    # contradicting-learning content with no agent/privacy/status filter, so a
    # merely-identified non-operator caller must NOT see it. Full detail now
    # additionally requires an operator/govern principal; every other identified
    # caller gets the same aggregate-only redaction as a recovery caller.
    from minni.principal import EffectivePrincipal, is_operator_principal

    stamped = params.get("_principal")
    is_operator = isinstance(stamped, EffectivePrincipal) and is_operator_principal(stamped)
    if params.get("_recovery") is not False or not is_operator:
        report = redact_health_report_for_recovery(report)

    return context.make_response(report, request_id)


def handle_hygiene_report(params: dict, request_id: Any, context: HealthContext) -> dict:
    """Run read-only vault/wiki hygiene checks and return JSON summary."""
    # G12: enforce stamped principal's allowed_vault_roots on any supplied vault (realpath checked)
    vault_path = params.get("vault") or params.get("vault_path") or context.default_config.vault_path
    err = context.guard_vault_root(params, vault_path, request_id, label="hygiene")
    if err:
        return err
    try:
        from minni.hygiene import run_hygiene_report

        summary = run_hygiene_report(Path(vault_path))
        return context.make_response(summary, request_id)
    except Exception as exc:
        context.logger.warning("hygiene_report degraded: %s", exc)
        return context.make_response({
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

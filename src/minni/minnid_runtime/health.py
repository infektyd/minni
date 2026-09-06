import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
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


def resolution_mix_stats(cursor: Any) -> dict:
    """#290: how accepted candidates were resolved — auto vs human vs the AFM loop.

    Auto-acceptance writes durable memory with no human in the loop. A channel
    like that must be observable, or "the operator turned the knob on once" and
    "the knob has been accepting everything for a month" look identical from the
    outside. Aggregate counts only: no content, no principal names.

    The modes are distinguished by the ``resolved_by`` stamp each writer sets —
    ``auto_accept_own(<agent>)`` here, ``afm-consolidation`` for the background
    pass, and a bare principal id for a human resolve.
    """
    cursor.execute(
        """
        SELECT resolved_by, COUNT(*) AS n
        FROM candidate_packets
        WHERE status = 'accepted' AND resolved_by IS NOT NULL
        GROUP BY resolved_by
        """
    )
    mix = {"auto_accept_own": 0, "manual": 0, "afm_consolidation": 0}
    for row in cursor.fetchall():
        who = str(dict(row).get("resolved_by") or "")
        count = int(dict(row).get("n") or 0)
        if who.startswith("auto_accept_own("):
            mix["auto_accept_own"] += count
        elif who == "afm-consolidation":
            mix["afm_consolidation"] += count
        else:
            mix["manual"] += count
    return mix


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
            # #290: auto-acceptance is a channel that writes durable memory
            # without a human in the loop, so it has to be COUNTED somewhere a
            # human looks. Aggregate-only (counts by resolution mode, no
            # content, no principals), matching the rest of this block.
            "resolution_mix": {"auto_accept_own": 0, "manual": 0,
                               "afm_consolidation": 0},

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

            # Same fallback the drain and the pass use — see
            # count_unusable_compact_files' docstring on why this is required
            # rather than defaulted.
            _cons = (
                (getattr(context.default_config, "afm_loop_schedule", {}) or {})
                .get("passes", {})
                .get("consolidation", {})
            ) or {}
            lifecycle["compact_distillation_unusable"] = count_unusable_compact_files(
                config=context.default_config,
                fallback_principal=str(
                    _cons.get("inbox_fallback_principal", "unknown")
                ),
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
                lifecycle["resolution_mix"] = resolution_mix_stats(c)
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
            lifecycle["resolution_mix"] = {
                "auto_accept_own": None,
                "manual": None,
                "afm_consolidation": None,
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


# fullmatch() is used at the call site, so no ^/$ anchors: a trailing
# newline must never smuggle an id through.
_ALIAS_PATH_RE = re.compile(r"learning://([0-9]+)")


@dataclass(frozen=True)
class LegacyAliasFinding:
    """One legacy alias verdict. Read-only: proposals are text, never writes."""

    kind: str  # "migrate_candidate" | "ambiguous" | "malformed"
    alias_doc_id: int
    alias_path: str
    learning_id: Optional[int]
    agent: Optional[str]
    canonical_doc_id: Optional[int]
    canonical_path: Optional[str]
    out_edge_count: int
    in_edge_count: int
    reason: str
    proposal: str


@dataclass(frozen=True)
class LegacyAliasDiagnostic:
    findings: tuple = ()
    scanned: int = 0
    candidates: int = 0
    ambiguous: int = 0
    malformed: int = 0
    truncated: bool = False


@contextmanager
def _alias_read_snapshot(conn: Any):
    """Coherent read-only snapshot that never commits caller state.

    Mirrors the graph-expansion pattern: SAVEPOINT when the caller already
    holds a transaction, else a deferred read transaction; both are rolled
    back / released, never committed. SELECT-only callers see one stable
    version across every check below.
    """
    cur = conn.cursor()
    nested = bool(getattr(conn, "in_transaction", False))
    savepoint = "minni_alias_diag_" + uuid.uuid4().hex
    if nested:
        cur.execute(f"SAVEPOINT {savepoint}")
    else:
        cur.execute("BEGIN DEFERRED")
    try:
        yield cur
    finally:
        try:
            if nested:
                sqlite3.Cursor.execute(cur, f"ROLLBACK TO SAVEPOINT {savepoint}")
                sqlite3.Cursor.execute(cur, f"RELEASE SAVEPOINT {savepoint}")
            else:
                conn.rollback()
        finally:
            cur.close()


def diagnose_legacy_aliases(db: Any, *, limit: int = 200) -> LegacyAliasDiagnostic:
    """Offline read-only diagnostic for legacy ``learning://<id>`` aliases.

    Only SELECTs inside one read snapshot; never writes, migrates, or
    classifies, and never commits the caller's transaction. A
    ``migrate_candidate`` is provable 1:1 ONLY when every check holds: the
    alias path full-matches ``learning://<digits>`` with a bounded id, the
    learning exists and is active, the alias owner matches
    ``learning:<agent>``, the alias itself carries no foreign join claim and
    no conflicting FTS evidence, a canonical ``_durable`` node exists at the
    durable address of that exact (agent, content) owned by the same agent,
    the canonical node is a live learning projection (not restricted/
    retired, not a wiki/other kind), the canonical FTS content equals the
    learning content where projection evidence exists (missing or
    conflicting evidence is ambiguity, not proof), the exact
    (learning, canonical) join row exists, and no OTHER learning maps to the
    canonical node. Everything else is ``ambiguous`` (with the blocking
    reason) or ``malformed`` — never guessed. The future migration copies
    the alias's out-edges to the canonical node and marks the alias
    superseded; this function only proposes that in text.
    """
    from minni.durable_projection import durable_doc_path

    limit = max(1, int(limit))
    getter = getattr(db, "_get_conn", None)
    conn = getter() if callable(getter) else db
    with _alias_read_snapshot(conn) as c:
        tables = {
            row[0]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "documents" not in tables:
            return LegacyAliasDiagnostic()
        total = c.execute(
            "SELECT COUNT(*) FROM documents WHERE path LIKE 'learning://%'"
        ).fetchone()[0]
        rows = c.execute(
            "SELECT doc_id, path, agent, privacy_level, page_status"
            " FROM documents WHERE path LIKE 'learning://%'"
            " ORDER BY doc_id LIMIT ?",
            (limit + 1,),
        ).fetchall()
        join_table = "learning_documents" in tables
        learnings_table = "learnings" in tables
        links_table = "memory_links" in tables
        fts_table = "vault_fts" in tables

        findings: list = []
        for alias in rows[:limit]:
            findings.append(_diagnose_one_alias(
                c, alias, db, join_table, learnings_table, links_table,
                fts_table, durable_doc_path,
            ))
    kinds = [f.kind for f in findings]
    return LegacyAliasDiagnostic(
        findings=tuple(findings),
        scanned=int(total),
        candidates=sum(1 for k in kinds if k == "migrate_candidate"),
        ambiguous=sum(1 for k in kinds if k == "ambiguous"),
        malformed=sum(1 for k in kinds if k == "malformed"),
        truncated=int(total) > limit,
    )


_MAX_SQLITE_ID = 9223372036854775807


def _diagnose_one_alias(
    c: Any, alias: Any, db: Any, join_table: bool, learnings_table: bool,
    links_table: bool, fts_table: bool, durable_doc_path: Any,
) -> LegacyAliasFinding:
    alias_doc_id = int(alias["doc_id"])
    alias_path = str(alias["path"])

    def _edges():
        out_edges = in_edges = 0
        if links_table:
            out_edges = c.execute(
                "SELECT COUNT(*) FROM memory_links WHERE source_doc_id = ?",
                (alias_doc_id,),
            ).fetchone()[0]
            in_edges = c.execute(
                "SELECT COUNT(*) FROM memory_links WHERE target_doc_id = ?",
                (alias_doc_id,),
            ).fetchone()[0]
        return int(out_edges), int(in_edges)

    def _verdict(kind, reason, proposal="", **kw):
        out_edges, in_edges = _edges()
        return LegacyAliasFinding(
            kind=kind, alias_doc_id=alias_doc_id, alias_path=alias_path,
            learning_id=kw.get("learning_id"), agent=kw.get("agent"),
            canonical_doc_id=kw.get("canonical_doc_id"),
            canonical_path=kw.get("canonical_path"),
            out_edge_count=out_edges, in_edge_count=in_edges,
            reason=reason, proposal=proposal,
        )

    # fullmatch: a trailing newline must not smuggle an id through.
    match = _ALIAS_PATH_RE.fullmatch(alias_path)
    if not match:
        return _verdict(
            "malformed",
            f"alias path {alias_path!r} is not exactly learning://<digits>; "
            "no learning id can be recovered, left alone",
        )
    digits = match.group(1).lstrip("0") or "0"
    if len(digits) > 19 or int(digits) > _MAX_SQLITE_ID:
        return _verdict(
            "malformed",
            f"alias id {match.group(1)!r} exceeds the SQLite integer range; "
            "bounded ids only, left alone",
        )
    learning_id = int(digits)
    if not learnings_table:
        return _verdict(
            "ambiguous", f"learning {learning_id}: learnings table absent, "
            "ownership unprovable", learning_id=learning_id)
    try:
        learning = c.execute(
            "SELECT learning_id, agent_id, content, status, superseded_by"
            " FROM learnings WHERE learning_id = ?",
            (learning_id,),
        ).fetchone()
    except OverflowError:
        return _verdict(
            "malformed",
            f"alias id {learning_id} is not a bounded SQLite integer; "
            "left alone",
            learning_id=None)
    if learning is None:
        return _verdict(
            "ambiguous", f"learning {learning_id} no longer exists; "
            "alias is orphaned, left alone", learning_id=learning_id)
    agent_id = learning["agent_id"]
    if (
        learning["superseded_by"] is not None
        or str(learning["status"] or "") in ("rejected", "expired", "superseded")
    ):
        return _verdict(
            "ambiguous", f"learning {learning_id} is retired "
            f"(status={learning['status']!r}); retired memories are never "
            "migrated", learning_id=learning_id, agent=agent_id)
    expected_owner = f"learning:{agent_id}"
    if alias["agent"] != expected_owner:
        return _verdict(
            "ambiguous", f"alias owner {alias['agent']!r} does not match "
            f"learning owner {expected_owner!r}; ownership unprovable",
            learning_id=learning_id, agent=agent_id)
    if alias["privacy_level"] == "blocked" or str(
            alias["page_status"] or "") != "accepted":
        return _verdict(
            "ambiguous", "alias node itself is restricted/retired; left alone",
            learning_id=learning_id, agent=agent_id)
    if join_table:
        # The alias is a URI-only claim: if the join table maps it to any
        # OTHER learning, the alias is a foreign/aggregate claim, not this
        # learning's private alias.
        foreign = c.execute(
            "SELECT learning_id FROM learning_documents WHERE doc_id = ?"
            " AND learning_id != ? LIMIT 1",
            (alias_doc_id, learning_id),
        ).fetchone()
        if foreign is not None:
            return _verdict(
                "ambiguous", f"alias node also maps learning "
                f"{foreign['learning_id']}: foreign/aggregate claim, never "
                "merged on URI alone",
                learning_id=learning_id, agent=agent_id)
    content = learning["content"] or ""
    vault_path = db.config.vault_path
    canonical_path = durable_doc_path(agent_id, "", vault_path, content)
    canonical = c.execute(
        "SELECT doc_id, agent, memory_kind, page_type, privacy_level,"
        " page_status FROM documents WHERE path = ?",
        (canonical_path,),
    ).fetchone()
    if canonical is None:
        return _verdict(
            "ambiguous", "no canonical _durable node at the durable address "
            "of this exact (agent, content); index/repair the learning first",
            learning_id=learning_id, agent=agent_id,
            canonical_path=canonical_path)
    canonical_doc_id = int(canonical["doc_id"])
    if canonical["agent"] != agent_id:
        return _verdict(
            "ambiguous", f"canonical node owner {canonical['agent']!r} does "
            f"not match learning owner {agent_id!r}; wrong-owner collision, "
            "left alone",
            learning_id=learning_id, agent=agent_id,
            canonical_doc_id=canonical_doc_id, canonical_path=canonical_path)
    kind = canonical["memory_kind"]
    if kind not in ("learning", None) or str(
            canonical["page_type"] or "") != "learning":
        return _verdict(
            "ambiguous", f"canonical node kind={kind!r} "
            f"page_type={canonical['page_type']!r} is not a learning "
            "projection; left alone",
            learning_id=learning_id, agent=agent_id,
            canonical_doc_id=canonical_doc_id, canonical_path=canonical_path)
    if canonical["privacy_level"] == "blocked" or str(
            canonical["page_status"] or "") != "accepted":
        return _verdict(
            "ambiguous", "canonical node is restricted/retired; migrating "
            "edges onto it would resurrect it, left alone",
            learning_id=learning_id, agent=agent_id,
            canonical_doc_id=canonical_doc_id, canonical_path=canonical_path)
    if not join_table:
        return _verdict(
            "ambiguous", "learning_documents join table absent: the exact "
            "mapping cannot be proven on this store",
            learning_id=learning_id, agent=agent_id,
            canonical_doc_id=canonical_doc_id, canonical_path=canonical_path)
    exact = c.execute(
        "SELECT 1 FROM learning_documents WHERE learning_id = ?"
        " AND doc_id = ?",
        (learning_id, canonical_doc_id),
    ).fetchone()
    if exact is None:
        return _verdict(
            "ambiguous", "no exact (learning, canonical) join row: mapping "
            "unproven, left alone",
            learning_id=learning_id, agent=agent_id,
            canonical_doc_id=canonical_doc_id, canonical_path=canonical_path)
    other = c.execute(
        "SELECT learning_id FROM learning_documents WHERE doc_id = ?"
        " AND learning_id != ? LIMIT 1",
        (canonical_doc_id, learning_id),
    ).fetchone()
    if other is not None:
        return _verdict(
            "ambiguous", f"canonical node also backs learning "
            f"{other['learning_id']}: N:1 aggregate, never conflated",
            learning_id=learning_id, agent=agent_id,
            canonical_doc_id=canonical_doc_id,
            canonical_path=canonical_path)
    if fts_table:
        # The durable path can collide with a stale mutated row: the
        # canonical FTS content must equal the learning content. Missing or
        # conflicting projection evidence is ambiguity, never proof.
        stored = c.execute(
            "SELECT content FROM vault_fts WHERE doc_id = ?",
            (canonical_doc_id,),
        ).fetchone()
        if stored is None or stored["content"] is None:
            return _verdict(
                "ambiguous", "canonical node has no FTS content evidence; "
                "content identity unproven, left alone",
                learning_id=learning_id, agent=agent_id,
                canonical_doc_id=canonical_doc_id,
                canonical_path=canonical_path)
        if stored["content"] != content:
            return _verdict(
                "ambiguous", "canonical FTS content conflicts with the "
                "learning content: stale collision, left alone",
                learning_id=learning_id, agent=agent_id,
                canonical_doc_id=canonical_doc_id,
                canonical_path=canonical_path)
        alias_stored = c.execute(
            "SELECT content FROM vault_fts WHERE doc_id = ?",
            (alias_doc_id,),
        ).fetchone()
        if (
            alias_stored is not None
            and alias_stored["content"] is not None
            and alias_stored["content"] != content
        ):
            return _verdict(
                "ambiguous", "alias FTS content conflicts with the learning "
                "content; left alone",
                learning_id=learning_id, agent=agent_id,
                canonical_doc_id=canonical_doc_id,
                canonical_path=canonical_path)
    else:
        return _verdict(
            "ambiguous", "vault_fts table absent: content evidence "
            "unavailable, left alone",
            learning_id=learning_id, agent=agent_id,
            canonical_doc_id=canonical_doc_id, canonical_path=canonical_path)
    return _verdict(
        "migrate_candidate",
        f"provable 1:1: live learning {learning_id} owned by {agent_id}, "
        "exact join to the canonical node at the durable address of that "
        "exact (agent, content) with matching FTS evidence, no other "
        "claimants",
        proposal=f"copy alias out-edges to canonical doc {canonical_doc_id} "
        f"and mark alias doc {alias_doc_id} superseded",
        learning_id=learning_id, agent=agent_id,
        canonical_doc_id=canonical_doc_id, canonical_path=canonical_path)


def format_legacy_alias_report(diag: LegacyAliasDiagnostic) -> str:
    """Human-readable rendering of a legacy-alias diagnostic."""
    lines = [
        "Legacy learning:// alias diagnostic (read-only, P2.3):",
        f"scanned={diag.scanned} candidates={diag.candidates} "
        f"ambiguous={diag.ambiguous} malformed={diag.malformed}"
        + (" TRUNCATED" if diag.truncated else ""),
    ]
    for finding in diag.findings:
        if finding.kind == "migrate_candidate":
            lines.append(
                f"CANDIDATE alias doc {finding.alias_doc_id} "
                f"({finding.alias_path}) owner {finding.agent} -> canonical "
                f"doc {finding.canonical_doc_id} ({finding.canonical_path}): "
                f"{finding.out_edge_count} out-edge(s), "
                f"{finding.in_edge_count} in-edge(s). "
                f"Proposal: {finding.proposal}.")
        elif finding.kind == "malformed":
            lines.append(
                f"MALFORMED alias doc {finding.alias_doc_id} "
                f"({finding.alias_path}): {finding.reason}.")
        else:
            lines.append(
                f"AMBIGUOUS alias doc {finding.alias_doc_id} "
                f"({finding.alias_path}): {finding.reason}.")
    lines.append(
        "Limitations: no migration was performed and none is proposed "
        "automatically; N:1 canonical aggregates, restricted/retired "
        "memories, owner mismatches, missing/conflicting content evidence, "
        "and malformed aliases are reported, never merged; identity rests "
        "on the live learning row plus the durable (agent, content) address "
        "with matching FTS evidence and an exact join row, not on titles "
        "or excerpts.")
    return "\n".join(lines)


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

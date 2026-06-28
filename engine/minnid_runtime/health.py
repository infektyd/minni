import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from config import DEFAULT_CONFIG
from db import SovereignDB


logger = logging.getLogger("minnid")


@dataclass(frozen=True)
class HealthContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    guard_vault_root: Callable[..., Optional[dict]]
    latency_snapshot: Callable[[], dict]
    metrics_snapshot: Callable[[], dict]
    afm_loop_enabled: Callable[[Any], bool]
    increment_request_count: Callable[[], None] | None = None
    request_count: Callable[[], int] = lambda: 0
    start_time: Callable[[], float] = lambda: time.time()
    version: str = "unknown"
    sovereign_db: Callable[..., Any] = SovereignDB
    default_config: Any = field(default_factory=lambda: DEFAULT_CONFIG)
    logger: logging.Logger = logger


def faiss_cache_status(config=DEFAULT_CONFIG) -> tuple[Path, bool]:
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

    uptime = time.time() - context.start_time()
    metrics = context.metrics_snapshot()
    return context.make_response({
        "daemon": {
            "version": context.version,
            "uptime_seconds": round(uptime, 1),
            "requests_served": context.request_count(),
            "socket_path": "[redacted]",
            "latencies": context.latency_snapshot(),
            "errors": metrics.get("errors", 0),
            "counters": metrics,
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


def handle_health_report(params: dict, request_id: Any, context: HealthContext) -> dict:
    """Return deeper read-only memory health diagnostics."""
    now = time.time()
    stale_cutoff = now - (30 * 24 * 60 * 60)
    report = {
        "stale_docs": [],
        "never_recalled": [],
        "contradicting_learnings": [],
        "vector_backend_lag": [],
        "faiss_cache_age_seconds": faiss_cache_age_seconds(context.default_config),
        "afm_loop": {
            "last_run_per_pass": {},
            "drafts_pending": 0,
            "drafts_pending_oldest": None,
            "afm_latency_p95": 0.0,
            "status": "disabled" if not context.afm_loop_enabled(context.default_config) else "ok",
        },
    }

    db = None
    try:
        try:
            from afm_writer import writer_status

            report["afm_loop"] = writer_status(context.default_config.vault_path)
            if not context.afm_loop_enabled(context.default_config):
                report["afm_loop"]["status"] = "disabled"
        except Exception as exc:
            report["afm_loop"]["status"] = "degraded"
            report["afm_loop"]["error"] = str(exc)

        db = context.sovereign_db(context.default_config)
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
    except Exception as exc:
        context.logger.warning("health_report degraded: %s", exc)
        report["error"] = str(exc)
    finally:
        if db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass

    return context.make_response(report, request_id)


def handle_hygiene_report(params: dict, request_id: Any, context: HealthContext) -> dict:
    """Run read-only vault/wiki hygiene checks and return JSON summary."""
    vault_path = params.get("vault") or params.get("vault_path") or context.default_config.vault_path
    err = context.guard_vault_root(params, vault_path, request_id, label="hygiene")
    if err:
        return err
    try:
        from hygiene import run_hygiene_report

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

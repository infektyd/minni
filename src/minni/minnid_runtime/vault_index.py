import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


logger = logging.getLogger("minnid")

MAX_VAULT_PAGE_CHARS = 256 * 1024


@dataclass(frozen=True)
class VaultIndexContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    make_mismatch_error: Callable[[str, str, Any], dict]
    handler_principal: Callable[..., tuple[Any, Optional[dict]]]
    guard_vault_root: Callable[..., Optional[dict]]
    lazy_retrieval: Callable[[], Any]
    agent_vault: Callable[[str], tuple[Path, bool]]
    record_latency: Callable[[str, float], None]
    increment_request_count: Callable[[], None] | None = None
    logger: logging.Logger = logger


def handle_vault_index_doc(params: dict, request_id: Any, context: VaultIndexContext) -> dict:
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
    if context.increment_request_count is not None:
        context.increment_request_count()
    started_at = time.perf_counter()

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err

    content = params.get("content", "")
    path = params.get("path", "")
    wire_agent = str(params.get("agent", "")).strip()
    if wire_agent and wire_agent != principal.agent_id:
        return context.make_mismatch_error(wire_agent, principal.agent_id, request_id)
    agent = principal.agent_id

    if not content:
        return context.make_error(-32602, "content is required", request_id)
    if not path:
        return context.make_error(-32602, "path is required", request_id)

    rel_path = str(path).replace("\\", "/").lstrip("/")
    if not rel_path or ".." in Path(rel_path).parts:
        return context.make_error(-32602, "path must be a relative path within the vault", request_id)

    vault_path = params.get("vault_path")
    if not vault_path:
        vault_path, _ = context.agent_vault(principal.agent_id)
    vault_path = str(vault_path)
    full_path = Path(vault_path).resolve() / rel_path
    root_err = context.guard_vault_root(params, full_path, request_id, label="vault_index_doc")
    if root_err:
        return root_err

    # SEC-015-style size cap: vault pages capped at 256 KiB (larger than learn
    # which is 64 KiB, because vault pages include rich frontmatter + prose).
    if len(content) > MAX_VAULT_PAGE_CHARS:
        return context.make_error(
            -32602,
            f"vault_index_doc content exceeds {MAX_VAULT_PAGE_CHARS} chars",
            request_id,
        )

    sigil = str(params.get("sigil", "❓"))
    privacy_level = str(params.get("privacy_level", "safe"))
    page_status = str(params.get("page_status", "accepted"))
    layer = str(params.get("layer", "knowledge"))

    from minni.indexer import (
        UNEMBEDDED_STATUSES,
        extract_explicit_frontmatter_status,
        is_vault_path_excluded,
    )

    def _retract_existing_if_any() -> bool:
        try:
            eng = context.lazy_retrieval()
            db_obj = getattr(eng, "db", None) if eng is not None else None
            if db_obj is None:
                return False
            retracted_any = False
            with db_obj.transaction() as c:
                for p_candidate in {str(full_path), str(rel_path)}:
                    rows = c.execute(
                        "SELECT doc_id FROM documents WHERE path = ?",
                        (p_candidate,),
                    ).fetchall()
                    for r in rows:
                        did = int(r["doc_id"] if hasattr(r, "keys") else r[0])
                        c.execute("DELETE FROM vault_fts WHERE doc_id = ?", (did,))
                        c.execute("DELETE FROM chunk_embeddings WHERE doc_id = ?", (did,))
                        c.execute(
                            "DELETE FROM memory_links WHERE source_doc_id = ? OR target_doc_id = ?",
                            (did, did),
                        )
                        c.execute("DELETE FROM documents WHERE doc_id = ?", (did,))
                        retracted_any = True
            return retracted_any
        except Exception:
            return False

    explicit_status = extract_explicit_frontmatter_status(content)
    path_excluded, path_reason = is_vault_path_excluded(
        full_path,
        root_dir=vault_path,
        explicit_status=explicit_status,
        is_rpc=True,
    )
    if path_excluded:
        retracted = _retract_existing_if_any()
        context.record_latency("vault_index_doc", time.perf_counter() - started_at)
        return context.make_response(
            {
                "status": "skipped",
                "indexed": False,
                "skipped": True,
                "retracted": retracted,
                "reason": "excluded_path",
                "detail": path_reason,
                "chunks": 0,
            },
            request_id,
        )

    effective_status = (
        explicit_status
        if explicit_status is not None
        else str(params.get("page_status") or "accepted").strip().lower()
    )
    if effective_status in UNEMBEDDED_STATUSES:
        retracted = _retract_existing_if_any()
        context.record_latency("vault_index_doc", time.perf_counter() - started_at)
        return context.make_response(
            {
                "status": "skipped",
                "indexed": False,
                "skipped": True,
                "retracted": retracted,
                "reason": f"excluded_status:{effective_status}",
                "chunks": 0,
            },
            request_id,
        )

    try:
        engine = context.lazy_retrieval()
        result = engine.index_durable_document(
            content=content,
            path=rel_path,
            agent=agent,
            sigil=sigil,
            privacy_level=privacy_level,
            page_status=page_status,
            layer=layer,
        )
        context.record_latency("vault_index_doc", time.perf_counter() - started_at)
        return context.make_response(result, request_id)
    except Exception as exc:
        context.logger.exception("vault_index_doc failed")
        return context.make_error(-32000, f"vault_index_doc error: {exc}", request_id)

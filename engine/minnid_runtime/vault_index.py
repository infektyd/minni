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
    """Index a vault page into semantic recall without learn/candidate governance."""
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

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional


logger = logging.getLogger("minnid")


@dataclass(frozen=True)
class AXContext:
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    handler_principal: Callable[..., tuple[Any, Optional[dict]]]
    lazy_writeback: Callable[[], Any]
    logger: logging.Logger = logger


def handle_ax_snapshot_store(params: dict, request_id: Any, context: AXContext) -> dict:
    from minni.ax_memory import AXMemory

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    app_name = str(params.get("app_name", "")).strip()
    tree_json = str(params.get("tree_json", ""))

    if not app_name or not tree_json:
        return context.make_error(-32602, "app_name and tree_json are required", request_id)

    try:
        # Shared daemon handle — per-call SovereignDB() instances leak their
        # thread-local connection (only close() releases it).
        db = context.lazy_writeback().db
        ax = AXMemory(db)
        snapshot_id = ax.add_snapshot(
            agent_id=agent_id,
            app_name=app_name,
            tree_json=tree_json,
            ttl_seconds=params.get("ttl_seconds", 3600),
        )
        return context.make_response({"snapshot_id": snapshot_id}, request_id)
    except Exception as exc:
        context.logger.exception("ax_snapshot_store failed")
        return context.make_error(-32000, f"ax_snapshot_store error: {exc}", request_id)


def handle_ax_snapshot_get(params: dict, request_id: Any, context: AXContext) -> dict:
    from minni.ax_memory import AXMemory

    principal, err = context.handler_principal(params, request_id)
    if err:
        return err
    agent_id = principal.agent_id
    app_name = params.get("app_name")

    try:
        db = context.lazy_writeback().db
        ax = AXMemory(db)
        snapshot = ax.get_latest_snapshot(agent_id=agent_id, app_name=app_name)
        return context.make_response({"snapshot": snapshot}, request_id)
    except Exception as exc:
        context.logger.exception("ax_snapshot_get failed")
        return context.make_error(-32000, f"ax_snapshot_get error: {exc}", request_id)

import asyncio
import inspect
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DispatchContext:
    methods: Mapping[str, Callable[..., Any]]
    recovery_allowed_methods: Collection[str]
    resolve_provenance: Callable[[dict], Any]
    enforce_method_capability: Callable[[str, Any, Any], dict | None]
    make_error: Callable[[int, str, Any], dict]
    make_response: Callable[[Any, Any], dict]
    obs: Any
    logger: logging.Logger


async def dispatch_request(request: dict, context: DispatchContext) -> dict:
    """Route a JSON-RPC request to the correct handler."""
    method_name = request.get("method", "")
    request_id = request.get("id")
    params = request.get("params", {}) or {}

    handler = context.methods.get(method_name)
    if handler is None:
        return context.make_error(
            -32601,
            f"Method not found: {method_name}",
            request_id,
        )

    # Handlers index params with .get(); a positional-array params (JSON-RPC
    # allows `"params": [...]`) would crash them with AttributeError, and
    # recovery-allowed methods (ping/status/...) reach the handler even before
    # identity resolves. Reject non-dict params loudly here so no handler ever
    # sees a list.
    if not isinstance(params, dict):
        return context.make_error(
            -32602,
            "Invalid params: expected a JSON object",
            request_id,
        )

    provenance = context.resolve_provenance(request)
    if (
        provenance.recovery is not None
        and method_name not in context.recovery_allowed_methods
    ):
        return context.make_response(provenance.recovery, request_id)
    # Trusted recovery flag: overwrite any caller-supplied value so a pre-identity
    # client cannot spoof authenticated detail out of recovery-allowed handlers
    # (e.g. health_report path/content redaction keys off this).
    params["_recovery"] = provenance.recovery is not None
    if provenance.principal is not None:
        params.setdefault("_principal", provenance.principal)

    cap_err = context.enforce_method_capability(
        method_name,
        provenance.principal,
        request_id,
    )
    if cap_err is not None:
        return cap_err

    try:
        if inspect.iscoroutinefunction(handler):
            result = await handler(params, request_id)
        else:
            result = await asyncio.to_thread(handler, params, request_id)
    except Exception:
        context.obs.incr("errors")
        context.obs.incr(f"errors.{method_name}")
        context.logger.exception("dispatch failed for method %s", method_name)
        raise

    if isinstance(result, dict) and "error" in result:
        context.obs.incr("errors")
        context.obs.incr(f"errors.{method_name}")
    return result

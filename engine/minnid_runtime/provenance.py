import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from principal import (
    EffectivePrincipal,
    IdentityMismatchError,
    make_capability_denied_error,
    make_mismatch_error,
    resolve_effective_principal,
)

from .rpc import make_error


logger = logging.getLogger("minnid")

RECOVERY_ALLOWED_METHODS = ("ping", "status", "health_report", "hygiene_report")

# P0 principal enforcement: required stamped capability per DB-backed RPC method.
# Methods with bespoke ownership gates (resolve_candidate, resolve_contradiction,
# minni_ack_handoff) are intentionally omitted; they enforce principal binding
# inside the handler. Recovery/diagnostic methods are also omitted.
RPC_CAPABILITY_REQUIREMENTS: Dict[str, str] = {
    "search": "search",
    "feedback": "feedback",
    "trace": "read",
    "expand": "read",
    "sm_drill": "read",
    "sm_export_pack": "export",
    "read": "read",
    "learn": "learn",
    "minni_subscribe_contradictions": "read",
    "log_event": "log_event",
    "daemon.handoff": "handoff",
    "handoff": "handoff",
    "minni_ack_handoff": "handoff",
    "minni_list_pending_handoffs": "handoff",
    "minni_await_handoff": "handoff",
    "stage_candidate": "learn",
    "list_candidates": "learn",
    "ax_snapshot_store": "read",
    "ax_snapshot_get": "read",
    "vault_index_doc": "learn",
    "daemon.compile": "read",
    "daemon.endorse": "govern",
}


def enforce_method_capability(
    method_name: str,
    principal: Optional[EffectivePrincipal],
    request_id: Any,
) -> Optional[dict]:
    """Return a JSON-RPC error when the stamped principal lacks the method cap."""
    if principal is None:
        return None
    required = RPC_CAPABILITY_REQUIREMENTS.get(method_name)
    if not required or principal.can(required):
        return None
    return make_capability_denied_error(
        required,
        method_name,
        request_id,
        principal_id=principal.agent_id,
    )


@dataclass(frozen=True)
class ProvenanceResolution:
    principal: Optional[EffectivePrincipal]
    recovery: Optional[dict]


def recover(
    reason: str,
    caller_ctx: dict,
    *,
    render_mode: str = "machine",
) -> dict | str:
    """Return the single fail-loud route for unresolved provenance.

    Unknown callers get a diagnostic route, never a synthesized/default identity
    and never silence. ``render_mode="machine"`` is the JSON-RPC payload;
    ``"human"`` is for interactive surfaces.
    """
    method = str(caller_ctx.get("method") or "unknown")
    supplied = caller_ctx.get("supplied_agent_id")
    route = {
        "zone": "pre_identity",
        "surface": "diagnostic",
        "allowed_methods": list(RECOVERY_ALLOWED_METHODS),
        "next": "Call status/health_report, then stamp the runtime with MINNI_AGENT_ID and a principals/<agent>.json entry.",
    }
    remediation = [
        "Stamp this runtime surface with MINNI_AGENT_ID.",
        "Create or fix the matching operator-owned principals/<agent>.json file.",
        "Send SIGHUP to minnid or restart it so identity caches reload.",
        f"Use one of the pre-identity diagnostic methods: {', '.join(RECOVERY_ALLOWED_METHODS)}.",
    ]
    if render_mode == "human":
        supplied_msg = f" supplied={supplied!r}" if supplied is not None else ""
        return (
            f"Minni provenance recovery required for method {method!r}{supplied_msg}: "
            f"{reason}. Stamp this runtime with MINNI_AGENT_ID and a matching "
            f"principals/<agent>.json file; diagnostics available via "
            f"{', '.join(RECOVERY_ALLOWED_METHODS)}."
        )
    if render_mode != "machine":
        raise ValueError("render_mode must be 'machine' or 'human'")
    return {
        "ok": False,
        "status": "recovery_required",
        "reason": reason,
        "identity": None,
        "caller": {
            "method": method,
            "supplied_agent_id": supplied,
        },
        "route": route,
        "remediation": remediation,
    }


def provenance_claim(method_name: str, params: dict) -> Optional[str]:
    if method_name in {"daemon.handoff", "handoff"}:
        claim = params.get("from_agent")
    else:
        claim = params.get("agent_id")
    if claim is None:
        return None
    claim = str(claim).strip()
    return claim or None


def resolve_provenance(request: dict, *, transport: str = "uds") -> ProvenanceResolution:
    """Resolve the caller at the daemon gate before any handler runs."""
    method_name = str(request.get("method", ""))
    params = request.get("params", {}) or {}
    if not isinstance(params, dict):
        return ProvenanceResolution(
            principal=None,
            recovery=recover(
                "invalid_params",
                {"method": method_name, "supplied_agent_id": None},
                render_mode="machine",
            ),
        )
    supplied = provenance_claim(method_name, params)
    try:
        principal = resolve_effective_principal(
            supplied_agent_id=supplied,
            transport=transport,
        )
    except IdentityMismatchError as exc:
        return ProvenanceResolution(
            principal=None,
            recovery=recover(
                "identity_mismatch",
                {
                    "method": method_name,
                    "supplied_agent_id": exc.supplied,
                    "stamped_agent_id": exc.stamped,
                },
                render_mode="machine",
            ),
        )
    except Exception as exc:
        return ProvenanceResolution(
            principal=None,
            recovery=recover(
                f"provenance_error: {exc}",
                {"method": method_name, "supplied_agent_id": supplied},
                render_mode="machine",
            ),
        )
    return ProvenanceResolution(principal=principal, recovery=None)


def handler_principal(
    params: dict,
    request_id: Any,
    *,
    claim_key: str = "agent_id",
) -> tuple[Optional[EffectivePrincipal], Optional[dict]]:
    """Return the dispatch-stamped principal, resolving only for direct calls."""
    stamped = params.get("_principal") if isinstance(params, dict) else None
    if isinstance(stamped, EffectivePrincipal):
        return stamped, None
    supplied = params.get(claim_key) if isinstance(params, dict) else None
    try:
        return (
            resolve_effective_principal(supplied_agent_id=supplied, transport="uds"),
            None,
        )
    except IdentityMismatchError as exc:
        return None, make_mismatch_error(exc.supplied, exc.stamped, request_id)


def guard_vault_root(
    params: dict, vault_path: str | Path, request_id: Any, *, label: str = "vault"
) -> Optional[dict]:
    """Resolve stamped principal and check allows_vault_root."""
    principal, err = handler_principal(params, request_id)
    if err:
        return err
    if not principal.allows_vault_root(vault_path):
        try:
            resolved = str(Path(vault_path).resolve())
        except Exception:
            resolved = str(vault_path)[:120]
        logger.warning(
            "vault_root_denied label=%s principal=%s resolved=%s",
            label,
            principal.agent_id,
            resolved[:120],
        )
        return make_error(
            -32003,
            f"vault_root_denied: {label} path outside principal.allowed_vault_roots (realpath + relative check; traversal/symlink escape rejected)",
            request_id,
        )
    return None


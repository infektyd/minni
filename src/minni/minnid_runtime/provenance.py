import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import minni.principal as principal_mod
from minni.principal import (
    EffectivePrincipal,
    IdentityMismatchError,
    make_capability_denied_error,
    make_mismatch_error,
)

from .rpc import make_error


logger = logging.getLogger("minnid")
# Patch target for provenance-gate tests; default resolver comes from principal.py.
resolve_effective_principal = principal_mod.resolve_effective_principal


@dataclass(frozen=True)
class ProvenanceContext:
    """DI surface for provenance resolution (mirrors other minnid_runtime *Context types)."""

    resolve_effective_principal: Callable[..., EffectivePrincipal] = field(
        default_factory=lambda: resolve_effective_principal
    )


RECOVERY_ALLOWED_METHODS = ("ping", "status", "health_report", "hygiene_report")

# P0 principal enforcement: required stamped capability per DB-backed RPC method.
# resolve_candidate keeps a bespoke in-handler gate (literal resolve_candidate /
# govern capability + ownership, see explicitly_allowed_operator) and is
# intentionally omitted here. Recovery/diagnostic methods
# (RECOVERY_ALLOWED_METHODS) are also omitted.
RPC_CAPABILITY_REQUIREMENTS: Dict[str, str] = {
    "search": "search",
    "feedback": "feedback",
    "trace": "read",
    "expand": "read",
    "sm_drill": "read",
    "sm_export_pack": "export",
    "read": "read",
    "learn": "learn",
    # resolve_contradiction writes a new (superseding) learning, so it needs the
    # write capability on top of the in-handler ownership check — a read-only
    # principal that happens to own a learning must not be able to mutate it.
    "resolve_contradiction": "learn",
    "minni_subscribe_contradictions": "read",
    "log_event": "log_event",
    "daemon.handoff": "handoff",
    "handoff": "handoff",
    "minni_ack_handoff": "handoff",
    "minni_list_pending_handoffs": "handoff",
    "minni_await_handoff": "handoff",
    "stage_candidate": "learn",
    "list_candidates": "learn",
    # ax_snapshot_store WRITES an accessibility snapshot — a write op, not read.
    "ax_snapshot_store": "learn",
    "ax_snapshot_get": "read",
    "vault_index_doc": "learn",
    "daemon.compile": "read",
    "daemon.endorse": "govern",
}


def make_reserved_agent_id_error(agent_id: str, method: str, request_id: Any) -> dict:
    """Distinct diagnostic for a wire claim of a reserved operator id (#121 Root B).

    Same fail-closed -32004 code, but the message names the actual cause —
    the reserved agent_id, not a missing grant — and the two operator-side
    ways out (neither of which is wire-controllable)."""
    return make_error(
        -32004,
        f"reserved_agent_id: reserved agent_id {agent_id!r} requires operator "
        f"context for {method!r} — omit agent_id for the zero-config operator "
        "path, or set MINNI_LOCAL_OPERATOR in the daemon environment "
        "(operator-controlled, never wire-supplied)",
        request_id,
    )


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
    # Resolver default-denies stay fail-closed but fail LOUD (#121): report WHY
    # instead of a bare capability_denied that is byte-identical to a
    # provisioned-but-ungranted caller. deny_reason is only ever set by
    # principal.py's default-deny paths, so provisioned callers are unaffected.
    deny_reason = getattr(principal, "deny_reason", None)
    if deny_reason == "unknown_identity":
        # A proper JSON-RPC ERROR, not a success-wrapped recovery envelope:
        # clients that only check for `error` (the shipped plugin included)
        # must fail loudly, never render the denial as an empty success
        # (PR #132 review, P1). error.message carries the human route for old
        # clients; error.data carries the full structured machine route.
        caller_ctx = {"method": method_name, "supplied_agent_id": principal.agent_id}
        return make_error(
            -32004,
            str(recover("unknown_identity", caller_ctx, render_mode="human")),
            request_id,
            data=recover("unknown_identity", caller_ctx, render_mode="machine"),
        )
    if deny_reason == "reserved_agent_id":
        return make_reserved_agent_id_error(principal.agent_id, method_name, request_id)
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
        "Author the matching operator-owned principals/<agent>.json file "
        "(engine/.venv/bin/python engine/tools/author_principals.py --apply "
        "for the shipped agents, or hand-author it) and chmod it 0600.",
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


def resolve_provenance(
    request: dict,
    *,
    transport: str = "uds",
    context: ProvenanceContext | None = None,
) -> ProvenanceResolution:
    """Resolve the caller at the daemon gate before any handler runs."""
    ctx = context or ProvenanceContext()
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
        principal = ctx.resolve_effective_principal(
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
    context: ProvenanceContext | None = None,
) -> tuple[Optional[EffectivePrincipal], Optional[dict]]:
    """Return the dispatch-stamped principal, resolving only for direct calls."""
    ctx = context or ProvenanceContext()
    stamped = params.get("_principal") if isinstance(params, dict) else None
    if isinstance(stamped, EffectivePrincipal):
        return stamped, None
    supplied = params.get(claim_key) if isinstance(params, dict) else None
    try:
        return (
            ctx.resolve_effective_principal(supplied_agent_id=supplied, transport="uds"),
            None,
        )
    except IdentityMismatchError as exc:
        return None, make_mismatch_error(exc.supplied, exc.stamped, request_id)


# G12 (SEC-003): vault root binding guard. Enforces that any vault_path accepted
# from wire (hygiene, compile, endorse, handoff derived) realpath-resolves inside
# the stamped EffectivePrincipal's allowed_vault_roots. Denies .. / symlink escapes.
# Structured error (-32003) + redacted log; no secret leak. Non-strict synthesis
# (G11) preserves prior wide-open behavior until operator ships principals/*.json.
def guard_vault_root(
    params: dict, vault_path: str | Path, request_id: Any, *, label: str = "vault"
) -> Optional[dict]:
    """Resolve stamped principal (honoring G11) and check allows_vault_root.
    Returns JSON-RPC error dict on denial/mismatch, or None if allowed.
    """
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

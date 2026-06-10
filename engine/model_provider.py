"""Model provider protocol for Minni engine call sites.

Two-language mirrored boundary (precedent: test_g03_contract_matrix.py): this
module mirrors plugins/minni/src/providers.ts with identical semantics. Call
sites (hyde, query_expand, afm_passes) route through a ProviderChain instead of
talking to the AFM transport directly, so additional local providers (mlx,
ollama) and an explicitly-gated cloud tier can be added later (P4-P6) without
touching retrieval code again.

Deliberately daemon-free: retrieval call sites must keep working when the
daemon is down, so the chain wraps the same in-process AFM bridge+native
transport that afm_provider.py already owns. With the default AFM-only chain
the wire behavior is byte-identical to the P0 golden contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Dict, List, Literal, Optional, Protocol, runtime_checkable

# Imported as a module (not by value) so tests can monkeypatch the canonical
# afm_provider boundary (invoke_native_afm, _default_bridge_client) and the
# chain picks the patched implementation up at call time.
import afm_provider
from afm_provider import DEFAULT_AFM_CHAT_COMPLETIONS_URL, BridgeClient, resolve_afm_mode

logger = logging.getLogger("sovereign.model_provider")

OperationClass = Literal["retrieval", "prepare", "extraction"]
ProviderTier = Literal["local", "cloud"]

_OPERATION_CLASSES = {"retrieval", "prepare", "extraction"}


@dataclass(frozen=True)
class ChatRequest:
    """A chat-completion request routed through the provider chain.

    `payload` is the OpenAI-compatible chat-completions body (shape frozen by
    the P0 goldens). `native_operation`/`native_payload` carry the AFM native
    helper escape hatch: when the AFM provider runs in native mode it sends
    that envelope verbatim instead of re-shaping the chat body.
    """

    payload: Dict[str, Any]
    operation: OperationClass
    url: Optional[str] = None
    timeout: float = 2.0
    # Optional JSON schema the caller expects the completion to satisfy.
    response_schema: Optional[Dict[str, Any]] = None
    native_operation: Optional[str] = None
    native_payload: Optional[Dict[str, Any]] = None
    mode: Optional[str] = None


@dataclass(frozen=True)
class ProviderResult:
    ok: bool
    data: Dict[str, Any]
    provider: str
    status: str
    error: Optional[str] = None
    fallback_used: bool = False


@runtime_checkable
class ModelProvider(Protocol):
    name: str
    tier: ProviderTier

    def supports(self, operation: OperationClass) -> bool: ...

    def chat(self, request: ChatRequest, client: Optional[BridgeClient] = None) -> ProviderResult: ...


class AfmProvider:
    """Wraps the existing AFM bridge+native transport as the first provider.

    Semantics are identical to afm_provider.afm_chat_completion: native mode
    delegates to the local JSON helper, auto prefers native then falls back to
    the bridge, and every failure is returned as data so callers can downgrade.
    """

    name = "afm"
    tier: ProviderTier = "local"

    def __init__(self, mode: Optional[str] = None):
        self._mode = mode

    def supports(self, operation: OperationClass) -> bool:
        return operation in _OPERATION_CLASSES

    def chat(self, request: ChatRequest, client: Optional[BridgeClient] = None) -> ProviderResult:
        resolved = resolve_afm_mode(request.mode if request.mode is not None else self._mode)
        if resolved == "off":
            return ProviderResult(ok=False, data={}, provider="off", status="off", error="AFM mode is off")

        if resolved in {"native", "auto"}:
            operation = request.native_operation or "chat_completion"
            payload = (
                request.native_payload
                if request.native_payload is not None
                else ({"payload": request.payload} if request.native_operation is None else request.payload)
            )
            native = afm_provider.invoke_native_afm(operation, payload, timeout=request.timeout)
            if native.ok:
                afm_provider.note_afm_generation_success(request.url or DEFAULT_AFM_CHAT_COMPLETIONS_URL, resolved)
            if native.ok or resolved == "native":
                return ProviderResult(
                    ok=native.ok,
                    data=native.data,
                    provider="native",
                    status=native.status,
                    error=native.error,
                )

        # G13 (SEC-004): bridge targets must be loopback or explicitly allowlisted
        # (MINNI_AFM_ALLOWED_TARGETS / MINNI_MODEL_ALLOWED_TARGETS); non-loopback
        # additionally requires HTTPS. Structured denial, no URL echo.
        from config import check_model_target

        decision = check_model_target(request.url or DEFAULT_AFM_CHAT_COMPLETIONS_URL)
        if not decision["allowed"]:
            if decision["reason"] == "https_required":
                error = "afm_target_denied: non-loopback model targets require https"
            else:
                error = "afm_target_denied: target is not loopback-only and not explicitly allowlisted by operator config"
            return ProviderResult(ok=False, data={}, provider="bridge", status="target_denied", error=error)

        bridge_client = client or afm_provider._default_bridge_client
        try:
            data = bridge_client(request.payload, request.url or DEFAULT_AFM_CHAT_COMPLETIONS_URL, request.timeout)
            # Honest health: a successful live call is a generation proof and
            # refreshes the cached probe (symmetric with the failure path below).
            afm_provider.note_afm_generation_success(request.url or DEFAULT_AFM_CHAT_COMPLETIONS_URL, resolved)
            return ProviderResult(
                ok=True,
                data=data if isinstance(data, dict) else {},
                provider="bridge",
                status="ok",
                fallback_used=resolved == "auto",
            )
        except Exception as exc:  # noqa: BLE001 - provider boundary must downgrade
            afm_provider.note_afm_generation_failure(request.url or DEFAULT_AFM_CHAT_COMPLETIONS_URL)
            return ProviderResult(
                ok=False,
                data={},
                provider="bridge",
                status="bridge_unavailable",
                error=str(exc),
                fallback_used=resolved == "auto",
            )

    def native_op(self, operation: str, payload: Dict[str, Any], timeout: float = 2.0) -> ProviderResult:
        """AFM-only native helper operation (e.g. compile_pass_proposals).

        Native-only by contract: bridge/off modes return a non-ok result so
        callers keep their existing returns-empty-in-bridge-mode behavior.
        """
        resolved = resolve_afm_mode(self._mode)
        if resolved not in {"native", "auto"}:
            return ProviderResult(
                ok=False,
                data={},
                provider=resolved if resolved == "off" else "bridge",
                status="native_unsupported",
                error=f"native operation {operation} requires native/auto AFM mode",
            )
        native = afm_provider.invoke_native_afm(operation, payload, timeout=timeout)
        return ProviderResult(
            ok=native.ok,
            data=native.data,
            provider="native",
            status=native.status,
            error=native.error,
        )


@dataclass
class OperationPolicy:
    local_only: bool = False


class ProviderChain:
    """Ordered provider fan-out with per-operation routing policy."""

    def __init__(
        self,
        providers: List[Any],
        operations: Optional[Dict[str, OperationPolicy]] = None,
    ):
        self.providers = list(providers)
        self.operations = dict(operations or {})

    def providers_for(self, operation: OperationClass) -> List[Any]:
        policy = self.operations.get(operation) or OperationPolicy()
        eligible = []
        for provider in self.providers:
            if not provider.supports(operation):
                continue
            if policy.local_only and getattr(provider, "tier", "local") != "local":
                continue
            eligible.append(provider)
        return eligible

    def chat(self, request: ChatRequest, client: Optional[BridgeClient] = None) -> ProviderResult:
        eligible = self.providers_for(request.operation)
        if not eligible:
            return ProviderResult(
                ok=False,
                data={},
                provider="none",
                status="no_provider",
                error=f"no provider eligible for operation {request.operation}",
            )
        last: Optional[ProviderResult] = None
        for provider in eligible:
            last = provider.chat(request, client=client)
            if last.ok:
                return last
        return last  # type: ignore[return-value]

    def native_op(self, operation: str, payload: Dict[str, Any], timeout: float = 2.0) -> ProviderResult:
        for provider in self.providers:
            handler = getattr(provider, "native_op", None)
            if callable(handler):
                return handler(operation, payload, timeout=timeout)
        return ProviderResult(
            ok=False,
            data={},
            provider="none",
            status="native_unsupported",
            error=f"no provider supports native operation {operation}",
        )


def default_provider_chain() -> ProviderChain:
    """Build the configured provider chain.

    P0-P3: the AFM provider is the only implemented backend; configured mlx/
    ollama/cloud entries (P4-P6) are parsed by config but skipped here until
    their transports exist. Retrieval defaults to local-only.
    """
    try:
        from config import load_providers_config

        cfg = load_providers_config()
    except Exception:  # noqa: BLE001 - config must never break retrieval
        cfg = None

    operations: Dict[str, OperationPolicy] = {"retrieval": OperationPolicy(local_only=True)}
    if cfg:
        for name, op_cfg in (cfg.get("operations") or {}).items():
            if name in _OPERATION_CLASSES and isinstance(op_cfg, dict):
                operations[name] = OperationPolicy(local_only=bool(op_cfg.get("localOnly", False)))

    providers: List[Any] = []
    for name in (cfg.get("chain") if cfg else None) or ["afm"]:
        if name == "afm":
            providers.append(AfmProvider())
        else:
            logger.debug("provider %s configured but not implemented yet; skipping", name)
    if not providers:
        providers = [AfmProvider()]
    return ProviderChain(providers, operations)

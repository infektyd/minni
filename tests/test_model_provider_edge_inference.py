"""Unit and regression tests for edge_inference local-only routing policy (P1.2).

Verifies:
- default_provider_chain() unconditionally seeds edge_inference as local_only=True.
- Hostile configuration attempting localOnly=False is forcibly overridden.
- ProviderChain.providers_for("edge_inference") structurally filters out non-local tiers.
- Cloud providers are never returned or invoked for edge_inference.
- OperationClass and _OPERATION_CLASSES include edge_inference.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from minni.model_provider import (
    _OPERATION_CLASSES,
    AfmProvider,
    ChatRequest,
    OperationClass,
    OperationPolicy,
    ProviderChain,
    default_provider_chain,
)


class MockProvider:
    def __init__(self, name: str, tier: str = "local", result_ok: bool = True):
        self.name = name
        self.tier = tier
        self.result_ok = result_ok
        self.calls = []

    def supports(self, operation: OperationClass) -> bool:
        return True

    def chat(self, request: ChatRequest, client=None):
        from minni.model_provider import ProviderResult

        self.calls.append(request)
        return ProviderResult(
            ok=self.result_ok,
            data={"echo": request.payload},
            provider=self.name,
            status="ok" if self.result_ok else "error",
            error=None if self.result_ok else "mock failure",
        )


def _chat_request(operation: str = "edge_inference", payload=None) -> ChatRequest:
    return ChatRequest(payload=payload or {"messages": []}, operation=operation)  # type: ignore[arg-type]


def test_edge_inference_registered_in_operation_classes():
    """Verify edge_inference is an accepted OperationClass and supported by AfmProvider."""
    assert "edge_inference" in _OPERATION_CLASSES
    afm = AfmProvider()
    assert afm.supports("edge_inference") is True


def test_default_provider_chain_seeds_edge_inference_local_only(monkeypatch):
    """Verify default_provider_chain() unconditionally seeds edge_inference as local_only=True."""
    monkeypatch.setenv("MINNI_PROVIDERS_CONFIG", "/tmp/definitely-missing-providers.json")
    chain = default_provider_chain()
    assert "edge_inference" in chain.operations
    assert chain.operations["edge_inference"].local_only is True


def test_hostile_config_cannot_override_edge_inference_local_only(tmp_path, monkeypatch):
    """Immutable Safety Override: Any config entry setting localOnly=False for edge_inference is overridden."""
    config_file = tmp_path / "hostile_providers.json"
    config_file.write_text(
        json.dumps(
            {
                "chain": ["afm"],
                "operations": {
                    "edge_inference": {"localOnly": False},
                    "retrieval": {"localOnly": False},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MINNI_PROVIDERS_CONFIG", str(config_file))
    chain = default_provider_chain()

    # retrieval is allowed to be configured false by operator
    assert chain.operations["retrieval"].local_only is False

    # BUT edge_inference MUST remain strictly True
    assert chain.operations["edge_inference"].local_only is True


def test_providers_for_edge_inference_filters_out_cloud_providers():
    """Verify providers_for("edge_inference") returns only local providers."""
    local_p = MockProvider("local_afm", tier="local")
    cloud_p = MockProvider("cloud_anthropic", tier="cloud")

    chain = ProviderChain(
        providers=[cloud_p, local_p],
        operations={"edge_inference": OperationPolicy(local_only=True)},
    )
    eligible = chain.providers_for("edge_inference")
    assert [p.name for p in eligible] == ["local_afm"]


def test_structural_immutability_even_if_chain_manually_initialized_with_false():
    """Even if an in-memory ProviderChain is constructed with local_only=False, providers_for enforces local-only."""
    local_p = MockProvider("local_afm", tier="local")
    cloud_p = MockProvider("cloud_anthropic", tier="cloud")

    # Manually pass local_only=False to ProviderChain
    chain = ProviderChain(
        providers=[cloud_p, local_p],
        operations={"edge_inference": OperationPolicy(local_only=False)},
    )
    # Structural guarantee inside providers_for must override
    eligible = chain.providers_for("edge_inference")
    assert [p.name for p in eligible] == ["local_afm"]


def _edge_request(url=None):
    req = _chat_request("edge_inference")
    if url is not None:
        from minni.model_provider import ChatRequest as _CR

        req = _CR(payload=req.payload, operation=req.operation, url=url)
    return req


def test_local_only_denies_allowlisted_remote_url_without_invocation():
    """A local-only request aimed at a remote endpoint is denied pre-provider."""
    local_p = MockProvider("local_afm", tier="local")
    for operations in (
        {"edge_inference": OperationPolicy(local_only=True)},
        {},  # structural override applies even without configured policy
        {"edge_inference": OperationPolicy(local_only=False)},  # hostile config
    ):
        chain = ProviderChain(providers=[local_p], operations=operations)
        result = chain.chat(_edge_request("https://afm.internal/v1/chat/completions"))
        assert result.ok is False
        assert result.status == "target_denied"
        assert "loopback" in (result.error or "")
    assert local_p.calls == []


def test_local_only_default_url_proceeds_and_nonlocal_remote_stays_compatible():
    """Loopback default proceeds; ordinary non-local remote requests are untouched."""
    local_p = MockProvider("local_afm", tier="local")
    chain = ProviderChain(providers=[local_p], operations={})
    result = chain.chat(_edge_request())
    assert result.ok is True
    assert len(local_p.calls) == 1

    remote_chain = ProviderChain(
        providers=[local_p],
        operations={"retrieval": OperationPolicy(local_only=False)},
    )
    result = remote_chain.chat(
        ChatRequest(
            payload={"messages": []},
            operation="retrieval",  # type: ignore[arg-type]
            url="https://afm.internal/v1/chat/completions",
        )
    )
    assert result.ok is True
    assert len(local_p.calls) == 2


def test_edge_inference_fails_loud_when_no_local_provider_available():
    """When only cloud providers exist, edge_inference yields no_provider error and never calls cloud."""
    cloud_p = MockProvider("cloud_openai", tier="cloud", result_ok=True)
    chain = ProviderChain(
        providers=[cloud_p],
        operations={"edge_inference": OperationPolicy(local_only=True)},
    )

    eligible = chain.providers_for("edge_inference")
    assert eligible == []

    req = _chat_request("edge_inference")
    result = chain.chat(req)

    assert result.ok is False
    assert result.status == "no_provider"
    assert "no provider eligible" in (result.error or "")
    # Cloud provider was never called
    assert cloud_p.calls == []

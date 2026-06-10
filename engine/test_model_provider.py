"""P2 provider protocol tests (mirror of plugins/minni/tests/providers.test.mjs).

The wire behavior of the default AFM-only chain is frozen separately by the
P0 goldens (test_afm_contract_golden.py); these tests cover the chain itself.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


class FakeProvider:
    def __init__(self, name, tier="local", result=None):
        self.name = name
        self.tier = tier
        self.result = result
        self.calls = []

    def supports(self, operation):
        return True

    def chat(self, request, client=None):
        from model_provider import ProviderResult

        self.calls.append(request)
        result = self.result or {}
        return ProviderResult(
            ok=result.get("ok", False),
            data=result.get("data", {}),
            provider=self.name,
            status=result.get("status", "ok" if result.get("ok") else "error"),
            error=result.get("error"),
        )


def _request(operation="prepare", **overrides):
    from model_provider import ChatRequest

    return ChatRequest(payload={"messages": []}, operation=operation, **overrides)


def test_default_provider_chain_is_afm_only(monkeypatch):
    from model_provider import default_provider_chain

    monkeypatch.setenv("MINNI_PROVIDERS_CONFIG", "/tmp/missing-minni-providers.json")
    chain = default_provider_chain()
    providers = chain.providers_for("prepare")
    assert len(providers) == 1
    assert providers[0].name == "afm"
    assert providers[0].tier == "local"


def test_retrieval_is_local_only_by_default(monkeypatch):
    from model_provider import OperationPolicy, ProviderChain

    cloud = FakeProvider("cloud-x", tier="cloud", result={"ok": True})
    local = FakeProvider("afm", tier="local", result={"ok": True})
    chain = ProviderChain([cloud, local], {"retrieval": OperationPolicy(local_only=True)})

    eligible = chain.providers_for("retrieval")
    assert [p.name for p in eligible] == ["afm"]


def test_chain_returns_first_ok_result_and_stops():
    from model_provider import ProviderChain

    first = FakeProvider("one", result={"ok": True, "data": {"winner": True}})
    second = FakeProvider("two", result={"ok": True})
    chain = ProviderChain([first, second])

    result = chain.chat(_request())
    assert result.ok is True
    assert result.provider == "one"
    assert second.calls == []


def test_chain_falls_through_failures_and_returns_last():
    from model_provider import ProviderChain

    first = FakeProvider("one", result={"ok": False, "error": "boom-one"})
    second = FakeProvider("two", result={"ok": False, "error": "boom-two"})
    chain = ProviderChain([first, second])

    result = chain.chat(_request())
    assert result.ok is False
    assert result.provider == "two"
    assert result.error == "boom-two"
    assert len(first.calls) == 1
    assert len(second.calls) == 1


def test_chain_structured_error_when_no_provider_eligible():
    from model_provider import OperationPolicy, ProviderChain

    cloud = FakeProvider("cloud-x", tier="cloud", result={"ok": True})
    chain = ProviderChain([cloud], {"retrieval": OperationPolicy(local_only=True)})

    result = chain.chat(_request(operation="retrieval"))
    assert result.ok is False
    assert result.provider == "none"
    assert "no provider eligible" in (result.error or "")


def test_afm_provider_off_mode_never_reaches_a_client():
    from model_provider import AfmProvider

    def forbidden(*_args, **_kwargs):
        raise AssertionError("off mode must not call a client")

    result = AfmProvider().chat(_request(mode="off"), client=forbidden)
    assert result.ok is False
    assert result.provider == "off"
    assert result.status == "off"


def test_afm_provider_bridge_delegates_to_injected_client():
    from model_provider import AfmProvider

    seen = []

    def client(payload, url, timeout):
        seen.append({"payload": payload, "url": url, "timeout": timeout})
        return {"choices": []}

    result = AfmProvider().chat(
        _request(mode="bridge", url="http://127.0.0.1:11437/v1/chat/completions", timeout=1.5),
        client=client,
    )
    assert result.ok is True
    assert result.provider == "bridge"
    assert seen[0]["url"] == "http://127.0.0.1:11437/v1/chat/completions"
    assert seen[0]["timeout"] == 1.5


def test_afm_provider_native_op_is_native_only(monkeypatch):
    from model_provider import AfmProvider

    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "bridge")
    result = AfmProvider().native_op("compile_pass_proposals", {"x": 1})
    assert result.ok is False
    assert result.status == "native_unsupported"


def test_chain_native_op_without_capable_provider():
    from model_provider import ProviderChain

    chain = ProviderChain([FakeProvider("fake")])
    result = chain.native_op("compile_pass_proposals", {})
    assert result.ok is False
    assert result.status == "native_unsupported"

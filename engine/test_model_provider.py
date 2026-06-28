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


def test_afm_provider_unset_mode_consults_env(tmp_path, monkeypatch):
    """Unset-mode resolution parity with providers.ts: an omitted request/provider
    mode must consult MINNI_AFM_PROVIDER_MODE (resolve_afm_mode), not default to
    bridge unconditionally."""
    from test_afm_contract_golden import _write_fake_helper

    from model_provider import AfmProvider

    helper, capture = _write_fake_helper(tmp_path, {"ok": True, "data": {"answer": "native answer"}})
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", os.fspath(helper))
    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "native")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("env-resolved native mode must not call the bridge")

    result = AfmProvider().chat(_request(timeout=5.0), client=forbidden)
    assert result.ok is True
    assert result.provider == "native"
    import json as _json

    envelope = _json.loads(capture.read_text(encoding="utf-8"))
    assert envelope["operation"] == "chat_completion"


def test_failed_native_call_invalidates_cached_probe(monkeypatch):
    """P1: 'invalidated on call failure' holds in native mode too (mirror of
    afm.ts callAfmJson noteAfmGenerationFailure)."""
    import afm_provider
    from afm_provider import DEFAULT_AFM_CHAT_COMPLETIONS_URL, note_afm_generation_success
    from model_provider import AfmProvider

    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", "/tmp/missing-native-helper-for-test")
    afm_provider.reset_afm_generation_probe_cache()
    note_afm_generation_success(DEFAULT_AFM_CHAT_COMPLETIONS_URL, "native")
    key = f"native|{DEFAULT_AFM_CHAT_COMPLETIONS_URL}"
    assert key in afm_provider._generation_probe_cache

    result = AfmProvider().chat(_request(mode="native"))
    assert result.ok is False
    assert key not in afm_provider._generation_probe_cache
    afm_provider.reset_afm_generation_probe_cache()


def test_native_op_failure_invalidates_cached_probe(monkeypatch):
    """Finding 4: native_op must carry the same probe-cache accounting as chat —
    a dead native helper must not leave a stale generation_verified=true for
    callers that only use native_op (e.g. compile_pass_proposals)."""
    import afm_provider
    from afm_provider import DEFAULT_AFM_CHAT_COMPLETIONS_URL, note_afm_generation_success
    from model_provider import AfmProvider

    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", "/tmp/missing-native-helper-for-test")
    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "native")
    afm_provider.reset_afm_generation_probe_cache()
    note_afm_generation_success(DEFAULT_AFM_CHAT_COMPLETIONS_URL, "native")
    key = f"native|{DEFAULT_AFM_CHAT_COMPLETIONS_URL}"
    assert key in afm_provider._generation_probe_cache

    result = AfmProvider().native_op("compile_pass_proposals", {"x": 1})
    assert result.ok is False
    assert key not in afm_provider._generation_probe_cache
    afm_provider.reset_afm_generation_probe_cache()


def test_native_op_recoverable_trip_preserves_cached_probe(monkeypatch):
    """PR84-1: a RECOVERABLE green-op trip (context_overflow / guardrail) means
    the helper is alive and generation-capable — it must NOT invalidate the
    generation-verified probe cache. A real (non-recoverable) failure still does."""
    import afm_provider
    from afm_provider import (
        AFMResult,
        DEFAULT_AFM_CHAT_COMPLETIONS_URL,
        note_afm_generation_success,
    )
    from model_provider import AfmProvider

    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "native")
    key = f"native|{DEFAULT_AFM_CHAT_COMPLETIONS_URL}"

    def _seed():
        afm_provider.reset_afm_generation_probe_cache()
        note_afm_generation_success(DEFAULT_AFM_CHAT_COMPLETIONS_URL, "native")
        assert key in afm_provider._generation_probe_cache

    # Recoverable trips keep the probe.
    for kind in ("context_overflow", "guardrail"):
        _seed()
        monkeypatch.setattr(
            afm_provider,
            "invoke_native_afm",
            lambda op, payload, timeout=2.0, _k=kind: AFMResult(
                ok=False, data={"error_kind": _k}, provider="native",
                status="error", error="recoverable trip",
            ),
        )
        result = AfmProvider().native_op("compile_pass_proposals", {"x": 1})
        assert result.ok is False
        assert key in afm_provider._generation_probe_cache, kind

    # A real failure (no/unknown error_kind) still invalidates the probe.
    _seed()
    monkeypatch.setattr(
        afm_provider,
        "invoke_native_afm",
        lambda op, payload, timeout=2.0: AFMResult(
            ok=False, data={}, provider="native", status="error", error="helper dead",
        ),
    )
    result = AfmProvider().native_op("compile_pass_proposals", {"x": 1})
    assert result.ok is False
    assert key not in afm_provider._generation_probe_cache
    afm_provider.reset_afm_generation_probe_cache()


def test_native_op_success_marks_verified_only_with_content(tmp_path, monkeypatch):
    """Finding 4 + finding 1 consistency: native_op success-marking respects the
    same content check as the probe — a contentless ok is neutral."""
    from test_afm_contract_golden import _write_fake_helper

    import afm_provider
    from afm_provider import DEFAULT_AFM_CHAT_COMPLETIONS_URL
    from model_provider import AfmProvider

    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "native")
    afm_provider.reset_afm_generation_probe_cache()
    key = f"native|{DEFAULT_AFM_CHAT_COMPLETIONS_URL}"

    # Contentless ok (typical compile-style op): neutral, no verified entry.
    helper, _ = _write_fake_helper(tmp_path, {"ok": True, "data": {"proposals": []}})
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", os.fspath(helper))
    hollow = AfmProvider().native_op("compile_pass_proposals", {}, timeout=10.0)
    assert hollow.ok is True
    assert key not in afm_provider._generation_probe_cache

    # Completion-shaped ok: counts as a generation proof.
    helper2, _ = _write_fake_helper(tmp_path, {"ok": True, "data": {"answer": "y"}})
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", os.fspath(helper2))
    verified = AfmProvider().native_op("chat_completion", {}, timeout=10.0)
    assert verified.ok is True
    assert key in afm_provider._generation_probe_cache
    afm_provider.reset_afm_generation_probe_cache()


def test_hollow_bridge_success_does_not_poison_probe_cache(monkeypatch):
    """Finding 1 mirror at the model_provider boundary: an HTTP-2xx body with
    no completion content must not mark the probe cache verified."""
    import afm_provider
    from afm_provider import DEFAULT_AFM_CHAT_COMPLETIONS_URL
    from model_provider import AfmProvider

    afm_provider.reset_afm_generation_probe_cache()
    result = AfmProvider().chat(_request(mode="bridge"), client=lambda *_a: {"status": "ok"})
    assert result.ok is True
    assert f"bridge|{DEFAULT_AFM_CHAT_COMPLETIONS_URL}" not in afm_provider._generation_probe_cache
    afm_provider.reset_afm_generation_probe_cache()


def test_chain_sanitizes_provider_errors_structurally():
    """SEC (P3): secret hygiene at the chain boundary, not per call site —
    a provider error embedding auth material is redacted before it can reach
    audit/inbox/status surfaces (mirror of providers.ts sanitizeChainResult)."""
    from model_provider import ProviderChain

    secret = "sk-test-vErYsEcReT-cLoUdKeY-12345"
    leaky = FakeProvider(
        "leaky",
        result={"ok": False, "error": f"HTTP 401 authorization: Bearer {secret} x-api-key={secret}"},
    )
    result = ProviderChain([leaky]).chat(_request())
    assert result.ok is False
    assert secret not in (result.error or "")
    assert "[redacted" in (result.error or "")

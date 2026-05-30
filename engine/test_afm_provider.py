"""AFM provider boundary tests.

The provider layer keeps native/bridge/off selection out of retrieval call sites
while preserving deterministic downgrade behavior on machines without native
Foundation Models support.
"""

import os
import sys
import stat

sys.path.insert(0, os.path.dirname(__file__))


def test_afm_chat_completion_off_skips_client():
    from afm_provider import afm_chat_completion

    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("off mode must not call an AFM endpoint")

    result = afm_chat_completion(
        {"messages": []},
        mode="off",
        client=forbidden_client,
    )

    assert result.ok is False
    assert result.provider == "off"
    assert result.status == "off"


def test_afm_chat_completion_native_reports_unavailable_without_helper(monkeypatch):
    from afm_provider import afm_chat_completion

    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", "/tmp/missing-sovereign-native-afm-helper")

    result = afm_chat_completion({"messages": []}, mode="native")

    assert result.ok is False
    assert result.provider == "native"
    assert result.status == "native_unavailable"
    assert "helper" in result.error


def test_afm_chat_completion_auto_uses_bridge_when_native_unavailable(monkeypatch):
    from afm_provider import afm_chat_completion

    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", "/tmp/missing-sovereign-native-afm-helper")
    calls = []

    def bridge_client(payload, url, timeout):
        calls.append((payload, url, timeout))
        return {"choices": [{"message": {"content": "bridge ok"}}]}

    result = afm_chat_completion(
        {"messages": [{"role": "user", "content": "ping"}]},
        mode="auto",
        client=bridge_client,
    )

    assert result.ok is True
    assert result.provider == "bridge"
    assert result.status == "ok"
    assert result.fallback_used is True
    assert calls


def test_afm_runtime_status_prefers_unified_provider_mode(monkeypatch):
    from afm_provider import afm_runtime_status

    monkeypatch.setenv("MINNI_AFM_MODE", "bridge")
    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "off")

    status = afm_runtime_status()

    assert status["mode"] == "off"
    assert status["status"] == "off"


def test_afm_runtime_status_reports_adapter_without_path(monkeypatch):
    from afm_provider import afm_runtime_status

    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setenv("MINNI_AFM_ADAPTER_PATH", "/Users/alice/private/extractor.fmadapter")

    status = afm_runtime_status()

    assert status["adapter_configured"] is True
    assert "adapter_path" not in status
    assert "/Users/alice" not in repr(status)


def test_afm_runtime_status_reports_missing_native_helper(monkeypatch):
    from afm_provider import afm_runtime_status

    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "native")
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", "/tmp/missing-sovereign-native-afm-helper")

    status = afm_runtime_status()

    assert status["status"] == "native_helper_missing"
    assert status["native_available"] is False


def test_afm_runtime_status_checks_native_helper_health(monkeypatch, tmp_path):
    from afm_provider import afm_runtime_status

    helper = tmp_path / "native-afm-helper"
    helper.write_text(
        "\n".join([
            "#!/usr/bin/env python3",
            "import json",
            "request = json.load(open(0))",
            "assert request['operation'] == 'health'",
            "print(json.dumps({",
            "  'ok': False,",
            "  'provider': 'native',",
            "  'backend': 'apple-foundation-models',",
            "  'availability': 'unavailable',",
            "  'error': 'model assets unavailable'",
            "}))",
        ]),
        encoding="utf-8",
    )
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "native")
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", os.fspath(helper))

    status = afm_runtime_status()

    assert status["status"] == "native_unavailable"
    assert status["native_available"] is False
    assert status["native_health"] == "foundation_models_unavailable"
    assert status["backend"] == "apple-foundation-models"
    assert status["availability"] == "unavailable"


def test_afm_runtime_status_reports_available_native_health(monkeypatch, tmp_path):
    from afm_provider import afm_runtime_status

    helper = tmp_path / "native-afm-helper"
    helper.write_text(
        "\n".join([
            "#!/usr/bin/env python3",
            "import json",
            "request = json.load(open(0))",
            "assert request['operation'] == 'health'",
            "print(json.dumps({",
            "  'ok': True,",
            "  'provider': 'native',",
            "  'backend': 'apple-foundation-models',",
            "  'availability': 'available',",
            "  'data': {'status': 'ok'}",
            "}))",
        ]),
        encoding="utf-8",
    )
    helper.chmod(helper.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "native")
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", os.fspath(helper))

    status = afm_runtime_status()

    assert status["status"] == "native_available"
    assert status["native_available"] is True
    assert status["native_health"] == "available"
    assert status["backend"] == "apple-foundation-models"
    assert status["availability"] == "available"

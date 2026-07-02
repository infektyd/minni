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


def test_default_bridge_client_rejects_oversized_response(monkeypatch):
    """R7: the AFM bridge is an unauthenticated loopback endpoint with no
    response cap otherwise — a port-squatter (or a misbehaving real AFM
    bridge) could stream an unbounded body and exhaust memory. The default
    client must refuse to buffer a response larger than
    _AFM_BRIDGE_MAX_RESPONSE_BYTES instead of reading it in full.

    Simulates an actual streaming socket.read(n)-style response object (only
    ever returns up to n bytes, like a real http.client response) so this
    only passes if the client itself calls read() with a bounded size — a
    read() with no/negative size (the pre-fix behavior) would stream the
    entire multi-terabyte body from this fake before ever reaching a cap
    check, which is exactly the DoS this closes.
    """
    import afm_provider

    total_size = afm_provider._AFM_BRIDGE_MAX_RESPONSE_BYTES * 4

    class _UnboundedStreamResp:
        """Mimics a real streaming socket response: read(-1)/read() drains
        the WHOLE stream (here: emulated as a large-but-finite body), while
        read(n>0) returns at most n bytes per call."""

        def __init__(self):
            self._remaining = total_size

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            if n is None or n < 0:
                n = self._remaining
            chunk = b"x" * min(n, self._remaining)
            self._remaining -= len(chunk)
            return chunk

    monkeypatch.setattr(
        afm_provider.urllib.request, "urlopen", lambda *a, **k: _UnboundedStreamResp()
    )

    try:
        afm_provider._default_bridge_client(
            {"messages": []}, "http://127.0.0.1:11437/v1/chat/completions", 2.0
        )
        raised_msg = None
    except ValueError as exc:
        raised_msg = str(exc)

    assert raised_msg is not None, "an oversized bridge response must raise, not buffer"
    assert "exceeded" in raised_msg and "refusing to buffer" in raised_msg, raised_msg


def test_default_bridge_client_allows_response_within_cap(monkeypatch):
    """R7 companion: a normal-sized response is unaffected by the cap."""
    import json as _json
    import afm_provider

    body = _json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode("utf-8")

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return body[:n] if n and n > 0 else body

    monkeypatch.setattr(
        afm_provider.urllib.request, "urlopen", lambda *a, **k: _FakeResp()
    )

    result = afm_provider._default_bridge_client(
        {"messages": []}, "http://127.0.0.1:11437/v1/chat/completions", 2.0
    )
    assert result["choices"][0]["message"]["content"] == "ok"


def test_generation_probe_timeout_env_falls_back_for_invalid_values(monkeypatch):
    from afm_provider import _generation_probe_timeout_seconds

    monkeypatch.setenv("MINNI_AFM_PROBE_TIMEOUT", "not-a-float")
    assert _generation_probe_timeout_seconds() == 10.0

    monkeypatch.setenv("MINNI_AFM_PROBE_TIMEOUT", "-1")
    assert _generation_probe_timeout_seconds() == 10.0

    monkeypatch.setenv("MINNI_AFM_PROBE_TIMEOUT", "2.5")
    assert _generation_probe_timeout_seconds() == 2.5


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

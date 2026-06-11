"""P1 honest health tests for the Python AFM boundary.

Mirrors plugins/minni/tests/afm-health.test.mjs: afm_runtime_status's ok /
generation_verified require a verified 1-token completion within the probe TTL;
a reachable HTTP endpoint alone never counts.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


GENERATION_ALIVE = {"choices": [{"message": {"content": "y"}}]}


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    from afm_provider import reset_afm_generation_probe_cache

    reset_afm_generation_probe_cache()
    yield
    reset_afm_generation_probe_cache()


def _alive_client(calls=None):
    def client(payload, url, timeout):
        if calls is not None:
            calls.append({"payload": payload, "url": url, "timeout": timeout})
        return GENERATION_ALIVE

    return client


def _dead_client(payload, url, timeout):
    raise RuntimeError("urlopen error [Errno 61] Connection refused")


def test_gate_generation_dead_means_not_ok(monkeypatch):
    from afm_provider import afm_runtime_status

    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "bridge")
    status = afm_runtime_status(probe_client=_dead_client)

    assert status["status"] == "bridge"
    assert status["ok"] is False
    assert status["generation_verified"] is False


def test_gate_working_generation_means_ok(monkeypatch):
    from afm_provider import afm_runtime_status

    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "bridge")
    calls = []
    status = afm_runtime_status(probe_client=_alive_client(calls))

    assert status["ok"] is True
    assert status["generation_verified"] is True
    assert status["probe_age_ms"] >= 0
    assert len(calls) == 1
    # The probe is a real (1-token) completion, not a /health GET.
    assert calls[0]["payload"]["max_tokens"] == 1
    assert calls[0]["payload"]["messages"] == [{"role": "user", "content": "ok"}]


def test_off_mode_reports_unverified_without_probing(monkeypatch):
    from afm_provider import afm_runtime_status

    monkeypatch.setenv("MINNI_AFM_PROVIDER_MODE", "off")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("off mode must not probe")

    status = afm_runtime_status(probe_client=forbidden)
    assert status["ok"] is False
    assert status["generation_verified"] is False
    assert status["probe_age_ms"] == 0


def test_verify_afm_generation_caches_within_ttl():
    from afm_provider import verify_afm_generation

    calls = []
    clock = {"t": 1_000_000.0}

    def now():
        return clock["t"]

    first = verify_afm_generation("bridge", client=_alive_client(calls), now=now)
    clock["t"] += 60.0
    second = verify_afm_generation("bridge", client=_alive_client(calls), now=now)

    assert first["ok"] is True
    assert first["probe_age_ms"] == 0
    assert second["ok"] is True
    assert second["probe_age_ms"] == 60_000
    assert len(calls) == 1


def test_verify_afm_generation_reprobes_after_ttl():
    from afm_provider import verify_afm_generation

    calls = []
    clock = {"t": 1_000_000.0}

    def now():
        return clock["t"]

    verify_afm_generation("bridge", client=_alive_client(calls), now=now)
    clock["t"] += 600.0  # past the 300s TTL
    refreshed = verify_afm_generation("bridge", client=_alive_client(calls), now=now)

    assert len(calls) == 2
    assert refreshed["probe_age_ms"] == 0


def test_note_afm_generation_failure_invalidates_cache():
    from afm_provider import (
        DEFAULT_AFM_CHAT_COMPLETIONS_URL,
        note_afm_generation_failure,
        verify_afm_generation,
    )

    calls = []
    verify_afm_generation("bridge", client=_alive_client(calls))
    note_afm_generation_failure(DEFAULT_AFM_CHAT_COMPLETIONS_URL)
    verify_afm_generation("bridge", client=_alive_client(calls))

    assert len(calls) == 2


def test_failed_bridge_call_invalidates_cached_probe():
    from afm_provider import afm_chat_completion, verify_afm_generation

    calls = []
    verify_afm_generation("bridge", client=_alive_client(calls))
    assert len(calls) == 1

    failed = afm_chat_completion({"messages": []}, mode="bridge", client=_dead_client)
    assert failed.ok is False

    verify_afm_generation("bridge", client=_alive_client(calls))
    assert len(calls) == 2


def test_generation_detail_is_sanitized():
    from afm_provider import verify_afm_generation

    def leaky_client(payload, url, timeout):
        raise RuntimeError("spawn /Users/alice/private/helper failed for extractor.fmadapter")

    health = verify_afm_generation("bridge", client=leaky_client)
    assert health["ok"] is False
    assert "/Users/alice" not in (health["detail"] or "")


def test_probe_requires_completion_content():
    from afm_provider import verify_afm_generation

    def hollow_client(payload, url, timeout):
        return {"status": "ok"}  # reachable HTTP, but no generated completion

    health = verify_afm_generation("bridge", client=hollow_client)
    assert health["ok"] is False
    assert health["generation_verified"] is False


def test_successful_live_call_refreshes_negative_cached_probe():
    """Symmetric positive signal: a real call succeeding IS a generation proof,
    so recovery from a transient outage must not wait out the negative TTL."""
    from afm_provider import afm_chat_completion, verify_afm_generation

    before = verify_afm_generation("bridge", client=_dead_client)
    assert before["ok"] is False

    live = afm_chat_completion({"messages": []}, mode="bridge", client=_alive_client())
    assert live.ok is True

    def forbidden(payload, url, timeout):
        raise AssertionError("live success refreshed the cache; no re-probe expected")

    after = verify_afm_generation("bridge", client=forbidden)
    assert after["ok"] is True
    assert after["generation_verified"] is True


def test_hollow_200_live_call_must_not_poison_probe_cache():
    """Finding 1 (cache poisoning): a bridge answering 200 {"status": "ok"}
    without completion content must NOT mark generation_verified=true; health
    still runs (and trusts) the real content-checking probe."""
    from afm_provider import afm_chat_completion, verify_afm_generation

    def hollow_client(payload, url, timeout):
        return {"status": "ok"}  # HTTP-2xx parseable JSON, zero choices

    live = afm_chat_completion({"messages": []}, mode="bridge", client=hollow_client)
    assert live.ok is True

    # Health must still run (and trust) the real content-checking probe.
    calls = []
    health = verify_afm_generation("bridge", client=_alive_client(calls))
    assert len(calls) == 1, "hollow 200 must not satisfy the generation probe"
    assert health["ok"] is True

    from afm_provider import reset_afm_generation_probe_cache

    reset_afm_generation_probe_cache()
    afm_chat_completion({"messages": []}, mode="bridge", client=hollow_client)
    poisoned = verify_afm_generation("bridge", client=_dead_client)
    assert poisoned["ok"] is False, "afm_ok must come from the real probe, not the hollow live call"
    assert poisoned["generation_verified"] is False


def test_hollow_200_live_call_is_neutral_for_verified_cache():
    """A contentless ok is neutral: it must not invalidate a verified probe."""
    from afm_provider import afm_chat_completion, verify_afm_generation

    calls = []
    verify_afm_generation("bridge", client=_alive_client(calls))
    assert len(calls) == 1

    afm_chat_completion({"messages": []}, mode="bridge", client=lambda *_a: {"status": "ok"})

    after = verify_afm_generation("bridge", client=_alive_client(calls))
    assert after["ok"] is True
    assert len(calls) == 1, "neutral signal must not force a re-probe"


# --- finding 3: cross-process persistent probe cache (mirror of afm.ts) --------


_PROBE_CACHE_KEY = "bridge|http://127.0.0.1:11437/v1/chat/completions"


def _persisted_cache_body(probed_at_ms, generation_verified=True):
    import json

    return json.dumps(
        {
            "version": 1,
            "entries": {
                _PROBE_CACHE_KEY: {
                    "reachable": True,
                    "generation_verified": generation_verified,
                    "detail": None,
                    "probed_at_ms": probed_at_ms,
                }
            },
        }
    )


def _forbidden_client(payload, url, timeout):
    raise AssertionError("warm persistent cache must skip the live probe")


def test_persistent_cache_warm_file_skips_live_probe(tmp_path, monkeypatch):
    import time

    import afm_provider
    from afm_provider import verify_afm_generation

    cache_file = tmp_path / "afm-probe-cache.json"
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", os.fspath(cache_file))
    cache_file.write_text(_persisted_cache_body(time.time() * 1000.0 - 1000.0), encoding="utf-8")
    afm_provider._generation_probe_cache.clear()  # simulate a fresh process

    health = verify_afm_generation("bridge", client=_forbidden_client)
    assert health["ok"] is True
    assert health["generation_verified"] is True


def test_persistent_cache_stale_file_forces_probe(tmp_path, monkeypatch):
    import time

    import afm_provider
    from afm_provider import verify_afm_generation

    cache_file = tmp_path / "afm-probe-cache.json"
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", os.fspath(cache_file))
    cache_file.write_text(_persisted_cache_body(time.time() * 1000.0 - 600_000.0), encoding="utf-8")
    afm_provider._generation_probe_cache.clear()

    calls = []
    health = verify_afm_generation("bridge", client=_alive_client(calls))
    assert len(calls) == 1, "stale file entry must re-probe"
    assert health["ok"] is True
    assert health["probe_age_ms"] < 60_000


def test_persistent_cache_corrupt_file_is_ignored(tmp_path, monkeypatch):
    import afm_provider
    from afm_provider import verify_afm_generation

    cache_file = tmp_path / "afm-probe-cache.json"
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", os.fspath(cache_file))
    cache_file.write_text("{not json", encoding="utf-8")
    afm_provider._generation_probe_cache.clear()

    calls = []
    health = verify_afm_generation("bridge", client=_alive_client(calls))
    assert len(calls) == 1, "corrupt file degrades to a normal probe"
    assert health["ok"] is True


def test_persistent_cache_probe_persists_result_for_next_process(tmp_path, monkeypatch):
    import json

    import afm_provider
    from afm_provider import verify_afm_generation

    cache_file = tmp_path / "afm-probe-cache.json"
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", os.fspath(cache_file))

    verify_afm_generation("bridge", client=_alive_client())
    persisted = json.loads(cache_file.read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert persisted["entries"][_PROBE_CACHE_KEY]["generation_verified"] is True
    assert isinstance(persisted["entries"][_PROBE_CACHE_KEY]["probed_at_ms"], (int, float))

    # The next "process" (cleared in-memory map) reuses the persisted probe.
    afm_provider._generation_probe_cache.clear()
    health = verify_afm_generation("bridge", client=_forbidden_client)
    assert health["ok"] is True


def test_persistent_cache_failed_live_call_invalidates_file_entry(tmp_path, monkeypatch):
    import json
    import time

    import afm_provider
    from afm_provider import afm_chat_completion

    cache_file = tmp_path / "afm-probe-cache.json"
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", os.fspath(cache_file))
    cache_file.write_text(_persisted_cache_body(time.time() * 1000.0 - 1000.0), encoding="utf-8")
    afm_provider._generation_probe_cache.clear()

    failed = afm_chat_completion({"messages": []}, mode="bridge", client=_dead_client)
    assert failed.ok is False
    persisted = json.loads(cache_file.read_text(encoding="utf-8"))
    assert _PROBE_CACHE_KEY not in persisted["entries"]


# --- native-mode generation health (mirror of afm-health.test.mjs) -------------


def test_native_generation_probe_verifies_on_content(tmp_path, monkeypatch):
    from test_afm_contract_golden import _write_fake_helper

    from afm_provider import verify_afm_generation

    helper, _ = _write_fake_helper(tmp_path, {"ok": True, "data": {"answer": "y"}})
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", os.fspath(helper))

    health = verify_afm_generation("native", timeout=10.0)
    assert health["ok"] is True
    assert health["generation_verified"] is True


def test_native_generation_probe_rejects_ok_without_content(tmp_path, monkeypatch):
    from test_afm_contract_golden import _write_fake_helper

    from afm_provider import verify_afm_generation

    helper, _ = _write_fake_helper(tmp_path, {"ok": True, "data": {}})
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", os.fspath(helper))

    health = verify_afm_generation("native", timeout=10.0)
    assert health["ok"] is False, "ok with empty output proves nothing about generation"
    assert health["generation_verified"] is False


def test_native_generation_probe_rejects_helper_failure(tmp_path, monkeypatch):
    from test_afm_contract_golden import _write_fake_helper

    from afm_provider import verify_afm_generation

    helper, _ = _write_fake_helper(tmp_path, {"ok": False, "error": "model unavailable"})
    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", os.fspath(helper))

    health = verify_afm_generation("native", timeout=10.0)
    assert health["ok"] is False
    assert health["generation_verified"] is False


def test_native_generation_probe_missing_helper(monkeypatch):
    from afm_provider import verify_afm_generation

    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", "/tmp/missing-native-health-helper")
    health = verify_afm_generation("native")
    assert health["ok"] is False
    assert health["generation_verified"] is False


def test_auto_mode_health_falls_back_to_bridge_probe(monkeypatch):
    from afm_provider import verify_afm_generation

    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", "/tmp/missing-native-health-helper")
    calls = []
    health = verify_afm_generation("auto", client=_alive_client(calls))
    assert health["ok"] is True
    assert len(calls) == 1
    # Auto resolved to bridge: the probe body is the chat payload, not the
    # wrapped native envelope.
    assert "payload" not in calls[0]["payload"]
    assert calls[0]["payload"]["max_tokens"] == 1


def test_failed_native_call_invalidates_cached_probe(monkeypatch):
    """P1: 'invalidated on call failure' holds in pure native mode (mirror of
    afm.ts callAfmJson noteAfmGenerationFailure on the native path)."""
    import afm_provider
    from afm_provider import (
        DEFAULT_AFM_CHAT_COMPLETIONS_URL,
        afm_chat_completion,
        note_afm_generation_success,
    )

    monkeypatch.setenv("MINNI_AFM_NATIVE_HELPER", "/tmp/missing-native-helper-for-test")
    note_afm_generation_success(DEFAULT_AFM_CHAT_COMPLETIONS_URL, "native")
    key = f"native|{DEFAULT_AFM_CHAT_COMPLETIONS_URL}"
    assert key in afm_provider._generation_probe_cache

    failed = afm_chat_completion({"messages": []}, mode="native")
    assert failed.ok is False
    assert key not in afm_provider._generation_probe_cache

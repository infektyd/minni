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

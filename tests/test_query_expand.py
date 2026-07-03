import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


class _AFMResult:
    def __init__(self, ok, data, status="native_available", error=None):
        self.ok = ok
        self.data = data
        self.status = status
        self.error = error


def test_summarize_with_afm_chunks_oversized_prompt(monkeypatch):
    import minni.query_expand as query_expand

    call_count = {"n": 0}

    def fake_native(operation, payload, timeout=2.0):
        assert operation == "neighborhood_summary"
        call_count["n"] += 1
        prompt = payload.get("prompt", "")
        if "chunk marker" in prompt:
            return _AFMResult(True, {"summary": f"partial summary {call_count['n']}"})
        return _AFMResult(True, {"summary": "final synthesized summary"})

    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("minni.afm_provider.invoke_native_afm", fake_native)

    long_prompt = "chunk marker filler text. " * 1000  # far over budget
    summary = query_expand.summarize_with_afm(long_prompt, timeout=1.5)

    assert call_count["n"] > 1
    assert summary == "final synthesized summary"


def test_summarize_with_afm_unchanged_for_short_prompt(monkeypatch):
    import minni.query_expand as query_expand

    call_count = {"n": 0}

    def fake_native(operation, payload, timeout=2.0):
        call_count["n"] += 1
        return _AFMResult(True, {"summary": "short summary"})

    monkeypatch.setenv("MINNI_AFM_MODE", "native")
    monkeypatch.setattr("minni.afm_provider.invoke_native_afm", fake_native)

    summary = query_expand.summarize_with_afm("a short prompt", timeout=1.5)

    assert call_count["n"] == 1
    assert summary == "short summary"

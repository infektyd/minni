"""Slice E security regressions: DoS/misc (R7, R9).

R10 (unscoped contradiction count) is covered by updated assertions in
test_correction_reinjection.py; X7 (sovrd lru_cache) is covered by
openclaw-extension/tests/test_content_hash_no_cache.py.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))


# ── R7: HyDE defaults off + AFM bridge response is byte-capped ──────────────


def test_hyde_disabled_by_default():
    """R7: hyde_enabled must default to False so the recall query is not posted
    to the unauthenticated loopback AFM endpoint unless explicitly opted in."""
    from minni.config import SovereignConfig

    assert SovereignConfig().hyde_enabled is False


def test_afm_bridge_client_caps_response_bytes(monkeypatch):
    """R7: the AFM bridge client rejects an oversized response body instead of
    buffering it unboundedly."""
    import minni.afm_provider as afm_provider

    cap = afm_provider._AFM_BRIDGE_MAX_RESPONSE_BYTES

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            # Return one byte past whatever the client asked for → over the cap.
            size = (n if n and n > 0 else cap + 1)
            return b"x" * size

    def _fake_urlopen(req, timeout=None):
        return _FakeResp()

    monkeypatch.setattr(afm_provider.urllib.request, "urlopen", _fake_urlopen)

    with pytest.raises(ValueError, match="exceeded"):
        afm_provider._default_bridge_client({"q": "hi"}, "http://127.0.0.1:11437", 1.0)


# ── R9: backend fan-out is deduped, capped, and rejects unknowns ─────────────


def _engine(tmp_path):
    import minni.db as db_mod
    from minni.config import SovereignConfig
    from minni.db import SovereignDB
    from minni.faiss_index import FAISSIndex
    from minni.retrieval import RetrievalEngine

    cfg = SovereignConfig(db_path=str(tmp_path / "e.db"), reranker_enabled=False)
    old = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old
    return RetrievalEngine(db_obj, cfg, FAISSIndex(cfg))


def test_normalize_backend_names_dedups(tmp_path):
    """R9: a duplicated backend list collapses to one — ['faiss-disk']*5 no
    longer builds five disk-loading backends."""
    engine = _engine(tmp_path)
    assert engine._normalize_backend_names(["faiss-disk"] * 5) == ["faiss-disk"]


def test_normalize_backend_names_rejects_unknown(tmp_path):
    """R9: an unknown backend name is rejected loudly (not silently skipped)."""
    engine = _engine(tmp_path)
    with pytest.raises(ValueError, match="unknown backend"):
        engine._normalize_backend_names(["faiss-disk", "evil-backend"])


def test_normalize_backend_names_caps_distinct(tmp_path):
    """R9: more than _MAX_BACKENDS distinct known members is rejected."""
    engine = _engine(tmp_path)
    # Temporarily widen the known set so we can exceed the cap with valid names.
    original = engine._KNOWN_BACKENDS
    try:
        engine._KNOWN_BACKENDS = ("a", "b", "c", "d", "e")
        with pytest.raises(ValueError, match="too many backends"):
            engine._normalize_backend_names(["a", "b", "c", "d", "e"])
    finally:
        engine._KNOWN_BACKENDS = original

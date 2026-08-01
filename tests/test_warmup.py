"""Daemon model warmup (post-bind, background).

Why this exists: every retrieval model is a first-call ``functools.cache``
singleton, so before warmup the FIRST search after a daemon restart paid the
whole cold load — embedder, cross-encoder, FAISS, plus live HuggingFace
revalidation calls — inside the caller's request. When that caller is a
prompt-time hook on a harness deadline it cannot extend, the turn is lost.

These are unit-level: the loaders are monkeypatched, so no real weights are
loaded and the test stays model-free (it belongs in the fast `make check` set).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import minni.minnid as minnid  # noqa: E402


class _FakeLoader:
    """Records that it was called, and how many times."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return object()


@pytest.fixture
def fake_models(monkeypatch):
    """Inject fake model loaders into the module `_warmup_models` imports from."""
    import minni.models as models

    embedder = _FakeLoader()
    cross_encoder = _FakeLoader()
    monkeypatch.setattr(models, "get_embedder", embedder)
    monkeypatch.setattr(models, "get_cross_encoder", cross_encoder)
    monkeypatch.setattr(minnid, "_lazy_retrieval", _FakeLoader())
    return {"embedder": embedder, "cross_encoder": cross_encoder}


def test_warmup_enabled_by_default(monkeypatch):
    monkeypatch.delenv("MINNI_WARMUP", raising=False)
    assert minnid._warmup_enabled() is True


@pytest.mark.parametrize("value", ["off", "OFF", " Off "])
def test_warmup_disabled_by_env(monkeypatch, value):
    monkeypatch.setenv("MINNI_WARMUP", value)
    assert minnid._warmup_enabled() is False


def test_warmup_disabled_only_by_off(monkeypatch):
    # Any other value keeps the default-ON posture — a typo must not silently
    # disable the thing that keeps first-recall fast.
    monkeypatch.setenv("MINNI_WARMUP", "yes")
    assert minnid._warmup_enabled() is True


def test_warmup_loads_embedder_and_reranker(fake_models, monkeypatch, caplog):
    monkeypatch.setattr(minnid.DEFAULT_CONFIG, "reranker_enabled", True)
    with caplog.at_level("INFO"):
        minnid._warmup_models()
    assert fake_models["embedder"].calls == 1
    assert fake_models["cross_encoder"].calls == 1
    # The duration is the operator's only evidence that warmup is doing its job.
    assert any("warmup complete" in record.message for record in caplog.records)


def test_warmup_skips_reranker_when_disabled(fake_models, monkeypatch):
    monkeypatch.setattr(minnid.DEFAULT_CONFIG, "reranker_enabled", False)
    minnid._warmup_models()
    assert fake_models["embedder"].calls == 1
    assert fake_models["cross_encoder"].calls == 0


def test_warmup_failure_never_raises(monkeypatch, caplog):
    """A warmup failure must not stop the daemon — the lazy path still works."""
    import minni.models as models

    def boom():
        raise RuntimeError("no network, no cache")

    monkeypatch.setattr(models, "get_embedder", boom)
    with caplog.at_level("WARNING"):
        minnid._warmup_models()  # must not raise
    assert any("warmup failed" in record.message for record in caplog.records)


def test_warmup_runner_offloads_to_thread(fake_models, monkeypatch):
    """
    The loads are seconds-long and synchronous; running them ON the event loop
    would block the accept loop and turn "slow first search" into "daemon
    unreachable". Assert the work happens on a different thread than the loop.
    """
    import asyncio
    import threading

    loop_thread = threading.get_ident()
    seen = {}

    real = minnid._warmup_models

    def recording():
        seen["thread"] = threading.get_ident()
        real()

    monkeypatch.setattr(minnid, "_warmup_models", recording)
    asyncio.run(minnid._warmup_runner())
    assert seen["thread"] != loop_thread

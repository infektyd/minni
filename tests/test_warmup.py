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


class _FakeFaiss:
    def __init__(self):
        self.ready = False


class _FakeRetrievalEngine:
    """Warmup must call _ensure_faiss_loaded with no deadline.

    Start with a stale leftover deadline so a missing clear would skip the
    27s rebuild floor the same way a reused to_thread worker would.
    """

    def __init__(self):
        self.faiss_index = _FakeFaiss()
        self.ensure_calls = 0
        self._deadline = 1_000.0 + 22.5
        self.ensure_deadline = "unset"

    def _current_deadline(self):
        return self._deadline

    def _set_current_deadline(self, value):
        self._deadline = value

    def _ensure_faiss_loaded(self):
        self.ensure_calls += 1
        self.ensure_deadline = self._current_deadline()
        self.faiss_index.ready = True


@pytest.fixture
def fake_models(monkeypatch):
    """Inject fake model loaders into the module `_warmup_models` imports from."""
    import minni.models as models

    embedder = _FakeLoader()
    cross_encoder = _FakeLoader()
    engine = _FakeRetrievalEngine()
    monkeypatch.setattr(models, "get_embedder", embedder)
    monkeypatch.setattr(models, "get_cross_encoder", cross_encoder)
    monkeypatch.setattr(minnid, "_lazy_retrieval", lambda: engine)
    # Warmup now unbounded-ensures vault engines too. Default to none so
    # these unit tests never construct a live per-vault RetrievalEngine.
    monkeypatch.setattr(minnid, "_all_vault_retrievals", lambda: [])
    return {"embedder": embedder, "cross_encoder": cross_encoder, "engine": engine}


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


def test_warmup_ensures_faiss_loaded_without_deadline(fake_models, monkeypatch):
    """Constructing the engine is not enough: default leftover never warms
    FAISS in-request, so warmup must call _ensure_faiss_loaded unbounded."""
    monkeypatch.setattr(minnid.DEFAULT_CONFIG, "reranker_enabled", False)
    minnid._warmup_models()
    engine = fake_models["engine"]
    assert engine.ensure_calls == 1
    assert engine.faiss_index.ready
    assert engine.ensure_deadline is None


def test_warmup_ensures_vault_faiss_loaded_without_deadline(fake_models, monkeypatch):
    """Warmup used to unbounded-ensure only `_lazy_retrieval()` (shared).
    `_lazy_vault_retrieval` builds a cold FAISSIndex; default leftover
    (22.5s / 27s) then skips in-request rebuild, so personal/combined
    hybrid never recovers. Vault engines must warm here too."""
    monkeypatch.setattr(minnid.DEFAULT_CONFIG, "reranker_enabled", False)
    vault_engine = _FakeRetrievalEngine()
    monkeypatch.setattr(
        minnid, "_all_vault_retrievals", lambda: [(vault_engine, "codex", "db")]
    )
    minnid._warmup_models()
    assert fake_models["engine"].ensure_calls == 1
    assert fake_models["engine"].ensure_deadline is None
    assert vault_engine.ensure_calls == 1
    assert vault_engine.faiss_index.ready
    assert vault_engine.ensure_deadline is None


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


def test_vault_watch_change_unbounded_ensures_vault_faiss(monkeypatch):
    """Vault-watch `_vault_retrieval_cache.clear()` constructs a new cold
    FAISSIndex on the next `_lazy_vault_retrieval`. Default leftover never
    rebuilds that in-request, so personal/combined hybrid stays FTS-only.
    After a change, unbounded-ensure the replacement vault engines."""
    import asyncio

    vault_engine = _FakeRetrievalEngine()
    monkeypatch.setattr(minnid, "_vault_watch_interval", lambda: 60)
    monkeypatch.setattr(
        minnid, "_vault_watch_sweep_once",
        lambda: {"codex-vault": {"indexed": 1, "pruned": 0, "chunks_purged": 0}},
    )
    monkeypatch.setattr(
        minnid, "_all_vault_retrievals", lambda: [(vault_engine, "codex", "db")]
    )
    minnid._vault_retrieval_cache["stale"] = ("old", "codex", "db")

    async def _run_one_tick():
        task = asyncio.create_task(minnid._vault_watch_runner())
        for _ in range(50):
            if vault_engine.ensure_calls:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run_one_tick())
    assert "stale" not in minnid._vault_retrieval_cache
    assert vault_engine.ensure_calls == 1, (
        "vault-watch cache clear must unbounded-ensure replacement engines; "
        "default leftover never rebuilds a cold vault FAISS in-request"
    )
    assert vault_engine.faiss_index.ready
    assert vault_engine.ensure_deadline is None

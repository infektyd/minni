"""Behavioral pins for #299: OMP/MKL/veclib threading pinned in minnid.main()
before any model import, plus the torch.set_num_threads(1) belt-and-suspenders
pin in models.py — the fix for the libomp fork-barrier SIGSEGV crash-loop that
#284's CPU pin exposed.

These reuse the real _run_main_with_stub_loop harness from
test_r7_retrieval_integrity.py (main() runs for real against a stub event
loop; only DB/socket/signal/logging side effects are stubbed) rather than
grepping main()'s source text — the same lesson that harness itself documents
("Campaign scar") applies here: a setdefault call wrapped in `if False:`
would still appear in the source but would set nothing.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_r7_retrieval_integrity import _run_main_with_stub_loop  # noqa: E402

_OMP_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")


@pytest.fixture(autouse=True)
def _clear_omp_env():
    """Every test in this file starts from a clean slate for the three vars,
    and restores whatever this process had (usually absent) afterward.

    monkeypatch.delenv(raising=False) does NOT register an undo entry when
    the var was already absent, so a test that later calls the real main()
    (which does os.environ.setdefault(...)) leaves the var set for every
    test file that runs after this one in the same pytest process — a real
    leak a cassandra pass caught. Snapshot/restore by hand instead.
    """
    saved = {name: os.environ.pop(name, None) for name in _OMP_VARS}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_main_pins_omp_threading_before_model_import(monkeypatch, tmp_path):
    """main() must set OMP_NUM_THREADS / MKL_NUM_THREADS / VECLIB_MAXIMUM_THREADS
    to "1" as real process env vars — proven by running main() for real and
    reading os.environ afterward, not by inspecting its source text."""
    for name in _OMP_VARS:
        assert name not in os.environ, f"test setup: {name} leaked from another test"

    _run_main_with_stub_loop(monkeypatch, tmp_path)

    for name in _OMP_VARS:
        assert os.environ.get(name) == "1", (
            f"main() must pin {name}=1 before any torch/faiss/sentence-transformers "
            f"import can occur (#299 libomp fork-barrier SIGSEGV)"
        )


def test_main_preserves_an_explicit_operator_override(monkeypatch, tmp_path):
    """setdefault, not a hard overwrite: an operator who set OMP_NUM_THREADS=4
    in the launchd plist env block must keep that choice."""
    monkeypatch.setenv("OMP_NUM_THREADS", "4")

    _run_main_with_stub_loop(monkeypatch, tmp_path)

    assert os.environ.get("OMP_NUM_THREADS") == "4", (
        "main() must not clobber an explicit operator override with setdefault"
    )
    # The other two vars were not overridden, so they still get pinned.
    assert os.environ.get("MKL_NUM_THREADS") == "1"
    assert os.environ.get("VECLIB_MAXIMUM_THREADS") == "1"


def test_pin_torch_threads_for_cpu_sets_single_thread(monkeypatch):
    """The models.py belt-and-suspenders pin: when CPU inference is actually
    resolved, torch.set_num_threads(1) is really called — checked against
    torch's own reported thread count, not a mock call assertion."""
    torch = pytest.importorskip("torch")
    import minni.models as models

    monkeypatch.setattr(models, "_TORCH_THREADS_PINNED", False)
    original = torch.get_num_threads()
    try:
        torch.set_num_threads(3)  # fixed non-1 baseline, not derived from `original`
        assert torch.get_num_threads() != 1

        models._pin_torch_threads_for_cpu_once("cpu")

        assert torch.get_num_threads() == 1, (
            "_pin_torch_threads_for_cpu_once('cpu') must actually call "
            "torch.set_num_threads(1)"
        )
    finally:
        torch.set_num_threads(original if original > 0 else 1)


def test_pin_torch_threads_is_noop_when_not_cpu(monkeypatch):
    """Batch tools (indexer/backfill) keep MPS auto-select and default
    threading — the pin must not fire when device is not 'cpu'."""
    torch = pytest.importorskip("torch")
    import minni.models as models

    monkeypatch.setattr(models, "_TORCH_THREADS_PINNED", False)
    original = torch.get_num_threads()
    try:
        torch.set_num_threads(3)
        before = torch.get_num_threads()

        models._pin_torch_threads_for_cpu_once("mps")
        models._pin_torch_threads_for_cpu_once(None)

        assert torch.get_num_threads() == before, (
            "the pin must be a no-op for non-CPU device resolutions"
        )
    finally:
        torch.set_num_threads(original if original > 0 else 1)


def test_get_embedder_actually_wires_the_pin_call(monkeypatch):
    """The unit tests above call _pin_torch_threads_for_cpu_once directly,
    which would stay green even if the call were deleted from get_embedder()
    itself. This test goes through the real get_embedder() call site — the
    thing #299 actually needs wired — and checks torch's real thread count
    as a side effect, not a mock/spy assertion on the call."""
    torch = pytest.importorskip("torch")
    pytest.importorskip("sentence_transformers")
    import minni.models as models

    models.get_embedder.cache_clear()
    monkeypatch.setattr(models, "_TORCH_THREADS_PINNED", False)
    monkeypatch.setenv("MINNI_MODEL_DEVICE", "cpu")
    original = torch.get_num_threads()
    try:
        # A fixed non-1 baseline, not max(2, original): if this process's
        # thread pool were ever legitimately 1 already, max(2, 1) still
        # works, but pin the baseline explicitly so the "!= 1" assertion
        # below can't be vacuously true regardless of what torch reports.
        torch.set_num_threads(3)
        assert torch.get_num_threads() != 1

        model = models.get_embedder()

        if model is None:
            pytest.skip("get_embedder() returned None (model load unavailable in this env)")
        assert torch.get_num_threads() == 1, (
            "get_embedder() resolved device='cpu' but did not wire the "
            "_pin_torch_threads_for_cpu_once call — the belt-and-suspenders "
            "pin never fired for real"
        )
    finally:
        torch.set_num_threads(original if original > 0 else 1)
        models.get_embedder.cache_clear()


def test_pin_torch_threads_only_pins_once(monkeypatch):
    """_TORCH_THREADS_PINNED must actually gate re-entry: a caller that resets
    torch's thread count after the first pin should not be silently re-pinned
    by a second get_embedder()-shaped call within the same process."""
    torch = pytest.importorskip("torch")
    import minni.models as models

    monkeypatch.setattr(models, "_TORCH_THREADS_PINNED", False)
    original = torch.get_num_threads()
    try:
        models._pin_torch_threads_for_cpu_once("cpu")
        assert torch.get_num_threads() == 1

        torch.set_num_threads(3)
        models._pin_torch_threads_for_cpu_once("cpu")

        assert torch.get_num_threads() != 1, (
            "a second call after the guard is set must be a no-op, proving "
            "_TORCH_THREADS_PINNED is actually read, not just written"
        )
    finally:
        torch.set_num_threads(original if original > 0 else 1)

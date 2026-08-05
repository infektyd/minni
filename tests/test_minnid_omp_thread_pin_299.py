"""Behavioral pins for #299: OMP/MKL/veclib threading pinned in minnid.main()
before any model import, plus the torch.set_num_threads(1) belt-and-suspenders
pin in models.py — the fix for the libomp fork-barrier SIGSEGV crash-loop that
#284's CPU pin exposed.

Every assertion here is about PROCESS-GLOBAL NATIVE STATE, so every test runs
in a fresh child process (#321).

The first cut of this file ran main() and loaded torch in-process, and that
made the full suite segfault. The mechanism is the one #299 itself documents:
env vars bind at native LOAD time, so `os.environ.setdefault("OMP_NUM_THREADS",
"1")` is correct and sufficient in the daemon — main() runs before any model
import — but inert in a pytest process where earlier tests have already
imported torch and faiss, each carrying its own bundled libomp (the double load
that KMP_DUPLICATE_LIB_OK=TRUE masks). These tests then drove
torch.set_num_threads() up and down and loaded a real sentence-transformers
model on top of that, and the process died later in the run:

    full suite                                  -> Fatal Python error: Segmentation fault
    full suite --ignore=<this file>             -> 2237 passed, 7 skipped
    this file ALONE                             ->    6 passed
    this file + tests/test_pr2_envelope.py      ->   60 passed

That is, the file passed in isolation and killed the suite in aggregate — it
needed the accumulated imports of the earlier tests. The crash surfaced in an
unrelated victim (tests/test_pr2_envelope.py, ~54% in, tqdm_monitor thread).

A child process is also the daemon's REAL shape: main() genuinely does run
before any model import there, so asserting the pin in a fresh process tests
what production does rather than an artifact of test ordering. The alternative
— skipping when torch/faiss are already imported — would have quietly stopped
testing anything on a normal full run, which is the silent-no-op class this
campaign exists to remove.

Assertions remain behavioural: the child's real os.environ and torch's real
reported thread count, never a grep of main()'s source text.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TESTS_DIR), "src")
_OMP_VARS = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")

# Children may import torch / sentence-transformers against a cold model cache.
_CHILD_TIMEOUT = 600


def _run_in_child(body: str, env_overrides: dict | None = None) -> dict:
    """Run *body* in a fresh interpreter and return the dict it prints.

    The body must print exactly one JSON object on a line prefixed with
    ``RESULT:``. Anything else the child writes (model download chatter,
    warnings) is ignored, so the contract does not depend on a clean stdout.
    """
    preamble = textwrap.dedent(
        """
        import json, os, sys
        sys.path.insert(0, {tests_dir!r})
        sys.path.insert(0, {src_dir!r})

        def emit(payload):
            print("RESULT:" + json.dumps(payload))

        """
    ).format(tests_dir=_TESTS_DIR, src_dir=_SRC_DIR)
    script = preamble + textwrap.dedent(body)

    env = dict(os.environ)
    # The parent's own pins must never leak in, or a child could pass by
    # inheriting a value main() never set.
    for name in _OMP_VARS:
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_overrides:
        env.update(env_overrides)

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=_CHILD_TIMEOUT,
        env=env,
    )
    marker = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    if not marker:
        pytest.fail(
            "child process produced no RESULT line "
            f"(exit {proc.returncode})\nstdout:\n{proc.stdout[-2000:]}\n"
            f"stderr:\n{proc.stderr[-2000:]}"
        )
    return json.loads(marker[-1][len("RESULT:"):])


# ---------------------------------------------------------------------------
# main() pins the three env vars
# ---------------------------------------------------------------------------

_MAIN_BODY = """
    import pathlib, tempfile
    import pytest
    from test_r7_retrieval_integrity import _run_main_with_stub_loop

    # The real harness — main() runs for real against a stub event loop, with
    # only DB/socket/signal/logging side effects stubbed. Reused rather than
    # reimplemented so this cannot drift from what the daemon actually does.
    with pytest.MonkeyPatch.context() as mp:
        _run_main_with_stub_loop(mp, pathlib.Path(tempfile.mkdtemp()))

    emit({name: os.environ.get(name) for name in %r})
""" % (_OMP_VARS,)


def test_main_pins_omp_threading_before_model_import():
    """main() must set all three vars to "1" as real process env vars, proven
    by running main() for real in a fresh process and reading os.environ
    there."""
    seen = _run_in_child(_MAIN_BODY)

    for name in _OMP_VARS:
        assert seen.get(name) == "1", (
            f"main() must pin {name}=1 before any torch/faiss/sentence-transformers "
            f"import can occur (#299 libomp fork-barrier SIGSEGV); child saw {seen}"
        )


def test_main_preserves_an_explicit_operator_override():
    """setdefault, not a hard overwrite: an operator who set OMP_NUM_THREADS=4
    in the launchd plist env block must keep that choice."""
    seen = _run_in_child(_MAIN_BODY, env_overrides={"OMP_NUM_THREADS": "4"})

    assert seen.get("OMP_NUM_THREADS") == "4", (
        "main() must not clobber an explicit operator override with setdefault; "
        f"child saw {seen}"
    )
    # The other two were not overridden, so they still get pinned.
    assert seen.get("MKL_NUM_THREADS") == "1"
    assert seen.get("VECLIB_MAXIMUM_THREADS") == "1"


def test_the_child_harness_would_notice_an_unpinned_main():
    """Guards the guard. If a child ever stopped running main() — a swallowed
    import error, a harness signature change — the assertions above would read
    a clean env and could pass for the wrong reason. Here no main() runs, so
    the vars must be absent; if this ever goes green with values set, the
    parent's environment is leaking into children and the tests above are
    vacuous."""
    body = "emit({name: os.environ.get(name) for name in %r})" % (_OMP_VARS,)
    seen = _run_in_child(body)

    assert seen == {name: None for name in _OMP_VARS}, (
        f"a child that never calls main() must see no pins; got {seen}"
    )


# ---------------------------------------------------------------------------
# models.py's torch pin
# ---------------------------------------------------------------------------
#
# These do not call main(), but they assert torch's process-global thread pool
# and one of them loads a real sentence-transformers model. Running them in the
# parent is what put torch's libomp and faiss's libomp in one process under
# thread churn, so they are children too.


def _torch_child(body: str) -> dict:
    """Run a torch-dependent body in a child, reporting unavailability back to
    the parent rather than failing on an environment without torch."""
    prelude = """
    try:
        import torch
    except Exception as exc:
        emit({"skip": "torch unavailable: %s" % exc})
        raise SystemExit(0)
    import minni.models as models
"""
    return _run_in_child(textwrap.dedent(prelude) + textwrap.dedent(body))


def _skip_if_requested(result: dict) -> dict:
    if "skip" in result:
        pytest.skip(result["skip"])
    return result


def test_pin_torch_threads_for_cpu_sets_single_thread():
    """The models.py belt-and-suspenders pin: when CPU inference is actually
    resolved, torch.set_num_threads(1) is really called — checked against
    torch's own reported thread count, not a mock call assertion."""
    result = _skip_if_requested(_torch_child("""
    models._TORCH_THREADS_PINNED = False
    torch.set_num_threads(3)  # fixed non-1 baseline
    before = torch.get_num_threads()

    models._pin_torch_threads_for_cpu_once("cpu")

    emit({"before": before, "after": torch.get_num_threads()})
"""))

    assert result["before"] != 1, "test setup: the baseline must not already be 1"
    assert result["after"] == 1, (
        "_pin_torch_threads_for_cpu_once('cpu') must actually call "
        f"torch.set_num_threads(1); got {result}"
    )


def test_pin_torch_threads_is_noop_when_not_cpu():
    """Batch tools (indexer/backfill) keep MPS auto-select and default
    threading — the pin must not fire when device is not 'cpu'."""
    result = _skip_if_requested(_torch_child("""
    models._TORCH_THREADS_PINNED = False
    torch.set_num_threads(3)
    before = torch.get_num_threads()

    models._pin_torch_threads_for_cpu_once("mps")
    models._pin_torch_threads_for_cpu_once(None)

    emit({"before": before, "after": torch.get_num_threads()})
"""))

    assert result["after"] == result["before"], (
        f"the pin must be a no-op for non-CPU device resolutions; got {result}"
    )


def test_pin_torch_threads_only_pins_once():
    """_TORCH_THREADS_PINNED must actually gate re-entry: a caller that resets
    torch's thread count after the first pin should not be silently re-pinned
    by a second get_embedder()-shaped call within the same process."""
    result = _skip_if_requested(_torch_child("""
    models._TORCH_THREADS_PINNED = False

    models._pin_torch_threads_for_cpu_once("cpu")
    first = torch.get_num_threads()

    torch.set_num_threads(3)
    models._pin_torch_threads_for_cpu_once("cpu")

    emit({"first": first, "second": torch.get_num_threads()})
"""))

    assert result["first"] == 1, "the first call must pin"
    assert result["second"] != 1, (
        "a second call after the guard is set must be a no-op, proving "
        f"_TORCH_THREADS_PINNED is actually read, not just written; got {result}"
    )


def test_get_embedder_actually_wires_the_pin_call():
    """The unit tests above call _pin_torch_threads_for_cpu_once directly,
    which would stay green even if the call were deleted from get_embedder()
    itself. This goes through the real get_embedder() call site — the thing
    #299 actually needs wired — and checks torch's real thread count as a side
    effect, not a mock/spy assertion on the call."""
    result = _skip_if_requested(_torch_child("""
    try:
        import sentence_transformers  # noqa: F401
    except Exception as exc:
        emit({"skip": "sentence_transformers unavailable: %s" % exc})
        raise SystemExit(0)

    models.get_embedder.cache_clear()
    models._TORCH_THREADS_PINNED = False
    os.environ["MINNI_MODEL_DEVICE"] = "cpu"
    torch.set_num_threads(3)
    before = torch.get_num_threads()

    model = models.get_embedder()
    if model is None:
        emit({"skip": "get_embedder() returned None (model load unavailable)"})
        raise SystemExit(0)

    emit({"before": before, "after": torch.get_num_threads()})
"""))

    assert result["before"] != 1, "test setup: the baseline must not already be 1"
    assert result["after"] == 1, (
        "get_embedder() resolved device='cpu' but did not wire the "
        "_pin_torch_threads_for_cpu_once call — the belt-and-suspenders pin "
        f"never fired for real; got {result}"
    )

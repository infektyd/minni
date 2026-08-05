"""Engine test hygiene.

Three isolation guarantees are established here:

1. MINNI_HOME isolation (PR92-4). ``config.py`` freezes ``CANONICAL_SOVEREIGN_HOME``
   and the ``SovereignConfig`` field defaults (``db_path`` / ``vault_path`` /
   ``faiss_*``) at IMPORT time from the ``MINNI_HOME`` env var. Isolation must
   therefore be established BEFORE any engine module is imported — i.e. at
   conftest module load, not inside a fixture (a per-test ``monkeypatch.setenv``
   runs too late to redirect the already-frozen default ``db_path``). If the
   operator already redirected ``MINNI_HOME`` away from the live home (the
   documented ``export MINNI_HOME=$(mktemp -d)``), we respect it; otherwise we
   force a throwaway session dir so ``make test`` / ``check`` / ``coverage`` can
   never read or mutate the operator's live ``~/.minni/minni.db``.

2. The AFM generation-probe cache persists across processes under
   ``~/.minni/run/afm-probe-cache.json`` (see ``afm_provider.py``); every test
   gets a per-test override pointed at its own tmpdir.

3. Worktree import resolution (#258). An editable install (``pip install -e .``)
   pins ``import minni`` to whichever checkout ran ``make setup`` last via a
   ``.pth`` file in site-packages — usually the primary checkout, not a git
   worktree. ``make test-engine`` / ``check`` / ``coverage-engine`` paper over
   this with ``PYTHONPATH=src`` on the recipe line (see Makefile), but a bare
   ``pytest`` (or ``pytest <one file>``) invoked from a worktree with no
   PYTHONPATH set silently imports the OTHER checkout's ``minni`` — green
   output that validated the wrong tree. ``pytest_configure`` below is a
   collection-independent hook (unlike a regular test, it always runs, even
   for `-k`-filtered or single-file invocations) that fails the whole session
   loudly, before any test executes, if the imported ``minni`` did not
   resolve under *this* rootdir's ``src/``. It intentionally does not
   silently fix ``sys.path`` — see #258 discussion: forcing resolution would
   paper over a wrong-tree pytest environment instead of surfacing it.
"""

import os
import tempfile

# --- (0) OMP/BLAS thread pinning: established at IMPORT, before ANY native
# library can load (#321).
#
# This box loads two OpenMP runtimes in one process — PyTorch's bundled libomp
# and FAISS's own — a double load that KMP_DUPLICATE_LIB_OK=TRUE in models.py
# silences rather than resolves. Under thread churn the pair can hit a libomp
# fork barrier and take the interpreter down with SIGSEGV, which is the daemon
# crash-loop #299 fixed by pinning these vars in minnid.main().
#
# The suite hit the same crash from the other side: a full unpinned run
# segfaulted in an unrelated victim (tests/test_pr2_envelope.py, ~54% in,
# tqdm_monitor thread) once tests had accumulated both runtimes. Measured, same
# worktree, back to back:
#
#     full suite, unpinned                                  -> SIGSEGV
#     full suite, OMP/MKL/VECLIB=1 in the environment       -> 2243 passed, 7 skipped
#
# It must live HERE, at conftest import, for the same reason MINNI_HOME does
# below: these variables are read by each math library when it LOADS. A fixture,
# or anything that runs after the first `import torch`, is far too late — the
# threads are already spun. That load-time binding is also exactly why
# minnid.main()'s own setdefault cannot protect a pytest process, where the
# imports have already happened by the time main() is called.
#
# setdefault, not assignment: an operator (or CI) who deliberately exports
# OMP_NUM_THREADS keeps their choice, matching main()'s own contract. The cost
# is that OMP-linked math runs single-threaded for the suite — measured at
# 62.5s against ~61s unpinned, i.e. in the noise.
for _omp_var in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_omp_var, "1")

# --- (1) MINNI_HOME isolation: established at IMPORT, before engine modules load.
_LIVE_MINNI_HOME = os.path.abspath(os.path.expanduser("~/.minni"))
_configured_home = os.environ.get("MINNI_HOME")
if not _configured_home or os.path.abspath(_configured_home) == _LIVE_MINNI_HOME:
    _session_home = tempfile.mkdtemp(prefix="minni-test-home-")
    os.environ["MINNI_HOME"] = _session_home
    # Don't leak the throwaway dir in the system tmp folder across runs.
    import atexit
    import shutil
    atexit.register(shutil.rmtree, _session_home, ignore_errors=True)

import pytest  # noqa: E402  (import after MINNI_HOME redirect, by design)

# --- (3) Worktree import resolution guard (#258).
_REPO_SRC = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir, "src"))


def pytest_configure(config):
    """Fail the whole session, before collection, if ``minni`` resolved wrong.

    Runs unconditionally — this is a pytest hook, not a collected test, so it
    cannot be skipped by scoping the run to one file or a ``-k`` filter (the
    gap a same-named test in test_worktree_import_resolution.py cannot close
    on its own). ``pytest.UsageError`` aborts before any test runs, so a
    wrong-tree run never gets to report a false green.
    """
    try:
        import minni
    except Exception:
        # No resolved import to check; whatever's importing minni downstream
        # (directly or via a broken module) will raise its own, more precise
        # error. Not this guard's job to paper over a different failure.
        return

    minni_file = getattr(minni, "__file__", None)
    if minni_file is None:
        # A namespace package (no __init__.py) has no __file__ — e.g. a stray
        # top-level minni/ directory shadowing the real package before
        # `make setup` has run an editable install at all. That is a real
        # setup problem, just not the #258 wrong-tree failure this guard
        # targets; surface it plainly instead of crashing on os.path.realpath(None).
        raise pytest.UsageError(
            "worktree import resolution (#258): `import minni` resolved to a "
            "namespace package with no __file__ (no minni package is actually "
            "installed/importable here). Run `make setup` first."
        )

    resolved = os.path.realpath(minni_file)
    if not resolved.startswith(_REPO_SRC + os.sep):
        raise pytest.UsageError(
            "worktree import resolution (#258): `import minni` resolved to "
            f"{resolved!r}, outside this rootdir's src/ ({_REPO_SRC!r}). An "
            "editable install from another checkout is winning the import. "
            "Run `make test-engine` / `make check` / `make coverage-engine` "
            f"(they set PYTHONPATH=src), or set "
            f"PYTHONPATH={_REPO_SRC!r} yourself before running "
            "`python -m pytest` from this worktree."
        )


@pytest.fixture(autouse=True)
def _isolated_engine_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", os.fspath(tmp_path / "afm-probe-cache.json"))

    # Force schema migrations to re-run for whatever db this test builds. db.py
    # tracks migrated paths in a process-global set; when a tmp db path is reused
    # across tests (deleted + recreated at the same path) the stale entry makes a
    # later test SKIP migrations and hit "no such table: vault_fts". Clearing the
    # globals per test makes migrations re-run idempotently (CREATE IF NOT
    # EXISTS); db.py itself documents ``_migrations_run = False`` as the supported
    # force-rerun hook for test setup.
    try:
        import minni.db as _db
        monkeypatch.setattr(_db, "_migrations_run", False, raising=False)
        _db._migrated_paths.clear()
        # Same per-test reset for the process-wide schema-DDL gate: a tmp db path
        # reused across tests (deleted + recreated) would otherwise carry a stale
        # "ready" entry and SKIP schema init, hitting "no such table: vault_fts".
        _db._schema_ready_paths.clear()
        # Same reasoning for the per-path shared-instance registry: a tmp db
        # path reused across tests would hand a later test the earlier test's
        # instance (and its already-open connections to the deleted file).
        _db.SovereignDB._shared_instances.clear()
    except Exception:
        pass

    yield

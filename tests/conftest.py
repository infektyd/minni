"""Engine test hygiene.

Three isolation guarantees are established here:

0. Worktree import resolution (#258). An editable install from a *primary*
   checkout puts that checkout's ``src/`` on ``sys.path`` via a ``.pth`` entry.
   pytest run from a git worktree would then resolve ``import minni`` to
   **main**, not the branch under test — green means the wrong tree passed.
   Before any engine import, prepend *this* tree's ``src/`` so it wins, and
   fail loudly if a later import still lands outside the running repo root.

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
"""

import os
import sys
import tempfile

# --- (0) This-tree src/ must win over an editable install from another checkout.
# tests/conftest.py → repo root is parent of tests/; package lives in src/minni.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
_SRC_ROOT = os.path.join(_REPO_ROOT, "src")
if os.path.isdir(os.path.join(_SRC_ROOT, "minni")):
    # Insert even if already present later on sys.path — path[0] must be ours.
    while _SRC_ROOT in sys.path:
        sys.path.remove(_SRC_ROOT)
    sys.path.insert(0, _SRC_ROOT)

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


def _assert_minni_from_this_tree() -> None:
    """Fail if ``import minni`` resolved outside the repo that owns this conftest.

    Realpath both sides so symlinked worktrees still compare equal when the
    package really is under this tree.
    """
    import minni  # noqa: PLC0415 — intentional late import after path fix

    package_file = os.path.realpath(getattr(minni, "__file__", "") or "")
    expected_prefix = os.path.realpath(_SRC_ROOT) + os.sep
    if not package_file.startswith(expected_prefix):
        raise RuntimeError(
            "import minni resolved outside this tree's src/ — worktree tests "
            "would validate the editable install's checkout, not the branch "
            f"under test.\n  minni.__file__={package_file!r}\n"
            f"  expected under={expected_prefix!r}\n"
            "Fix: run via `make test-engine` / `make check` (PYTHONPATH=src) "
            "or ensure tests/conftest.py prepends this tree's src/."
        )


# Run once at collection time so a wrong-tree condition fails before any test
# body can pass against the primary checkout by accident.
_assert_minni_from_this_tree()


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

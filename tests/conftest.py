"""Engine test hygiene.

Two isolation guarantees are established here:

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
import tempfile

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
    except Exception:
        pass

    yield

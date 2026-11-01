"""Engine test hygiene.

Three isolation guarantees are established here:

1. MINNI_HOME isolation (PR92-4). ``config.py`` freezes ``CANONICAL_SOVEREIGN_HOME``
   and the ``MinniConfig`` field defaults (``db_path`` / ``vault_path`` /
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

import atexit
import json
import os
from pathlib import Path
import pwd
import shutil
import sys
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

# --- (1) Hermetic HOME, XDG_*, and MINNI_HOME isolation: established at IMPORT
# and reinforced per-test via the autouse fixture below.
_PRE_FIXTURE_REAL_HOME = os.path.realpath(
    os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir
)
# Preserve local read-only HuggingFace model cache so SentenceTransformer
# offline loads (`local_files_only=True`) still find pre-cached weights
# after HOME and XDG_CACHE_HOME are redirected to throwaway directories.
os.environ.setdefault(
    "HF_HOME", os.path.join(_PRE_FIXTURE_REAL_HOME, ".cache", "huggingface")
)

_LIVE_MINNI_HOME = os.path.abspath(os.path.join(_PRE_FIXTURE_REAL_HOME, ".minni"))
_SESSION_HOME = tempfile.mkdtemp(prefix="minni-test-home-")
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)

if (
    not os.environ.get("HOME")
    or os.path.realpath(os.environ["HOME"]) == _PRE_FIXTURE_REAL_HOME
):
    os.environ["HOME"] = _SESSION_HOME

for _xdg_var, _xdg_rel in (
    ("XDG_CONFIG_HOME", ".config"),
    ("XDG_DATA_HOME", os.path.join(".local", "share")),
    ("XDG_CACHE_HOME", ".cache"),
    ("XDG_STATE_HOME", os.path.join(".local", "state")),
):
    _xdg_val = os.environ.get(_xdg_var)
    if not _xdg_val or os.path.realpath(_xdg_val).startswith(_PRE_FIXTURE_REAL_HOME):
        _xdg_dir = os.path.join(_SESSION_HOME, _xdg_rel)
        os.makedirs(_xdg_dir, exist_ok=True)
        os.environ[_xdg_var] = _xdg_dir

_configured_home = os.environ.get("MINNI_HOME")
if not _configured_home or os.path.realpath(_configured_home) == os.path.realpath(_LIVE_MINNI_HOME):
    _session_minni_home = os.path.join(_SESSION_HOME, ".minni")
    os.makedirs(_session_minni_home, exist_ok=True)
    os.environ["MINNI_HOME"] = _session_minni_home

import pytest  # noqa: E402  (import after MINNI_HOME redirect, by design)

# --- (3) Worktree import resolution guard (#258).
_REPO_SRC = os.path.realpath(os.path.join(os.path.dirname(__file__), os.pardir, "src"))


def _is_inside(path: str, ancestor: str) -> bool:
    """True when *path* lies inside *ancestor*, decided by the FILESYSTEM (#331).

    The obvious spelling — ``realpath(path).startswith(realpath(ancestor))`` —
    is a string compare, and ``realpath`` resolves symlinks but does NOT
    normalise case. On case-insensitive APFS (the macOS default)
    ``~/Projects/minni`` and ``~/Projects/Minni`` are the same directory and
    compare unequal, so invoking pytest through the lowercase spelling tripped
    this guard as a false positive and blocked a legitimate run. Observed live
    during PR verification; the workaround was retyping the path in canonical
    case, which is not something a guard should demand.

    ``os.path.samestat`` compares st_dev/st_ino, so it answers "is this the
    same directory" the way the kernel does. That is also why this is not
    ``normcase``: normcase would hardcode case-insensitive semantics for every
    macOS run, which is wrong on a case-SENSITIVE volume (APFS can be
    formatted that way, and /Volumes mounts often are) — there two spellings
    really are different directories and the guard must still refuse.

    Walks parents rather than comparing the leaf, because the question is
    containment, not identity.
    """
    try:
        ancestor_stat = os.stat(ancestor)
    except OSError:
        # No ancestor to be inside of. Refuse rather than pass by accident —
        # a missing src/ is itself a broken checkout.
        return False

    current = os.path.realpath(path)
    while True:
        try:
            if os.path.samestat(os.stat(current), ancestor_stat):
                return True
        except OSError:
            pass  # a path component we cannot stat is simply not the ancestor
        parent = os.path.dirname(current)
        if parent == current:  # reached the filesystem root
            return False
        current = parent


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
    if not _is_inside(resolved, _REPO_SRC):
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
def _isolated_engine_state(request, tmp_path_factory, monkeypatch):
    isolated_home = tmp_path_factory.mktemp("isolated_home")
    isolated_minni_home = isolated_home / ".minni"
    xdg_config = isolated_home / ".config"
    xdg_data = isolated_home / ".local" / "share"
    xdg_cache = isolated_home / ".cache"
    xdg_state = isolated_home / ".local" / "state"
    for d in (
        isolated_home,
        isolated_minni_home,
        xdg_config,
        xdg_data,
        xdg_cache,
        xdg_state,
    ):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", os.fspath(isolated_home))
    monkeypatch.setenv("USERPROFILE", os.fspath(isolated_home))
    monkeypatch.setenv("MINNI_HOME", os.fspath(isolated_minni_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", os.fspath(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", os.fspath(xdg_data))
    monkeypatch.setenv("XDG_CACHE_HOME", os.fspath(xdg_cache))
    monkeypatch.setenv("XDG_STATE_HOME", os.fspath(xdg_state))
    monkeypatch.setenv("MINNI_AFM_PROBE_CACHE", os.fspath(isolated_home / "afm-probe-cache.json"))

    if "minni.minnid" in sys.modules:
        _minnid = sys.modules["minni.minnid"]
        monkeypatch.setattr(_minnid, "SECURE_RUN_DIR", isolated_minni_home / "run", raising=False)
        monkeypatch.setattr(
            _minnid,
            "DEFAULT_SOCKET_PATH",
            isolated_minni_home / "run" / "minnid.sock",
            raising=False,
        )

    if (
        request.node.name
        == "test_part2_coordinator_durable_learn_promotes_synchronously_while_blocking_harvest_noise"
    ):
        pdir = isolated_minni_home / "principals"
        pdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(pdir, 0o700)
        for pid in ("claude-code", "local"):
            pfile = pdir / f"{pid}.json"
            pfile.write_text(
                json.dumps(
                    {
                        "agent_id": pid if pid != "local" else "main",
                        "workspace_id": "default",
                        "capabilities": ["search", "read", "learn", "resolve_candidate"],
                        "auto_accept_own": True,
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(pfile, 0o600)

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
        _db.MinniDB._shared_instances.clear()
    except Exception:
        pass

    assert os.path.realpath(os.path.expanduser("~")) != _PRE_FIXTURE_REAL_HOME, (
        "Hermetic HOME guard failed before test: expanduser('~') resolved to real home"
    )
    yield
    assert os.path.realpath(os.path.expanduser("~")) != _PRE_FIXTURE_REAL_HOME, (
        "Hermetic HOME guard failed after test: expanduser('~') resolved to real home"
    )

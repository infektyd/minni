"""fd-ceiling defenses: per-path shared SovereignDB + RLIMIT_NOFILE raise.

The daemon's sync RPC handlers run on a thread pool, and SovereignDB caches
one never-closed sqlite connection per (instance, thread). Multiple daemon
subsystems constructing their own instance on the same file therefore
multiplied open fds to instances x threads x (db + wal) — enough to breach
the default soft fd limit under sustained multi-agent load, after which
accept() fails with EMFILE and every client sees EPIPE while launchd still
reports the job running. SovereignDB.shared() collapses instances per db
file; minnid._raise_fd_ceiling() lifts the soft limit toward the hard one.
"""

import os
import resource

from minni.config import SovereignConfig, DEFAULT_CONFIG
from minni.db import SovereignDB


def _cfg(tmp_path, name: str) -> SovereignConfig:
    from dataclasses import replace

    return replace(
        DEFAULT_CONFIG,
        db_path=str(tmp_path / name),
        vault_path=str(tmp_path / "vault"),
    )


def test_shared_returns_same_instance_for_same_path(tmp_path):
    cfg = _cfg(tmp_path, "a.db")
    assert SovereignDB.shared(cfg) is SovereignDB.shared(cfg)


def test_shared_keys_on_path_not_config_object(tmp_path):
    # Two distinct config objects pointing at the same file share one instance.
    assert SovereignDB.shared(_cfg(tmp_path, "a.db")) is SovereignDB.shared(
        _cfg(tmp_path, "a.db")
    )


def test_shared_distinct_paths_get_distinct_instances(tmp_path):
    assert SovereignDB.shared(_cfg(tmp_path, "a.db")) is not SovereignDB.shared(
        _cfg(tmp_path, "b.db")
    )


def test_shared_normalizes_path_spellings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from dataclasses import replace

    absolute = _cfg(tmp_path, "a.db")
    relative = replace(absolute, db_path="a.db")
    assert SovereignDB.shared(absolute) is SovereignDB.shared(relative)


def test_shared_instance_is_usable(tmp_path):
    db = SovereignDB.shared(_cfg(tmp_path, "a.db"))
    conn = db._get_conn()
    assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_plain_constructor_is_untouched(tmp_path):
    # Non-daemon callers (CLI one-shots, tests) keep per-instance semantics.
    cfg = _cfg(tmp_path, "a.db")
    assert SovereignDB(cfg) is not SovereignDB(cfg)
    assert SovereignDB(cfg) is not SovereignDB.shared(cfg)


def test_raise_fd_ceiling_never_lowers_and_respects_hard_limit():
    from minni.minnid import _raise_fd_ceiling

    soft_before, hard_before = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        result = _raise_fd_ceiling()
        soft_after, hard_after = resource.getrlimit(resource.RLIMIT_NOFILE)
        assert result == soft_after
        assert soft_after >= soft_before
        assert hard_after == hard_before
        if hard_before != resource.RLIM_INFINITY:
            assert soft_after <= hard_before
        # Idempotent: a second call is a no-op at the same ceiling.
        assert _raise_fd_ceiling() == soft_after
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_before, hard_before))


def test_raise_fd_ceiling_caps_at_target():
    from minni.minnid import _raise_fd_ceiling

    soft_before, hard_before = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        target = soft_before + 1 if soft_before < 1 << 20 else soft_before
        assert _raise_fd_ceiling(target=target) in (target, soft_before)
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft_before, hard_before))


# --- PR #190 review follow-ups (Codex P2s) ---


def test_shared_pins_relative_db_path_to_absolute(tmp_path, monkeypatch):
    # A relative first spelling must not leave the cached instance opening
    # cwd-relative paths after the process later chdirs elsewhere.
    monkeypatch.chdir(tmp_path)
    from dataclasses import replace

    inst = SovereignDB.shared(replace(_cfg(tmp_path, "a.db"), db_path="a.db"))
    assert os.path.isabs(inst.config.db_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    inst._get_conn().execute("SELECT 1")
    assert (tmp_path / "a.db").exists()
    assert not (other / "a.db").exists()


def test_failed_migration_is_retried_on_next_use(tmp_path, monkeypatch):
    # The swallowed-migration-failure retry contract must survive instance
    # reuse: shared() keeps instances for the process lifetime, so the
    # instance flag may not latch True while the path is un-ready.
    import minni.db as db_mod
    import minni.migrations as migrations_mod

    def boom(conn):
        raise sqlite_error_stub

    sqlite_error_stub = RuntimeError("transient: database is locked")
    monkeypatch.setattr(migrations_mod, "run_migrations", boom)

    cfg = _cfg(tmp_path, "a.db")
    inst = SovereignDB.shared(cfg)
    inst._get_conn()
    key = os.path.abspath(cfg.db_path)
    assert key not in db_mod._migrated_paths
    assert inst._schema_initialized is False

    monkeypatch.undo()
    monkeypatch.setattr(db_mod, "_migrations_run", False, raising=False)
    assert inst is SovereignDB.shared(cfg)
    inst._get_conn()
    assert key in db_mod._migrated_paths
    assert inst._schema_initialized is True


def test_rpc_worker_count_defends_against_malformed_env(monkeypatch):
    from minni.minnid import _rpc_worker_count

    for raw, expected in [
        ("", 8),
        ("   ", 8),
        ("not-a-number", 8),
        ("12.5", 8),
        ("0", 1),
        ("-3", 1),
        ("4", 4),
    ]:
        monkeypatch.setenv("MINNI_RPC_WORKERS", raw)
        assert _rpc_worker_count() == expected, f"raw={raw!r}"
    monkeypatch.delenv("MINNI_RPC_WORKERS")
    assert _rpc_worker_count() == 8

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

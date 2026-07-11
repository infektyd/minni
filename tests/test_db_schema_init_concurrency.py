"""Regression coverage for process-wide SQLite schema initialization."""

import threading
import time

from minni import db as db_mod
from minni.config import SovereignConfig


def test_schema_initialization_is_serialized_across_db_instances(tmp_path, monkeypatch):
    cfg = SovereignConfig(
        db_path=str(tmp_path / "minni.db"),
        faiss_index_path=str(tmp_path / "minni.faiss"),
        vault_path=str(tmp_path / "vault"),
        graph_export_dir=str(tmp_path / "graphs"),
    )
    first = db_mod.SovereignDB(cfg)
    second = db_mod.SovereignDB(cfg)
    original = db_mod.SovereignDB._init_schema
    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    def observed_init(self, conn):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return original(self, conn)
        finally:
            with counter_lock:
                active -= 1

    monkeypatch.setattr(db_mod.SovereignDB, "_init_schema", observed_init)
    errors = []

    def open_and_query(db):
        try:
            with db.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM vault_fts")
                cursor.fetchone()
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=open_and_query, args=(first,)),
        threading.Thread(target=open_and_query, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert max_active == 1

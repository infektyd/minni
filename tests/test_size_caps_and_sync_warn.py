"""SEC-015 size caps and cloud-sync startup-warning tests.

Covers:
  - _handle_learn rejects content longer than 64 KiB with code -32602.
  - _handle_learn accepts content of exactly 64 KiB.
  - _warn_if_sync_root emits a warning for paths under ~/Dropbox/...
  - _warn_if_sync_root stays silent for ordinary paths (~/minni/...).

The 1 MiB readuntil limit on the Unix socket is exercised at the asyncio
StreamReader level and is not unit-tested here — exercising it requires
binding a real socket. The sync-root + content-cap coverage protects the
parsing path that runs immediately after the body is read, which is what
the limit is designed to bound.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def setup_hermetic_principals(tmp_path, monkeypatch):
    import minni.principal as principal
    import minni.minnid_runtime.provenance as provenance
    import minni.minnid as minnid
    import json

    pdir = tmp_path / "principals"
    pdir.mkdir(exist_ok=True)
    original_resolve = principal.resolve_effective_principal

    def _patched_resolve(
        *,
        supplied_agent_id=None,
        transport="uds",
        principals_dir=None,
        operator_context=False,
    ):
        target_dir = principals_dir or pdir
        if supplied_agent_id is None:
            f = target_dir / "local.json"
            f.write_text(
                json.dumps(
                    {
                        "agent_id": "main",
                        "workspace_id": "default",
                        "capabilities": ["*"],
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(f, 0o600)
            return original_resolve(
                supplied_agent_id=None,
                transport=transport,
                principals_dir=target_dir,
            )

        target_agent = str(supplied_agent_id).strip()
        if not target_agent:
            return original_resolve(
                supplied_agent_id=None,
                transport=transport,
                principals_dir=target_dir,
            )

        f = target_dir / f"{target_agent}.json"
        f.write_text(
            json.dumps(
                {
                    "agent_id": target_agent,
                    "workspace_id": "default",
                    "capabilities": ["*"],
                }
            ),
            encoding="utf-8",
        )
        os.chmod(f, 0o600)

        op_ctx = operator_context or target_agent in principal.OPERATOR_RESERVED_AGENT_IDS
        return original_resolve(
            supplied_agent_id=supplied_agent_id,
            transport=transport,
            principals_dir=target_dir,
            operator_context=op_ctx,
        )

    monkeypatch.setattr(principal, "resolve_effective_principal", _patched_resolve)
    monkeypatch.setattr(provenance, "resolve_effective_principal", _patched_resolve)
    monkeypatch.setattr(minnid, "resolve_effective_principal", _patched_resolve)


# ---------------------------------------------------------------------------
# Helpers — keep parity with engine/test_pr6_contradictions.py
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path):
    """Return (SovereignDB, SovereignConfig) backed by a temporary SQLite file."""
    import minni.db as db_mod
    from minni.config import SovereignConfig

    db_path = str(tmp_path / "test.db")
    cfg = SovereignConfig(db_path=db_path)

    old_flag = db_mod._migrations_run
    db_mod._migrations_run = False
    try:
        db_obj = db_mod.SovereignDB(cfg)
        db_obj._get_conn()
    finally:
        db_mod._migrations_run = old_flag

    return db_obj, cfg


def _patch_writeback_no_model(tmp_path: Path, monkeypatch):
    """Patch the global _writeback singleton at a fresh test DB and disable
    the embedder so _handle_learn does not invoke any model."""
    import minni.minnid as minnid
    import minni.writeback as wb_mod

    db_obj, cfg = _make_db(tmp_path)
    wb = wb_mod.WriteBackMemory(db_obj, cfg)
    monkeypatch.setattr(minnid, "_writeback", wb)

    # Replace the .model property so contradiction detection + encode skip.
    # monkeypatch.setattr unwinds the change at the end of the test.
    monkeypatch.setattr(
        wb_mod.WriteBackMemory,
        "model",
        property(lambda self: None),
        raising=False,
    )
    return wb


def _dispatch(method: str, params: dict) -> dict:
    from minni.minnid import _dispatch_sync as _d
    return _d({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    })


# ---------------------------------------------------------------------------
# SEC-015 — _handle_learn content cap
# ---------------------------------------------------------------------------

class TestLearnContentCap:
    def test_learn_rejects_content_over_64kib(self, tmp_path, monkeypatch):
        """Content longer than 64 KiB must be rejected with -32602 before
        any embedder, contradiction detection, or DB write runs."""
        # The cap fires before _writeback is even consulted; still, patch it
        # so an accidental bypass surfaces as a real DB write rather than a
        # mysterious AttributeError.
        _patch_writeback_no_model(tmp_path, monkeypatch)

        oversized = "x" * (64 * 1024 + 1)  # 64 KiB + 1
        resp = _dispatch("learn", {
            "content": oversized,
            "agent_id": "test",
            "category": "general",
        })
        assert "error" in resp, f"expected error, got {resp}"
        assert resp["error"]["code"] == -32602
        assert "exceeds" in resp["error"]["message"].lower()

    def test_learn_accepts_content_at_exactly_64kib(self, tmp_path, monkeypatch):
        """Content of exactly 64 KiB must pass the size guard."""
        _patch_writeback_no_model(tmp_path, monkeypatch)

        payload = "y" * (64 * 1024)  # exactly 64 KiB
        assert len(payload) == 64 * 1024
        resp = _dispatch("learn", {
            "content": payload,
            "agent_id": "test",
            "category": "general",
        })

        # The size guard must NOT reject; downstream branches may produce ok
        # or contradiction, but never a -32602 from the cap.
        assert "error" not in resp or resp["error"]["code"] != -32602, (
            f"unexpected size-cap rejection at exactly 64 KiB: {resp}"
        )

    def test_learn_rejects_oversized_summary_field(self, tmp_path, monkeypatch):
        """Auxiliary fields (summary/title) > 4 KiB are rejected."""
        _patch_writeback_no_model(tmp_path, monkeypatch)

        resp = _dispatch("learn", {
            "content": "short",
            "summary": "z" * (4 * 1024 + 1),
        })
        assert "error" in resp
        assert resp["error"]["code"] == -32602
        assert "summary" in resp["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Cloud-sync hygiene — _warn_if_sync_root
# ---------------------------------------------------------------------------

class TestWarnIfSyncRoot:
    def test_warns_for_path_under_dropbox(self, tmp_path, monkeypatch, caplog):
        """A path under <home>/Dropbox/... must trigger a warning."""
        from minni.minnid import _warn_if_sync_root

        # Pretend $HOME is tmp_path so we can stage a fake Dropbox folder.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        sync_dir = tmp_path / "Dropbox" / "foo"
        sync_dir.mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="minnid"):
            _warn_if_sync_root("vault", sync_dir)

        msgs = [r.getMessage() for r in caplog.records]
        assert any("cloud-sync root" in m and "Dropbox" in m for m in msgs), (
            f"expected Dropbox warning, got: {msgs}"
        )

    def test_warns_for_icloud_mobile_documents(self, tmp_path, monkeypatch, caplog):
        from minni.minnid import _warn_if_sync_root

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        icloud_dir = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "vault"
        icloud_dir.mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="minnid"):
            _warn_if_sync_root("vault", icloud_dir)

        msgs = [r.getMessage() for r in caplog.records]
        assert any("Mobile Documents" in m for m in msgs), (
            f"expected iCloud warning, got: {msgs}"
        )

    def test_no_warning_for_normal_path(self, tmp_path, monkeypatch, caplog):
        """A path under a normal project directory must not trigger a warning."""
        from minni.minnid import _warn_if_sync_root

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        normal_dir = tmp_path / "minni" / "foo"
        normal_dir.mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="minnid"):
            _warn_if_sync_root("vault", normal_dir)

        msgs = [r.getMessage() for r in caplog.records]
        assert not any("cloud-sync root" in m for m in msgs), (
            f"unexpected sync-root warning for normal path: {msgs}"
        )

    def test_no_warning_for_path_outside_home(self, tmp_path, monkeypatch, caplog):
        """A path outside $HOME (e.g. /tmp/...) must not trigger a warning,
        even if the absolute string contains 'Dropbox' downstream."""
        from minni.minnid import _warn_if_sync_root

        # Move home elsewhere so tmp_path is NOT under home.
        elsewhere = tmp_path / "fake_home"
        elsewhere.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: elsewhere))

        outside = tmp_path / "etc" / "Dropbox"
        outside.mkdir(parents=True)

        with caplog.at_level(logging.WARNING, logger="minnid"):
            _warn_if_sync_root("vault", outside)

        msgs = [r.getMessage() for r in caplog.records]
        assert not any("cloud-sync root" in m for m in msgs), msgs

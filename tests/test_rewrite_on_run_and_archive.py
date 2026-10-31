"""Tests for §4.4 idempotent deploys, CAS abort on concurrent mutation, and archive-first backup/lock hygiene."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minni.wire import custom_refresh as refresh
from minni.wire.manifest import sha256_file
from minni.wire.paths import install_lock_path


def _seed_payload(base: Path, version: str) -> Path:
    root = base / version
    (root / "dist").mkdir(parents=True, exist_ok=True)
    (root / "dist" / "server.js").write_text("console.log('fixture');\n", encoding="utf-8")
    (root / "payload-manifest.json").write_text(
        json.dumps({
            "schema": 1,
            "version": version,
            "files": {"dist/server.js": sha256_file(root / "dist" / "server.js")},
        }),
        encoding="utf-8",
    )
    return root


def test_cas_aborts_on_concurrent_mutation_and_custom_refresh_deletes_plugin_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(refresh.shutil, "which", lambda command: f"/bin/{command}")

    base = tmp_path / ".minni" / "plugin"
    old_root = _seed_payload(base, "0.6.0+git.11111111")
    new_root = _seed_payload(base, "0.6.0+git.22222222")

    # 1. Single ~/.minni/plugin/.install.lock path (§4.4 #3)
    assert install_lock_path("0.6.0+git.22222222") == base / ".install.lock"

    # 2. Seed v2 wired.json and a custom muse config with plugin-root cwd + a stale .minni-backup-* file
    config = tmp_path / refresh._CUSTOM["muse"]
    config.parent.mkdir(parents=True, exist_ok=True)
    stale_backup = config.parent / (config.name + ".minni-backup-legacy123")
    stale_backup.write_text('{"legacy": true}\n', encoding="utf-8")

    entry = {
        "command": "node",
        "args": [str(old_root / "dist/server.js")],
        "cwd": str(old_root),
        "env": {"MINNI_AGENT_ID": "muse", "CUSTOM": "preserve"},
        "enabled": True,
    }
    config.write_text(json.dumps({"mcpServers": {"minni": entry}}), encoding="utf-8")
    (base / "wired.json").write_text(
        json.dumps({
            "schema_version": 2,
            "schema": 2,
            "generation": 1,
            "wires": [
                {
                    "platform": "muse",
                    "config_path": str(config),
                    "install_root": str(old_root),
                    "version": old_root.name,
                    "workspace": None,
                    "wired_at": "2026-10-31T00:00:00Z",
                }
            ],
        }),
        encoding="utf-8",
    )

    # Refresh under v2 / archive_first: deletes plugin-root cwd, writes backup to ~/.minni/archive/<ts>/,
    # and sweeps stale .minni-backup-* out of host config dir!
    res = refresh.refresh_custom_wires(new_root=new_root, archive_first=True)
    assert res["exit_code"] == 0
    row = res["results"][0]
    assert row["status"] == "refreshed"
    updated_doc = json.loads(config.read_text(encoding="utf-8"))
    assert "cwd" not in updated_doc["mcpServers"]["minni"]
    assert updated_doc["mcpServers"]["minni"]["args"] == [str(new_root / "dist/server.js")]
    # Zero .minni-backup-* files left in host config directory!
    assert list(config.parent.glob("*.minni-backup-*")) == []
    # Backup is inside ~/.minni/archive/
    backup_path = Path(row["backup"])
    assert backup_path.is_file()
    assert str(tmp_path / ".minni" / "archive") in str(backup_path)

    # 3. Read-compare-skip idempotency: second run does zero writes and preserves mtime_ns
    mtime_after_first = config.stat().st_mtime_ns
    res_second = refresh.refresh_custom_wires(new_root=new_root, archive_first=True)
    assert res_second["exit_code"] == 0
    assert res_second["results"][0]["status"] == "skipped"
    assert config.stat().st_mtime_ns == mtime_after_first

    # 4. CAS abort on concurrent mutation during validation leaves zero .minni-backup-* in host dir
    third_root = _seed_payload(base, "0.6.0+git.33333333")
    real_payload = refresh._payload

    def _mutate_during_payload_check(root_arg: Path, base_arg: Path):
        out = real_payload(root_arg, base_arg)
        if root_arg == third_root:
            # Simulate concurrent host write to config before CAS check
            doc = json.loads(config.read_text(encoding="utf-8"))
            doc["concurrentHostKey"] = "live-session-edit"
            config.write_text(json.dumps(doc), encoding="utf-8")
        return out

    monkeypatch.setattr(refresh, "_payload", _mutate_during_payload_check)
    res_cas = refresh.refresh_custom_wires(new_root=third_root, archive_first=True)
    assert res_cas["exit_code"] == 1
    assert res_cas["results"][0]["status"] == "failed"
    assert "changed during validation" in res_cas["results"][0]["error"]
    # Concurrent host edit preserved, not overwritten!
    assert json.loads(config.read_text(encoding="utf-8"))["concurrentHostKey"] == "live-session-edit"
    # Zero .minni-backup-* files in host config directory!
    assert list(config.parent.glob("*.minni-backup-*")) == []

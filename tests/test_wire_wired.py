"""Unit tests for wired.json upsert and out-of-band detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minni.wire.wired import WireRecord, _detect_out_of_band, upsert_wire


def test_detect_out_of_band_helper():
    assert _detect_out_of_band(1, 0) is not None
    assert _detect_out_of_band(0, 0) is None


def test_upsert_wire_happy_path_no_warning(tmp_path, monkeypatch):
    base = tmp_path / "plugin"
    base.mkdir()
    monkeypatch.setattr("minni.wire.wired.plugin_base", lambda: base)
    record = WireRecord(
        platform="claude-code",
        config_path=str(tmp_path / ".claude.json"),
        install_root=str(base / "0.2.0"),
        version="0.2.0",
        workspace=None,
        wired_at="2026-07-04T00:00:00Z",
    )
    data, warning = upsert_wire(record)
    assert warning is None
    assert data["generation"] == 1
    on_disk = json.loads((base / "wired.json").read_text(encoding="utf-8"))
    assert on_disk["generation"] == 1


def test_upsert_wire_oob_warning(tmp_path, monkeypatch):
    base = tmp_path / "plugin"
    base.mkdir()
    wired_json = base / "wired.json"
    wired_json.write_text(
        json.dumps({"schema": 1, "generation": 2, "wires": []}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("minni.wire.wired.plugin_base", lambda: base)

    real_load = __import__("minni.wire.wired", fromlist=["_load_wired"])._load_wired
    calls = {"n": 0}

    def load_side_effect(path: Path) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            return {"schema": 1, "generation": 0, "wires": []}
        return real_load(path)

    monkeypatch.setattr("minni.wire.wired._load_wired", load_side_effect)

    record = WireRecord(
        platform="codex",
        config_path=str(tmp_path / "config.toml"),
        install_root=str(base / "0.2.0"),
        version="0.2.0",
        workspace=None,
        wired_at="2026-07-04T00:00:00Z",
    )
    _, warning = upsert_wire(record)
    assert warning is not None
    assert "out-of-band" in warning
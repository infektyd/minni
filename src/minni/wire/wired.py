"""wired.json upsert under flock (§4.4 step 7)."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from minni.wire.manifest import utc_now_iso
from minni.wire.paths import plugin_base


@dataclass(frozen=True)
class WireRecord:
    platform: str
    config_path: str
    install_root: str
    version: str
    workspace: str | None
    wired_at: str


def _detect_out_of_band(pre_write_gen: int, start_gen: int) -> str | None:
    if pre_write_gen != start_gen:
        return "wired.json generation changed since read; possible out-of-band edit"
    return None


def _load_wired(path: Path) -> dict:
    if not path.exists():
        return {"schema": 1, "generation": 0, "wires": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("schema", 1)
    data.setdefault("generation", 0)
    data.setdefault("wires", [])
    return data


def upsert_wire(
    record: WireRecord,
    *,
    dry_run: bool = False,
) -> tuple[dict, str | None]:
    """Return (updated wired.json dict, warning or None)."""
    wired_json = plugin_base() / "wired.json"
    wired_lock = plugin_base() / "wired.lock"
    wired_json.parent.mkdir(parents=True, exist_ok=True)
    warning: str | None = None

    lock_fd = os.open(str(wired_lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = _load_wired(wired_json)
        start_gen = int(data.get("generation", 0))
        wires: list[dict] = list(data.get("wires", []))
        entry = {
            "platform": record.platform,
            "config_path": record.config_path,
            "install_root": record.install_root,
            "version": record.version,
            "workspace": record.workspace,
            "wired_at": record.wired_at,
        }
        replaced = False
        for idx, wire in enumerate(wires):
            if (
                wire.get("platform") == record.platform
                and wire.get("config_path") == record.config_path
            ):
                wires[idx] = entry
                replaced = True
                break
        if not replaced:
            wires.append(entry)
        data["wires"] = wires
        data["generation"] = start_gen + 1
        if dry_run:
            return data, warning

        pre_write = _load_wired(wired_json)
        warning = _detect_out_of_band(
            int(pre_write.get("generation", 0)),
            start_gen,
        )
        if warning:
            print(f"[wire] warning: {warning}", file=sys.stderr)

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=wired_json.parent, prefix=".wired-", suffix=".json",
        )
        os.close(tmp_fd)
        try:
            Path(tmp_path).write_text(
                json.dumps(data, indent=2) + "\n", encoding="utf-8",
            )
            os.replace(tmp_path, wired_json)
        finally:
            if Path(tmp_path).exists():
                Path(tmp_path).unlink(missing_ok=True)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    return data, warning


def wired_install_roots() -> set[str]:
    data = _load_wired(plugin_base() / "wired.json")
    return {
        str(w.get("install_root", ""))
        for w in data.get("wires", [])
        if w.get("install_root")
    }


def make_record(
    platform: str,
    config_path: Path,
    install_root: Path,
    version: str,
    workspace: str | None,
) -> WireRecord:
    return WireRecord(
        platform=platform,
        config_path=str(config_path.expanduser()),
        install_root=str(install_root),
        version=version,
        workspace=workspace,
        wired_at=utc_now_iso(),
    )
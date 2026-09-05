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



def _wire_entry(record: WireRecord) -> dict:
    return {
        "platform": record.platform,
        "config_path": record.config_path,
        "install_root": record.install_root,
        "version": record.version,
        "workspace": record.workspace,
        "wired_at": record.wired_at,
    }

def upsert_wire(
    record: WireRecord,
    *,
    dry_run: bool = False,
    expected_record: dict | None = None,
) -> tuple[dict, str | None]:
    """Return (updated wired.json dict, warning or None)."""
    wired_json = plugin_base() / "wired.json"
    wired_lock = plugin_base() / "wired.lock"
    warning: str | None = None

    if dry_run:
        # A dry run must not write to HOME at all — and the mkdir + O_CREAT
        # on the lock file below ARE writes (measured: dry-run `wire all` on
        # a payload-present source tree left ~/.minni/plugin/wired.lock in a
        # pristine HOME). An unlocked read is acceptable for a preview; the
        # write path below still takes the exclusive lock.
        data = _load_wired(wired_json)
        wires: list[dict] = list(data.get("wires", []))
        if expected_record is not None:
            matches = [wire for wire in wires
                       if wire.get("platform") == record.platform
                       and wire.get("config_path") == record.config_path]
            if matches != [expected_record]:
                raise ValueError("wire registration changed before publication")
        entry = _wire_entry(record)
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
        data["generation"] = int(data.get("generation", 0)) + 1
        return data, warning

    wired_json.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(wired_lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = _load_wired(wired_json)
        start_gen = int(data.get("generation", 0))
        wires: list[dict] = list(data.get("wires", []))
        if expected_record is not None:
            matches = [wire for wire in wires
                       if wire.get("platform") == record.platform
                       and wire.get("config_path") == record.config_path]
            if matches != [expected_record]:
                raise ValueError("wire registration changed before publication")
        entry = _wire_entry(record)
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


def wired_record(platform: str) -> dict | None:
    """The most recent wire record for a platform, or None if never wired."""
    data = _load_wired(plugin_base() / "wired.json")
    for wire in reversed(data.get("wires", [])):
        if isinstance(wire, dict) and wire.get("platform") == platform:
            return wire
    return None


def wired_install_roots() -> set[str]:
    data = _load_wired(plugin_base() / "wired.json")
    return {
        str(w.get("install_root", ""))
        for w in data.get("wires", [])
        if w.get("install_root")
    }


def retire_platform(platform: str, *, dry_run: bool = False) -> tuple[dict, int]:
    """Remove all wired.json rows for ``platform``. Return (data, removed_count).

    Used when wire skips a platform for missing host config root so a zombie
    record cannot keep a lagging install_root "active" forever.
    """
    wired_json = plugin_base() / "wired.json"
    wired_lock = plugin_base() / "wired.lock"
    if dry_run:
        # Same no-write contract as upsert_wire's dry-run path: no mkdir,
        # no lock-file creation. Unlocked read is fine for a preview.
        data = _load_wired(wired_json)
        wires = list(data.get("wires", []))
        kept = [w for w in wires if str(w.get("platform") or "") != platform]
        data["wires"] = kept
        return data, len(wires) - len(kept)
    wired_json.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(wired_lock), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = _load_wired(wired_json)
        start_gen = int(data.get("generation", 0))
        wires: list[dict] = list(data.get("wires", []))
        kept = [w for w in wires if str(w.get("platform") or "") != platform]
        removed = len(wires) - len(kept)
        if removed == 0:
            data["wires"] = kept
            return data, removed
        data["wires"] = kept
        data["generation"] = start_gen + 1
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
        return data, removed
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


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
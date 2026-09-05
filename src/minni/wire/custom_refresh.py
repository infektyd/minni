"""Refresh registered Muse/Devin plain-JSON MCP bindings, not host integrations.

No discovery/activation, native hooks, pruning, or session management. Other
custom formats remain explicitly unsupported until their contracts are known.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from minni.wire.manifest import PayloadManifest, sha256_file, utc_now_iso
from minni.wire.paths import plugin_base
from minni.wire.platform import CANONICAL_FLEET
from minni.wire.wired import WireRecord, upsert_wire

_CUSTOM = {"muse": ".muse/mcp.json", "devin": ".config/devin/mcp_config.json"}
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[+.-][A-Za-z0-9_.+-]+)?$")
_BACKUP_KEEP = 1


def _prune_backups(config: Path, keep: Path | None = None) -> None:
    """Cap sibling refresh backups so repeated syncs cannot accumulate files."""
    prefix = config.name + ".minni-backup-"
    found = [
        path for path in config.parent.iterdir()
        if path.name.startswith(prefix) and path.is_file() and not path.is_symlink()
    ]
    survivors = {os.path.realpath(keep)} if keep is not None else set()
    for path in sorted(found, key=lambda p: p.stat().st_mtime_ns, reverse=True):
        if len(survivors) >= _BACKUP_KEEP:
            break
        survivors.add(os.path.realpath(path))
    for path in found:
        if os.path.realpath(path) not in survivors:
            path.unlink(missing_ok=True)


def wire_report_root(text: str) -> Path | None:
    """Accept a unique installer result root, never guess from directory age."""
    decoder = json.JSONDecoder()
    roots = set()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            doc, _ = decoder.raw_decode(text[index:])
        except ValueError:
            continue
        if not isinstance(doc, dict) or not {"schema", "status", "results"} <= doc.keys():
            continue
        root, version = doc.get("install_root"), doc.get("payload_version")
        if isinstance(root, str) and isinstance(version, str) and Path(root).name == version:
            roots.add(root)
        else:
            roots.add(None)
    if len(roots) != 1 or None in roots:
        return None
    return Path(roots.pop())


def _version_root(root: Path, base: Path) -> bool:
    return root.is_absolute() and root.parent.resolve() == base.resolve() and bool(_VERSION.fullmatch(root.name))


def _payload(root: Path, base: Path) -> PayloadManifest:
    if not _version_root(root, base) or root.is_symlink():
        raise ValueError("unsupported payload root")
    server = root / "dist/server.js"
    if server.is_symlink() or not server.is_file() or (root / "dist").is_symlink():
        raise ValueError("payload server missing or symlinked")
    manifest = PayloadManifest.load(root / "payload-manifest.json")
    if manifest.version != root.name:
        raise ValueError("payload manifest version differs from directory")
    if manifest.files.get("dist/server.js") != sha256_file(server):
        raise ValueError("payload server hash mismatch")
    return manifest


def _replace(path: Path, content: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".minni-refresh-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _check_record(record: dict) -> None:
    rows = json.loads((plugin_base() / "wired.json").read_text())["wires"]
    matches = [row for row in rows if row.get("platform") == record.get("platform")
               and row.get("config_path") == record.get("config_path")]
    if matches != [record]:
        raise ValueError("registered custom binding changed during validation")


def _refresh(record: dict, new_root: Path | None, *, dry_run: bool) -> dict:
    platform = record.get("platform")
    result = {"platform": platform, "status": "skipped"}
    if platform not in _CUSTOM:
        return {**result, "reason": "unsupported custom host/config format"}
    home = Path(os.environ.get("HOME") or Path.home())
    config = Path(str(record.get("config_path", "")))
    if config != home / _CUSTOM[platform] or config.is_symlink():
        return {**result, "reason": "unsupported custom config location or symlink"}
    if not config.is_file():
        return {**result, "reason": "registered custom config absent"}
    original = config.read_bytes()
    data = json.loads(original)
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers"), dict):
        raise ValueError("custom MCP configuration malformed")
    entry = data["mcpServers"].get("minni")
    if entry is None:
        return {**result, "reason": "Minni binding absent; no activation"}
    if entry is False:
        return {**result, "reason": "Minni binding disabled; preserved"}
    if not isinstance(entry, dict):
        raise ValueError("custom Minni binding malformed")
    if entry.get("enabled") is False or entry.get("disabled") is True:
        return {**result, "reason": "Minni binding disabled; preserved"}
    if not shutil.which(platform):
        return {**result, "reason": "custom host executable unavailable"}
    command = entry.get("command")
    if (not isinstance(command, str) or Path(command).name not in {"node", "nodejs"}
            or entry.get("type", "stdio") != "stdio"
            or entry.get("transport", "stdio") != "stdio" or entry.get("url")):
        return {**result, "reason": "unsupported custom MCP launcher/transport"}
    if not shutil.which(command):
        raise ValueError("custom MCP node launcher unavailable")
    base = plugin_base()
    old_root = Path(str(record.get("install_root", "")))
    _payload(old_root, base)
    if entry.get("args") != [str(old_root / "dist/server.js")]:
        return {**result, "reason": "custom MCP arguments differ from registered payload; preserved"}
    if new_root is None:
        return {**result, "status": "failed", "reason":
                "installer supplied no target (custom-only fleet is not installed by wire); "
                "use custom_refresh --new-root with an existing verified payload"}
    if (dry_run and _version_root(new_root, base)
            and not new_root.exists() and not new_root.is_symlink()):
        return {**result, "status": "dry-run", "target_validation": "not_validated",
                "reason": "refresh planned after installer creates target; target payload not yet validated"}
    manifest = _payload(new_root, base)
    entry["args"] = [str(new_root / "dist/server.js")]
    cwd = entry.get("cwd")
    notes = []
    if isinstance(cwd, str) and _version_root(Path(cwd), base):
        try:
            _payload(Path(cwd), base)
        except (OSError, ValueError, KeyError, TypeError):
            notes.append("cwd preserved: existing directory is not a verified Minni payload")
        else:
            entry["cwd"] = str(new_root)
    if (old_root == new_root and record.get("version") == manifest.version
            and json.loads(original) == data):
        return {**result, "reason": "registered MCP binding already current"}
    replacement = (json.dumps(data, indent=2) + "\n").encode()
    if dry_run:
        return {"platform": platform, "status": "dry-run", "reason": "would refresh registered MCP binding", "notes": notes}
    _check_record(record)
    mode = stat.S_IMODE(config.stat().st_mode)
    backup_fd, backup_name = tempfile.mkstemp(prefix=config.name + ".minni-backup-", dir=config.parent)
    with os.fdopen(backup_fd, "wb") as backup:
        backup.write(original)
    _prune_backups(config, keep=Path(backup_name))
    # Optimistic concurrency check avoids overwriting edits made while validating.
    if config.read_bytes() != original:
        raise ValueError("custom config changed during validation; backup retained")
    _replace(config, replacement, mode)
    try:
        if config.read_bytes() != replacement:
            raise ValueError("custom config readback differs")
        upsert_wire(WireRecord(platform=platform, config_path=str(config),
                    install_root=str(new_root), version=manifest.version,
                    workspace=record.get("workspace"), wired_at=utc_now_iso()),
                    expected_record=record)
    except Exception:
        if config.read_bytes() == replacement:
            _replace(config, original, mode)
        raise
    return {"platform": platform, "status": "refreshed", "backup": backup_name,
            "runtime": "not_probed", "native_hooks": "unchanged", "notes": notes}


def refresh_custom_wires(*, dry_run: bool = False, new_root: Path | None = None) -> dict:
    base = plugin_base()
    results = []
    try:
        registry = base / "wired.json"
        data = json.loads(registry.read_text()) if registry.exists() else {"wires": []}
        records = data.get("wires")
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("wire registry malformed")
    except Exception as exc:
        return {"name": "custom_mcp_refresh", "exit_code": 1, "reason": f"wire registry unreadable ({type(exc).__name__})"}
    target = Path(new_root) if new_root is not None else None
    for record in records:
        if record.get("platform") in CANONICAL_FLEET:
            continue
        try:
            results.append(_refresh(record, target, dry_run=dry_run))
        except Exception as exc:
            results.append({"platform": record.get("platform"), "status": "failed",
                            "reason": f"custom refresh failed ({type(exc).__name__}); configuration backup retained if a write was attempted"})
    return {"name": "custom_mcp_refresh", "exit_code": int(any(row["status"] == "failed" for row in results)),
            "results": results,
            "skipped": all(row["status"] in {"skipped", "dry-run"} for row in results)}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    target_args = parser.add_mutually_exclusive_group()
    target_args.add_argument("--new-root", type=Path)
    target_args.add_argument("--wire-report", type=Path)
    args = parser.parse_args()
    target = args.new_root
    if args.wire_report is not None:
        target = wire_report_root(args.wire_report.read_text())
    result = refresh_custom_wires(dry_run=args.dry_run, new_root=target)
    print(json.dumps(result, indent=2))
    raise SystemExit(result["exit_code"])

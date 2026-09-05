"""Install Kilo's native bridge alongside its wire-managed MCP configuration."""
from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from minni.wire.writers import load_json, normalize_workspace_id, update_kilo_config


def _is_managed_bridge(content: bytes) -> bool:
    if content.startswith(b"// Managed by minni wire kilocode.\n"):
        return True
    # Legacy installs had no managed header. Require the bridge's distinctive
    # implementation together, never an environment-variable name alone.
    return all(fragment in content for fragment in (
        b'import { spawn } from "node:child_process";',
        b"function runHook(event, payload)",
        b'spawn("node", [HOOK_SCRIPT, event]',
        b"env: { ...process.env, ...HOOK_ENV }",
        b'"experimental.session.compacting"',
        b"export default MinniPlugin;",
        b"MINNI_KILOCODE_AGENT_ID",
    ))


def _replace_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _snapshot(path: Path) -> tuple[bytes, int] | None:
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlinked Kilo configuration: {path}")
    if not path.exists():
        return None
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _restore(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        _replace_bytes(path, *snapshot)


def install_kilo_bridge(
    install_root: Path, agent: str, vault: Path, socket: Path,
    workspace: Path, afm_env: dict[str, str] | None = None,
) -> tuple[Path, dict[str, object]]:
    """Publish bridge and MCP bindings; restore both host files on write failure.

    Installation proves file/configuration state, not that an existing Kilo
    process loaded the plugin or delivered an event. No host is restarted.
    """
    install_root = install_root.resolve()
    template = install_root / "kilo" / "minni-plugin.js"
    hook = install_root / "dist" / "kilocode-hook.js"
    if not template.is_file() or not hook.is_file():
        raise ValueError("Kilo payload is missing its native bridge or compiled hook; rebuild the payload")
    source = template.read_text(encoding="utf-8")
    if not all(marker in source for marker in ("__MINNI_KILO_HOOK_SCRIPT__", "__MINNI_KILO_HOOK_ENV__")):
        raise ValueError("Kilo bridge template does not expose the required binding markers")
    config_path = Path.home() / ".config" / "kilo" / "kilo.json"
    bridge_path = config_path.parent / "plugin" / "minni.js"
    config_before = _snapshot(config_path)
    bridge_before = _snapshot(bridge_path)
    # Parse before publishing either host file, so malformed configuration is
    # not converted into a partial install or a new empty document.
    config = load_json(config_path)
    if not isinstance(config, dict) or not isinstance(config.get("mcp", {}), dict):
        raise ValueError("Kilo configuration must contain an object-valued mcp table")
    if bridge_before is not None and not _is_managed_bridge(bridge_before[0]):
        raise ValueError(f"refusing to overwrite an unrecognized plugin at {bridge_path}")
    env = {
        **(afm_env or {}),
        "MINNI_KILOCODE_AGENT_ID": agent,
        "MINNI_KILOCODE_VAULT_PATH": str(vault),
        "MINNI_SOCKET_PATH": str(socket),
        "MINNI_KILOCODE_WORKSPACE_ID": normalize_workspace_id(str(workspace)),
    }
    rendered = (
        "// Managed by minni wire kilocode.\n"
        f"const __MINNI_KILO_HOOK_SCRIPT__ = {json.dumps(str(hook))};\n"
        f"const __MINNI_KILO_HOOK_ENV__ = {json.dumps(env, sort_keys=True)};\n"
        + source
    ).encode("utf-8")
    bridge_attempted = config_attempted = False
    try:
        bridge_attempted = True
        _replace_bytes(bridge_path, rendered)
        config_attempted = True
        update_kilo_config(install_root / "dist" / "server.js", agent, vault, socket, workspace, afm_env)
        if bridge_path.read_bytes() != rendered:
            raise OSError("Kilo bridge readback did not match the published binding")
        entry = load_json(config_path).get("mcp", {}).get("minni", {})
        expected_env = {
            "MINNI_AGENT_ID": agent, "MINNI_VAULT_PATH": str(vault),
            "MINNI_SOCKET_PATH": str(socket),
            "MINNI_WORKSPACE_ID": normalize_workspace_id(str(workspace)),
            **(afm_env or {}),
        }
        if (entry.get("command") != ["node", str(install_root / "dist" / "server.js")]
                or entry.get("enabled") is not True
                or entry.get("environment") != expected_env):
            raise OSError("Kilo MCP readback did not match the published binding")
    except Exception as exc:
        rollback_errors = []
        for path, before, attempted in (
            (config_path, config_before, config_attempted),
            (bridge_path, bridge_before, bridge_attempted),
        ):
            if attempted:
                try:
                    _restore(path, before)
                except OSError:
                    rollback_errors.append(str(path))
        if rollback_errors:
            raise OSError("Kilo installation failed; rollback also failed for " + ", ".join(rollback_errors)) from exc
        raise
    return config_path, {
        "installed": True,
        "bridge_path": str(bridge_path),
        "hook_entry": str(hook),
        "host_delivery": "not_verified",
    }

"""Post-wire verification probes (§4.4 step 6)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VerifyResult:
    handshake: bool = False
    hook_dry_run: bool = False
    config_readback: bool = False
    errors: list[str] | None = None


def mcp_handshake(server_path: Path, timeout: float = 15.0) -> bool:
    node = shutil.which("node")
    if not node or not server_path.is_file():
        return False
    proc = subprocess.Popen(
        [node, str(server_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        req = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "minni-wire", "version": "0.0.0"},
            },
        }) + "\n"
        assert proc.stdin is not None
        proc.stdin.write(req)
        proc.stdin.flush()
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line:
            return False
        resp = json.loads(line)
        return "result" in resp or "error" in resp
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError):
        return False
    finally:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def hook_dry_run(hook_path: Path, event: str = "SessionStart") -> bool:
    node = shutil.which("node")
    if not node or not hook_path.is_file():
        return False
    try:
        proc = subprocess.run(
            [node, str(hook_path), event],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _readback_server_path(config_path: Path, config_kind: str) -> str | None:
    if not config_path.exists():
        return None
    if config_kind == "claude-json":
        data = json.loads(config_path.read_text(encoding="utf-8"))
        args = data.get("mcpServers", {}).get("minni", {}).get("args", [])
        return str(args[0]) if args else None
    if config_kind == "kilo-json":
        data = json.loads(config_path.read_text(encoding="utf-8"))
        cmd = data.get("mcp", {}).get("minni", {}).get("command", [])
        if isinstance(cmd, list) and len(cmd) >= 2:
            return str(cmd[1])
        return None
    if config_kind == "toml":
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
        args = data.get("mcp_servers", {}).get("minni", {}).get("args", [])
        return str(args[0]) if args else None
    if config_kind == "mcp-json-only":
        data = json.loads(config_path.read_text(encoding="utf-8"))
        args = data.get("mcpServers", {}).get("minni", {}).get("args", [])
        return str(args[0]) if args else None
    if config_kind == "antigravity":
        data = json.loads(config_path.read_text(encoding="utf-8"))
        args = data.get("mcpServers", {}).get("minni", {}).get("args", [])
        return str(args[-1]) if args else None
    return None


def config_readback(
    config_path: Path | None,
    config_kind: str,
    expected_server: Path,
) -> bool:
    if config_path is None:
        return config_kind in ("antigravity", "gemini-provisional")
    stamped = _readback_server_path(config_path, config_kind)
    if stamped is None:
        return False
    return Path(stamped).resolve() == expected_server.resolve()


def run_verify(
    install_root: Path,
    hook_entry: str | None,
    config_path: Path | None,
    config_kind: str,
) -> VerifyResult:
    server = install_root / "dist" / "server.js"
    result = VerifyResult(errors=[])
    result.handshake = mcp_handshake(server)
    if hook_entry:
        result.hook_dry_run = hook_dry_run(install_root / hook_entry)
    else:
        result.hook_dry_run = True
    result.config_readback = config_readback(
        config_path, config_kind, server,
    )
    return result
"""Post-wire verification probes (§4.4 step 6)."""

from __future__ import annotations

import json
import math
import os
import selectors
import shutil
import signal
import subprocess
import time
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
    """Probe initialize within a wall-clock budget, then reap the probe process.

    Pipes stay open for the MCP session: communicate() would wait for server
    exit, and readline() can wait forever on a silent or partial response.
    """
    node = shutil.which("node")
    if not node or not server_path.is_file() or not math.isfinite(timeout) or timeout <= 0:
        return False
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            [node, str(server_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except OSError:
        return False
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
        proc.stdin.write(req.encode("utf-8"))
        proc.stdin.flush()
        assert proc.stdout is not None
        buffered = bytearray()
        received = 0
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return False
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    return False
                received += len(chunk)
                if received > 1024 * 1024:
                    return False
                buffered.extend(chunk)
                while b"\n" in buffered:
                    if time.monotonic() >= deadline:
                        return False
                    line, _, rest = buffered.partition(b"\n")
                    buffered = bytearray(rest)
                    resp = json.loads(line)
                    if not isinstance(resp, dict) or resp.get("jsonrpc") != "2.0":
                        return False
                    # A server can emit notifications before its reply.
                    if "id" not in resp and isinstance(resp.get("method"), str):
                        continue
                    if type(resp.get("id")) is not int or resp["id"] != 1 or "error" in resp:
                        return False
                    result = resp.get("result")
                    if not isinstance(result, dict) or result.get("protocolVersion") != "2024-11-05":
                        return False
                    info = result.get("serverInfo")
                    return (
                        isinstance(result.get("capabilities"), dict)
                        and isinstance(info, dict)
                        and all(isinstance(info.get(key), str) and bool(info[key].strip())
                                for key in ("name", "version"))
                    )
    except (ValueError, OSError, subprocess.SubprocessError):
        return False
    finally:
        # The probe owns this new process group; do not leave helper children
        # behind when initialization fails or when a valid server stays alive.
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            # Darwin can report EPERM for an already-exited session leader.
            # Popen.kill polls first, so an exited child still gets reaped.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        finally:
            proc.stdin.close()
            proc.stdout.close()


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

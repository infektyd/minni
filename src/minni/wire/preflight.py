"""Preflight checks before wire touches the filesystem."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from minni.wire.platform import config_root_exists

NODE_INSTALL_HINT = "Install Node.js 20+ (macOS: brew install node)"


def parse_node_version(raw: str) -> tuple[int, int, int] | None:
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", raw.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def check_node(min_version: int = 20) -> tuple[bool, str]:
    node = shutil.which("node")
    if not node:
        return False, f"node not found on PATH; {NODE_INSTALL_HINT}"
    try:
        out = subprocess.check_output([node, "--version"], text=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return False, f"node --version failed: {exc}; {NODE_INSTALL_HINT}"
    parsed = parse_node_version(out)
    if parsed is None:
        return False, f"cannot parse node version {out.strip()!r}; {NODE_INSTALL_HINT}"
    major, _, _ = parsed
    if major < min_version:
        return False, (
            f"node {out.strip()} is older than {min_version}; {NODE_INSTALL_HINT}"
        )
    return True, out.strip()


def check_config_root(platform: str) -> tuple[bool, str]:
    ok, probed = config_root_exists(platform)
    if ok:
        return True, ""
    paths = ", ".join(probed)
    return False, (
        f"no config root found for {platform} (probed: {paths}); "
        "create the platform config or use --install-root"
    )


def informational_probe(binary: str) -> dict[str, object]:
    path = shutil.which(binary)
    return {"present": path is not None, "path": path}


def preflight_platform(platform: str) -> list[str]:
    errors: list[str] = []
    ok, msg = check_node()
    if not ok:
        errors.append(msg)
    if platform in ("gemini", "antigravity"):
        if not informational_probe("agy")["present"]:
            print(
                "[wire] agy not on PATH; guard-hook registration will be skipped",
                file=sys.stderr,
            )
        if not informational_probe("mcp-env-run")["present"]:
            print(
                "[wire] mcp-env-run not on PATH; gemini surfaces will use command: node",
                file=sys.stderr,
            )
    if platform not in ("generic", "gemini"):
        ok_root, root_msg = check_config_root(platform)
        if not ok_root and platform != "antigravity":
            errors.append(root_msg)
    return errors
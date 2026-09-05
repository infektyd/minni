"""Inspect existing Hermes source bindings without writing host configuration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def _yaml(data: bytes) -> dict:
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            if key in result:
                raise ValueError("duplicate YAML key")
            result[key] = loader.construct_object(value_node)
        return result

    UniqueLoader.add_constructor("tag:yaml.org,2002:map", mapping)
    result = yaml.load(data, Loader=UniqueLoader)
    if not isinstance(result, dict):
        raise ValueError("configuration must be a mapping")
    return result


def inspect_hermes(*, repo: Path | None, new_root: Path | None = None, dry_run: bool = False) -> dict:
    """Report artifact evidence separately from unobserved loaded-session state."""
    result = {"name": "hermes_source_binding", "exit_code": 0, "configuration": "unchanged", "runtime": "not_probed"}
    home = Path(os.environ.get("HOME") or Path.home())
    config = home / ".hermes/config.yaml"

    def skip(reason):
        return {**result, "skipped": True, "reason": reason}

    def incomplete(reason):
        return {**result, "exit_code": 1, "status": "incomplete", "reason": reason}

    if not config.exists() and not config.is_symlink():
        return skip("Hermes configuration absent; no activation")
    if config.is_symlink():
        return incomplete("Hermes configuration symlink preserved; not validated")
    try:
        original = config.read_bytes()
        data = _yaml(original)
        servers = data.get("mcp_servers", {})
        if not isinstance(servers, dict):
            raise ValueError("invalid server table")
        entry = servers.get("minni")
        if entry is None:
            return skip("Hermes Minni binding absent; no activation")
        if not isinstance(entry, dict):
            raise ValueError("invalid Minni entry")
        if entry.get("enabled") is False:
            return skip("Hermes Minni binding disabled; preserved")
        if "enabled" in entry and not isinstance(entry["enabled"], bool):
            raise ValueError("invalid enabled flag")
        executable = home / ".local/bin/hermes"
        if not shutil.which("hermes") and not (executable.is_file() and os.access(executable, os.X_OK)):
            return skip("Hermes executable unavailable; configuration preserved")
        env = entry.get("env", {})
        if not isinstance(env, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise ValueError("invalid environment mapping")
        if repo is None:
            return incomplete("Packaged sync cannot refresh the existing Hermes source binding; build its checkout")
        repo = repo.resolve()
        server = repo / "plugins/minni/dist/server.js"
        if (
            entry.get("command") not in ("node", "nodejs")
            or entry.get("args") != [str(server)]
            or entry.get("url")
            or entry.get("transport", "stdio") != "stdio"
        ):
            return incomplete("Hermes launcher does not match this checkout; preserved without migration")
        if dry_run:
            return {
                **result,
                "status": "dry-run",
                "artifact": "not_validated",
                "reason": "Would validate source artifact after build; existing sessions require /reload-mcp",
            }
        if server.is_symlink() or server.parent.is_symlink() or not server.is_file():
            return incomplete("Hermes source server missing or symlinked")
        # Do not inherit a caller git context (for example a pre-push hook).
        git_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

        def git(*args):
            return subprocess.check_output(
                ["git", "-C", str(repo), *args], env=git_env, stderr=subprocess.DEVNULL, text=True
            ).strip()

        sha = git("rev-parse", "HEAD")
        build = json.loads((server.parent / "build-manifest.json").read_bytes())
        if git("status", "--porcelain") or build.get("git_dirty") is not False or build.get("git_sha") != sha:
            return incomplete("Hermes source/build provenance is dirty or stale")
        if new_root is None:
            return incomplete("No installer payload available to verify Hermes source bytes")
        from minni.wire.custom_refresh import _payload
        from minni.wire.paths import plugin_base

        manifest = _payload(Path(new_root), plugin_base())
        digest = hashlib.sha256(server.read_bytes()).hexdigest()
        if manifest.git_sha != sha or manifest.files.get("dist/server.js") != "sha256:" + digest:
            return incomplete("Hermes source bytes differ from verified installer payload")
        if config.is_symlink() or config.read_bytes() != original:
            return incomplete("Hermes configuration changed during validation")
        return {
            **result,
            "status": "artifact_current",
            "artifact": "verified",
            "git_sha": sha,
            "server_sha256": digest,
            "reload_required": True,
            "reason": "Source binding verified; existing Hermes sessions require /reload-mcp, new sessions load current code",
        }
    except Exception as exc:
        return incomplete(f"Hermes binding validation failed ({type(exc).__name__}); configuration preserved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--wire-report", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from minni.wire.custom_refresh import wire_report_root

    target = wire_report_root(args.wire_report.read_text()) if args.wire_report else None
    result = inspect_hermes(repo=args.repo, new_root=target, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()

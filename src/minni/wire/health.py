"""Unified drift health check and canonical .minni/plugin uncatalogued scanner (§4.7)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minni.wire.containers import resolve_verified_plugin_root
from minni.wire.gc import inspect_running_mcp_servers
from minni.wire.manifest import PayloadManifest
from minni.wire.registry import (
    HOST_SURFACE_CATALOG,
    evaluate_mcp_surface_diff,
    expand_for_home,
    expand_host_surface_catalog,
    resolve_home,
)

_SCAN_DIR_RELATIVES: tuple[str, ...] = (
    ".claude",
    ".codex",
    ".gemini",
    ".grok",
    ".cursor",
    ".config/kilo",
    ".config/muse",
    ".config/devin",
    ".muse",
    ".agents",
    ".local/bin",
    "Library/Application Support/Claude",
)

_EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({
    "node_modules",
    ".git",
    "Cache",
    "GPUCache",
    "Code Cache",
    "DawnCache",
    "logs",
    "projects",
    "sessions",
    "shell-snapshots",
    "statsig",
    "telemetry",
    "archive",
    "crashpad",
    "blob_storage",
    "Local Storage",
    "Session Storage",
})

_EXCLUDED_FILE_NAMES: frozenset[str] = frozenset({
    "history.jsonl",
    ".DS_Store",
})

_ALLOWED_SCAN_SUFFIXES: frozenset[str] = frozenset({
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".sh",
    ".zsh",
    ".js",
    ".mjs",
    ".md",
    "",
})

_MAX_SCAN_FILE_BYTES = 2 * 1024 * 1024
_PLUGIN_REF_RE = re.compile(r"(\.minni/plugin|sovereign-memory)")


def _catalogued_paths_and_prefixes(home: Path) -> tuple[set[str], tuple[str, ...]]:
    expanded = expand_host_surface_catalog(home=home)
    exact_paths: set[str] = set()
    container_prefixes: list[str] = [
        str(home / ".minni" / "plugin"),
        str(home / ".minni" / "archive"),
        str(home / ".agents" / "plugins" / "Minni@Minni"),
        str(home / ".agents" / "plugins" / "minni@minni"),
        str(home / ".cursor" / "plugins" / "local" / "minni"),
        str(home / ".claude" / "plugins" / "marketplaces" / "minni-local"),
        str(home / ".claude" / "local-marketplaces" / "minni"),
    ]
    for hspec in expanded.values():
        for surf in hspec.get("surfaces", []):
            if not isinstance(surf, dict):
                continue
            p = str(surf.get("path") or "")
            if p:
                exact_paths.add(p)
                try:
                    exact_paths.add(str(Path(p).resolve()))
                except OSError:
                    pass
            if surf.get("kind") in ("plugin_container", "claude_marketplace"):
                if p:
                    container_prefixes.append(p)
    # Also include legacy muse config.json as a known surface
    exact_paths.add(str(home / ".muse" / "config.json"))
    return exact_paths, tuple(container_prefixes)


def scan_for_uncatalogued_plugin_refs(home: Path | None = None) -> list[dict[str, str]]:
    """Scan host config directories for any uncatalogued file referencing `.minni/plugin` (§4.7)."""
    resolved_home = resolve_home(home)
    exact_paths, container_prefixes = _catalogued_paths_and_prefixes(resolved_home)
    findings: list[dict[str, str]] = []

    def _is_catalogued(fpath: Path) -> bool:
        s = str(fpath)
        if s in exact_paths:
            return True
        try:
            sr = str(fpath.resolve())
            if sr in exact_paths:
                return True
        except OSError:
            sr = s
        for pref in container_prefixes:
            if s == pref or s.startswith(pref + "/") or sr == pref or sr.startswith(pref + "/"):
                return True
        return False

    def _inspect_file(fpath: Path) -> None:
        if fpath.name in _EXCLUDED_FILE_NAMES or fpath.name.endswith(".jsonl"):
            return
        if fpath.suffix.lower() not in _ALLOWED_SCAN_SUFFIXES:
            return
        if _is_catalogued(fpath):
            return
        try:
            st = fpath.stat()
            if st.st_size > _MAX_SCAN_FILE_BYTES:
                return
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        m = _PLUGIN_REF_RE.search(text)
        if m:
            findings.append({
                "path": str(fpath),
                "matched": m.group(1),
                "reason": "uncatalogued_file_references_minni_plugin",
            })

    # Check top-level ~/.claude.json
    claude_top = resolved_home / ".claude.json"
    if claude_top.is_file():
        _inspect_file(claude_top)

    for rel in _SCAN_DIR_RELATIVES:
        root_dir = resolved_home / rel
        if not root_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=False):
            dp = Path(dirpath)
            dirnames[:] = [
                d for d in dirnames
                if d not in _EXCLUDED_DIR_NAMES and not _is_catalogued(dp / d)
            ]
            for fn in filenames:
                fp = dp / fn
                if fp.is_symlink():
                    continue
                _inspect_file(fp)

    return findings


@dataclass
class HostPropagationHealthReport:
    ok: bool
    current_valid: bool
    current_version: str = ""
    current_git_sha: str = ""
    current_error: str = ""
    drifted_surfaces: list[dict[str, Any]] = field(default_factory=list)
    uncatalogued_refs: list[dict[str, str]] = field(default_factory=list)
    running_stale_servers: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "current_valid": self.current_valid,
            "current_version": self.current_version,
            "current_git_sha": self.current_git_sha,
            "current_error": self.current_error,
            "drifted_surfaces": list(self.drifted_surfaces),
            "uncatalogued_refs": list(self.uncatalogued_refs),
            "running_stale_servers": list(self.running_stale_servers),
        }


def audit_host_propagation_health(
    *,
    home: Path | None = None,
    expected_git_sha: str | None = None,
) -> HostPropagationHealthReport:
    """Run the unified 4-layer drift health check across `current`, catalogued surfaces, uncatalogued scan, and live PIDs (§4.7)."""
    resolved_home = resolve_home(home)
    plugin_dir = resolved_home / ".minni" / "plugin"
    current_link = plugin_dir / "current"

    current_valid = False
    current_version = ""
    current_git_sha = ""
    current_error = ""

    try:
        target = resolve_verified_plugin_root(current_link, expected_base=plugin_dir)
        manifest = PayloadManifest.load(target / "payload-manifest.json")
        current_version = manifest.version
        current_git_sha = manifest.git_sha
        if expected_git_sha and current_git_sha and not current_git_sha.startswith(expected_git_sha) and not expected_git_sha.startswith(current_git_sha):
            current_error = f"current git_sha {current_git_sha} != expected {expected_git_sha}"
        else:
            current_valid = True
    except Exception as exc:
        current_error = str(exc)

    drifted_surfaces: list[dict[str, Any]] = []
    for host_id, hspec in HOST_SURFACE_CATALOG.items():
        for surf in hspec.get("surfaces", []):
            if not isinstance(surf, dict):
                continue
            kind = str(surf.get("kind") or "")
            spath = Path(expand_for_home(str(surf.get("path") or ""), home=resolved_home))
            if kind == "mcp_json" and spath.is_file():
                try:
                    doc = json.loads(spath.read_text(encoding="utf-8"))
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unparseable_json:{exc}"]})
                    continue
                if not isinstance(doc, dict):
                    continue
                ckey = "mcp" if surf.get("key") == "mcp.minni" else "mcpServers"
                servers = doc.get(ckey)
                if not isinstance(servers, dict) or "minni" not in servers:
                    continue
                diff = evaluate_mcp_surface_diff(
                    servers.get("minni"),
                    host_id=host_id,
                    agent_id=str(hspec["agent_id"]),
                    vault_path=str(hspec["vault_path"]),
                    surface_spec=surf,
                    home=resolved_home,
                )
                if diff.has_delta:
                    drifted_surfaces.append({
                        "host": host_id,
                        "path": str(spath),
                        "reasons": diff.reasons,
                    })
            elif kind == "claude_plugin_registry" and spath.is_file():
                try:
                    doc = json.loads(spath.read_text(encoding="utf-8"))
                    plugins = doc.get("plugins", {}) if isinstance(doc, dict) else {}
                    entries = plugins.get("minni@minni-local") or plugins.get("minni@minni") or []
                    for item in entries if isinstance(entries, list) else []:
                        if not isinstance(item, dict):
                            continue
                        inst_p = str(item.get("installPath") or "")
                        proj_p = str(item.get("projectPath") or "")
                        ver = str(item.get("version") or "")
                        sha = str(item.get("gitCommitSha") or "")
                        reasons: list[str] = []
                        if "~" in inst_p or "~" in proj_p:
                            reasons.append("literal_tilde_in_installed_plugins")
                        if current_version and ver != current_version:
                            reasons.append(f"version_mismatch:{ver}!={current_version}")
                        if current_git_sha and sha != current_git_sha:
                            reasons.append(f"sha_mismatch:{sha}!={current_git_sha}")
                        if reasons:
                            drifted_surfaces.append({
                                "host": host_id,
                                "path": str(spath),
                                "reasons": reasons,
                            })
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unparseable_json:{exc}"]})

    uncatalogued_refs = scan_for_uncatalogued_plugin_refs(resolved_home)
    all_running = inspect_running_mcp_servers(
        home=resolved_home,
        target_version=current_version or "",
        target_sha=current_git_sha or expected_git_sha or "",
    )
    running_stale = [r for r in all_running if r.get("stale")]

    ok = current_valid and not drifted_surfaces and not uncatalogued_refs and not running_stale
    return HostPropagationHealthReport(
        ok=ok,
        current_valid=current_valid,
        current_version=current_version,
        current_git_sha=current_git_sha,
        current_error=current_error,
        drifted_surfaces=drifted_surfaces,
        uncatalogued_refs=uncatalogued_refs,
        running_stale_servers=running_stale,
    )

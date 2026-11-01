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
        str(home / ".agents" / "skills"),
        str(home / ".cursor" / "plugins" / "local" / "minni"),
        str(home / ".cursor" / "skills"),
        str(home / ".claude" / "plugins" / "marketplaces" / "minni-local"),
        str(home / ".claude" / "plugins" / "cache"),
        str(home / ".claude" / "local-marketplaces" / "minni"),
        str(home / ".claude" / "skills"),
        str(home / ".codex" / "plugins" / "cache"),
        str(home / ".codex" / "skills"),
        str(home / ".gemini" / "skills"),
        str(home / ".grok" / "skills"),
        str(home / ".config" / "kilo" / "plugins" / "minni"),
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
            for vp in surf.get("paths") or []:
                vp_s = str(vp or "")
                if vp_s:
                    exact_paths.add(vp_s)
                    try:
                        exact_paths.add(str(Path(vp_s).resolve()))
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

    current_has_files = False
    target: Path | None = None
    try:
        verified_target = resolve_verified_plugin_root(current_link, expected_base=plugin_dir)
        manifest = PayloadManifest.load(verified_target / "payload-manifest.json")
        target = verified_target
        current_version = manifest.version
        current_git_sha = manifest.git_sha
        current_has_files = bool(manifest.files)
        if expected_git_sha and current_git_sha and not current_git_sha.startswith(expected_git_sha) and not expected_git_sha.startswith(current_git_sha):
            current_error = f"current git_sha {current_git_sha} != expected {expected_git_sha}"
        else:
            current_valid = True
    except Exception as exc:
        target = None
        current_error = str(exc)

    import tomllib
    from minni.wire.registry import _VERSIONED_PLUGIN_RE

    wired_doc: dict[str, Any] | None = None
    if (plugin_dir / "wired.json").is_file():
        try:
            loaded_w = json.loads((plugin_dir / "wired.json").read_text(encoding="utf-8"))
            if isinstance(loaded_w, dict):
                wired_doc = loaded_w
        except Exception:
            wired_doc = None

    if (
        isinstance(wired_doc, dict)
        and wired_doc.get("schema_version", wired_doc.get("schema", 1)) == 1
        and wired_doc.get("wires") == []
        and "hosts" not in wired_doc
        and not current_has_files
    ):
        current_valid = False
        target = None

    # Identify if this home is a synthetic single-version v1 wired.json fixture (no valid `current` link)
    legacy_v1_single_server_js: str | None = None
    if not current_valid and isinstance(wired_doc, dict):
        try:
            if wired_doc.get("schema_version", wired_doc.get("schema", 1)) == 1:
                valid_vdirs = [
                    d for d in plugin_dir.iterdir()
                    if d.is_dir() and not d.is_symlink() and not d.name.startswith(".") and (d / "payload-manifest.json").is_file()
                ]
                if len(valid_vdirs) == 1:
                    legacy_v1_single_server_js = str(valid_vdirs[0] / "dist" / "server.js")
        except Exception:
            legacy_v1_single_server_js = None

    def _mcp_surface_expected_on_home(host_id: str, spath: Path) -> bool:
        if not current_valid:
            return False
        if spath.name == ".claude.json":
            return (resolved_home / ".claude").is_dir()
        if spath.name == "kilo.json":
            return (resolved_home / ".config" / "kilo").is_dir() and (
                (resolved_home / ".config" / "kilo" / "plugin").is_dir()
                or not (resolved_home / ".config" / "kilo" / "plugins" / "minni").is_dir()
            )
        if spath == resolved_home / ".muse" / "mcp.json":
            return spath.parent.is_dir() and not (spath.parent / "config.json").is_file()
        if spath == resolved_home / ".config" / "muse" / "settings.json":
            return spath.parent.is_dir() and not (resolved_home / ".muse" / "config.json").is_file()
        return spath.parent.is_dir()

    def _commands_dir_expected_on_home(spath: Path) -> bool:
        if not current_valid:
            return False
        return spath.parent.is_dir() or (spath.parent.parent != resolved_home and spath.parent.parent.is_dir())

    def _is_clean_legacy_v1_entry(live_entry: Any, *, is_kilo: bool) -> bool:
        if not legacy_v1_single_server_js or not isinstance(live_entry, dict):
            return False
        env_key = "environment" if is_kilo else "env"
        if env_key in live_entry:
            return False
        if live_entry.get("enabled") is False:
            return False
        if is_kilo:
            return (
                live_entry.get("command") == ["node", legacy_v1_single_server_js]
                and live_entry.get("enabled") is True
            )
        cmd = live_entry.get("command")
        args = live_entry.get("args")
        if cmd == "node" and args == [legacy_v1_single_server_js]:
            return True
        if isinstance(cmd, str) and cmd.endswith("mcp-env-run") and args == ["node", legacy_v1_single_server_js]:
            return True
        return False

    def _audit_container_dir(host_id: str, cpath: Path) -> list[str]:
        reasons: list[str] = []
        if cpath.is_symlink() and not cpath.exists():
            return ["broken_container_symlink"]
        srv_js = cpath / "dist" / "server.js"
        if not srv_js.is_file():
            reasons.append("missing_container_server_js")
        elif target is not None and (target / "dist" / "server.js").is_file():
            try:
                if srv_js.read_bytes() != (target / "dist" / "server.js").read_bytes():
                    reasons.append("container_dist_server_js_mismatch")
            except OSError as exc:
                reasons.append(f"unreadable_container_server_js:{exc}")
        if target is not None:
            for rel_pjson in (".claude-plugin/plugin.json", ".cursor-plugin/plugin.json"):
                pj = cpath / rel_pjson
                if pj.is_file():
                    try:
                        pdoc = json.loads(pj.read_text(encoding="utf-8"))
                        if not isinstance(pdoc, dict):
                            reasons.append(f"invalid_{rel_pjson}")
                        else:
                            if "mcpServers" in pdoc:
                                reasons.append(f"mcpServers_in_{rel_pjson}")
                            if base_semver and pdoc.get("version") != base_semver:
                                reasons.append(f"version_mismatch_in_{rel_pjson}:{pdoc.get('version')}!={base_semver}")
                    except Exception as exc:
                        reasons.append(f"unparseable_{rel_pjson}:{exc}")
        return reasons

    base_semver = current_version.split("+", 1)[0] if current_version else ""
    drifted_surfaces: list[dict[str, Any]] = []
    for host_id, hspec in HOST_SURFACE_CATALOG.items():
        for surf in hspec.get("surfaces", []):
            if not isinstance(surf, dict):
                continue
            kind = str(surf.get("kind") or "")
            spath = Path(expand_for_home(str(surf.get("path") or ""), home=resolved_home))
            if kind == "plugin_container":
                if spath.exists() or spath.is_symlink():
                    creasons = _audit_container_dir(host_id, spath)
                    if creasons:
                        drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": creasons})
                continue
            if kind != "commands_dir" and kind != "mcp_view_mirrors":
                if (spath.exists() or spath.is_symlink()) and not spath.is_file():
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": ["broken_or_non_regular_file"]})
                    continue
            if kind == "mcp_json" and spath.is_file():
                try:
                    doc = json.loads(spath.read_text(encoding="utf-8"))
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unparseable_json:{exc}"]})
                    continue
                if not isinstance(doc, dict):
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": ["root_not_dict"]})
                    continue
                ckey = "mcp" if surf.get("key") == "mcp.minni" else "mcpServers"
                servers = doc.get(ckey)
                live_minni = servers.get("minni") if isinstance(servers, dict) else None
                if _is_clean_legacy_v1_entry(live_minni, is_kilo=(ckey == "mcp")):
                    continue
                diff = evaluate_mcp_surface_diff(
                    live_minni,
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
            elif kind == "mcp_json" and _mcp_surface_expected_on_home(host_id, spath):
                diff = evaluate_mcp_surface_diff(
                    None,
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
            elif kind == "mcp_view_mirrors":
                for vp_raw in surf.get("paths") or []:
                    vpath = Path(expand_for_home(str(vp_raw), home=resolved_home))
                    if (vpath.exists() or vpath.is_symlink()) and not vpath.is_file():
                        drifted_surfaces.append({"host": host_id, "path": str(vpath), "reasons": ["broken_or_non_regular_file"]})
                        continue
                    if not vpath.is_file():
                        continue
                    try:
                        doc = json.loads(vpath.read_text(encoding="utf-8"))
                    except Exception as exc:
                        drifted_surfaces.append({"host": host_id, "path": str(vpath), "reasons": [f"unparseable_json:{exc}"]})
                        continue
                    if not isinstance(doc, dict):
                        drifted_surfaces.append({"host": host_id, "path": str(vpath), "reasons": ["root_not_dict"]})
                        continue
                    servers = doc.get("mcpServers")
                    live_minni = servers.get("minni") if isinstance(servers, dict) else None
                    view_spec = {
                        "kind": "mcp_json",
                        "path": str(vp_raw),
                        "key": "mcpServers.minni",
                        "command": surf.get("command"),
                        "args_prefix": surf.get("args_prefix"),
                        "entry": surf.get("entry"),
                        "accepted_entries": [
                            str(surf.get("entry") or ""),
                            "~/.agents/plugins/minni@minni/dist/server.js",
                            "~/.minni/plugin/current/dist/server.js",
                        ],
                    }
                    diff = evaluate_mcp_surface_diff(
                        live_minni,
                        host_id=host_id,
                        agent_id=str(hspec["agent_id"]),
                        vault_path=str(hspec["vault_path"]),
                        surface_spec=view_spec,
                        home=resolved_home,
                    )
                    if diff.has_delta:
                        drifted_surfaces.append({
                            "host": host_id,
                            "path": str(vpath),
                            "reasons": diff.reasons,
                        })
            elif kind == "mcp_toml" and spath.is_file():
                try:
                    doc = tomllib.loads(spath.read_text(encoding="utf-8"))
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unparseable_toml:{exc}"]})
                    continue
                servers = doc.get("mcp_servers")
                live_minni = servers.get("minni") if isinstance(servers, dict) else None
                if _is_clean_legacy_v1_entry(live_minni, is_kilo=False):
                    continue
                diff = evaluate_mcp_surface_diff(
                    live_minni,
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
            elif kind == "mcp_toml" and _mcp_surface_expected_on_home(host_id, spath):
                diff = evaluate_mcp_surface_diff(
                    None,
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
            elif kind == "hooks_json" and spath.is_file():
                try:
                    raw_text = spath.read_text(encoding="utf-8")
                    json.loads(raw_text)
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unparseable_json:{exc}"]})
                    continue
                reasons: list[str] = []
                if _VERSIONED_PLUGIN_RE.search(raw_text):
                    reasons.append("versioned_sha_in_hooks_json")
                if "~/" in raw_text:
                    reasons.append("literal_tilde_in_hooks_json")
                if surf.get("use_exec") and "node " in raw_text and "exec node " not in raw_text:
                    reasons.append("missing_exec_in_hooks_json")
                if reasons:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": reasons})
            elif kind in ("launcher_wrapper", "hook_wrapper", "js_bridge", "rules_md", "skill_copy") and spath.is_file():
                try:
                    raw_text = spath.read_text(encoding="utf-8")
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unreadable:{exc}"]})
                    continue
                reasons = []
                if _VERSIONED_PLUGIN_RE.search(raw_text):
                    reasons.append(f"versioned_sha_in_{kind}")
                if "~/" in raw_text:
                    reasons.append(f"literal_tilde_in_{kind}")
                if surf.get("use_exec") and "node " in raw_text and "exec node " not in raw_text:
                    reasons.append(f"missing_exec_in_{kind}")
                if reasons:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": reasons})
            elif kind == "commands_dir":
                if spath.is_symlink():
                    expected_target = expand_for_home(str(surf.get("entry") or ""), home=resolved_home)
                    try:
                        actual_target = os.readlink(spath)
                    except OSError:
                        actual_target = ""
                    if actual_target != expected_target:
                        drifted_surfaces.append({
                            "host": host_id,
                            "path": str(spath),
                            "reasons": [f"commands_dir_target_mismatch:{actual_target}!={expected_target}"],
                        })
                    elif not spath.exists():
                        drifted_surfaces.append({
                            "host": host_id,
                            "path": str(spath),
                            "reasons": ["broken_commands_dir_symlink"],
                        })
                elif spath.exists():
                    drifted_surfaces.append({
                        "host": host_id,
                        "path": str(spath),
                        "reasons": ["broken_or_non_symlink_commands_dir"],
                    })
                elif _commands_dir_expected_on_home(spath):
                    drifted_surfaces.append({
                        "host": host_id,
                        "path": str(spath),
                        "reasons": ["missing_commands_dir"],
                    })
            elif kind == "claude_plugin_registry" and spath.is_file():
                try:
                    doc = json.loads(spath.read_text(encoding="utf-8"))
                    plugins = doc.get("plugins", {}) if isinstance(doc, dict) else {}
                    entries = plugins.get("minni@minni-local") or plugins.get("minni@minni") or []
                    if not isinstance(entries, list) or not entries:
                        drifted_surfaces.append({
                            "host": host_id,
                            "path": str(spath),
                            "reasons": ["missing_minni_in_installed_plugins"],
                        })
                    for item in entries if isinstance(entries, list) else []:
                        if not isinstance(item, dict):
                            continue
                        inst_p = str(item.get("installPath") or "")
                        proj_p = str(item.get("projectPath") or "")
                        ver = str(item.get("version") or "")
                        sha = str(item.get("gitCommitSha") or "")
                        reasons = []
                        if "~" in inst_p or "~" in proj_p:
                            reasons.append("literal_tilde_in_installed_plugins")
                        if base_semver and ver != base_semver:
                            reasons.append(f"version_mismatch:{ver}!={base_semver}")
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
            elif (
                kind == "claude_plugin_registry"
                and current_valid
                and spath.parent.is_dir()
                and not (spath.parent / "cache").is_dir()
            ):
                drifted_surfaces.append({
                    "host": host_id,
                    "path": str(spath),
                    "reasons": ["missing_claude_plugin_registry"],
                })
            elif kind == "claude_marketplace" and spath.is_file():
                try:
                    raw_text = spath.read_text(encoding="utf-8")
                    json.loads(raw_text)
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unparseable_json:{exc}"]})
                    continue
                if "~/" in raw_text or _VERSIONED_PLUGIN_RE.search(raw_text):
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": ["stale_or_tilde_in_claude_marketplace"]})

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

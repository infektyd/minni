"""Unified drift health check and canonical .minni/plugin uncatalogued scanner (§4.7)."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minni.wire.containers import resolve_verified_plugin_root
from minni.wire.gc import inspect_running_mcp_servers
from minni.wire.manifest import PayloadManifest
from minni.wire.registry import (
    HOST_SURFACE_CATALOG,
    _VERSIONED_PLUGIN_RE,
    evaluate_mcp_surface_diff,
    expand_for_home,
    expand_host_surface_catalog,
    node_invocation_lacks_exec,
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

# Scanner denies data stores ONLY by explicit home-relative path prefixes
# (`UNCATALOGUED_SCAN_DENYLIST_REL_PREFIXES`), NEVER by directory name (§4.7 R8 P2).
# `extensions/`, `cache/`, and `node_modules/` under host config roots stay
# scannable unless an explicit home-relative prefix lists them.
UNCATALOGUED_SCAN_DENYLIST_DIRS: frozenset[str] = frozenset()

# Explicit, documented deny-list of home-relative path prefixes for data-store
# subtrees only (brain, conversations, file-history, backups, muse-code-worker,
# session stores, Electron/Chromium browser caches) (§4.7 R8 P2).
UNCATALOGUED_SCAN_DENYLIST_REL_PREFIXES: tuple[str, ...] = (
    ".gemini/antigravity/brain",
    ".gemini/antigravity/conversations",
    ".gemini/antigravity/html_artifacts",
    ".gemini/antigravity/scratch",
    ".gemini/antigravity/worktrees",
    ".gemini/antigravity-ide/brain",
    ".gemini/antigravity-ide/conversations",
    ".gemini/antigravity-ide/browser_recordings",
    ".gemini/antigravity-ide/html_artifacts",
    ".gemini/antigravity-ide/scratch",
    ".gemini/antigravity-cli/brain",
    ".gemini/antigravity-cli/conversations",
    ".gemini/antigravity-cli/scratch",
    ".gemini/antigravity-backup",
    ".gemini/antigravity-browser-profile",
    ".gemini/tmp",
    ".gemini/history",
    ".agents/backups",
    ".agents/archive-20260925-agents-md",
    ".codex/muse-code-worker",
    ".codex/grok-build-peer",
    ".codex/antigravity-peer",
    ".codex/cursor-peer",
    ".codex/backups",
    ".codex/sessions",
    ".codex/archived_sessions",
    ".codex/memories",
    ".codex/memories_v2",
    ".claude/file-history",
    ".claude/backups",
    ".claude/session-env",
    ".claude/sessions",
    ".claude/shell-snapshots",
    ".claude/paste-cache",
    "Library/Application Support/Claude/local-agent-mode-sessions",
    "Library/Application Support/Claude/claude-code-sessions",
    "Library/Application Support/Claude/Cache",
    "Library/Application Support/Claude/Code Cache",
    "Library/Application Support/Claude/GPUCache",
    "Library/Application Support/Claude/GPUPersistentCache",
    "Library/Application Support/Claude/DawnGraphiteCache",
    "Library/Application Support/Claude/DawnWebGPUCache",
    "Library/Application Support/Claude/GraphiteDawnCache",
    "Library/Application Support/Claude/IndexedDB",
    "Library/Application Support/Claude/Local Storage",
    "Library/Application Support/Claude/Session Storage",
    "Library/Application Support/Claude/WebStorage",
    "Library/Application Support/Claude/blob_storage",
    "Library/Application Support/Claude/Crashpad",
    "Library/Application Support/Claude/Partitions",
    ".grok/memory",
    ".grok/memory-v2",
    ".grok/memtrace",
    ".grok/sessions",
    ".grok/relocations",
    ".grok/scratch",
    ".grok/worktrees",
    ".cursor/legacy-plugins",
    ".cursor/chats",
    ".cursor/acp-sessions",
    ".cursor/ai-tracking",
    ".cursor/browser-logs",
    ".cursor/plans",
    ".cursor/worktrees",
)

UNCATALOGUED_SCAN_DENYLIST_FILES: frozenset[str] = frozenset({
    "history.jsonl",
    "plugin-directory-cache-v2.json",
    ".DS_Store",
    "LOCK",
    "LOG",
    "Cookies",
    "Preferences",
})

_EXCLUDED_DIR_NAMES: frozenset[str] = UNCATALOGUED_SCAN_DENYLIST_DIRS
_EXCLUDED_FILE_NAMES: frozenset[str] = UNCATALOGUED_SCAN_DENYLIST_FILES

# Known config-file extensions in host config locations
_CONFIG_FILE_SUFFIXES: frozenset[str] = frozenset({
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".sh",
    ".zsh",
    ".js",
    ".mjs",
})

_BINARY_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xca\xfe\xba\xbe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\x7fELF",
)

# Known config depth ceiling relative to each scan root (e.g. ~/.claude/plugins/cache/<mkt>/<pkg>/<ver>/skills/<skill>/SKILL.md is depth 8)
_DEFAULT_MAX_CONFIG_DEPTH = 10
_MAX_CONFIG_REL_DEPTH_BY_ROOT: dict[str, int] = {
    "Library/Application Support/Claude": 4,
    ".local/bin": 2,
}

_MAX_SCAN_FILE_BYTES = 2 * 1024 * 1024
_PLUGIN_REF_RE = re.compile(r"(\.minni/plugin)")


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


def _is_denylisted_rel_path(rel_to_home: str) -> bool:
    norm = rel_to_home.strip("/")
    for pref in UNCATALOGUED_SCAN_DENYLIST_REL_PREFIXES:
        if norm == pref or norm.startswith(pref + "/"):
            return True
    return False


def _is_native_binary_file(fpath: Path) -> bool:
    """Return True if `fpath` starts with Mach-O or ELF executable binary magic bytes."""
    try:
        with fpath.open("rb") as f:
            head = f.read(4)
        return len(head) == 4 and head.startswith(_BINARY_MAGIC_PREFIXES)
    except OSError:
        return False


def _is_real_host_config_for_size_cap(fpath: Path, root_dir: Path) -> bool:
    """Return True if an oversized file is a real host config (not an extension/cache/node_modules bundle)."""
    try:
        rel_parts = fpath.relative_to(root_dir).parts
    except ValueError:
        rel_parts = (fpath.name,)
    parent_dirs = {p.lower() for p in rel_parts[:-1]}
    if parent_dirs & {
        "extensions",
        "cache",
        "marketplace-cache",
        "marketplaces",
        "bundled-marketplaces",
        "node_modules",
        ".tmp",
        "dist",
        "assets",
        "vendor",
        "webview",
        "out",
        "third_party",
    }:
        return False
    suffix = fpath.suffix.lower()
    if suffix not in {".json", ".toml", ".yaml", ".yml"}:
        return False
    lower_name = fpath.name.lower()
    if lower_name in {
        ".claude.json",
        "claude_desktop_config.json",
        "installed_plugins.json",
        "marketplace.json",
        "mcp.json",
        "mcp_config.json",
        "hooks.json",
        "minni.json",
        "settings.json",
        "config.json",
        "kilo.json",
        "config.toml",
        "gemini-extension.json",
        "plugin.json",
    }:
        return True
    stem = fpath.stem.lower()
    return any(tok in stem for tok in ("mcp", "hook", "setting", "config", "minni", "claude", "kilo"))


def _is_config_shaped_file(fpath: Path, root_dir: Path, rel_root: str) -> bool:
    """Return True if `fpath` is a config-shaped file (json/toml/yaml/js/sh/md skill/workflow/rule) at a known config depth."""
    name = fpath.name
    if name in UNCATALOGUED_SCAN_DENYLIST_FILES:
        return False
    lower_name = name.lower()
    if lower_name.endswith((".jsonl", ".log", ".bak", ".orig", ".tmp", ".lock", ".sqlite", ".db", ".par", ".headers")):
        return False
    if ".backup" in lower_name or ".minni-backup-" in lower_name or lower_name.startswith(".muse-launcher."):
        return False

    try:
        rel_parts = fpath.relative_to(root_dir).parts
    except ValueError:
        return False

    max_depth = _MAX_CONFIG_REL_DEPTH_BY_ROOT.get(rel_root, _DEFAULT_MAX_CONFIG_DEPTH)
    if len(rel_parts) > max_depth:
        return False

    suffix = fpath.suffix.lower()
    if suffix in _CONFIG_FILE_SUFFIXES:
        return True
    if suffix == ".md":
        parent_dirs = {p.lower() for p in rel_parts[:-1]}
        return name == "SKILL.md" or bool(parent_dirs & {"skills", "rules", "workflows", "commands"})
    if suffix == "":
        parent_dirs = {p.lower() for p in rel_parts[:-1]}
        if not (rel_root == ".local/bin" or bool(parent_dirs & {"bin", "scripts", "hooks"})):
            return False
        if fpath.is_symlink() and not fpath.exists():
            return "minni" in lower_name
        if _is_native_binary_file(fpath):
            return False
        return True
    return False


def scan_for_uncatalogued_plugin_refs(home: Path | None = None) -> list[dict[str, str]]:
    """Scan host config directories for any uncatalogued config-shaped file referencing `.minni/plugin` (§4.7)."""
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

    def _is_symlink_to_config_shaped(fpath: Path, root_dir: Path, rel_root: str) -> bool:
        if not fpath.is_symlink():
            return False
        try:
            resolved_target = fpath.resolve(strict=True)
        except OSError:
            return False
        if not resolved_target.is_file():
            return False
        if _is_config_shaped_file(fpath, root_dir, rel_root):
            return True
        return _is_config_shaped_file(resolved_target, resolved_target.parent, "")

    def _inspect_file(fpath: Path, root_dir: Path, rel_root: str) -> None:
        try:
            rel_to_home = str(fpath.relative_to(resolved_home))
        except ValueError:
            rel_to_home = ""
        if rel_to_home and _is_denylisted_rel_path(rel_to_home):
            return
        if _is_catalogued(fpath):
            return
        is_cfg = _is_config_shaped_file(fpath, root_dir, rel_root) or _is_symlink_to_config_shaped(
            fpath, root_dir, rel_root
        )
        if not is_cfg:
            if fpath.is_symlink():
                try:
                    link_target = os.readlink(fpath)
                except OSError:
                    link_target = ""
                m_link = _PLUGIN_REF_RE.search(link_target)
                if m_link:
                    findings.append({
                        "path": str(fpath),
                        "matched": m_link.group(1),
                        "reason": "uncatalogued_file_references_minni_plugin",
                    })
            return
        try:
            st = fpath.stat()
        except OSError:
            findings.append({
                "path": str(fpath),
                "matched": "<unreadable>",
                "reason": "unreadable_config_file",
            })
            return
        if st.st_size > _MAX_SCAN_FILE_BYTES:
            if _is_real_host_config_for_size_cap(fpath, root_dir):
                findings.append({
                    "path": str(fpath),
                    "matched": f"<{st.st_size}_bytes>",
                    "reason": "config_file_exceeds_size_cap",
                })
            return
        try:
            with fpath.open("r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            findings.append({
                "path": str(fpath),
                "matched": "<unreadable>",
                "reason": "unreadable_config_file",
            })
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
    if claude_top.exists() or claude_top.is_symlink():
        _inspect_file(claude_top, resolved_home, "")

    for rel in _SCAN_DIR_RELATIVES:
        root_dir = resolved_home / rel
        try:
            os.lstat(root_dir)
        except FileNotFoundError:
            continue
        except OSError:
            if not any(f.get("path") == str(root_dir) and f.get("reason") == "unreadable_config_dir" for f in findings):
                findings.append({
                    "path": str(root_dir),
                    "matched": "<unreadable_dir>",
                    "reason": "unreadable_config_dir",
                })
            continue
        try:
            root_st = os.stat(root_dir)
        except OSError:
            if not any(f.get("path") == str(root_dir) and f.get("reason") == "unreadable_config_dir" for f in findings):
                findings.append({
                    "path": str(root_dir),
                    "matched": "<unreadable_dir>",
                    "reason": "unreadable_config_dir",
                })
            continue
        if not stat.S_ISDIR(root_st.st_mode):
            continue
        max_depth = _MAX_CONFIG_REL_DEPTH_BY_ROOT.get(rel, _DEFAULT_MAX_CONFIG_DEPTH)
        visited_real_dirs: set[tuple[int, int]] = {(root_st.st_dev, root_st.st_ino)}

        def _on_walk_error(err: OSError, _fallback_root: Path = root_dir) -> None:
            err_path = str(getattr(err, "filename", None) or _fallback_root)
            if not any(f.get("path") == err_path and f.get("reason") == "unreadable_config_dir" for f in findings):
                findings.append({
                    "path": err_path,
                    "matched": "<unreadable_dir>",
                    "reason": "unreadable_config_dir",
                })

        for dirpath, dirnames, filenames in os.walk(root_dir, followlinks=True, onerror=_on_walk_error):
            dp = Path(dirpath)
            try:
                cur_depth = len(dp.relative_to(root_dir).parts)
            except ValueError:
                cur_depth = 0
            if cur_depth >= max_depth:
                dirnames[:] = []
            else:
                kept_dirs: list[str] = []
                for d in dirnames:
                    cand = dp / d
                    if _is_catalogued(cand):
                        continue
                    try:
                        cand_rel = str(cand.relative_to(resolved_home))
                    except ValueError:
                        cand_rel = ""
                    if cand_rel and _is_denylisted_rel_path(cand_rel):
                        continue
                    if cand.is_symlink() and not cand.exists():
                        _inspect_file(cand, root_dir, rel)
                        continue
                    try:
                        cst = cand.stat()
                        key = (cst.st_dev, cst.st_ino)
                        if key in visited_real_dirs:
                            continue
                        visited_real_dirs.add(key)
                    except OSError:
                        if not any(f.get("path") == str(cand) and f.get("reason") == "unreadable_config_dir" for f in findings):
                            findings.append({
                                "path": str(cand),
                                "matched": "<unreadable_dir>",
                                "reason": "unreadable_config_dir",
                            })
                        continue
                    kept_dirs.append(d)
                dirnames[:] = kept_dirs
            for fn in filenames:
                fp = dp / fn
                _inspect_file(fp, root_dir, rel)

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


def evaluate_text_surface_health(
    raw_text: str,
    *,
    kind: str,
    surf_spec: dict[str, Any],
) -> list[str]:
    """Evaluate post-flip health predicates on a text surface's content (`H23`)."""
    if kind == "claude_marketplace":
        try:
            json.loads(raw_text)
        except Exception as exc:
            return [f"unparseable_json:{exc}"]
        if "~/" in raw_text or _VERSIONED_PLUGIN_RE.search(raw_text):
            return ["stale_or_tilde_in_claude_marketplace"]
        return []
    if kind == "hooks_json":
        try:
            doc = json.loads(raw_text)
        except Exception as exc:
            return [f"unparseable_json:{exc}"]
        if not isinstance(doc, dict):
            return ["unparseable_json:hooks_json is not a JSON object"]
    reasons: list[str] = []
    if _VERSIONED_PLUGIN_RE.search(raw_text):
        reasons.append(f"versioned_sha_in_{kind}")
    if "~/" in raw_text:
        reasons.append(f"literal_tilde_in_{kind}")
    if surf_spec.get("use_exec") and node_invocation_lacks_exec(raw_text, kind):
        reasons.append(f"missing_exec_in_{kind}")
    return reasons


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
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unreadable:{exc}"]})
                    continue
                reasons = evaluate_text_surface_health(raw_text, kind=kind, surf_spec=surf)
                if reasons:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": reasons})
            elif kind in ("launcher_wrapper", "hook_wrapper", "js_bridge", "rules_md", "skill_copy") and spath.is_file():
                try:
                    raw_text = spath.read_text(encoding="utf-8")
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unreadable:{exc}"]})
                    continue
                reasons = evaluate_text_surface_health(raw_text, kind=kind, surf_spec=surf)
                if reasons:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": reasons})
            elif kind == "commands_dir":
                if spath.is_symlink():
                    expected_target = expand_for_home(str(surf.get("entry") or ""), home=resolved_home)
                    try:
                        actual_target = os.readlink(spath)
                    except OSError:
                        actual_target = ""
                    cmd_reasons: list[str] = []
                    if actual_target != expected_target:
                        cmd_reasons.append(f"commands_dir_target_mismatch:{actual_target}!={expected_target}")
                    if not spath.exists() or not spath.is_dir() or (target is not None and not (target / "commands").is_dir()):
                        cmd_reasons.append("broken_commands_dir_symlink")
                    if cmd_reasons:
                        drifted_surfaces.append({
                            "host": host_id,
                            "path": str(spath),
                            "reasons": cmd_reasons,
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
                except Exception as exc:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": [f"unreadable:{exc}"]})
                    continue
                reasons = evaluate_text_surface_health(raw_text, kind=kind, surf_spec=surf)
                if reasons:
                    drifted_surfaces.append({"host": host_id, "path": str(spath), "reasons": reasons})

    uncatalogued_refs = scan_for_uncatalogued_plugin_refs(resolved_home)
    if legacy_v1_single_server_js and isinstance(wired_doc, dict):
        v1_plats = {
            str(w.get("platform") or "")
            for w in (wired_doc.get("wires") or [])
            if isinstance(w, dict)
        }
        v1_skip_prefixes: list[str] = []
        if "claude-code" in v1_plats:
            v1_skip_prefixes.append(str(resolved_home / ".claude" / "plugins" / "cache") + "/")
        if "codex" in v1_plats:
            v1_skip_prefixes.append(str(resolved_home / ".codex" / "plugins" / "cache") + "/")
        if v1_skip_prefixes:
            uncatalogued_refs = [
                r for r in uncatalogued_refs
                if not any(str(r.get("path") or "").startswith(p) for p in v1_skip_prefixes)
            ]
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

"""Claude Code plugin-surface registration against the wire-managed tree.

Claude Code loads a plugin's hooks, skills and commands from the `installPath`
recorded in ~/.claude/plugins/installed_plugins.json. Nothing else is consulted:
the `@marketplace` suffix in the `minni@minni` key is a namespacing convention,
and the marketplace source matters only during an install/update flow. So wire
can serve the whole plugin surface from ~/.minni/plugin/<version> — the tree it
already installs, hashes and verifies — by owning that one registration.

The path recorded is the *versioned* install root, never ~/.minni/plugin/current:
`current` is a deliberately release-only pointer (see the gate in install.py and
its tests), and GC's reference scan matches literal version-dir strings, so a
registration behind a symlink would be invisible to it. Claude Code reads plugin
manifests once per session, so a versioned path that changes between wires costs
nothing — the next session reads the current file.

See docs/design/DESIGN-wire-claude-plugin-adoption.md.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from minni.wire.paths import user_home
from minni.wire.wired import wired_record

PLUGIN_KEY = "minni@minni"
MARKETPLACE_NAME = "minni"
USER_SCOPE = "user"


class ClaudePluginError(Exception):
    """A Claude Code config could not be read or safely updated."""


def installed_plugins_path() -> Path:
    return user_home() / ".claude" / "plugins" / "installed_plugins.json"


def known_marketplaces_path() -> Path:
    return user_home() / ".claude" / "plugins" / "known_marketplaces.json"


def legacy_cache_root() -> Path:
    """The retired marketplace's cache tree: ~/.claude/plugins/cache/minni."""
    return user_home() / ".claude" / "plugins" / "cache" / MARKETPLACE_NAME


def claude_desktop_config_path() -> Path:
    return (
        user_home() / "Library" / "Application Support" / "Claude"
        / "claude_desktop_config.json"
    )


def _now_iso_ms() -> str:
    """Millisecond ISO-8601, matching the stamps Claude Code writes itself."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _load_json_doc(path: Path, default: dict) -> dict:
    """Read a live Claude config, or `default` when it is absent or empty.

    A corrupt file raises. These documents hold other plugins' registrations and
    other MCP servers; recovering our own entry by overwriting theirs is not a
    trade this code gets to make on the user's behalf.
    """
    if not path.exists():
        return dict(default)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ClaudePluginError(f"cannot read {path}: {exc}") from exc
    if not text.strip():
        return dict(default)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClaudePluginError(
            f"{path} is not valid JSON ({exc}); refusing to overwrite it",
        ) from exc
    if not isinstance(data, dict):
        raise ClaudePluginError(f"{path} is not a JSON object; refusing to overwrite it")
    return data


def _atomic_write_json(path: Path, data: dict) -> None:
    """Replace a live config in one rename, preserving its mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}-", suffix=".tmp")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        tmp_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def register_claude_plugin(
    install_root: Path,
    version: str,
    *,
    git_sha: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Idempotently point Claude Code's `minni@minni` plugin at `install_root`.

    Only the user-scope entry is ours. Every other plugin key, every other-scope
    entry, and every unrecognized field is preserved. When installPath, version
    and gitCommitSha already match, `lastUpdated` is left alone so a re-wire of
    an unchanged version rewrites nothing at all.
    """
    path = installed_plugins_path()
    doc = _load_json_doc(path, {"version": 2, "plugins": {}})
    doc.setdefault("version", 2)
    plugins = doc.get("plugins")
    if not isinstance(plugins, dict):
        if plugins is not None:
            raise ClaudePluginError(f"{path}: 'plugins' is not an object")
        plugins = {}
    plugins = dict(plugins)

    raw_entries = plugins.get(PLUGIN_KEY)
    entries = [e for e in raw_entries if isinstance(e, dict)] if isinstance(raw_entries, list) else []

    index = next(
        (i for i, e in enumerate(entries) if e.get("scope", USER_SCOPE) == USER_SCOPE),
        None,
    )
    existing = entries[index] if index is not None else None

    install_path = str(install_root)
    merged = dict(existing) if existing else {}
    merged["scope"] = USER_SCOPE
    merged["installPath"] = install_path
    merged["version"] = version
    merged["installedAt"] = merged.get("installedAt") or _now_iso_ms()
    if git_sha and git_sha != "unknown":
        merged["gitCommitSha"] = git_sha
    else:
        # A sha we cannot vouch for describes some other tree; drop it.
        merged.pop("gitCommitSha", None)

    def _identity(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k != "lastUpdated"}

    if existing is not None and _identity(existing) == _identity(merged):
        return {
            "path": str(path),
            "install_path": install_path,
            "version": version,
            "changed": False,
            "created": False,
        }

    merged["lastUpdated"] = _now_iso_ms()
    if index is None:
        entries.append(merged)
    else:
        entries[index] = merged
    plugins[PLUGIN_KEY] = entries
    doc["plugins"] = plugins

    if not dry_run:
        _atomic_write_json(path, doc)

    return {
        "path": str(path),
        "install_path": install_path,
        "version": version,
        "changed": True,
        "created": existing is None,
    }


def retire_claude_marketplace(*, dry_run: bool = False) -> dict[str, object]:
    """Drop the `minni` marketplace entry from known_marketplaces.json.

    Wire needs no marketplace to register a plugin, and leaving one behind keeps
    `/plugin update` armed to re-copy whatever directory it points at over the
    tree wire manages.
    """
    path = known_marketplaces_path()
    if not path.exists():
        return {"path": str(path), "changed": False, "reason": "no known_marketplaces.json"}
    doc = _load_json_doc(path, {})
    if MARKETPLACE_NAME not in doc:
        return {"path": str(path), "changed": False, "reason": "no minni marketplace entry"}
    removed = doc.pop(MARKETPLACE_NAME)
    source = ""
    if isinstance(removed, dict):
        source = str(removed.get("installLocation") or removed.get("source", ""))
    if not dry_run:
        _atomic_write_json(path, doc)
    return {"path": str(path), "changed": True, "removed_source": source}


def repoint_claude_desktop(
    install_root: Path, *, dry_run: bool = False,
) -> dict[str, object]:
    """Move Claude Desktop's minni server onto the wire tree.

    Desktop is a separate product with a disjoint config tree, but its
    mcpServers.minni entry was written pointing into the marketplace cache that
    the cutover deletes. Only args[0] moves; env (workspace id, AFM settings),
    other servers and unrelated top-level keys are preserved.
    """
    path = claude_desktop_config_path()
    if not path.parent.exists():
        return {"path": str(path), "changed": False, "reason": "Claude Desktop not installed"}
    doc = _load_json_doc(path, {})
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get("minni"), dict):
        return {"path": str(path), "changed": False, "reason": "no minni entry"}

    entry = dict(servers["minni"])
    server_js = str(install_root / "dist" / "server.js")
    args = entry.get("args")
    args = [str(a) for a in args] if isinstance(args, list) else []
    if args[:1] == [server_js]:
        return {"path": str(path), "changed": False, "reason": "already on the wire tree"}

    entry["args"] = [server_js, *args[1:]]
    servers = dict(servers)
    servers["minni"] = entry
    doc["mcpServers"] = servers
    if not dry_run:
        _atomic_write_json(path, doc)
    return {"path": str(path), "changed": True, "server": server_js}


def legacy_cache_dirs() -> list[Path]:
    """Version dirs under the retired marketplace cache, newest name last."""
    root = legacy_cache_root() / "minni"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())


def remove_legacy_cache(
    install_root: Path, *, dry_run: bool = False,
) -> dict[str, object]:
    """Delete ~/.claude/plugins/cache/minni once nothing points into it."""
    root = legacy_cache_root()
    if not root.exists():
        return {"path": str(root), "changed": False, "reason": "already absent"}
    if root.is_symlink():
        raise ClaudePluginError(f"{root} is a symlink; refusing to remove it")
    expected_parent = user_home() / ".claude" / "plugins" / "cache"
    if root.resolve().parent != expected_parent.resolve():
        raise ClaudePluginError(f"{root} does not resolve under {expected_parent}")
    if root.resolve() in install_root.resolve().parents:
        raise ClaudePluginError(
            f"refusing to remove {root}: the wired install root {install_root} is inside it",
        )
    removed = [str(p) for p in legacy_cache_dirs()]
    if not dry_run:
        shutil.rmtree(root)
    return {"path": str(root), "changed": True, "removed_versions": removed}


def adopt_claude_code(
    *, apply: bool = False, keep_legacy_cache: bool = False,
) -> dict[str, object]:
    """One-time cutover from the marketplace/cache install to the wire tree.

    Every step is individually idempotent and reports whether it changed
    anything, so a re-run after a partial failure finishes the remainder instead
    of compounding. Without `apply` nothing is written.
    """
    record = wired_record("claude-code")
    if not record:
        raise ClaudePluginError(
            "claude-code is not wired yet; run `minni wire claude-code` first",
        )
    install_root = Path(str(record.get("install_root", "")))
    version = str(record.get("version", ""))
    if not install_root.is_dir():
        raise ClaudePluginError(f"wired install root does not exist: {install_root}")
    for required in (".claude-plugin/plugin.json", "hooks/hooks.json"):
        if not (install_root / required).is_file():
            raise ClaudePluginError(f"{install_root} is not a plugin tree: missing {required}")

    git_sha = None
    manifest_path = install_root / "payload-manifest.json"
    if manifest_path.is_file():
        try:
            git_sha = str(json.loads(manifest_path.read_text(encoding="utf-8")).get("git_sha") or "")
        except (OSError, json.JSONDecodeError) as exc:
            raise ClaudePluginError(f"cannot read {manifest_path}: {exc}") from exc

    dry_run = not apply
    steps: dict[str, object] = {
        "register": register_claude_plugin(
            install_root, version, git_sha=git_sha, dry_run=dry_run,
        ),
        "claude_desktop": repoint_claude_desktop(install_root, dry_run=dry_run),
        "marketplace": retire_claude_marketplace(dry_run=dry_run),
    }
    if keep_legacy_cache:
        steps["legacy_cache"] = {
            "changed": False, "reason": "kept (--keep-legacy-cache)",
            "dirs": [str(p) for p in legacy_cache_dirs()],
        }
    else:
        steps["legacy_cache"] = remove_legacy_cache(install_root, dry_run=dry_run)

    return {
        "schema": 1,
        "applied": apply,
        "install_root": str(install_root),
        "version": version,
        "steps": steps,
    }

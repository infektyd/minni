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
import re
import shutil
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from minni.wire.paths import plugin_base, user_home
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
    pending: dict[str, dict] | None = None,
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
    if raw_entries is not None and not isinstance(raw_entries, list):
        # Same policy as 'plugins' above: a shape we do not understand holds
        # somebody's registration, and quietly replacing it with a list of our
        # own is not a trade this code gets to make.
        raise ClaudePluginError(f"{path}: plugins['{PLUGIN_KEY}'] is not a list")
    # Entries we do not understand are carried through untouched rather than
    # dropped: only the user-scope dict is ours to rewrite.
    entries = list(raw_entries) if isinstance(raw_entries, list) else []

    index = next(
        (
            i for i, e in enumerate(entries)
            if isinstance(e, dict) and e.get("scope", USER_SCOPE) == USER_SCOPE
        ),
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
        plugins[PLUGIN_KEY] = entries
        doc["plugins"] = plugins
        if pending is not None:
            pending[str(path)] = doc
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
    if pending is not None:
        pending[str(path)] = doc

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
    install_root: Path, *, dry_run: bool = False, pending: dict[str, dict] | None = None,
) -> dict[str, object]:
    """Move Claude Desktop's minni server onto the wire tree.

    Desktop is a separate product with a disjoint config tree, but its
    mcpServers.minni entry was written pointing into the marketplace cache that
    the cutover deletes.

    The rewrite targets *the argument that actually points into the legacy
    cache*, not args[0]: an entry like ["--inspect", ".../cache/.../server.js"]
    keeps its flag and moves the path. env (workspace id, AFM settings), other
    servers and unrelated top-level keys are preserved. When no argument points
    into the cache this is a no-op — Desktop is pointed at something that is not
    ours to move, and remove_legacy_cache's scan is what decides whether the
    deletion is still safe.
    """
    return _move_desktop_arg(
        install_root,
        legacy_cache_root(),
        "legacy cache",
        dry_run=dry_run,
        pending=pending,
        refuse_command=True,
    )


def follow_claude_desktop(
    install_root: Path, *, dry_run: bool = False, pending: dict[str, dict] | None = None,
) -> dict[str, object]:
    """Keep an already-adopted Claude Desktop on the current wire tree.

    Desktop records a *versioned* path, so a wire that installs a new version
    strands it on the old one — which GC then prunes out from under it. This
    moves any argument already inside ~/.minni/plugin onto the freshly wired
    root, and is a no-op on machines that have not run the adoption cutover:
    only a path under the wire tree is ever rewritten.
    """
    return _move_desktop_arg(
        install_root,
        plugin_base(),
        "wire tree",
        dry_run=dry_run,
        pending=pending,
        refuse_command=False,
    )


def _move_desktop_arg(
    install_root: Path,
    under: Path,
    what: str,
    *,
    dry_run: bool,
    pending: dict[str, dict] | None,
    refuse_command: bool,
) -> dict[str, object]:
    """Point Desktop's minni server at `install_root`, moving args under `under`."""
    path = claude_desktop_config_path()
    if not path.parent.exists():
        return {"path": str(path), "changed": False, "reason": "Claude Desktop not installed"}
    doc = _load_json_doc(path, {})
    servers = doc.get("mcpServers")
    if not isinstance(servers, dict) or not isinstance(servers.get("minni"), dict):
        return {"path": str(path), "changed": False, "reason": "no minni entry"}

    entry = dict(servers["minni"])
    server_js = str(install_root / "dist" / "server.js")

    command = entry.get("command")
    if refuse_command and isinstance(command, str) and _is_under(command, under):
        # A launcher living inside the tree we are about to delete. Guessing a
        # replacement command is how you silently break someone's Desktop.
        raise ClaudePluginError(
            f"{path}: mcpServers.minni.command points into {under} ({command}); "
            "repoint it by hand before adopting",
        )

    raw_args = entry.get("args")
    args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
    if server_js in args:
        return {"path": str(path), "changed": False, "reason": "already on the wire tree"}

    # Only the server entrypoint moves. An argument that merely *lives* under
    # the same tree (a --config path, say) is not a second server pointer, and
    # rewriting it to server.js would be silent corruption. Anything else left
    # inside the legacy cache is caught by remove_legacy_cache's scan, loudly.
    targets = [
        i for i, a in enumerate(args)
        if _is_under(a, under) and Path(a).name == "server.js"
    ]
    if not targets:
        return {
            "path": str(path), "changed": False,
            "reason": f"no server.js argument points into the {what}",
        }

    new_args = list(args)
    for i in targets:
        new_args[i] = server_js
    entry["args"] = new_args
    servers = dict(servers)
    servers["minni"] = entry
    doc["mcpServers"] = servers
    if not dry_run:
        _atomic_write_json(path, doc)
    if pending is not None:
        pending[str(path)] = doc
    return {
        "path": str(path), "changed": True, "server": server_js,
        "replaced": [args[i] for i in targets],
    }


def claude_adopt_pending() -> bool:
    """True while the retired marketplace or its cache tree is still present.

    Registration alone does not disarm `/plugin update`: as long as the `minni`
    marketplace entry survives, an update reinstalls into the cache and rewrites
    installPath off the wire tree.
    """
    try:
        marketplaces = _load_json_doc(known_marketplaces_path(), {})
    except ClaudePluginError:
        # Unreadable is not "clean"; surfacing the nudge is the safe direction.
        return True
    return MARKETPLACE_NAME in marketplaces or (legacy_cache_root() / MARKETPLACE_NAME).is_dir()


def legacy_scan_paths() -> list[Path]:
    """Configs that may name a path inside the legacy cache.

    known_marketplaces.json is deliberately absent: its `minni` entry is the one
    reference the cutover retires itself, one step earlier.
    """
    home = user_home()
    return [
        installed_plugins_path(),
        home / ".claude.json",
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        claude_desktop_config_path(),
    ]


def _nfc(value: str) -> str:
    """NFC-normalize, so an NFD spelling of an accented path still compares equal.

    macOS filesystems treat the two as the same file, but Python string
    comparison does not, and a config written from a differently-normalized
    source would otherwise read as "not a reference" and clear a deletion.
    """
    return unicodedata.normalize("NFC", value)


def _is_under(value: str, root: Path) -> bool:
    """True when `value` is an absolute path at or below `root`.

    Compared component-wise via relative_to, so `.../cache/minnix` does not
    match `.../cache/minni`. Non-paths ("--inspect", "node") fall out as
    non-absolute.
    """
    if not value:
        return False
    try:
        candidate = Path(_nfc(value))
    except (TypeError, ValueError):
        return False
    if not candidate.is_absolute():
        return False
    for base in {root, root.resolve()}:
        try:
            candidate.relative_to(Path(_nfc(str(base))))
        except ValueError:
            continue
        return True
    return False


def _iter_json_strings(node: object, trail: str = "") -> object:
    """Every string leaf in a JSON document, with a dotted path to it.

    Walking the whole document rather than known keys is the point: the cache
    path turns up in installPath, in argv, in env values and in fields no
    version of this code has seen yet.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_json_strings(value, f"{trail}.{key}" if trail else str(key))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _iter_json_strings(value, f"{trail}[{i}]")
    elif isinstance(node, str):
        yield trail, node


def legacy_cache_referrers(*, overrides: dict[str, dict] | None = None) -> list[str]:
    """Every live reference into the legacy cache, as "<file>: <field> -> <path>".

    A config that cannot be parsed is itself reported: an unreadable file is not
    evidence that nothing points into the tree, and this list gates an rmtree.
    """
    root = legacy_cache_root()
    overrides = overrides or {}
    # Structured matching alone is not enough to authorise an rmtree. Claude
    # Code's hook entries are *shell command strings* ("node <path>/hook.js
    # SessionStart"), not argv arrays, so the cache path routinely appears
    # embedded in a larger string that Path() cannot classify. A substring pass
    # over the same string leaves catches those.
    #
    # The needle carries a trailing boundary, because a bare substring
    # re-introduces the exact bug _is_under exists to avoid: ".../cache/minni"
    # is a prefix of ".../cache/minni-tools", so an unrelated marketplace would
    # block the cutover with a refusal the operator cannot act on. Only
    # [A-Za-z0-9_.-] suppresses a match, and a genuine reference is always
    # followed by "/" or a string terminator.
    #
    # Deliberately NOT a needle: the literal "~/.claude/plugins/cache/minni".
    # ~/.claude.json persists per-project prompt history, and this repo's own
    # docs and --help text contain that string, so any session that discussed
    # the migration would poison the file permanently and leave
    # --keep-legacy-cache as the only exit -- i.e. the cutover could never
    # complete. Claude Code does not expand "~" in these configs either, so the
    # on-disk risk it would cover is not real.
    needles = {_nfc(str(root)).lower(), _nfc(str(root.resolve())).lower()}
    boundary = re.compile(
        "|".join(re.escape(n) + r"(?![\w.\-])" for n in sorted(needles)),
    )
    found: list[str] = []
    for path in legacy_scan_paths():
        key = str(path)
        if key in overrides:
            doc: object = overrides[key]
        elif not path.exists():
            continue
        else:
            try:
                doc = _load_json_doc(path, {})
            except ClaudePluginError as exc:
                found.append(f"{path}: unreadable, cannot verify ({exc})")
                continue
        for trail, value in _iter_json_strings(doc):
            if _is_under(value, root):
                found.append(f"{path}: {trail} -> {value}")
            elif boundary.search(_nfc(value).lower()):
                # Report the field, never the text. This branch fires precisely
                # when the path is embedded in a shell command string, which is
                # exactly where people inline `FOO_TOKEN=...`; and over
                # ~/.claude.json the surrounding text is verbatim prompt
                # history. A dotted trail points at the offending field without
                # copying secrets into stderr, CI logs and bug reports.
                found.append(f"{path}: {trail} mentions {root}")
    return found


def legacy_cache_dirs() -> list[Path]:
    """Version dirs under the retired marketplace cache, newest name last."""
    root = legacy_cache_root() / "minni"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())


def remove_legacy_cache(
    install_root: Path,
    *,
    dry_run: bool = False,
    overrides: dict[str, dict] | None = None,
) -> dict[str, object]:
    """Delete the retired plugin cache once nothing points into it.

    Two properties this guarantees, both of which the earlier version did not:

    * **It refuses while anything still references the tree.** Every config in
      `legacy_scan_paths()` is scanned for a path under `legacy_cache_root()` —
      all plugins and all scopes in installed_plugins.json, ~/.claude.json,
      settings.json and Claude Desktop's config. A single live reference aborts
      the deletion and names the offender, rather than leaving it dangling.
    * **It deletes only what it enumerates.** The target is the plugin dir
      `<cache>/minni/minni`, not the whole marketplace dir. A sibling plugin
      cached under the same marketplace survives and is reported in
      `siblings_kept`; the marketplace dir is removed only once it is empty.

    `overrides` lets a caller scan the documents that *will* be on disk rather
    than the ones currently there, so a dry run answers the same question the
    apply run does instead of tripping over registrations adopt itself rewrites.
    """
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

    referrers = legacy_cache_referrers(overrides=overrides)
    if referrers:
        listed = "\n  ".join(referrers)
        raise ClaudePluginError(
            f"refusing to remove {root}: still referenced by\n  {listed}\n"
            "repoint or remove these before adopting "
            "(or re-run with --keep-legacy-cache)",
        )

    target = root / MARKETPLACE_NAME
    if target.is_symlink():
        raise ClaudePluginError(f"{target} is a symlink; refusing to remove it")
    # Everything inside the target, not just the version dirs: rmtree takes
    # stray files and symlinks too, and a report that omits them is exactly the
    # under-reporting this function was fixed to stop doing for siblings.
    removed = sorted(str(p) for p in target.iterdir()) if target.is_dir() else []
    siblings = sorted(str(p) for p in root.iterdir() if p != target)

    if not dry_run:
        if target.exists():
            shutil.rmtree(target)
        if not any(root.iterdir()):
            root.rmdir()

    return {
        "path": str(target),
        "changed": True,
        "removed_versions": removed,
        "siblings_kept": siblings,
    }


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
    # The cache scan must see the configs as they will be *after* the steps
    # below, or a dry run would refuse over the very registrations adopt is
    # rewriting — and would disagree with what `--apply` actually does.
    overrides: dict[str, dict] = {}
    steps: dict[str, object] = {
        "register": register_claude_plugin(
            install_root, version, git_sha=git_sha, dry_run=dry_run, pending=overrides,
        ),
        "claude_desktop": repoint_claude_desktop(
            install_root, dry_run=dry_run, pending=overrides,
        ),
        "marketplace": retire_claude_marketplace(dry_run=dry_run),
    }

    if keep_legacy_cache:
        steps["legacy_cache"] = {
            "changed": False, "reason": "kept (--keep-legacy-cache)",
            "dirs": [str(p) for p in legacy_cache_dirs()],
        }
    else:
        steps["legacy_cache"] = remove_legacy_cache(
            install_root, dry_run=dry_run, overrides=overrides,
        )

    return {
        "schema": 1,
        "applied": apply,
        "install_root": str(install_root),
        "version": version,
        "steps": steps,
    }

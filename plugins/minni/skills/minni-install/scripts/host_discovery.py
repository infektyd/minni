"""Read-only optional-host discovery. Mirrored in the standalone installer.

Presence is evidence of installation, never proof of runtime readiness. Config
alone cannot establish host availability. No host process is executed here.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
import tomllib

# Desktop chat applications are not interchangeable with their agent runtimes.
_SPECS = {
    "codex": (("codex",), ("Codex.app",), (".codex/config.toml",)),
    "claude-code": (("claude",), (), (".claude.json", ".claude/plugins/installed_plugins.json",
                                     ".claude/settings.json")),
    "kilocode": (("kilo", "kilocode"), (), (".config/kilo/kilo.json",)),
    "gemini": (("gemini",), (), (".gemini/settings.json", ".gemini/extensions/minni/gemini-extension.json")),
    "antigravity": (("agy", "antigravity"), ("Antigravity.app", "Antigravity IDE.app"),
                    (".gemini/config/mcp_config.json", ".gemini/antigravity/mcp_config.json",
                     ".gemini/antigravity-ide/mcp_config.json", ".gemini/antigravity-cli/plugins/minni/mcp_config.json")),
    "grok": (("grok", "grok-beta"), (), (".grok/config.toml",)),
    "cursor": (("cursor", "cursor-agent"), ("Cursor.app",),
               (".cursor/mcp.json", ".cursor/plugins/local/minni/.mcp.json")),
}
# Standard user-local and Homebrew/npm launcher locations. Presence is checked
# without executing a host; stale or non-executable symlinks do not qualify.
_SYSTEM_LAUNCHER_ROOTS = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))

_ALIASES = {"claude": "claude-code", "kilo": "kilocode", "agy": "antigravity",
            "grok-build": "grok", "grok-beta": "grok", "grok-tui": "grok",
            "antigravity-cli": "antigravity", "antigravity-ide": "antigravity"}


@dataclass(frozen=True)
class HostPresence:
    platform: str
    availability: str
    available: bool
    configured: bool | None
    config_present: bool
    executables: tuple[str, ...] = ()
    applications: tuple[str, ...] = ()
    config_errors: tuple[str, ...] = ()
    runtime: str = "not_probed"
    binding_disabled: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _binding(config: dict) -> tuple[bool, bool]:
    """Return (present, explicitly disabled), never exposing config values.

    TOML/Kilo use enabled; JSON MCP surfaces may use disabled. Conservatively
    preserve a disabled Minni entry even if a second view is still enabled.
    """
    present = disabled = False
    for key in ("mcp_servers", "mcpServers", "mcp"):
        if key not in config:
            continue
        servers = config[key]
        if not isinstance(servers, dict):
            raise ValueError("MCP server table must be an object")
        for name, entry in servers.items():
            normalized = str(name).lower().replace("_", "-")
            if not (normalized in {"minni", "sovereign-memory"}
                    or normalized.startswith(("minni-", "sovereign-memory-"))):
                continue
            if isinstance(entry, dict):
                present = True
                disabled = disabled or entry.get("enabled") is False or entry.get("disabled") is True
            elif entry is False:
                present = disabled = True
            else:
                raise ValueError("Minni binding must be an object or disabled")
    return present, disabled


# Upper bound for a single host config read. ~/.claude.json accumulates
# per-project prompt history in normal use and routinely passes the old 4 MiB
# cut-off; 64 MiB still bounds memory while accepting established installs.
# Anything larger stays a fail-closed config error, never a silent skip.
_MAX_CONFIG_BYTES = 64 * 1024 * 1024


def _claude_registry_binding(config: dict) -> tuple[bool, bool]:
    """Active `minni@minni` user-scope registration in installed_plugins.json.

    The registry carries no enabled flag: a user-scope entry with an
    installPath IS the active binding (that is exactly what wire writes and
    what Claude Code reads hooks/skills/commands from). Entries without an
    installPath, non-user scopes, and malformed shapes do not count — an
    unrecognized shape raises so discovery fails closed instead of guessing.
    """
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        raise ValueError("plugin registry must be an object")
    entries = plugins.get("minni@minni")
    if entries is None:
        return False, False
    if not isinstance(entries, list):
        raise ValueError("plugin registration must be a list")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("scope", "user") != "user":
            continue
        if entry.get("installPath"):
            return True, False
    return False, False


def _claude_settings_disabled(config: dict) -> bool:
    """Explicit operator disable of `minni@minni` in ~/.claude/settings.json.

    Installed is not active: Claude Code records per-plugin enablement under
    `enabledPlugins`. An explicit `false` means the operator turned Minni off,
    and bulk discovery must preserve that intent rather than reactivate it.
    Absent key (or absent file) means no recorded disable. Anything else
    raises so discovery fails closed instead of guessing.
    """
    plugins = config.get("enabledPlugins")
    if plugins is None:
        return False
    if not isinstance(plugins, dict):
        raise ValueError("enabledPlugins must be an object")
    if "minni@minni" not in plugins:
        return False
    value = plugins["minni@minni"]
    if not isinstance(value, bool):
        raise ValueError("enabledPlugins entry must be a boolean")
    return value is False


def discover_host(platform: str, *, home: Path | None = None, path: str | None = None,
                  app_roots: tuple[Path, ...] | None = None,
                  launcher_roots: tuple[Path, ...] | None = None) -> HostPresence:
    platform = platform.strip().lower().replace("_", "-")
    platform = _ALIASES.get(platform, platform)
    home = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    if platform not in _SPECS:
        return HostPresence(platform, "unknown", False, None, False)
    commands, apps, configs = _SPECS[platform]
    roots = app_roots if app_roots is not None else (Path("/Applications"), home / "Applications")
    executables = []
    for command in commands:
        found = shutil.which(command, path=path)
        if found:
            executables.append(found)
    # An explicit path argument is a caller-supplied search boundary. Normal
    # scheduled calls use ambient PATH plus these known launcher roots.
    launchers = launcher_roots if launcher_roots is not None else (
        (home / ".local/bin", *_SYSTEM_LAUNCHER_ROOTS) if path is None else ()
    )
    for root in launchers:
        for command in commands:
            candidate = Path(root) / command
            if candidate.is_file() and os.access(candidate, os.X_OK):
                executables.append(str(candidate))
    applications = []
    for root in roots:
        for name in apps:
            app = Path(root) / name
            # A directory with an .app suffix alone is not installation evidence.
            if not (app / "Contents/Info.plist").is_file():
                continue
            applications.append(str(app))
            if platform == "codex":
                cli = app / "Contents/Resources/codex"
                if cli.is_file() and os.access(cli, os.X_OK):
                    executables.append(str(cli))
    configured = False
    binding_disabled = False
    present = False
    errors = []
    for relative in configs:
        target = home / relative
        if not target.exists():
            continue
        present = True
        try:
            # Configuration errors may contain credentials; report category only.
            if target.stat().st_size > _MAX_CONFIG_BYTES:
                raise ValueError("oversized config")
            text = target.read_text(encoding="utf-8")
            config = tomllib.loads(text) if target.suffix == ".toml" else json.loads(text)
            if not isinstance(config, dict):
                raise ValueError("config must be object")
            if target.name == "installed_plugins.json":
                found, disabled = _claude_registry_binding(config)
            elif platform == "claude-code" and relative == ".claude/settings.json":
                found, disabled = False, _claude_settings_disabled(config)
            else:
                found, disabled = _binding(config)
            configured = configured or found
            binding_disabled = binding_disabled or disabled
        except (OSError, ValueError, UnicodeError) as exc:
            errors.append(type(exc).__name__)
    available = bool(executables or applications)
    return HostPresence(platform, "available" if available else "unavailable", available,
                        True if configured else (None if errors else False), present,
                        tuple(dict.fromkeys(executables)), tuple(applications), tuple(errors),
                        binding_disabled=binding_disabled)


def host_decision(platform: str, *, bulk: bool = False, home: Path | None = None,
                  path: str | None = None, app_roots: tuple[Path, ...] | None = None,
                  launcher_roots: tuple[Path, ...] | None = None) -> dict:
    """Decide before Node checks, payload builds, bootstrap or config mutations.

    Explicit generic remains the deliberate headless/custom-host escape; its
    mandatory install-root/agent validation belongs to the calling command.
    """
    if platform.strip().lower() == "generic" and not bulk:
        return {"eligible": True, "status": "ready", "reason": "explicit generic integration",
                "host": HostPresence("generic", "explicit", False, None, False).to_dict()}
    host = discover_host(platform, home=home, path=path, app_roots=app_roots, launcher_roots=launcher_roots)
    if host.config_errors:
        return {"eligible": False, "status": "failed",
                "reason": "host configuration unreadable; repair before wiring",
                "host": host.to_dict()}
    reason = "host available"
    eligible = host.available
    if not eligible:
        reason = "host unavailable: no supported executable or application found" if host.availability == "unavailable" else "host availability unknown"
    elif bulk and host.binding_disabled:
        eligible = False
        reason = "Minni binding explicitly disabled; bulk update preserves operator intent"
    elif bulk and host.configured is not True:
        eligible = False
        reason = "existing Minni binding unreadable" if host.configured is None else "host available but no existing Minni binding; explicit wire required"
    return {"eligible": eligible, "status": "ready" if eligible else "skipped",
            "reason": reason, "host": host.to_dict()}

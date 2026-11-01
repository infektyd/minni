"""Canonical Host Surface Catalog (§4.2) and Keyed-Subset + Forbidden-Key Diff Engine."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minni.wire.paths import user_home

FORBIDDEN_ENV_KEYS: frozenset[str] = frozenset({
    "MINNI_WORKSPACE_ID",
    "MINNI_CODEX_WORKSPACE_ID",
    "MINNI_KILOCODE_WORKSPACE_ID",
    "MINNI_CURSOR_WORKSPACE_ID",
    "MINNI_CLAUDECODE_WORKSPACE_ID",
    "MINNI_GEMINI_WORKSPACE_ID",
    "MINNI_GROK_WORKSPACE_ID",
})

ALLOWED_AFM_MODES: frozenset[str] = frozenset({"native", "bridge", "auto", "off"})

_VERSIONED_PLUGIN_RE = re.compile(r"\.minni/plugin/[^/\"'\s]+\+git\.[0-9a-fA-F]+")


def resolve_home(home: Path | None = None) -> Path:
    return Path(home) if home is not None else user_home()


def expand_for_home(value: str, home: Path | None = None) -> str:
    """Expand leading `~` or `~/` using the explicit `home` or `user_home()` (`N7`)."""
    base = str(resolve_home(home))
    if value == "~":
        return base
    if value.startswith("~/"):
        return f"{base}/{value[2:]}"
    return value


def _expand_obj(obj: Any, home: Path | None = None, base_semver: str = "0.6.0") -> Any:
    if isinstance(obj, str):
        replaced = obj.replace("{base_semver}", base_semver)
        return expand_for_home(replaced, home=home)
    if isinstance(obj, list):
        return [_expand_obj(item, home=home, base_semver=base_semver) for item in obj]
    if isinstance(obj, dict):
        return {
            k: _expand_obj(v, home=home, base_semver=base_semver)
            for k, v in obj.items()
        }
    return copy.deepcopy(obj)


# Declarative catalog of all 28 host surface groups across the fleet (§2 & §4.2).
# Literal `~` here is expanded via `expand_host_surface_catalog(home)` before any
# disk read, diff, or write (`N7`).
HOST_SURFACE_CATALOG: dict[str, dict[str, Any]] = {
    "claude-code": {
        "agent_id": "claude-code",
        "vault_path": "~/.minni/claudecode-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "continuous_cas",
        "lock_glob": "~/.claude.json.tmp.*",
        "surfaces": [
            {
                "kind": "mcp_json",
                "path": "~/.claude.json",
                "key": "mcpServers.minni",
                "command": "node",
                "entry": "~/.minni/plugin/current/dist/server.js",
            },
            {
                "kind": "claude_plugin_registry",
                "path": "~/.claude/plugins/installed_plugins.json",
                "key": "plugins.minni@minni",
                "entry": "~/.minni/plugin/current",
                "tracks_version_and_sha": True,
            },
            {
                "kind": "claude_marketplace",
                "path": "~/.claude/local-marketplaces/minni/.claude-plugin/marketplace.json",
                "entry": "~/.claude/local-marketplaces/minni/plugins/minni-{base_semver}",
            },
        ],
    },
    "claude-desktop": {
        "agent_id": "claude-code",
        "vault_path": "~/.minni/claudecode-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "rewrite_while_running_and_quit_zero_delta",
        "process_match": {
            "bundle_id": "com.anthropic.claudefordesktop",
            "process_names": ["Claude"],
        },
        "surfaces": [
            {
                "kind": "mcp_json",
                "path": "~/Library/Application Support/Claude/claude_desktop_config.json",
                "key": "mcpServers.minni",
                "entry": "~/.minni/plugin/current/dist/server.js",
                "accepted_entries": [
                    "~/.minni/plugin/current/dist/server.js",
                    "~/.agents/plugins/Minni@Minni/dist/server.js",
                ],
            },
        ],
    },
    "codex": {
        "agent_id": "codex",
        "vault_path": "~/.minni/codex-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "cas_on_delta",
        "surfaces": [
            {
                "kind": "mcp_toml",
                "path": "~/.codex/config.toml",
                "key": "mcp_servers.minni",
                "command": "node",
                "entry": "~/.minni/plugin/current/dist/server.js",
            },
            {
                "kind": "hooks_json",
                "path": "~/.codex/hooks.json",
                "entry": "~/.minni/plugin/current/dist/codex-hook.js",
                "use_exec": True,
            },
            {
                "kind": "skill_copy",
                "path": "~/.codex/skills/astra-minni/SKILL.md",
                "entry": "~/.codex/skills/astra-minni/SKILL.md",
            },
        ],
    },
    "grok-build": {
        "agent_id": "grok-build",
        "vault_path": "~/.minni/grok-build-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "lock_and_cas_on_delta",
        "lock_glob": "~/.grok/.config.toml.lock",
        "process_match": {"process_names": ["grok"]},
        "surfaces": [
            {
                "kind": "mcp_toml",
                "path": "~/.grok/config.toml",
                "key": "mcp_servers.minni",
                "command": "node",
                "entry": "~/.minni/plugin/current/dist/server.js",
            },
            {
                "kind": "hooks_json",
                "path": "~/.grok/hooks/minni.json",
                "entry": "~/.minni/plugin/current/dist/grok-hook.js",
                "use_exec": True,
            },
            {
                "kind": "hooks_json",
                "path": "~/.grok/hooks.json",
                "entry": "~/.minni/plugin/current/dist/grok-hook.js",
                "use_exec": True,
            },
            {
                "kind": "rules_md",
                "path": "~/.grok/rules/minni.md",
                "entry": "~/.minni/plugin/current/dist/cli.js",
            },
        ],
    },
    "antigravity": {
        "agent_id": "gemini",
        "vault_path": "~/.minni/gemini-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "cas_on_delta",
        "ui_scanner_gate": {
            "container_mode": "staged_copy",
            "verified": False,
            "verified_at": None,
            "probe_evidence": "",
        },
        "surfaces": [
            {
                "kind": "plugin_container",
                "path": "~/.agents/plugins/Minni@Minni",
                "container_mode": "symlink_subtrees",
                "dist_symlink": "~/.minni/plugin/current/dist",
            },
            {
                "kind": "mcp_json",
                "path": "~/.gemini/settings.json",
                "key": "mcpServers.minni",
                "command": "node",
                "entry": "~/.agents/plugins/Minni@Minni/dist/server.js",
                "accepted_entries": [
                    "~/.agents/plugins/Minni@Minni/dist/server.js",
                    "~/.agents/plugins/minni@minni/dist/server.js",
                    "~/.minni/plugin/current/dist/server.js",
                ],
            },
            {
                "kind": "mcp_json",
                "path": "~/.gemini/config/mcp_config.json",
                "key": "mcpServers.minni",
                "command": "~/.agents/bin/mcp-env-run",
                "args_prefix": ["node"],
                "entry": "~/.agents/plugins/Minni@Minni/dist/server.js",
                "accepted_entries": [
                    "~/.agents/plugins/Minni@Minni/dist/server.js",
                    "~/.agents/plugins/minni@minni/dist/server.js",
                    "~/.minni/plugin/current/dist/server.js",
                ],
            },
            {
                "kind": "mcp_json",
                "path": "~/.gemini/antigravity/mcp_config.json",
                "key": "mcpServers.minni",
                "command": "~/.agents/bin/mcp-env-run",
                "args_prefix": ["node"],
                "entry": "~/.agents/plugins/Minni@Minni/dist/server.js",
                "accepted_entries": [
                    "~/.agents/plugins/Minni@Minni/dist/server.js",
                    "~/.agents/plugins/minni@minni/dist/server.js",
                    "~/.minni/plugin/current/dist/server.js",
                ],
            },
            {
                "kind": "mcp_json",
                "path": "~/.gemini/antigravity-ide/mcp_config.json",
                "key": "mcpServers.minni",
                "command": "~/.agents/bin/mcp-env-run",
                "args_prefix": ["node"],
                "entry": "~/.agents/plugins/Minni@Minni/dist/server.js",
                "accepted_entries": [
                    "~/.agents/plugins/Minni@Minni/dist/server.js",
                    "~/.agents/plugins/minni@minni/dist/server.js",
                    "~/.minni/plugin/current/dist/server.js",
                ],
            },
            {
                "kind": "mcp_json",
                "path": "~/.gemini/antigravity-cli/plugins/minni/mcp_config.json",
                "key": "mcpServers.minni",
                "command": "~/.agents/bin/mcp-env-run",
                "args_prefix": ["node"],
                "entry": "~/.agents/plugins/Minni@Minni/dist/server.js",
                "accepted_entries": [
                    "~/.agents/plugins/Minni@Minni/dist/server.js",
                    "~/.agents/plugins/minni@minni/dist/server.js",
                    "~/.minni/plugin/current/dist/server.js",
                ],
            },
            {
                "kind": "mcp_view_mirrors",
                "paths": [
                    "~/.agents/mcp-servers/views/.gemini__config__mcp_config.json.json",
                    "~/.agents/mcp-servers/views/.gemini__antigravity__mcp_config.json.json",
                    "~/.agents/mcp-servers/views/.gemini__antigravity-ide__mcp_config.json.json",
                    "~/.agents/mcp-servers/views/.gemini__antigravity-cli__plugins__minni__mcp_config.json.json",
                ],
                "command": "~/.agents/bin/mcp-env-run",
                "args_prefix": ["node"],
                "entry": "~/.agents/plugins/Minni@Minni/dist/server.js",
            },
            {
                "kind": "hooks_json",
                "path": "~/.gemini/config/plugins/minni/hooks.json",
                "entry": "~/.agents/plugins/Minni@Minni/dist/gemini-hook.js",
                "use_exec": True,
            },
            {
                "kind": "commands_dir",
                "path": "~/.gemini/commands/minni",
                "entry": "~/.minni/plugin/current/commands",
            },
        ],
    },
    "cursor": {
        "agent_id": "cursor",
        "vault_path": "~/.minni/cursor-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "cas_on_delta",
        "lock_glob": "~/.cursor/.mcp.json.lock",
        "process_match": {"process_names": ["Cursor"]},
        "ui_scanner_gate": {
            "container_mode": "staged_copy",
            "verified": False,
            "verified_at": None,
            "probe_evidence": "",
        },
        "surfaces": [
            {
                "kind": "plugin_container",
                "path": "~/.cursor/plugins/local/minni",
                "container_mode": "symlink_subtrees",
                "dist_symlink": "~/.minni/plugin/current/dist",
            },
            {
                "kind": "mcp_json",
                "path": "~/.cursor/mcp.json",
                "key": "mcpServers.minni",
                "entry": "~/.local/bin/minni-mcp",
                "accepted_entries": [
                    "~/.local/bin/minni-mcp",
                    "~/.cursor/plugins/local/minni/dist/server.js",
                    "~/.minni/plugin/current/dist/server.js",
                ],
            },
            {
                "kind": "launcher_wrapper",
                "path": "~/.local/bin/minni-mcp",
                "entry": "~/.cursor/plugins/local/minni/dist/server.js",
                "use_exec": True,
            },
            {
                "kind": "hooks_json",
                "path": "~/.cursor/hooks.json",
                "entry": "./hooks/minni-cursor.sh",
            },
            {
                "kind": "hook_wrapper",
                "path": "~/.cursor/hooks/minni-cursor.sh",
                "entry": "~/.cursor/plugins/local/minni/dist/cursor-hook.js",
                "use_exec": True,
            },
        ],
    },
    "kilocode": {
        "agent_id": "kilocode",
        "vault_path": "~/.minni/kilocode-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "cas_on_delta",
        "surfaces": [
            {
                "kind": "mcp_json",
                "path": "~/.config/kilo/kilo.json",
                "key": "mcp.minni",
                "command": "node",
                "entry": "~/.minni/plugin/current/dist/server.js",
            },
            {
                "kind": "js_bridge",
                "path": "~/.config/kilo/plugin/minni.js",
                "entry": "~/.minni/plugin/current/dist/kilocode-hook.js",
            },
        ],
    },
    "devin": {
        "agent_id": "devin",
        "vault_path": "~/.minni/devin-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "cas_on_delta",
        "process_match": {"process_names": ["devin"]},
        "surfaces": [
            {
                "kind": "mcp_json",
                "path": "~/.config/devin/mcp_config.json",
                "key": "mcpServers.minni",
                "command": "node",
                "entry": "~/.minni/plugin/current/dist/server.js",
            },
            {
                "kind": "hooks_json",
                "path": "~/.config/devin/config.json",
                "entry": "~/.config/devin/scripts/minni-hook.sh",
            },
            {
                "kind": "hook_wrapper",
                "path": "~/.config/devin/scripts/minni-hook.sh",
                "entry": "~/.minni/plugin/current/dist/hook.js",
                "use_exec": True,
            },
        ],
    },
    "muse": {
        "agent_id": "muse",
        "vault_path": "~/.minni/muse-vault",
        "status": "wired",
        "rewrite_on_run_hazard": "lockfile_and_cas_on_delta",
        "lock_glob": "~/.config/muse/.settings.json.lock",
        "process_match": {"process_names": ["muse"]},
        "surfaces": [
            {
                "kind": "mcp_json",
                "path": "~/.config/muse/settings.json",
                "key": "mcpServers.minni",
                "command": "node",
                "entry": "~/.minni/plugin/current/dist/server.js",
            },
            {
                "kind": "mcp_json",
                "path": "~/.muse/mcp.json",
                "key": "mcpServers.minni",
                "command": "node",
                "entry": "~/.minni/plugin/current/dist/server.js",
            },
        ],
    },
}


def expand_host_surface_catalog(
    home: Path | None = None,
    base_semver: str = "0.6.0",
) -> dict[str, dict[str, Any]]:
    """Return a deep copy of `HOST_SURFACE_CATALOG` with all `~` paths expanded (`N7`)."""
    return _expand_obj(HOST_SURFACE_CATALOG, home=home, base_semver=base_semver)


def effective_container_mode(host_id: str, wired_data: dict[str, Any] | None = None) -> str:
    """Return the effective container mode for `antigravity` or `cursor` (`R4-3`).

    Defaults to `'staged_copy'` whenever `ui_scanner_gate.verified` is absent or `False`.
    """
    if isinstance(wired_data, dict):
        hosts = wired_data.get("hosts")
        if isinstance(hosts, dict):
            host_obj = hosts.get(host_id)
            if isinstance(host_obj, dict):
                gate = host_obj.get("ui_scanner_gate")
                if isinstance(gate, dict) and gate.get("verified") is True:
                    mode = str(gate.get("container_mode") or "")
                    if mode in ("symlink_subtrees", "staged_copy"):
                        return mode
    return "staged_copy"


def is_forbidden_plugin_cwd(cwd_value: str, home: Path | None = None) -> bool:
    """Return True if `cwd_value` points inside a plugin installation or container directory."""
    if not cwd_value:
        return False
    expanded = expand_for_home(cwd_value, home=home)
    base = resolve_home(home)
    forbidden_prefixes = (
        str(base / ".minni" / "plugin"),
        str(base / ".cursor" / "plugins"),
        str(base / ".agents" / "plugins"),
    )
    for prefix in forbidden_prefixes:
        if expanded == prefix or expanded.startswith(prefix + "/"):
            return True
    return ".minni/plugin" in expanded


@dataclass
class McpSurfaceDiff:
    has_delta: bool
    reasons: list[str] = field(default_factory=list)
    desired_entry: dict[str, Any] = field(default_factory=dict)


def evaluate_mcp_surface_diff(
    live_entry: dict[str, Any] | None,
    *,
    host_id: str,
    agent_id: str,
    vault_path: str,
    surface_spec: dict[str, Any],
    home: Path | None = None,
    workspace_pinned_by_operator: bool = False,
) -> McpSurfaceDiff:
    """Evaluate Keyed-Subset + Forbidden-Key Diff Semantics for an MCP entry (§4.2)."""
    reasons: list[str] = []
    expanded_vault = expand_for_home(vault_path, home=home)
    canonical_entry = expand_for_home(str(surface_spec.get("entry") or ""), home=home)
    raw_accepted = surface_spec.get("accepted_entries") or [canonical_entry]
    accepted_entries = [expand_for_home(str(e), home=home) for e in raw_accepted]
    expected_command = surface_spec.get("command")
    if isinstance(expected_command, str):
        expected_command = expand_for_home(expected_command, home=home)
    args_prefix = [
        expand_for_home(str(a), home=home)
        for a in (surface_spec.get("args_prefix") or [])
    ]

    is_kilo = surface_spec.get("key") == "mcp.minni"

    if not isinstance(live_entry, dict):
        reasons.append("missing_or_non_dict_entry")
        live_entry = {}

    # 1. Command & Entry Equivalence
    desired_entry = copy.deepcopy(live_entry)
    if is_kilo:
        cmd_list = live_entry.get("command")
        if not isinstance(cmd_list, list) or len(cmd_list) < 2:
            reasons.append("invalid_kilo_command_list")
            desired_entry["command"] = ["node", canonical_entry]
        else:
            script_arg = str(cmd_list[-1])
            if "~" in script_arg or _VERSIONED_PLUGIN_RE.search(script_arg) or script_arg not in accepted_entries:
                reasons.append(f"entry_mismatch:{script_arg}")
                desired_entry["command"] = ["node", canonical_entry]
        if live_entry.get("enabled") is not True:
            reasons.append("kilo_not_enabled")
            desired_entry["enabled"] = True
        if live_entry.get("type") != "local":
            desired_entry.setdefault("type", "local")
    else:
        live_cmd = live_entry.get("command")
        live_args = live_entry.get("args")
        if not isinstance(live_cmd, str) or not live_cmd:
            reasons.append("missing_command")
            live_cmd = expected_command or "node"
        elif "~" in live_cmd:
            reasons.append("literal_tilde_in_command")
            live_cmd = expand_for_home(live_cmd, home=home)

        if expected_command and expected_command.endswith("mcp-env-run"):
            # Antigravity wrapper rule (P2-5)
            if live_cmd != expected_command:
                reasons.append(f"command_mismatch:{live_cmd}!={expected_command}")
                live_cmd = expected_command

        if not isinstance(live_args, list):
            live_args = []
        # Check for literal ~ or versioned plugin path in args
        if any(isinstance(a, str) and "~" in a for a in live_args):
            reasons.append("literal_tilde_in_args")
        if any(isinstance(a, str) and _VERSIONED_PLUGIN_RE.search(a) for a in live_args):
            reasons.append("versioned_sha_in_args")

        if canonical_entry.endswith("minni-mcp") and live_cmd in accepted_entries:
            # Cursor wrapper as command directly
            desired_entry["command"] = live_cmd
        else:
            script_arg = str(live_args[-1]) if live_args else ""
            expected_len = len(args_prefix) + 1
            prefix_ok = (
                len(live_args) == expected_len
                and [str(x) for x in live_args[: len(args_prefix)]] == args_prefix
            )
            if not prefix_ok or script_arg not in accepted_entries:
                reasons.append(f"entry_or_args_mismatch:{live_args}")
                desired_entry["args"] = [*args_prefix, canonical_entry]
            else:
                desired_entry["args"] = [*args_prefix, script_arg]
            desired_entry["command"] = live_cmd

    # 2. Forbidden plugin-root cwd check (§4.2 #4)
    if "cwd" in desired_entry:
        cwd_val = str(desired_entry.get("cwd") or "")
        if "~" in cwd_val or is_forbidden_plugin_cwd(cwd_val, home=home):
            reasons.append(f"forbidden_cwd:{cwd_val}")
            desired_entry.pop("cwd", None)

    # 3. Environment Keyed-Subset + Forbidden-Key Diff (§4.2 #2-#4)
    env_key = "environment" if is_kilo else "env"
    raw_env = live_entry.get(env_key)
    if not isinstance(raw_env, dict):
        reasons.append(f"missing_{env_key}")
        raw_env = {}

    desired_env: dict[str, str] = {}
    for k, v in raw_env.items():
        k_str = str(k)
        v_str = str(v)
        if k_str in FORBIDDEN_ENV_KEYS or k_str.startswith("SOVEREIGN_"):
            reasons.append(f"forbidden_env_key:{k_str}")
            continue
        if k_str == "MINNI_PINNED_WORKSPACE_ID" and not workspace_pinned_by_operator:
            reasons.append("unpinned_operator_workspace_key")
            continue
        if k_str == "MINNI_AFM_NATIVE_HELPER":
            expanded_helper = expand_for_home(v_str, home=home)
            if "~" in v_str:
                reasons.append("literal_tilde_in_MINNI_AFM_NATIVE_HELPER")
            if _VERSIONED_PLUGIN_RE.search(expanded_helper):
                reasons.append(f"versioned_sha_in_MINNI_AFM_NATIVE_HELPER:{expanded_helper}")
                cur_helper = expand_for_home("~/.minni/plugin/current/dist/minni-afm-helper", home=home)
                if Path(cur_helper).is_file():
                    desired_env[k_str] = cur_helper
                continue
            if not Path(expanded_helper).is_file():
                reasons.append(f"missing_MINNI_AFM_NATIVE_HELPER:{expanded_helper}")
                continue
            desired_env[k_str] = expanded_helper
            continue
        if k_str == "MINNI_AFM_PROVIDER_MODE":
            if v_str not in ALLOWED_AFM_MODES:
                reasons.append(f"invalid_MINNI_AFM_PROVIDER_MODE:{v_str}")
                continue
            desired_env[k_str] = v_str
            continue
        if k_str == "MINNI_SOCKET_PATH":
            expanded_sock = expand_for_home(v_str, home=home)
            if "~" in v_str or not expanded_sock:
                reasons.append("literal_tilde_or_empty_MINNI_SOCKET_PATH")
            desired_env[k_str] = expanded_sock
            continue
        if k_str in ("MINNI_CODEX_VAULT_PATH", "MINNI_KILOCODE_VAULT_PATH"):
            expanded_cv = expand_for_home(v_str, home=home)
            if "~" in v_str or expanded_cv != expanded_vault:
                reasons.append(f"vault_mirror_mismatch:{k_str}")
            desired_env[k_str] = expanded_vault
            continue
        if k_str in ("MINNI_CODEX_AGENT_ID", "MINNI_KILOCODE_AGENT_ID"):
            if v_str != agent_id:
                reasons.append(f"agent_mirror_mismatch:{k_str}")
            desired_env[k_str] = agent_id
            continue
        desired_env[k_str] = v_str

    # Required keys: MINNI_AGENT_ID and MINNI_VAULT_PATH
    if raw_env.get("MINNI_AGENT_ID") != agent_id:
        reasons.append(f"agent_id_mismatch:{raw_env.get('MINNI_AGENT_ID')}!={agent_id}")
    desired_env["MINNI_AGENT_ID"] = agent_id

    live_vault = str(raw_env.get("MINNI_VAULT_PATH") or "")
    if "~" in live_vault:
        reasons.append("literal_tilde_in_MINNI_VAULT_PATH")
    if live_vault != expanded_vault:
        reasons.append(f"vault_path_mismatch:{live_vault}!={expanded_vault}")
    desired_env["MINNI_VAULT_PATH"] = expanded_vault

    desired_entry[env_key] = desired_env
    return McpSurfaceDiff(
        has_delta=bool(reasons),
        reasons=reasons,
        desired_entry=desired_entry,
    )

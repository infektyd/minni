"""Per-platform config writers ported from propagate.py (Phase 1 independent copy)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

DEFAULT_SOCKET = Path("~/.minni/run/minnid.sock").expanduser()
GEMINI_MCP_ENV_RUN = Path("~/.agents/bin/mcp-env-run").expanduser()
GEMINI_IDE_TYPE_NAME = "exa.cascade_plugins_pb.CascadePluginCommandTemplate"
GEMINI_SURFACE_CONFIGS = (
    "~/.gemini/config/mcp_config.json",
    "~/.gemini/antigravity/mcp_config.json",
    "~/.gemini/antigravity-ide/mcp_config.json",
    "~/.gemini/antigravity-cli/plugins/minni/mcp_config.json",
)
GEMINI_LEGACY_GRANT_MARKERS = ("mcp(sovereign-memory", "mcp(sovereign_memory", "sovereign_")
MINNI_READONLY_TOOLS = (
    "minni_recall", "minni_drill", "minni_status", "minni_audit_tail",
    "minni_audit_report", "minni_route", "minni_list_pending_handoffs",
    "minni_ping_agent_inbox", "minni_ping_agent_status",
)
MINNI_READONLY_GRANTS = tuple(f"mcp(minni/{tool})" for tool in MINNI_READONLY_TOOLS)
MINNI_WILDCARD_GRANT = "mcp(minni/*)"
AGY_PLUGIN_NAME = "minni"
AGY_PLUGINS_DIR = "~/.gemini/config/plugins"
AGY_DIST_TOKEN = "__MINNI_GEMINI_DIST__"


def normalize_workspace_id(value: str | None) -> str:
    if not value:
        return ""
    value = str(value).strip()
    if not value:
        return ""
    if value.startswith("workspace-"):
        return "workspace-" + value[len("workspace-"):].lower()
    basename = os.path.basename(value.rstrip("/"))
    if not basename:
        return ""
    return "workspace-" + basename.lower()


def vault_for(agent: str) -> Path:
    if agent == "codex":
        return Path("~/.minni/codex-vault").expanduser()
    if agent in {"claude", "claude-code"}:
        return Path("~/.minni/claudecode-vault").expanduser()
    if agent == "gemini":
        canonical = Path("~/.minni/gemini-vault").expanduser()
        legacy = Path("~/.gemini/minni-vault").expanduser()
        if not canonical.exists() and legacy.exists() and any(legacy.iterdir()):
            sys.stderr.write(
                f"[wire] gemini vault still at legacy path: {legacy}\n"
                f"  Move it to the canonical layout: mv {legacy} {canonical}\n",
            )
            return legacy
        return canonical
    candidate = Path(f"~/.minni/{agent}-vault").expanduser()
    minni_root = Path("~/.minni").expanduser().resolve()
    resolved = candidate.resolve()
    if not str(resolved).startswith(str(minni_root) + os.sep):
        raise ValueError(
            f"vault path {resolved} escapes ~/.minni for agent {agent!r}",
        )
    return candidate


def _vault_path_is_safe(value: str, agent: str) -> bool:
    expected = vault_for(agent)
    minni_root = Path("~/.minni").expanduser()
    try:
        candidate = Path(value).expanduser()
    except Exception:
        return False
    if str(candidate) != str(expected):
        return False
    is_under_minni = str(candidate) == str(minni_root) or str(candidate).startswith(
        str(minni_root) + os.sep,
    )
    is_gemini_legacy = str(candidate) == str(Path("~/.gemini/minni-vault").expanduser())
    if not (is_under_minni or is_gemini_legacy):
        return False
    try:
        if candidate.is_symlink():
            return False
        if candidate.exists():
            st = candidate.stat()
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                return False
    except OSError:
        return False
    return True


def _validate_preserved_identity(ex_env: dict, agent: str) -> dict:
    validated = dict(ex_env)
    expected_vault = str(vault_for(agent))
    expected_socket = str(DEFAULT_SOCKET)
    if validated.get("MINNI_AGENT_ID") != agent and "MINNI_AGENT_ID" in validated:
        validated["MINNI_AGENT_ID"] = agent
    if "MINNI_VAULT_PATH" in validated and not _vault_path_is_safe(
        str(validated["MINNI_VAULT_PATH"]), agent,
    ):
        validated["MINNI_VAULT_PATH"] = expected_vault
    if (
        "MINNI_SOCKET_PATH" in validated
        and str(validated["MINNI_SOCKET_PATH"]) != expected_socket
    ):
        validated["MINNI_SOCKET_PATH"] = expected_socket
    return validated


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    # A zero-byte config (touch'd placeholder, truncated write) is an empty
    # doc, not a parse error — wire must not fail with "Expecting value".
    if not text.strip():
        return {}
    return json.loads(text)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def mcp_json(
    server_path: Path,
    agent: str,
    vault: Path,
    socket_path: Path,
    workspace: Path,
    *,
    target_path: Path | None = None,
    explicit_workspace: bool = False,
    pre_existing_env: dict | None = None,
    afm_env: dict[str, str] | None = None,
) -> dict:
    normalized_workspace = normalize_workspace_id(str(workspace))
    env = {
        "MINNI_AGENT_ID": agent,
        "MINNI_VAULT_PATH": str(vault),
        "MINNI_SOCKET_PATH": str(socket_path),
        "MINNI_WORKSPACE_ID": normalized_workspace,
    }
    env.update(afm_env or {})
    ex_env: dict = {}
    if pre_existing_env is not None:
        ex_env = pre_existing_env
    elif target_path is not None and target_path.exists():
        try:
            ex = load_json(target_path)
            ex_env = ex.get("mcpServers", {}).get("minni", {}).get("env", {}) or {}
        except Exception:
            pass
    if ex_env:
        ex_env = _validate_preserved_identity(ex_env, agent)
        for key in (
            "MINNI_AGENT_ID", "MINNI_VAULT_PATH", "MINNI_SOCKET_PATH",
            "MINNI_AFM_PROVIDER_MODE", "MINNI_AFM_NATIVE_HELPER",
        ):
            if key in ex_env:
                env[key] = ex_env[key]
        if "MINNI_WORKSPACE_ID" in ex_env and not explicit_workspace:
            env["MINNI_WORKSPACE_ID"] = ex_env["MINNI_WORKSPACE_ID"]
    cwd = server_path.parent.parent if server_path.parent.name == "dist" else server_path.parent
    return {
        "mcpServers": {
            "minni": {
                "command": "node",
                "args": [str(server_path)],
                "cwd": str(cwd),
                "env": env,
            },
        },
    }


def update_claude_config(
    server_path: Path, agent: str, vault: Path, socket_path: Path,
    workspace: Path, afm_env: dict[str, str] | None = None,
) -> Path:
    path = Path("~/.claude.json").expanduser()
    data = load_json(path)
    data.setdefault("mcpServers", {})["minni"] = {
        "type": "stdio",
        "command": "node",
        "args": [str(server_path)],
        "env": {
            "MINNI_AGENT_ID": agent,
            "MINNI_VAULT_PATH": str(vault),
            "MINNI_SOCKET_PATH": str(socket_path),
            "MINNI_WORKSPACE_ID": normalize_workspace_id(str(workspace)),
            **(afm_env or {}),
        },
    }
    write_json(path, data)
    return path


def update_kilo_config(
    server_path: Path, agent: str, vault: Path, socket_path: Path,
    workspace: Path, afm_env: dict[str, str] | None = None,
) -> Path:
    path = Path("~/.config/kilo/kilo.json").expanduser()
    data = load_json(path)
    data.setdefault("mcp", {})["minni"] = {
        "type": "local",
        "command": ["node", str(server_path)],
        "enabled": True,
        "env": {
            "MINNI_AGENT_ID": agent,
            "MINNI_VAULT_PATH": str(vault),
            "MINNI_SOCKET_PATH": str(socket_path),
            "MINNI_WORKSPACE_ID": normalize_workspace_id(str(workspace)),
            **(afm_env or {}),
        },
    }
    write_json(path, data)
    return path


def gemini_minni_entry(
    server_path: Path,
    agent: str,
    vault: Path,
    socket_path: Path,
    workspace: Path,
    afm_env: dict[str, str] | None = None,
    type_name: str | None = None,
    *,
    launcher_fallback: bool = False,
) -> dict:
    entry: dict = {}
    if type_name:
        entry["$typeName"] = type_name
    use_wrapper = (
        not launcher_fallback
        and (shutil.which("mcp-env-run") or GEMINI_MCP_ENV_RUN.exists())
    )
    if use_wrapper:
        entry["command"] = str(GEMINI_MCP_ENV_RUN)
        entry["args"] = ["node", str(server_path)]
    else:
        entry["command"] = "node"
        entry["args"] = [str(server_path)]
    entry["cwd"] = str(Path(server_path).parent.parent)
    entry["env"] = {
        "MINNI_AGENT_ID": agent,
        "MINNI_VAULT_PATH": str(vault),
        "MINNI_SOCKET_PATH": str(socket_path),
        "MINNI_WORKSPACE_ID": normalize_workspace_id(str(workspace)),
        **(afm_env or {}),
    }
    return entry


def write_view_entry(
    view_path: Path,
    server_path: Path,
    agent: str,
    vault: Path,
    socket_path: Path,
    workspace: Path,
    afm_env: dict[str, str] | None = None,
    *,
    launcher_fallback: bool = False,
) -> bool:
    if not view_path.exists():
        return False
    data = load_json(view_path)
    if not isinstance(data, dict):
        return False
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return False
    type_name = None
    for key, value in servers.items():
        if key == "minni":
            continue
        if isinstance(value, dict) and "$typeName" in value:
            type_name = value["$typeName"]
            break
    new_entry = gemini_minni_entry(
        server_path, agent, vault, socket_path, workspace, afm_env, type_name,
        launcher_fallback=launcher_fallback,
    )
    if servers.get("minni") == new_entry and "sovereign-memory" not in servers:
        return True
    servers.pop("sovereign-memory", None)
    servers["minni"] = new_entry
    write_json(view_path, data)
    return True


def _find_allow_owner(node: object, container_key: str, leaf: str) -> dict | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == container_key and isinstance(value, dict) and isinstance(value.get(leaf), list):
                return value
        for value in node.values():
            found = _find_allow_owner(value, container_key, leaf)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_allow_owner(value, container_key, leaf)
            if found is not None:
                return found
    return None


def ensure_permission_grant(
    path: Path,
    key_path: list[str],
    grants: tuple[str, ...] = MINNI_READONLY_GRANTS,
    legacy_markers: tuple[str, ...] = GEMINI_LEGACY_GRANT_MARKERS,
) -> bool:
    if not path.exists():
        return False
    data = load_json(path)
    if not isinstance(data, dict):
        return False
    leaf = key_path[-1]
    owner = _find_allow_owner(data, key_path[-2], leaf) if len(key_path) >= 2 else None
    if owner is None:
        owner = data
        for key in key_path[:-1]:
            child = owner.get(key)
            if not isinstance(child, dict):
                child = {}
                owner[key] = child
            owner = child
    allow = owner.get(leaf)
    if not isinstance(allow, list):
        allow = []
    filtered = [
        g for g in allow
        if str(g) != MINNI_WILDCARD_GRANT
        and not any(marker in str(g) for marker in legacy_markers)
    ]
    for grant in grants:
        if grant not in filtered:
            filtered.append(grant)
    if owner.get(leaf) == filtered:
        return True
    owner[leaf] = filtered
    write_json(path, data)
    return True


def update_antigravity_config(
    install_root: Path,
    agent: str,
    vault: Path,
    socket_path: Path,
    workspace: Path,
    afm_env: dict[str, str] | None = None,
) -> dict[str, object]:
    server_path = install_root / "dist" / "server.js"
    launcher_fallback = not (
        shutil.which("mcp-env-run") or GEMINI_MCP_ENV_RUN.exists()
    )
    written: list[str] = []
    for surface in GEMINI_SURFACE_CONFIGS:
        surface_path = Path(surface).expanduser()
        target = surface_path.resolve() if surface_path.exists() else surface_path
        if write_view_entry(
            target, server_path, agent, vault, socket_path, workspace, afm_env,
            launcher_fallback=launcher_fallback,
        ):
            written.append(str(target))
    grants = {
        "~/.gemini/config/config.json": ["globalPermissionGrants", "allow"],
        "~/.gemini/antigravity-cli/settings.json": ["permissions", "allow"],
    }
    granted: list[str] = []
    for path_str, key_path in grants.items():
        if ensure_permission_grant(Path(path_str).expanduser(), key_path):
            granted.append(path_str)
    return {
        "views_written": written,
        "grants_updated": granted,
        "launcher_fallback": launcher_fallback,
    }


def update_agy_plugin_hooks(install_root: Path) -> dict[str, object]:
    template = install_root / "hooks" / "hooks-gemini.json"
    if not template.exists():
        return {"installed": False, "reason": f"missing hooks template: {template}"}
    agy = shutil.which("agy")
    if not agy:
        return {
            "installed": False,
            "reason": "agy CLI not found on PATH; hook registration skipped",
        }
    hooks_data = json.loads(template.read_text(encoding="utf-8"))
    hooks_data.pop("_comment", None)
    stamped = json.dumps(hooks_data, indent=2).replace(
        AGY_DIST_TOKEN, str(install_root / "dist"),
    )
    staging_root = Path(tempfile.mkdtemp(prefix="minni-agy-plugin-"))
    enable_note = ""
    try:
        staging = staging_root / AGY_PLUGIN_NAME
        staging.mkdir()
        (staging / "plugin.json").write_text(
            json.dumps({"name": AGY_PLUGIN_NAME}) + "\n", encoding="utf-8",
        )
        (staging / "hooks.json").write_text(stamped + "\n", encoding="utf-8")
        try:
            subprocess.run(
                [agy, "plugin", "install", str(staging)],
                check=True, capture_output=True, text=True, timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)
            return {"installed": False, "reason": f"agy plugin install failed: {detail.strip()}"}
        enable = subprocess.run(
            [agy, "plugin", "enable", AGY_PLUGIN_NAME],
            capture_output=True, text=True, timeout=60,
        )
        if enable.returncode != 0:
            enable_note = (enable.stderr.strip() or enable.stdout.strip())
            if "already enabled" not in enable_note.lower():
                return {
                    "installed": False,
                    "reason": f"agy plugin enable failed: {enable_note or 'unknown error'}",
                }
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    installed_hooks = Path(AGY_PLUGINS_DIR).expanduser() / AGY_PLUGIN_NAME / "hooks.json"
    ok = (
        installed_hooks.exists()
        and str(install_root / "dist") in installed_hooks.read_text(encoding="utf-8")
    )
    result: dict[str, object] = {
        "installed": ok,
        "hooks_path": str(installed_hooks),
        "hook_entry": str(install_root / "dist" / "gemini-hook.js"),
    }
    if not ok:
        result["reason"] = (
            "agy plugin install completed but stamped hooks.json not found"
        )
    if enable_note:
        result["enable_note"] = enable_note
    return result


def _toml_basic_str(value: object) -> str:
    # TOML basic strings forbid raw control characters — an unescaped newline in
    # e.g. a --workspace basename corrupts the target config (and a crafted value
    # could break out of the string and inject TOML sections).
    out: list[str] = []
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def replace_toml_sections(
    path: Path, sections: dict[str, str], *, preserve_surface_env: bool = False,
) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if preserve_surface_env and path.exists() and "mcp_servers.minni.env" in sections:
        try:
            data = tomllib.loads(text)
            ex_env = data.get("mcp_servers", {}).get("minni", {}).get("env", {}) or {}
            if ex_env:
                try:
                    fresh_env = (
                        tomllib.loads(sections["mcp_servers.minni.env"])
                        .get("mcp_servers", {})
                        .get("minni", {})
                        .get("env", {})
                        or {}
                    )
                except Exception:
                    fresh_env = {}
                expected_agent = fresh_env.get("MINNI_AGENT_ID")
                if expected_agent:
                    ex_env = _validate_preserved_identity(ex_env, expected_agent)
                preserved_lines = []
                for key in (
                    "MINNI_AGENT_ID", "MINNI_VAULT_PATH", "MINNI_SOCKET_PATH",
                    "MINNI_WORKSPACE_ID", "MINNI_AFM_PROVIDER_MODE", "MINNI_AFM_NATIVE_HELPER",
                ):
                    if key in ex_env:
                        val = ex_env[key]
                    elif key in fresh_env:
                        val = fresh_env[key]
                    else:
                        continue
                    preserved_lines.append(f'{key} = "{_toml_basic_str(val)}"')
                if preserved_lines:
                    sections["mcp_servers.minni.env"] = (
                        "[mcp_servers.minni.env]\n" + "\n".join(preserved_lines)
                    )
        except Exception:
            pass
    for name in sections:
        pattern = re.compile(rf"(?ms)^\[{re.escape(name)}\]\n.*?(?=^\[|\Z)")
        text = pattern.sub("", text)
    text = text.rstrip() + "\n\n" + "\n\n".join(sections.values()).rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def update_toml_mcp_config(
    path: Path,
    server_path: Path,
    agent: str,
    vault: Path,
    socket_path: Path,
    workspace: Path,
    *,
    explicit_workspace: bool = False,
    afm_env: dict[str, str] | None = None,
) -> None:
    replace_toml_sections(
        path,
        {
            "mcp_servers.minni": (
                "[mcp_servers.minni]\n"
                'command = "node"\n'
                f'args = ["{_toml_basic_str(server_path)}"]\n'
                "enabled = true"
            ),
            "mcp_servers.minni.env": (
                "[mcp_servers.minni.env]\n"
                f'MINNI_AGENT_ID = "{_toml_basic_str(agent)}"\n'
                f'MINNI_VAULT_PATH = "{_toml_basic_str(vault)}"\n'
                f'MINNI_SOCKET_PATH = "{_toml_basic_str(socket_path)}"\n'
                f'MINNI_WORKSPACE_ID = "{_toml_basic_str(normalize_workspace_id(str(workspace)))}"'
                + "".join(
                    f'\n{k} = "{_toml_basic_str(v)}"' for k, v in (afm_env or {}).items()
                )
            ),
        },
        preserve_surface_env=not explicit_workspace,
    )


def bootstrap_vault(agent: str) -> Path:
    vault = vault_for(agent)
    if vault.is_symlink():
        raise ValueError(f"refusing symlinked vault root: {vault}")
    if vault.exists() and not vault.is_dir():
        raise ValueError(f"vault path exists but is not a directory: {vault}")
    vault.mkdir(parents=True, exist_ok=True)
    for child in ("raw", "wiki", "logs", "schema", "inbox", "outbox"):
        (vault / child).mkdir(exist_ok=True)
    schema = vault / "schema" / "AGENTS.md"
    if not schema.exists():
        schema.write_text(
            f"# {agent} Minni Vault\n\n"
            "This is an actual per-agent vault directory.\n",
            encoding="utf-8",
        )
    index = vault / "index.md"
    if not index.exists():
        index.write_text(f"# {agent} Vault Index\n\n", encoding="utf-8")
    log = vault / "log.md"
    if not log.exists():
        log.write_text(f"# {agent} Vault Log\n\n", encoding="utf-8")
    return vault


def native_afm_env(repo_root: Path | None) -> dict[str, str]:
    if repo_root is None:
        return {}
    for sub in (Path("src") / "minni", Path("engine")):
        helper = repo_root / sub / "native_afm_helper"
        if helper.exists():
            return {
                "MINNI_AFM_PROVIDER_MODE": "native",
                "MINNI_AFM_NATIVE_HELPER": str(helper),
            }
    return {}
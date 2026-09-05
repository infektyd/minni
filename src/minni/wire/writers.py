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
# Worker allowlist is post-claim only. One tool: minni_thread_worker_update
# (actions already include start/progress/block/scar/propose_structure/complete).
# Do not merge these into MINNI_READONLY_GRANTS. Claim stays orchestrator-side.
MINNI_WORKER_TOOLS = (
    "minni_thread_worker_update",
)
MINNI_WORKER_GRANTS = tuple(f"mcp(minni/{tool})" for tool in MINNI_WORKER_TOOLS)
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


def _filter_dead_afm_helper(ex_env: dict) -> dict:
    """Drop a preserved AFM helper path that is gone on disk.

    Surface preserve must not re-stamp MINNI_AFM_NATIVE_HELPER when the path no
    longer exists — otherwise wire/propagate redeploy re-poisons the field
    that check_deployments --strict / sync-root step 6 gate on (D14), and
    make sync-root can never heal the live machine without a hand-edit.
    When the helper is dead and mode was ``native``, also drop mode so a live
    ``native_afm_env()`` result can replace both keys.
    """
    out = dict(ex_env)
    helper = out.get("MINNI_AFM_NATIVE_HELPER")
    if helper is None:
        return out
    helper_path = Path(str(helper)).expanduser()
    if helper_path.is_file():
        return out
    out.pop("MINNI_AFM_NATIVE_HELPER", None)
    if str(out.get("MINNI_AFM_PROVIDER_MODE", "")).lower() == "native":
        out.pop("MINNI_AFM_PROVIDER_MODE", None)
    return out


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


def _mirror_codex_hook_env(env: dict, agent: str) -> None:
    """Mirror resolved generic identity into MINNI_CODEX_* for codex.

    The Codex hook entrypoint reads only MINNI_CODEX_* (never generic MINNI_*
    the MCP server uses). Without the mirror, a custom MINNI_VAULT_PATH leaves
    hooks writing audit/inbox under ~/.minni/codex-vault while MCP points at
    the configured vault. Called after surface preservation so hooks track
    whatever vault the install actually resolved.
    """
    if agent != "codex":
        return
    # Always re-derive from the resolved generic identity so a stale
    # MINNI_CODEX_* cannot split hooks from MCP (never raw-preserve CODEX_*).
    env["MINNI_CODEX_AGENT_ID"] = env.get("MINNI_AGENT_ID", "codex")
    if "MINNI_VAULT_PATH" in env:
        env["MINNI_CODEX_VAULT_PATH"] = env["MINNI_VAULT_PATH"]
    if "MINNI_WORKSPACE_ID" in env:
        env["MINNI_CODEX_WORKSPACE_ID"] = env["MINNI_WORKSPACE_ID"]


def mcp_json(
    server_path: Path,
    agent: str,
    vault: Path,
    socket_path: Path,
    workspace: Path | None,
    *,
    target_path: Path | None = None,
    explicit_workspace: bool = False,
    dynamic_workspace: bool = False,
    pre_existing_env: dict | None = None,
    afm_env: dict[str, str] | None = None,
) -> dict:
    env = {
        "MINNI_AGENT_ID": agent,
        "MINNI_VAULT_PATH": str(vault),
        "MINNI_SOCKET_PATH": str(socket_path),
    }
    if workspace is not None:
        env["MINNI_WORKSPACE_ID"] = normalize_workspace_id(str(workspace))
    env.update(afm_env or {})
    ex_env: dict = {}
    if pre_existing_env is not None:
        ex_env = pre_existing_env
    elif target_path is not None and target_path.exists():
        try:
            ex = load_json(target_path)
            ex_env = ex.get("mcpServers", {}).get("minni", {}).get("env", {}) or {}
        except Exception as exc:
            # D10 twin: unparseable .mcp.json must not drop surface env.
            raise ValueError(
                f"cannot parse existing .mcp.json at {target_path}: {exc}. "
                "Refusing to rewrite mcpServers.minni.env — the surface's "
                "preserved env would be silently dropped. Fix or remove the "
                "file, then re-run."
            ) from exc
    if agent == "codex":
        env = _resolve_codex_env(env, ex_env, explicit_workspace=explicit_workspace,
                                 dynamic_workspace=dynamic_workspace)
    elif ex_env:
        ex_env = _filter_dead_afm_helper(
            _validate_preserved_identity(ex_env, agent),
        )
        # Never carry raw MINNI_CODEX_* from the surface — re-derive after
        # generic identity resolves (parity with propagate X2).
        for key in (
            "MINNI_AGENT_ID", "MINNI_VAULT_PATH", "MINNI_SOCKET_PATH",
            "MINNI_AFM_PROVIDER_MODE", "MINNI_AFM_NATIVE_HELPER",
        ):
            if key in ex_env:
                env[key] = ex_env[key]
        if "MINNI_WORKSPACE_ID" in ex_env and not explicit_workspace:
            env["MINNI_WORKSPACE_ID"] = ex_env["MINNI_WORKSPACE_ID"]
    _mirror_codex_hook_env(env, agent)
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


def validate_codex_env(existing: object) -> None:
    """MCP env values are strings; reject invalid TOML/JSON before any writes."""
    if not isinstance(existing, dict):
        raise ValueError("existing Codex MCP env must be an object")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in existing.items()):
        raise ValueError("existing Codex MCP env must map string keys to string values")


def _resolve_codex_env(
    fresh: dict, existing: dict, *, explicit_workspace: bool, dynamic_workspace: bool,
) -> dict:
    """Preserve the host's env while resolving only its workspace policy.

    No new workspace means dynamic, but absence of a CLI override must not
    erase an existing deliberate pin. A legacy Codex-only pin also counts.
    """
    if explicit_workspace and dynamic_workspace:
        raise ValueError("--workspace and --dynamic-workspace are mutually exclusive")
    validate_codex_env(existing)
    preserved = _filter_dead_afm_helper(_validate_preserved_identity(existing, "codex"))
    env = {**fresh, **preserved}
    workspace = fresh.get("MINNI_WORKSPACE_ID")
    if not explicit_workspace:
        workspace = preserved.get("MINNI_WORKSPACE_ID", preserved.get("MINNI_CODEX_WORKSPACE_ID", workspace))
    env.pop("MINNI_WORKSPACE_ID", None)
    env.pop("MINNI_CODEX_WORKSPACE_ID", None)
    if not dynamic_workspace and workspace is not None:
        env["MINNI_WORKSPACE_ID"] = workspace
    _mirror_codex_hook_env(env, "codex")
    return env


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
    # Kilo's McpLocal schema is strict and names this key "environment", not
    # "env" (Claude/Codex spelling). Writing "env" makes Kilo reject the whole
    # config file with ConfigInvalidError and refuse to start AT ALL — it takes
    # down the entire CLI, not just Minni. Parity with propagate.update_kilo_config.
    data.setdefault("mcp", {})["minni"] = {
        "type": "local",
        "command": ["node", str(server_path)],
        "enabled": True,
        "environment": {
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


def ensure_worker_permission_grant(
    path: Path,
    key_path: list[str],
    grants: tuple[str, ...] = MINNI_WORKER_GRANTS,
    legacy_markers: tuple[str, ...] = GEMINI_LEGACY_GRANT_MARKERS,
) -> bool:
    """Write worker grants onto a worker allowlist only.

    Still strips ``mcp(minni/*)``. That wildcard would grant structural
    thread tools. The named worker grant is not a wildcard, so the strip
    must leave ``mcp(minni/minni_thread_worker_update)`` in place.

    Not called from ``update_antigravity_config``. Default install stays
    the readonly nine.
    """
    return ensure_permission_grant(
        path, key_path, grants=grants, legacy_markers=legacy_markers,
    )


def _view_has_minni_binding(view_path: Path) -> bool | None:
    """Whether a Gemini/Antigravity view already carries a Minni binding.

    True when a `minni`/`sovereign-memory` server entry exists (same name
    normalization as host discovery); False when the view parses without one;
    None when it cannot be read. Bulk refresh must never activate a view the
    operator never enabled, so only True views are eligible there — an
    unreadable view is not provably configured and is left untouched.
    """
    try:
        data = load_json(view_path)
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    for name in servers:
        normalized = str(name).lower().replace("_", "-")
        if normalized in {"minni", "sovereign-memory"} or normalized.startswith(
            ("minni-", "sovereign-memory-")
        ):
            return True
    return False


def update_antigravity_config(
    install_root: Path,
    agent: str,
    vault: Path,
    socket_path: Path,
    workspace: Path,
    afm_env: dict[str, str] | None = None,
    *,
    existing_only: bool = False,
) -> dict[str, object]:
    server_path = install_root / "dist" / "server.js"
    launcher_fallback = not (
        shutil.which("mcp-env-run") or GEMINI_MCP_ENV_RUN.exists()
    )
    written: list[str] = []
    skipped_unconfigured: list[str] = []
    for surface in GEMINI_SURFACE_CONFIGS:
        surface_path = Path(surface).expanduser()
        target = surface_path.resolve() if surface_path.exists() else surface_path
        if existing_only and _view_has_minni_binding(target) is not True:
            # Bulk/refresh path: a view without a Minni binding stays exactly
            # as the operator left it — refreshing it would activate an
            # integration that was never enabled.
            if target.exists():
                skipped_unconfigured.append(str(target))
            continue
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
        "views_skipped_unconfigured": skipped_unconfigured,
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
            "error_class": "missing_cli",
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
        # D10 (#232): an unparseable existing config is a hard error, never a
        # silent fall-through — swallowing it here rewrote the env section
        # WITHOUT the surface's preserved values while reporting success.
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(
                f"cannot parse existing TOML at {path}: {exc}. Refusing to "
                "rewrite [mcp_servers.minni.env] — the surface's preserved env "
                "would be silently dropped. Fix or remove the file, then re-run."
            ) from exc
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
            # Drop dead AFM helper before merge so fresh_env (live helper) wins.
            ex_env = _filter_dead_afm_helper(ex_env)
            # Resolve generic identity only — never carry raw MINNI_CODEX_*
            # from the surface (X2). Re-derive mirrors from the resolved
            # identity when the fresh section is for codex.
            resolved_env: dict = {}
            for key in (
                "MINNI_AGENT_ID", "MINNI_VAULT_PATH", "MINNI_SOCKET_PATH",
                "MINNI_WORKSPACE_ID", "MINNI_AFM_PROVIDER_MODE",
                "MINNI_AFM_NATIVE_HELPER",
            ):
                if key in ex_env:
                    resolved_env[key] = ex_env[key]
                elif key in fresh_env:
                    resolved_env[key] = fresh_env[key]
            if any(k.startswith("MINNI_CODEX_") for k in fresh_env) or (
                str(resolved_env.get("MINNI_AGENT_ID") or "") == "codex"
            ):
                _mirror_codex_hook_env(resolved_env, "codex")
            if resolved_env:
                preserved_lines = [
                    f'{k} = "{_toml_basic_str(v)}"' for k, v in resolved_env.items()
                ]
                sections["mcp_servers.minni.env"] = (
                    "[mcp_servers.minni.env]\n" + "\n".join(preserved_lines)
                )
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
    workspace: Path | None,
    *,
    explicit_workspace: bool = False,
    dynamic_workspace: bool = False,
    afm_env: dict[str, str] | None = None,
) -> None:
    if agent == "codex":
        # Resolve before replacing sections, including custom env. The legacy
        # generic section merger intentionally keeps its non-Codex behavior.
        existing = {}
        if path.exists():
            try:
                existing = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers", {}).get("minni", {}).get("env", {})
            except (tomllib.TOMLDecodeError, AttributeError) as exc:
                raise ValueError(f"cannot parse existing TOML at {path}; refusing to rewrite Codex env") from exc
        env = _resolve_codex_env(
            {"MINNI_AGENT_ID": agent, "MINNI_VAULT_PATH": str(vault),
             "MINNI_SOCKET_PATH": str(socket_path), **(afm_env or {}),
             **({"MINNI_WORKSPACE_ID": normalize_workspace_id(str(workspace))} if workspace is not None else {})},
            existing, explicit_workspace=explicit_workspace, dynamic_workspace=dynamic_workspace,
        )
        replace_toml_sections(path, {
            "mcp_servers.minni": '[mcp_servers.minni]\ncommand = "node"\n'
                f'args = ["{_toml_basic_str(server_path)}"]\nenabled = true',
            "mcp_servers.minni.env": "[mcp_servers.minni.env]\n" + "\n".join(
                f'"{_toml_basic_str(key)}" = "{_toml_basic_str(value)}"' for key, value in env.items()
            ),
        })
        return
    ws = normalize_workspace_id(str(workspace))
    env_lines = [
        f'MINNI_AGENT_ID = "{_toml_basic_str(agent)}"',
        f'MINNI_VAULT_PATH = "{_toml_basic_str(vault)}"',
        f'MINNI_SOCKET_PATH = "{_toml_basic_str(socket_path)}"',
        f'MINNI_WORKSPACE_ID = "{_toml_basic_str(ws)}"',
    ]
    for k, v in (afm_env or {}).items():
        env_lines.append(f'{k} = "{_toml_basic_str(v)}"')
    if agent == "codex":
        # Codex hooks read only MINNI_CODEX_*; stamp mirrors from resolved identity.
        env_lines.extend([
            f'MINNI_CODEX_AGENT_ID = "{_toml_basic_str(agent)}"',
            f'MINNI_CODEX_VAULT_PATH = "{_toml_basic_str(vault)}"',
            f'MINNI_CODEX_WORKSPACE_ID = "{_toml_basic_str(ws)}"',
        ])
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
                "[mcp_servers.minni.env]\n" + "\n".join(env_lines)
            ),
        },
        preserve_surface_env=not explicit_workspace,
    )


def bootstrap_vault(agent: str) -> Path:
    from minni.vault_layout import (
        _reject_symlink_or_escape,
        _resolved_vault_root,
        _seed_exclusive_file,
    )

    vault = vault_for(agent)
    if vault.is_symlink():
        raise ValueError(f"refusing symlinked vault root: {vault}")
    if vault.exists() and not vault.is_dir():
        raise ValueError(f"vault path exists but is not a directory: {vault}")
    vault.mkdir(parents=True, exist_ok=True)
    root_real = _resolved_vault_root(vault)
    for child in ("raw", "wiki", "logs", "schema", "inbox", "outbox"):
        dest = vault / child
        _reject_symlink_or_escape(dest, root_real, child)
        dest.mkdir(exist_ok=True)
    schema = vault / "schema" / "AGENTS.md"
    _reject_symlink_or_escape(schema, root_real, "schema/AGENTS.md")
    _seed_exclusive_file(
        schema,
        f"# {agent} Minni Vault\n\n"
        "This is an actual per-agent vault directory.\n",
    )
    _seed_exclusive_file(vault / "index.md", f"# {agent} Vault Index\n\n")
    _seed_exclusive_file(vault / "log.md", f"# {agent} Vault Log\n\n")
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

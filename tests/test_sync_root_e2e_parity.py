"""Round 15 end-to-end sync-root writer/checker parity and idempotency suite.

Builds a hermetic temp HOME populated with every host the operator runs:
- claude-code (incl. local marketplace, cache, settings.json, and ~/.claude.json)
- claude-desktop (~/Library/Application Support/Claude/claude_desktop_config.json)
- codex (~/.codex/config.toml, hooks.json, skills/minni/SKILL.md)
- grok-build (~/.grok/config.toml, hooks/minni.json, hooks.json, rules/minni.md)
- antigravity (container ~/.agents/plugins/Minni@Minni, settings.json, all 4
  mcp_config views, config.json with mcp_view_mirrors, antigravity-cli/settings.json,
  config/plugins/minni/hooks.json, commands/minni)
- cursor (container ~/.cursor/plugins/local/minni, mcp.json with hand-written
  ~/.local/bin/minni-mcp wrapper shape, hooks.json, hooks/minni-cursor.sh)
- kilocode (~/.config/kilo/kilo.json, plugin/minni.js)
- devin (~/.config/devin/mcp_config.json with custom args, config.json,
  scripts/minni-hook.sh)
- muse (~/.config/muse/settings.json and ~/.muse/mcp.json with custom args)

Then runs the exact step sequence from scripts/update_root.sh (backed by a
shared step specification and a drift-detection test against scripts/update_root.sh)
and asserts:
1. Both check_versions.py and check_deployments.py --strict report zero problems.
2. Running the sequence a second time in a row performs zero writes and stays green.
3. A stale running MCP server PID fixture is reported visibly as
   "restart these host apps: <names>" without failing check_versions,
   check_deployments --strict, or sync-root verification.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_ROOT_SH = REPO_ROOT / "scripts" / "update_root.sh"
PROPAGATE_PY = REPO_ROOT / "plugins" / "minni" / "skills" / "minni-install" / "scripts" / "propagate.py"
CHECK_VERSIONS_PY = REPO_ROOT / "scripts" / "check_versions.py"
CHECK_DEPLOYMENTS_PY = REPO_ROOT / "scripts" / "check_deployments.py"


@dataclass(frozen=True)
class SyncRootStepSpec:
    step_id: str
    script_marker: str


# Shared canonical step sequence executed by scripts/update_root.sh in steps 4 and 6.
SYNC_ROOT_E2E_STEPS: tuple[SyncRootStepSpec, ...] = (
    SyncRootStepSpec(
        step_id="wire_all",
        script_marker="-m minni.minni_cli wire all --from-repo \"$REPO\" --no-prune --force-reinstall",
    ),
    SyncRootStepSpec(
        step_id="cutover_apply",
        script_marker="-m minni.wire.cutover --repo \"$REPO\" --apply",
    ),
    SyncRootStepSpec(
        step_id="custom_refresh",
        script_marker="-m minni.wire.custom_refresh --wire-report",
    ),
    SyncRootStepSpec(
        step_id="propagate_antigravity",
        script_marker="propagate.py \\\n      --repo \"$REPO\" update-plugin --platform antigravity --existing-only --no-build",
    ),
    SyncRootStepSpec(
        step_id="propagate_cursor",
        script_marker="propagate.py \\\n      --repo \"$REPO\" update-plugin --platform cursor --existing-only --no-build",
    ),
    SyncRootStepSpec(
        step_id="grok_hooks_rules",
        script_marker="hooks = mod.update_grok_hooks(root, existing_only=True)",
    ),
    SyncRootStepSpec(
        step_id="check_versions",
        script_marker="\"$VENV_PY\" scripts/check_versions.py",
    ),
    SyncRootStepSpec(
        step_id="check_deployments_strict",
        script_marker="\"$VENV_PY\" scripts/check_deployments.py --strict",
    ),
)


def extract_update_root_apply_steps(script_text: str | None = None) -> list[str]:
    """Parse scripts/update_root.sh and extract the ordered apply + verify step IDs."""
    text = script_text if script_text is not None else UPDATE_ROOT_SH.read_text(encoding="utf-8")
    # Slice from step 4/6 through step 6b so we inspect the exact redeploy + verify pipeline.
    start = text.find('say "step 4/6:')
    end = text.find('say "step 6b:')
    assert start != -1 and end != -1 and start < end, "Could not locate step 4/6..6b in scripts/update_root.sh"
    pipeline = text[start:end]

    patterns: list[tuple[str, str]] = [
        (
            "wire_all",
            r"running:\s+%s\s+-m\s+minni\.minni_cli\s+wire\s+all\s+--from-repo\s+%s\s+--no-prune\s+--force-reinstall",
        ),
        ("cutover_apply", r"-m\s+minni\.wire\.cutover\s+--repo\s+\"\$REPO\"\s+--apply"),
        ("custom_refresh", r"-m\s+minni\.wire\.custom_refresh\s+--wire-report"),
        (
            "propagate_antigravity",
            r"running:\s+propagate update-plugin --platform antigravity\b.*?update-plugin\s+--platform\s+antigravity\s+--existing-only\s+--no-build",
        ),
        (
            "propagate_cursor",
            r"running:\s+propagate update-plugin --platform cursor\b.*?update-plugin\s+--platform\s+cursor\s+--existing-only\s+--no-build",
        ),
        (
            "grok_hooks_rules",
            r"running:\s+refresh grok hooks/rules from active wire install root\b.*?update_grok_hooks\(root,\s*existing_only=True\)",
        ),
        ("check_versions", r"running:\s+%s\s+scripts/check_versions\.py"),
        ("check_deployments_strict", r"running:\s+%s\s+scripts/check_deployments\.py\s+--strict"),
    ]

    found_positions: list[tuple[int, str]] = []
    for step_id, pat in patterns:
        matches = list(re.finditer(pat, pipeline, flags=re.S))
        assert len(matches) == 1, f"Expected exactly 1 match for {step_id!r} in update_root.sh, found {len(matches)}"
        found_positions.append((matches[0].start(), step_id))

    found_positions.sort(key=lambda item: item[0])
    return [step_id for _pos, step_id in found_positions]


def _extract_grok_refresh_heredoc() -> str:
    """Extract the exact inline Python heredoc for grok hooks/rules from scripts/update_root.sh."""
    text = UPDATE_ROOT_SH.read_text(encoding="utf-8")
    m = re.search(
        r'if ! "\$VENV_PY" - "\$REPO" <<\'PY\'\n(.*?)\nPY\b',
        text,
        flags=re.S,
    )
    assert m is not None, "Could not extract grok refresh Python heredoc from scripts/update_root.sh"
    return m.group(1)


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


from minni.wire.cutover import _stage_repo_payload_to_temp as _ORIG_STAGE_REPO_PAYLOAD_TO_TEMP


def _clean_dev_version(repo_root: Path) -> str:
    from minni.wire.from_repo import _git_sha
    from minni.wire.manifest import read_pyproject_version

    return f"{read_pyproject_version(repo_root)}+git.{_git_sha(repo_root)[:8]}"


def _stage_clean_repo_payload(repo_root: Path, **kwargs: Any):
    """Stage the repo payload using _stage_repo_payload_to_temp with git_dirty=False."""
    from minni.wire.manifest import PayloadManifest, sha256_file

    kwargs.setdefault("version_override", _clean_dev_version(repo_root))
    tmp, manifest = _ORIG_STAGE_REPO_PAYLOAD_TO_TEMP(repo_root, **kwargs)
    bm_path = tmp / "dist" / "build-manifest.json"
    if bm_path.is_file():
        bm = json.loads(bm_path.read_text(encoding="utf-8"))
        if bm.get("git_dirty"):
            bm["git_dirty"] = False
            bm_path.write_text(json.dumps(bm, indent=2) + "\n", encoding="utf-8")
            files = dict(manifest.files)
            files["dist/build-manifest.json"] = sha256_file(bm_path)
            manifest_path = tmp / "payload-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": manifest.schema,
                        "version": manifest.version,
                        "git_sha": manifest.git_sha,
                        "built_at": manifest.built_at,
                        "node_engine": manifest.node_engine,
                        "files": files,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            files["payload-manifest.json"] = sha256_file(manifest_path)
            manifest = PayloadManifest(
                schema=manifest.schema,
                version=manifest.version,
                git_sha=manifest.git_sha,
                built_at=manifest.built_at,
                node_engine=manifest.node_engine,
                files=files,
                path=manifest_path,
            )
    return tmp, manifest


def _populate_container_from_staged(container_dir: Path, staged_dir: Path) -> None:
    container_dir.mkdir(parents=True, exist_ok=True)
    for item in (
        "dist",
        "skills",
        "commands",
        "hooks",
        "frontend",
        ".claude-plugin",
        ".codex-plugin",
        ".cursor-plugin",
        ".kilocode-plugin",
        ".gemini-plugin",
        "package.json",
        "README.md",
    ):
        src = staged_dir / item
        if not src.exists():
            continue
        dest = container_dir / item
        if dest.exists() or dest.is_symlink():
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest)
            else:
                dest.unlink()
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)


def build_operator_shaped_home(home: Path) -> dict[str, Any]:
    """Populate `home` with all 9 operator hosts matching the operator's live shapes."""
    from minni.wire.containers import (
        flip_current_pointer,
        render_antigravity_container_manifests,
        render_cursor_container_manifests,
    )
    from minni.wire.cutover import _install_staged_payload_to_home

    home.mkdir(parents=True, exist_ok=True)
    staged_tmp, manifest = _stage_clean_repo_payload(REPO_ROOT)
    try:
        vdir = _install_staged_payload_to_home(staged_tmp, manifest, home)
        flip_current_pointer(manifest.version, home=home)
        base_semver = manifest.version.split("+", 1)[0]
        plugin_dir = home / ".minni" / "plugin"
        current_dir = plugin_dir / "current"
        cur_server = str(current_dir / "dist" / "server.js")
        sock_path = str(home / ".minni" / "run" / "minnid.sock")

        # 1. claude-code (incl. local marketplace, settings.json, installed_plugins.json, and ~/.claude.json)
        _write_json(
            home / ".claude.json",
            {
                "numStartups": 42,
                "oauthAccount": {"accountUuid": "00000000-0000-0000-0000-000000000000"},
                "mcpServers": {
                    "minni": {
                        "command": "node",
                        "args": [cur_server],
                        "env": {
                            "MINNI_AGENT_ID": "claude-code",
                            "MINNI_VAULT_PATH": str(home / ".minni" / "claudecode-vault"),
                        },
                    }
                },
            },
        )
        mkt_local_root = home / ".claude" / "plugins" / "marketplaces" / "minni-local"
        mkt_local_plugin = mkt_local_root / "plugins" / "minni"
        _populate_container_from_staged(mkt_local_plugin, vdir)
        pjson = mkt_local_plugin / ".claude-plugin" / "plugin.json"
        if pjson.is_file():
            doc = json.loads(pjson.read_text(encoding="utf-8"))
            doc["version"] = base_semver
            doc.pop("mcpServers", None)
            _write_json(pjson, doc)
        (mkt_local_plugin / ".mcp.json").unlink(missing_ok=True)
        _write_json(
            home / ".claude" / "local-marketplaces" / "minni" / ".claude-plugin" / "marketplace.json",
            {
                "name": "minni",
                "owner": {"name": "Minni"},
                "plugins": [
                    {
                        "name": "minni",
                        "source": f"./plugins/minni-{base_semver}",
                        "description": "Minni local plugin",
                        "version": base_semver,
                    }
                ],
            },
        )
        _write_json(
            home / ".claude" / "plugins" / "installed_plugins.json",
            {
                "version": 2,
                "plugins": {
                    "minni@minni": [
                        {
                            "scope": "user",
                            "installPath": str(mkt_local_plugin),
                            "version": base_semver,
                            "installedAt": "2026-10-31T00:00:00Z",
                            "lastUpdated": "2026-10-31T00:00:00Z",
                            "gitCommitSha": manifest.git_sha,
                        }
                    ]
                },
            },
        )

        # 2. claude-desktop
        _write_json(
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
            {
                "preferences": {"coworkScheduledTasksEnabled": True},
                "mcpServers": {
                    "minni": {
                        "command": "node",
                        "args": [cur_server],
                        "env": {
                            "MINNI_AGENT_ID": "claude-code",
                            "MINNI_VAULT_PATH": str(home / ".minni" / "claudecode-vault"),
                        },
                    }
                },
            },
        )

        # 3. codex
        codex_dir = home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        codex_vault = str(home / ".minni" / "codex-vault")
        (codex_dir / "config.toml").write_text(
            'model = "gpt-5.4"\n\n'
            "[mcp_servers.minni]\n"
            'command = "node"\n'
            f'args = ["{cur_server}"]\n'
            "enabled = true\n\n"
            "[mcp_servers.minni.env]\n"
            'MINNI_AGENT_ID = "codex"\n'
            'MINNI_CODEX_AGENT_ID = "codex"\n'
            f'MINNI_VAULT_PATH = "{codex_vault}"\n'
            f'MINNI_CODEX_VAULT_PATH = "{codex_vault}"\n'
            f'MINNI_SOCKET_PATH = "{sock_path}"\n',
            encoding="utf-8",
        )
        codex_hook_js = str(current_dir / "dist" / "codex-hook.js")
        _write_json(
            codex_dir / "hooks.json",
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": f"exec node '{codex_hook_js}' session-start", "timeout": 10}]}
                    ],
                    "PreToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [{"type": "command", "command": f"exec node '{codex_hook_js}' pre-tool-use", "timeout": 5}],
                        }
                    ],
                    "PostToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [{"type": "command", "command": f"exec node '{codex_hook_js}' post-tool-use", "timeout": 5}],
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": f"exec node '{codex_hook_js}' user-prompt-submit", "timeout": 5}
                            ]
                        }
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": f"exec node '{codex_hook_js}' stop", "timeout": 10}]}
                    ],
                }
            },
        )
        codex_skill_dst = codex_dir / "skills" / "astra-minni" / "SKILL.md"
        codex_skill_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vdir / "skills" / "minni" / "SKILL.md", codex_skill_dst)

        # 4. grok-build (with hooks/rules)
        grok_dir = home / ".grok"
        grok_dir.mkdir(parents=True, exist_ok=True)
        grok_vault = str(home / ".minni" / "grok-build-vault")
        (grok_dir / "config.toml").write_text(
            "[mcp_servers.minni]\n"
            'command = "node"\n'
            f'args = ["{cur_server}"]\n'
            "enabled = true\n\n"
            "[mcp_servers.minni.env]\n"
            'MINNI_AGENT_ID = "grok-build"\n'
            f'MINNI_VAULT_PATH = "{grok_vault}"\n'
            f'MINNI_SOCKET_PATH = "{sock_path}"\n',
            encoding="utf-8",
        )
        grok_hook_js = str(current_dir / "dist" / "grok-hook.js")
        grok_hooks_doc = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": f"exec node '{grok_hook_js}' pre-tool-use", "timeout": 5}],
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": f"exec node '{grok_hook_js}' post-tool-use", "timeout": 5}],
                    }
                ],
                "SessionStart": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {"type": "command", "command": f"exec node '{grok_hook_js}' session-start", "timeout": 10}
                        ],
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {"type": "command", "command": f"exec node '{grok_hook_js}' user-prompt-submit", "timeout": 5}
                        ],
                    }
                ],
                "Stop": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": f"exec node '{grok_hook_js}' stop", "timeout": 10}],
                    }
                ],
            }
        }
        _write_json(grok_dir / "hooks" / "minni.json", grok_hooks_doc)
        _write_json(grok_dir / "hooks.json", grok_hooks_doc)
        (grok_dir / "rules").mkdir(parents=True, exist_ok=True)
        (grok_dir / "rules" / "minni.md").write_text(
            f"<!-- minni:grok-rules v{base_semver} -->\n"
            f"# Minni Memory Rules\nRun `node {current_dir}/dist/cli.js`\n",
            encoding="utf-8",
        )

        # 5. antigravity (mcp-env-run wrapper, container, settings.json, all 4 mcp_config views + mirrors, hooks, commands)
        mcp_env_run = home / ".agents" / "bin" / "mcp-env-run"
        mcp_env_run.parent.mkdir(parents=True, exist_ok=True)
        mcp_env_run.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "if len(sys.argv) < 2:\n"
            "    raise SystemExit('usage: mcp-env-run <command> [args...]')\n"
            "os.execvpe(sys.argv[1], sys.argv[1:], os.environ)\n",
            encoding="utf-8",
        )
        mcp_env_run.chmod(0o755)

        ag_container = home / ".agents" / "plugins" / "Minni@Minni"
        _populate_container_from_staged(ag_container, vdir)
        for rel_p, doc in render_antigravity_container_manifests(
            home=home,
            base_semver=base_semver,
        ).items():
            _write_json(ag_container / rel_p, doc)

        gemini_vault = str(home / ".minni" / "gemini-vault")
        ag_server = str(ag_container / "dist" / "server.js")
        ag_hook_container = str(ag_container / "dist" / "gemini-hook.js")
        _write_json(
            home / ".gemini" / "settings.json",
            {
                "security": {"auth": {"selectedType": "oauth-personal"}},
                "mcpServers": {
                    "minni": {
                        "command": "node",
                        "args": [ag_server],
                        "env": {
                            "MINNI_AGENT_ID": "gemini",
                            "MINNI_VAULT_PATH": gemini_vault,
                        },
                    }
                },
            },
        )
        ag_views = [
            home / ".gemini" / "config" / "mcp_config.json",
            home / ".gemini" / "antigravity" / "mcp_config.json",
            home / ".gemini" / "antigravity-ide" / "mcp_config.json",
            home / ".gemini" / "antigravity-cli" / "plugins" / "minni" / "mcp_config.json",
        ]
        ag_mirrors = [
            home / ".agents" / "mcp-servers" / "views" / ".gemini__config__mcp_config.json.json",
            home / ".agents" / "mcp-servers" / "views" / ".gemini__antigravity__mcp_config.json.json",
            home / ".agents" / "mcp-servers" / "views" / ".gemini__antigravity-ide__mcp_config.json.json",
            home / ".agents" / "mcp-servers" / "views" / ".gemini__antigravity-cli__plugins__minni__mcp_config.json.json",
        ]
        view_doc = {
            "mcpServers": {
                "typesafe": {"command": "node", "args": ["/opt/typesafe/server.js"]},
                "minni": {
                    "command": str(mcp_env_run),
                    "args": ["node", ag_server],
                    "env": {
                        "MINNI_AGENT_ID": "gemini",
                        "MINNI_VAULT_PATH": gemini_vault,
                        "MINNI_SOCKET_PATH": sock_path,
                    },
                },
            }
        }
        for vp in [*ag_views, *ag_mirrors]:
            _write_json(vp, view_doc)

        _write_json(
            home / ".gemini" / "config" / "plugins" / "minni" / "hooks.json",
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"name": "minni-session-start", "type": "command", "command": f"exec node '{ag_hook_container}' session-start", "timeout": 10000}
                            ],
                        }
                    ],
                    "BeforeAgent": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"name": "minni-before-agent", "type": "command", "command": f"exec node '{ag_hook_container}' before-agent", "timeout": 5000}
                            ],
                        }
                    ],
                    "BeforeTool": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"name": "minni-before-tool", "type": "command", "command": f"exec node '{ag_hook_container}' before-tool", "timeout": 5000}
                            ],
                        }
                    ],
                    "AfterTool": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"name": "minni-after-tool", "type": "command", "command": f"exec node '{ag_hook_container}' after-tool", "timeout": 5000}
                            ],
                        }
                    ],
                    "SessionEnd": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"name": "minni-session-end", "type": "command", "command": f"exec node '{ag_hook_container}' session-end", "timeout": 10000}
                            ],
                        }
                    ],
                }
            },
        )
        ag_cmds = home / ".gemini" / "commands"
        ag_cmds.mkdir(parents=True, exist_ok=True)
        os.symlink(str(current_dir / "commands"), ag_cmds / "minni")

        # 6. cursor (container, mcp.json, hand-written ~/.local/bin/minni-mcp wrapper, hooks.json, minni-cursor.sh)
        cursor_container = home / ".cursor" / "plugins" / "local" / "minni"
        _populate_container_from_staged(cursor_container, vdir)
        for rel_p, doc in render_cursor_container_manifests(
            home=home,
            base_semver=base_semver,
        ).items():
            _write_json(cursor_container / rel_p, doc)

        wrapper_bin = home / ".local" / "bin" / "minni-mcp"
        wrapper_bin.parent.mkdir(parents=True, exist_ok=True)
        wrapper_bin.write_text(
            "#!/usr/bin/env bash\n"
            "# Hand-maintained Cursor MCP wrapper\n"
            'export MINNI_AGENT_ID="${MINNI_AGENT_ID:-cursor}"\n'
            f'export MINNI_VAULT_PATH="${{MINNI_VAULT_PATH:-{home}/.minni/cursor-vault}}"\n'
            f'exec node "{cursor_container}/dist/server.js" "$@"\n',
            encoding="utf-8",
        )
        wrapper_bin.chmod(0o755)
        cursor_vault = str(home / ".minni" / "cursor-vault")
        _write_json(
            home / ".cursor" / "mcp.json",
            {
                "mcpServers": {
                    "minni": {
                        "command": str(wrapper_bin),
                        "args": [],
                        "env": {
                            "MINNI_AGENT_ID": "cursor",
                            "MINNI_VAULT_PATH": cursor_vault,
                            "MINNI_SOCKET_PATH": sock_path,
                        },
                    }
                }
            },
        )
        cursor_sh = home / ".cursor" / "hooks" / "minni-cursor.sh"
        cursor_sh.parent.mkdir(parents=True, exist_ok=True)
        cursor_sh.write_text(
            "#!/usr/bin/env bash\n"
            f'exec node "{cursor_container}/dist/cursor-hook.js" "$@"\n',
            encoding="utf-8",
        )
        cursor_sh.chmod(0o755)
        _write_json(
            home / ".cursor" / "hooks.json",
            {
                "version": 1,
                "hooks": {
                    "sessionStart": [{"command": "./hooks/minni-cursor.sh session-start", "timeout": 10}],
                    "beforeSubmitPrompt": [{"command": "./hooks/minni-cursor.sh before-submit-prompt", "timeout": 5}],
                    "preToolUse": [{"command": "./hooks/minni-cursor.sh pre-tool-use", "timeout": 5}],
                    "postToolUse": [{"command": "./hooks/minni-cursor.sh post-tool-use", "timeout": 5}],
                    "stop": [{"command": "./hooks/minni-cursor.sh stop", "timeout": 10}],
                },
            },
        )

        # 7. kilocode
        kilo_vault = str(home / ".minni" / "kilocode-vault")
        _write_json(
            home / ".config" / "kilo" / "kilo.json",
            {
                "$schema": "https://kilo.ai/config.json",
                "mcp": {
                    "minni": {
                        "type": "local",
                        "command": ["node", cur_server],
                        "environment": {
                            "MINNI_AGENT_ID": "kilocode",
                            "MINNI_VAULT_PATH": kilo_vault,
                            "MINNI_SOCKET_PATH": sock_path,
                        },
                        "enabled": True,
                    }
                },
            },
        )
        kilo_bridge = home / ".config" / "kilo" / "plugin" / "minni.js"
        kilo_bridge.parent.mkdir(parents=True, exist_ok=True)
        kilo_bridge.write_text(
            "// minni-managed-kilo-bridge\n"
            f'export {{ MinniKiloPlugin }} from "file://{current_dir}/dist/kilocode-hook.js";\n',
            encoding="utf-8",
        )

        # 8. devin (with custom args pointing at current/dist/server.js while wired.json records versioned root)
        devin_vault = str(home / ".minni" / "devin-vault")
        devin_mcp = home / ".config" / "devin" / "mcp_config.json"
        _write_json(
            devin_mcp,
            {
                "mcpServers": {
                    "typesafe": {"command": "/opt/typesafe-mcp", "args": [], "transport": "stdio"},
                    "minni": {
                        "command": "node",
                        "args": [cur_server],
                        "env": {
                            "MINNI_AGENT_ID": "devin",
                            "MINNI_VAULT_PATH": devin_vault,
                            "MINNI_SOCKET_PATH": sock_path,
                        },
                        "transport": "stdio",
                    },
                },
            },
        )
        devin_hook_sh = home / ".config" / "devin" / "scripts" / "minni-hook.sh"
        devin_hook_sh.parent.mkdir(parents=True, exist_ok=True)
        devin_hook_sh.write_text(
            "#!/usr/bin/env bash\n"
            f'exec node "{current_dir}/dist/hook.js" "$@"\n',
            encoding="utf-8",
        )
        devin_hook_sh.chmod(0o755)
        _write_json(
            home / ".config" / "devin" / "config.json",
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {"type": "command", "command": f"{devin_hook_sh} user-prompt-submit", "timeout": 5}
                            ]
                        }
                    ]
                }
            },
        )

        # 9. muse (with custom args in settings.json and .muse/mcp.json)
        muse_vault = str(home / ".minni" / "muse-vault")
        muse_settings = home / ".config" / "muse" / "settings.json"
        muse_mcp = home / ".muse" / "mcp.json"
        for mp in (muse_settings, muse_mcp):
            _write_json(
                mp,
                {
                    "mcpServers": {
                        "minni": {
                            "command": "node",
                            "args": [cur_server],
                            "env": {
                                "MINNI_AGENT_ID": "muse",
                                "MINNI_VAULT_PATH": muse_vault,
                            },
                        }
                    }
                },
            )

        # Seed wired.json including custom registrations for devin and muse so custom_refresh exercises them.
        _write_json(
            plugin_dir / "wired.json",
            {
                "schema": 2,
                "schema_version": 2,
                "active_version": manifest.version,
                "git_commit_sha": manifest.git_sha,
                "updated_at": "2026-10-31T00:00:00Z",
                "hosts": {},
                "wires": [
                    {
                        "platform": "claude-code",
                        "agent": "claude-code",
                        "workspace": "global",
                        "config_path": str(home / ".claude.json"),
                        "install_root": str(vdir),
                        "version": manifest.version,
                        "wired_at": "2026-10-31T00:00:00Z",
                    },
                    {
                        "platform": "codex",
                        "agent": "codex",
                        "workspace": "global",
                        "config_path": str(codex_dir / "config.toml"),
                        "install_root": str(vdir),
                        "version": manifest.version,
                        "wired_at": "2026-10-31T00:00:00Z",
                    },
                    {
                        "platform": "kilocode",
                        "agent": "kilocode",
                        "workspace": "global",
                        "config_path": str(home / ".config" / "kilo" / "kilo.json"),
                        "install_root": str(vdir),
                        "version": manifest.version,
                        "wired_at": "2026-10-31T00:00:00Z",
                    },
                    {
                        "platform": "grok",
                        "agent": "grok",
                        "workspace": "global",
                        "config_path": str(grok_dir / "config.toml"),
                        "install_root": str(vdir),
                        "version": manifest.version,
                        "wired_at": "2026-10-31T00:00:00Z",
                    },
                    {
                        "platform": "devin",
                        "agent": "devin",
                        "workspace": "global",
                        "config_path": str(devin_mcp),
                        "install_root": str(vdir),
                        "version": manifest.version,
                        "wired_at": "2026-10-31T00:00:00Z",
                        "extras": {"custom": True},
                    },
                    {
                        "platform": "muse",
                        "agent": "muse",
                        "workspace": "global",
                        "config_path": str(muse_mcp),
                        "install_root": str(vdir),
                        "version": manifest.version,
                        "wired_at": "2026-10-31T00:00:00Z",
                        "extras": {"custom": True},
                    },
                ],
            },
        )
        return {
            "version": manifest.version,
            "git_sha": manifest.git_sha,
            "base_semver": base_semver,
            "vdir": vdir,
        }
    finally:
        shutil.rmtree(staged_tmp, ignore_errors=True)


def snapshot_home_state(home: Path) -> dict[str, tuple[str, int, str]]:
    """Capture (kind, mtime_ns, sha256_or_linktarget) for every file and symlink under `home`."""
    state: dict[str, tuple[str, int, str]] = {}
    for root, dirs, files in os.walk(home, followlinks=False):
        root_p = Path(root)
        rel_root = root_p.relative_to(home)
        if rel_root.parts and rel_root.parts[:3] == (".minni", "run", "mcp-servers"):
            continue
        for d in sorted(dirs):
            dp = root_p / d
            rel = dp.relative_to(home).as_posix()
            if dp.is_symlink():
                st = dp.lstat()
                state[rel] = ("symlink", st.st_mtime_ns, os.readlink(dp))
        for f in sorted(files):
            fp = root_p / f
            rel = fp.relative_to(home).as_posix()
            if rel.startswith(".minni/run/mcp-servers/"):
                continue
            st = fp.lstat()
            if fp.is_symlink():
                state[rel] = ("symlink", st.st_mtime_ns, os.readlink(fp))
            elif fp.is_file():
                digest = hashlib.sha256(fp.read_bytes()).hexdigest()
                state[rel] = ("file", st.st_mtime_ns, digest)
    return state


def diff_home_snapshots(
    before: dict[str, tuple[str, int, str]],
    after: dict[str, tuple[str, int, str]],
) -> list[str]:
    """Return human-readable descriptions of any added, removed, or modified/rewritten paths."""
    changes: list[str] = []
    all_keys = sorted(set(before) | set(after))
    for k in all_keys:
        if k not in before:
            changes.append(f"added: {k}")
        elif k not in after:
            changes.append(f"removed: {k}")
        elif before[k] != after[k]:
            b_kind, b_mtime, b_val = before[k]
            a_kind, a_mtime, a_val = after[k]
            if b_val != a_val or b_kind != a_kind:
                changes.append(f"content_changed: {k} ({b_kind}:{b_val[:12]} -> {a_kind}:{a_val[:12]})")
            elif b_mtime != a_mtime:
                changes.append(f"rewritten_same_content (mtime changed): {k}")
    return changes


def run_sync_root_sequence(home: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Execute the shared SYNC_ROOT_E2E_STEPS sequence against `home`."""
    # Guard against step-list drift before running.
    assert extract_update_root_apply_steps() == [s.step_id for s in SYNC_ROOT_E2E_STEPS]

    import minni.minni_cli as minni_cli
    import minni.wire.cutover as wire_cutover
    import minni.wire.custom_refresh as wire_custom_refresh
    import minni.wire.flow as wire_flow
    import minni.wire.manifest as wire_manifest

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MINNI_CHECK_VERSIONS_HOME", str(home))
    monkeypatch.setenv("MINNI_CHECK_DEPLOYMENTS_HOME", str(home))
    monkeypatch.setenv("MINNI_CHECK_DEPLOYMENTS_SKIP_REPO", "1")

    cv_mod = _load_script_module("_e2e_check_versions", CHECK_VERSIONS_PY)
    cd_mod = _load_script_module("_e2e_check_deployments", CHECK_DEPLOYMENTS_PY)
    prop_mod = _load_script_module("_e2e_propagate", PROPAGATE_PY)
    grok_heredoc = _extract_grok_refresh_heredoc()

    canonical_ver = cv_mod.read_pyproject_version()
    monkeypatch.setenv("MINNI_CHECK_VERSIONS_INSTALLED_OVERRIDE", canonical_ver)

    # Hermetic isolation: don't run npm run build in the worktree or pgrep host desktop apps.
    monkeypatch.setattr(wire_manifest, "dev_version", _clean_dev_version)
    monkeypatch.setattr(wire_flow, "build_from_repo", _stage_clean_repo_payload)
    monkeypatch.setattr(wire_cutover, "_stage_repo_payload_to_temp", lambda repo_root, **kw: _stage_clean_repo_payload(repo_root, **kw))
    monkeypatch.setattr(wire_cutover, "_detect_running_rewrite_hosts", lambda: set())
    monkeypatch.setattr(cd_mod, "source_dirty", lambda: False)

    step_results: dict[str, Any] = {}
    wire_report_path = home / ".minni" / "run" / "last-wire-report.json"
    wire_report_path.parent.mkdir(parents=True, exist_ok=True)

    for step in SYNC_ROOT_E2E_STEPS:
        sid = step.step_id
        if sid == "wire_all":
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = minni_cli.main(
                    ["wire", "all", "--from-repo", str(REPO_ROOT), "--no-prune", "--force-reinstall"]
                )
            wire_report_path.write_text(buf_out.getvalue(), encoding="utf-8")
            step_results[sid] = {"rc": rc, "stdout": buf_out.getvalue(), "stderr": buf_err.getvalue()}

        elif sid == "cutover_apply":
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = wire_cutover.main(["--repo", str(REPO_ROOT), "--home", str(home), "--apply"])
            step_results[sid] = {"rc": rc, "stdout": buf_out.getvalue(), "stderr": buf_err.getvalue()}

        elif sid == "custom_refresh":
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                target = wire_custom_refresh.wire_report_root(wire_report_path.read_text(encoding="utf-8"))
                cr_res = wire_custom_refresh.refresh_custom_wires(dry_run=False, new_root=target)
                print(json.dumps(cr_res, indent=2))
                rc = int(cr_res["exit_code"])
            step_results[sid] = {"rc": rc, "stdout": buf_out.getvalue(), "stderr": buf_err.getvalue()}

        elif sid == "propagate_antigravity":
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    str(PROPAGATE_PY),
                    "--repo",
                    str(REPO_ROOT),
                    "update-plugin",
                    "--platform",
                    "antigravity",
                    "--existing-only",
                    "--no-build",
                ]
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    try:
                        prop_mod.main()
                        rc = 0
                    except SystemExit as exc:
                        rc = int(exc.code or 0)
            finally:
                sys.argv = old_argv
            step_results[sid] = {"rc": rc, "stdout": buf_out.getvalue(), "stderr": buf_err.getvalue()}

        elif sid == "propagate_cursor":
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    str(PROPAGATE_PY),
                    "--repo",
                    str(REPO_ROOT),
                    "update-plugin",
                    "--platform",
                    "cursor",
                    "--existing-only",
                    "--no-build",
                ]
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    try:
                        prop_mod.main()
                        rc = 0
                    except SystemExit as exc:
                        rc = int(exc.code or 0)
            finally:
                sys.argv = old_argv
            step_results[sid] = {"rc": rc, "stdout": buf_out.getvalue(), "stderr": buf_err.getvalue()}

        elif sid == "grok_hooks_rules":
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            old_argv = sys.argv[:]
            try:
                sys.argv = ["-", str(REPO_ROOT)]
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    try:
                        exec(compile(grok_heredoc, str(UPDATE_ROOT_SH), "exec"), {"__name__": "__main__"})
                        rc = 0
                    except SystemExit as exc:
                        rc = int(exc.code or 0)
            finally:
                sys.argv = old_argv
            step_results[sid] = {"rc": rc, "stdout": buf_out.getvalue(), "stderr": buf_err.getvalue()}

        elif sid == "check_versions":
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                mismatches, notes = cv_mod.check_deployed(canonical_ver)
                rc = cv_mod.main([])
            step_results[sid] = {
                "rc": rc,
                "mismatches": mismatches,
                "notes": notes,
                "stdout": buf_out.getvalue(),
                "stderr": buf_err.getvalue(),
            }

        elif sid == "check_deployments_strict":
            buf_out = io.StringIO()
            buf_err = io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                problems, notes = cd_mod.check_wire_launch_bindings(home)
                rc = cd_mod.main(["--strict"])
            step_results[sid] = {
                "rc": rc,
                "problems": problems,
                "notes": notes,
                "stdout": buf_out.getvalue(),
                "stderr": buf_err.getvalue(),
            }

        else:
            raise AssertionError(f"Unhandled step_id: {sid}")

    wire_report_path.unlink(missing_ok=True)
    return step_results


def test_sync_root_step_sequence_matches_update_root_sh() -> None:
    """Fail if scripts/update_root.sh and SYNC_ROOT_E2E_STEPS drift in step order or flags."""
    script_text = UPDATE_ROOT_SH.read_text(encoding="utf-8")
    for spec in SYNC_ROOT_E2E_STEPS:
        assert spec.script_marker in script_text, (
            f"Step {spec.step_id!r} marker {spec.script_marker!r} missing from scripts/update_root.sh"
        )
    extracted = extract_update_root_apply_steps(script_text)
    expected = [spec.step_id for spec in SYNC_ROOT_E2E_STEPS]
    assert extracted == expected, f"update_root.sh sequence {extracted} != SYNC_ROOT_E2E_STEPS {expected}"


def test_sync_root_e2e_all_hosts_parity_and_idempotency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run 1 converges all 9 operator hosts with zero check_versions/check_deployments problems;
    Run 2 in a row makes zero disk writes and stays green.
    """
    home = tmp_path / "home"
    build_operator_shaped_home(home)

    # Run 1: full sync-root sequence
    run1 = run_sync_root_sequence(home, monkeypatch)
    for sid in (
        "wire_all",
        "cutover_apply",
        "custom_refresh",
        "propagate_antigravity",
        "propagate_cursor",
        "grok_hooks_rules",
    ):
        assert run1[sid]["rc"] == 0, f"Run 1 step {sid} failed: {run1[sid]}"

    cv1 = run1["check_versions"]
    cd1 = run1["check_deployments_strict"]
    assert cv1["mismatches"] == [], f"Run 1 check_versions mismatches: {cv1['mismatches']}\nstderr:\n{cv1['stderr']}"
    assert cv1["rc"] == 0, f"Run 1 check_versions exited {cv1['rc']}:\n{cv1['stdout']}\n{cv1['stderr']}"
    assert cd1["problems"] == [], f"Run 1 check_deployments problems: {cd1['problems']}\nstdout:\n{cd1['stdout']}"
    assert cd1["rc"] == 0, f"Run 1 check_deployments --strict exited {cd1['rc']}:\n{cd1['stdout']}\n{cd1['stderr']}"

    # Snapshot all files and symlinks under HOME after Run 1
    snap_after_run1 = snapshot_home_state(home)

    # Run 2: idempotency — second run in a row must make zero writes and stay green
    run2 = run_sync_root_sequence(home, monkeypatch)
    for sid in (
        "wire_all",
        "cutover_apply",
        "custom_refresh",
        "propagate_antigravity",
        "propagate_cursor",
        "grok_hooks_rules",
        "check_versions",
        "check_deployments_strict",
    ):
        assert run2[sid]["rc"] == 0, f"Run 2 step {sid} failed: {run2[sid]}"
    assert run2["check_versions"]["mismatches"] == []
    assert run2["check_deployments_strict"]["problems"] == []

    snap_after_run2 = snapshot_home_state(home)
    writes_on_run2 = diff_home_snapshots(snap_after_run1, snap_after_run2)
    assert writes_on_run2 == [], f"Second sync-root run performed writes:\n" + "\n".join(writes_on_run2)


def test_sync_root_e2e_stale_running_mcp_server_reports_restart_notice_without_failing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H36: A running MCP server started before the sync is reported visibly as
    'restart these host apps: <names>', not as a verification failure that makes sync-root refuse.
    """
    home = tmp_path / "home"
    build_operator_shaped_home(home)

    # Seed a live PID lease started from an older plugin version before the sync.
    lease_dir = home / ".minni" / "run" / "mcp-servers"
    lease_dir.mkdir(parents=True, exist_ok=True)
    stale_ver_root = home / ".minni" / "plugin" / "0.5.9+git.00000000"
    (stale_ver_root / "dist").mkdir(parents=True, exist_ok=True)
    (stale_ver_root / "dist" / "server.js").write_text("// old server\n", encoding="utf-8")
    _write_json(
        lease_dir / f"{os.getpid()}.json",
        {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "agent_id": "claude-code",
            "version_root": str(stale_ver_root),
            "git_sha": "0000000000000000000000000000000000000000",
        },
    )

    res = run_sync_root_sequence(home, monkeypatch)
    cv = res["check_versions"]
    cd = res["check_deployments_strict"]

    assert cv["mismatches"] == [], f"Stale PID must not cause check_versions mismatches: {cv['mismatches']}"
    assert cv["rc"] == 0, f"Stale PID must not make check_versions fail: {cv['stdout']}\n{cv['stderr']}"
    assert "restart these host apps: claude-code" in cv["stdout"], (
        f"Expected visible restart notice in check_versions stdout, got:\n{cv['stdout']}"
    )

    assert cd["problems"] == [], f"Stale PID must not cause check_deployments problems: {cd['problems']}"
    assert cd["rc"] == 0, f"Stale PID must not make check_deployments --strict fail: {cd['stdout']}\n{cd['stderr']}"
    assert "restart these host apps: claude-code" in cd["stdout"], (
        f"Expected visible restart notice in check_deployments stdout, got:\n{cd['stdout']}"
    )

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402


def test_claude_refresh_updates_matching_native_marketplace(tmp_path):
    plan = propagate.claude_refresh_plan(
        {"minni": {"source": {"source": "directory", "path": str(tmp_path)}}},
        [{"id": "minni@minni", "scope": "user"}, {"id": "minni@minni", "scope": "project"}],
        tmp_path,
    )
    assert plan == [
        ["claude", "plugin", "marketplace", "update", "minni"],
        ["claude", "plugin", "update", "minni@minni", "--scope", "user"],
    ]


def test_claude_refresh_repoints_stale_marketplace_and_installs_missing_user_scope(tmp_path):
    desired = tmp_path / "new"
    desired.mkdir()
    stale = tmp_path / "old"
    stale.mkdir()
    plan = propagate.claude_refresh_plan(
        {"minni": {"source": {"source": "directory", "path": str(stale)}}},
        [{"id": "minni@minni", "scope": "project"}],
        desired,
    )
    assert plan == [
        ["claude", "plugin", "marketplace", "remove", "minni", "--scope", "user"],
        ["claude", "plugin", "marketplace", "add", str(desired.resolve()), "--scope", "user"],
        ["claude", "plugin", "install", "minni@minni", "--scope", "user"],
    ]


def test_claude_repoint_reinstalls_even_if_pre_swap_snapshot_was_installed(tmp_path):
    desired = tmp_path / "new"
    desired.mkdir()
    stale = tmp_path / "old"
    stale.mkdir()
    plan = propagate.claude_refresh_plan(
        {"minni": {"source": {"source": "directory", "path": str(stale)}}},
        [{"id": "minni@minni", "scope": "user"}],
        desired,
    )
    assert plan[-1] == ["claude", "plugin", "install", "minni@minni", "--scope", "user"]


def test_claude_refresh_enables_disabled_user_plugin(tmp_path):
    plan = propagate.claude_refresh_plan(
        {"minni": {"source": {"source": "directory", "path": str(tmp_path)}}},
        [{"id": "minni@minni", "scope": "user", "enabled": False}],
        tmp_path,
    )
    assert plan[-1] == ["claude", "plugin", "enable", "minni@minni", "--scope", "user"]


def test_claude_project_only_marketplace_is_not_treated_as_user_scope(tmp_path):
    # Project/local declarations are intentionally absent from the user
    # settings map consumed by the planner.
    plan = propagate.claude_refresh_plan({}, [], tmp_path)
    assert plan[0] == ["claude", "plugin", "marketplace", "add", str(tmp_path.resolve()), "--scope", "user"]
    assert not any(command[3:5] == ["remove", "minni"] for command in plan)


def test_plugin_source_version_does_not_depend_on_wired_runtime(tmp_path):
    plugin = tmp_path / "plugins/minni"
    plugin.mkdir(parents=True)
    (plugin / "package.json").write_text(json.dumps({"version": "0.3.0"}))
    assert propagate.plugin_source_version(tmp_path) == "0.3.0"

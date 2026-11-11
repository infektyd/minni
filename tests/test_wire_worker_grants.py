"""Wave 1: agy default readonly and worker allowlists use ONLY the 12-tool v1 surface.

Default readonly grants are the 6 read-only v1 MCP tools. Worker grants are the
3 bounded worker write tools (`minni_thread_update`, `minni_thread_scar`, `minni_learn`).
The global `mcp(minni/*)` strip stays, and named worker grants survive that strip.
"""

from __future__ import annotations

import inspect
import json

from minni.wire import writers


V1_TWELVE_TOOLS = {
    "minni_recall",
    "minni_drill",
    "minni_learn",
    "minni_list_candidates",
    "minni_resolve_candidate",
    "minni_status",
    "minni_handoff_send",
    "minni_handoff_inbox",
    "minni_thread_create",
    "minni_thread_status",
    "minni_thread_update",
    "minni_thread_scar",
}
READONLY_SIX = (
    "minni_recall",
    "minni_drill",
    "minni_status",
    "minni_list_candidates",
    "minni_handoff_inbox",
    "minni_thread_status",
)
WORKER_TOOLS = (
    "minni_thread_update",
    "minni_thread_scar",
    "minni_learn",
)
WORKER_GRANTS = tuple(f"mcp(minni/{tool})" for tool in WORKER_TOOLS)
FORBIDDEN_COORDINATOR_OR_RETIRED_TOOLS = (
    "minni_thread_create",
    "minni_resolve_candidate",
    "minni_handoff_send",
    "minni_thread_worker_update",
    "minni_thread_claim",
    "minni_thread_history",
    "minni_thread_diff",
    "minni_thread_revision",
    "minni_thread_replan",
    "minni_thread_assign",
    "minni_thread_activate",
    "minni_thread_deactivate",
    "minni_thread_restore",
    "minni_thread_ready",
    "minni_thread_events",
)


def test_default_readonly_list_uses_only_v1_readonly_tools():
    assert writers.MINNI_READONLY_TOOLS == READONLY_SIX
    assert set(writers.MINNI_READONLY_TOOLS) <= V1_TWELVE_TOOLS
    assert writers.MINNI_READONLY_GRANTS == tuple(
        f"mcp(minni/{tool})" for tool in READONLY_SIX
    )
    for tool in WORKER_TOOLS:
        assert tool not in writers.MINNI_READONLY_TOOLS
        assert f"mcp(minni/{tool})" not in writers.MINNI_READONLY_GRANTS
    assert writers.MINNI_WILDCARD_GRANT not in writers.MINNI_READONLY_GRANTS
    assert writers.MINNI_WILDCARD_GRANT == "mcp(minni/*)"


def test_worker_grants_use_only_v1_worker_tools():
    assert writers.MINNI_WORKER_TOOLS == WORKER_TOOLS
    assert set(writers.MINNI_WORKER_TOOLS) <= V1_TWELVE_TOOLS
    assert writers.MINNI_WORKER_GRANTS == WORKER_GRANTS
    assert writers.MINNI_WORKER_GRANTS != writers.MINNI_READONLY_GRANTS
    assert not (set(writers.MINNI_WORKER_TOOLS) & set(writers.MINNI_READONLY_TOOLS))
    for name in FORBIDDEN_COORDINATOR_OR_RETIRED_TOOLS:
        assert name not in writers.MINNI_WORKER_TOOLS
        assert f"mcp(minni/{name})" not in writers.MINNI_WORKER_GRANTS


def test_default_grant_path_still_strips_wildcard_and_never_writes_worker(tmp_path):
    path = tmp_path / "default.json"
    path.write_text(
        json.dumps({"permissions": {"allow": ["command(ls)", "mcp(minni/*)"]}}),
        encoding="utf-8",
    )
    assert writers.ensure_permission_grant(path, ["permissions", "allow"]) is True
    allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert "mcp(minni/*)" not in allow
    assert "command(ls)" in allow
    for grant in writers.MINNI_READONLY_GRANTS:
        assert grant in allow
    for grant in WORKER_GRANTS:
        assert grant not in allow
    assert "mcp(minni/minni_thread_create)" not in allow


def test_worker_path_writes_only_worker_grant_and_keeps_wildcard_strip(tmp_path):
    path = tmp_path / "worker.json"
    path.write_text(
        json.dumps({"permissions": {"allow": ["command(ls)", "mcp(minni/*)"]}}),
        encoding="utf-8",
    )
    assert writers.ensure_worker_permission_grant(path, ["permissions", "allow"]) is True
    allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == ["command(ls)", *WORKER_GRANTS]
    assert "mcp(minni/*)" not in allow
    for grant in writers.MINNI_READONLY_GRANTS:
        assert grant not in allow


def test_worker_strip_does_not_wipe_named_worker_grant(tmp_path):
    path = tmp_path / "keep-worker.json"
    path.write_text(
        json.dumps({"permissions": {"allow": [WORKER_GRANTS[0], "mcp(minni/*)"]}}),
        encoding="utf-8",
    )
    assert writers.ensure_worker_permission_grant(path, ["permissions", "allow"]) is True
    allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
    for grant in WORKER_GRANTS:
        assert allow.count(grant) == 1
    assert "mcp(minni/*)" not in allow
    before = path.read_text(encoding="utf-8")
    assert writers.ensure_worker_permission_grant(path, ["permissions", "allow"]) is True
    assert path.read_text(encoding="utf-8") == before


def test_antigravity_default_path_does_not_call_worker_writer():
    source = inspect.getsource(writers.update_antigravity_config)
    assert "ensure_permission_grant" in source
    assert "ensure_worker_permission_grant" not in source
    assert "MINNI_WORKER_GRANTS" not in source
    default_source = inspect.getsource(writers.ensure_permission_grant)
    assert "MINNI_READONLY_GRANTS" in default_source
    assert "MINNI_WORKER_GRANTS" not in default_source
    worker_source = inspect.getsource(writers.ensure_worker_permission_grant)
    assert "MINNI_WORKER_GRANTS" in worker_source
    assert "MINNI_READONLY_GRANTS" not in worker_source

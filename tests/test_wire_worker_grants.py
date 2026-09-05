"""Wave 5: agy worker allowlist is a separate grant path, not the default nine.

Default readonly grants stay CANNOT. Worker grants are exactly
mcp(minni/minni_thread_worker_update). The global mcp(minni/*) strip stays.
The named worker grant survives that strip on the worker path.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from minni.wire import writers


READONLY_NINE = (
    "minni_recall",
    "minni_drill",
    "minni_status",
    "minni_audit_tail",
    "minni_audit_report",
    "minni_route",
    "minni_list_pending_handoffs",
    "minni_ping_agent_inbox",
    "minni_ping_agent_status",
)
WORKER_TOOL = "minni_thread_worker_update"
WORKER_GRANT = f"mcp(minni/{WORKER_TOOL})"
FORBIDDEN_THREAD_TOOLS = (
    "minni_thread_claim",
    "minni_thread_update",
    "minni_thread_status",
    "minni_thread_history",
    "minni_thread_diff",
    "minni_thread_revision",
    "minni_thread_create",
    "minni_thread_replan",
    "minni_thread_assign",
    "minni_thread_activate",
    "minni_thread_deactivate",
    "minni_thread_restore",
    "minni_thread_scar",
    "minni_thread_ready",
    "minni_thread_events",
)


def test_default_readonly_list_is_still_the_nine():
    assert writers.MINNI_READONLY_TOOLS == READONLY_NINE
    assert writers.MINNI_READONLY_GRANTS == tuple(
        f"mcp(minni/{tool})" for tool in READONLY_NINE
    )
    assert WORKER_TOOL not in writers.MINNI_READONLY_TOOLS
    assert WORKER_GRANT not in writers.MINNI_READONLY_GRANTS
    assert writers.MINNI_WILDCARD_GRANT not in writers.MINNI_READONLY_GRANTS
    assert writers.MINNI_WILDCARD_GRANT == "mcp(minni/*)"


def test_worker_grants_are_exactly_worker_update():
    assert writers.MINNI_WORKER_TOOLS == (WORKER_TOOL,)
    assert writers.MINNI_WORKER_GRANTS == (WORKER_GRANT,)
    assert writers.MINNI_WORKER_GRANTS != writers.MINNI_READONLY_GRANTS
    assert not (set(writers.MINNI_WORKER_TOOLS) & set(writers.MINNI_READONLY_TOOLS))
    for name in FORBIDDEN_THREAD_TOOLS:
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
    assert WORKER_GRANT not in allow
    assert not any(str(g).startswith("mcp(minni/minni_thread_") for g in allow)


def test_worker_path_writes_only_worker_grant_and_keeps_wildcard_strip(tmp_path):
    path = tmp_path / "worker.json"
    path.write_text(
        json.dumps({"permissions": {"allow": ["command(ls)", "mcp(minni/*)"]}}),
        encoding="utf-8",
    )
    assert writers.ensure_worker_permission_grant(path, ["permissions", "allow"]) is True
    allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow == ["command(ls)", WORKER_GRANT]
    assert "mcp(minni/*)" not in allow
    for grant in writers.MINNI_READONLY_GRANTS:
        assert grant not in allow


def test_worker_strip_does_not_wipe_named_worker_grant(tmp_path):
    path = tmp_path / "keep-worker.json"
    path.write_text(
        json.dumps({"permissions": {"allow": [WORKER_GRANT, "mcp(minni/*)"]}}),
        encoding="utf-8",
    )
    assert writers.ensure_worker_permission_grant(path, ["permissions", "allow"]) is True
    allow = json.loads(path.read_text(encoding="utf-8"))["permissions"]["allow"]
    assert allow.count(WORKER_GRANT) == 1
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

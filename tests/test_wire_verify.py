"""Real Node probes validate initialize, bounded reads, and process cleanup."""
import json
import os
import shutil
import signal
import time

import pytest

from minni.wire.verify import mcp_handshake


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="Node required for real MCP probes")

REPLY = {"jsonrpc": "2.0", "id": 1, "result": {
    "protocolVersion": "2024-11-05", "capabilities": {},
    "serverInfo": {"name": "fixture", "version": "1"},
}}


def server(tmp_path, behavior):
    script = tmp_path / "server.cjs"
    pidfile = tmp_path / "pid"
    script.write_text(
        "const fs=require('node:fs');"
        f"fs.writeFileSync({json.dumps(str(pidfile))},String(process.pid));"
        "process.stdin.once('data',data=>{"
        "const req=JSON.parse(data);if(req.method!=='initialize')process.exit(9);"
        + behavior + "});setInterval(()=>{},1000);",
        encoding="utf-8",
    )
    return script, pidfile


def assert_reaped(pidfile):
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("behavior", ["", "process.stdout.write('{');"])
def test_silent_or_partial_server_cannot_exceed_read_budget(tmp_path, behavior):
    script, pidfile = server(tmp_path, behavior)
    started = time.monotonic()
    assert mcp_handshake(script, timeout=.3) is False
    assert time.monotonic() - started < 2
    assert_reaped(pidfile)


@pytest.mark.parametrize("reply", [
    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": "failure"}},
    {**REPLY, "id": 2},
    {**REPLY, "id": True},
    {**REPLY, "jsonrpc": "1.0"},
    {**REPLY, "result": {}},
    {**REPLY, "result": {**REPLY["result"], "protocolVersion": "wrong"}},
    {**REPLY, "result": {**REPLY["result"], "capabilities": []}},
    {**REPLY, "result": {**REPLY["result"], "serverInfo": {"name": "fixture"}}},
    {**REPLY, "result": {**REPLY["result"], "serverInfo": {"name": "", "version": "1"}}},
    [],
])
def test_error_or_invalid_initialize_is_not_success_and_reaps_child(tmp_path, reply):
    wire = json.dumps(reply) + "\n"
    script, pidfile = server(tmp_path, f"setTimeout(()=>process.stdout.write({json.dumps(wire)}),25);")
    assert mcp_handshake(script, timeout=2) is False
    assert_reaped(pidfile)


def test_valid_initialize_accepts_notification_and_partial_frames_then_reaps(tmp_path):
    wire = json.dumps(REPLY) + "\n"
    first, rest = wire[:20], wire[20:]
    notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}) + "\n"
    script, pidfile = server(tmp_path,
        f"process.stdout.write({json.dumps(notification + first)});"
        f"setTimeout(()=>process.stdout.write({json.dumps(rest)}),30);"
    )
    assert mcp_handshake(script, timeout=2) is True
    assert_reaped(pidfile)


@pytest.mark.parametrize("behavior", ["process.exit(0);", "process.stdout.write('invalid json\\n');", "process.stdout.write('x'.repeat(1024*1024+1));"])
def test_eof_malformed_and_oversize_frames_fail_and_reap(tmp_path, behavior):
    script, pidfile = server(tmp_path, behavior)
    assert mcp_handshake(script, timeout=2) is False
    assert_reaped(pidfile)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_budget_does_not_start_process(tmp_path, timeout):
    script, pidfile = server(tmp_path, "")
    assert mcp_handshake(script, timeout=timeout) is False
    assert not pidfile.exists()


@pytest.mark.skipif(os.name != "posix", reason="probe process-group cleanup is POSIX")
@pytest.mark.parametrize("success", [True, False])
def test_probe_reaps_leader_and_stops_its_real_descendant(tmp_path, success):
    child_pidfile = tmp_path / "child-pid"
    child_code = "process.stdout.write('ready');setInterval(()=>{},1000);"
    reply = f"process.stdout.write({json.dumps(json.dumps(REPLY) + chr(10))});" if success else ""
    behavior = (
        "const {spawn}=require('node:child_process');"
        f"const child=spawn(process.execPath,['-e',{json.dumps(child_code)}],{{stdio:['ignore','pipe','ignore']}});"
        f"fs.writeFileSync({json.dumps(str(child_pidfile))},String(child.pid));"
        f"child.stdout.once('data',()=>{{{reply}}});"
    )
    script, pidfile = server(tmp_path, behavior)
    try:
        assert mcp_handshake(script, timeout=.5) is success
        assert_reaped(pidfile)
        child_pid = int(child_pidfile.read_text())
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.01)
        else:
            pytest.fail("probe descendant remains after process-group cleanup")
    finally:
        # A regression must not leave the deliberately persistent fixture alive.
        for path in (child_pidfile, pidfile):
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402


def test_grok_hooks_are_native_portable_and_idempotent(tmp_path):
    install = tmp_path / "Agent Plugins" / "minni@minni"
    vault = tmp_path / ".minni/grok-build-vault"
    socket = tmp_path / ".minni/run/minnid.sock"
    propagate.update_grok_hooks(
        install, "grok-build", vault, socket, tmp_path / "Repo With Spaces", home=tmp_path,
    )
    target = tmp_path / ".grok/hooks/minni.json"
    first = target.read_text()
    data = json.loads(first)
    assert set(data["hooks"]) == set(propagate.GROK_HOOK_EVENTS)
    for event, groups in data["hooks"].items():
        hook = groups[0]["hooks"][0]
        command = hook["command"]
        assert "grok-hook.js" in command
        assert "dist/hook.js" not in command
        assert "MINNI_CLAUDECODE_" not in command
        assert "MINNI_GROK_AGENT_ID=grok-build" in command
        assert "MINNI_GROK_VAULT_PATH=" in command
        assert "MINNI_GROK_WORKSPACE_ID='workspace-repo with spaces'" in command
        assert "MINNI_SOCKET_PATH=" in command
        assert "'" in command
        assert hook["timeout"] in (20, 30)
        assert command.endswith(event)
    propagate.update_grok_hooks(
        install, "grok-build", vault, socket, tmp_path / "Repo With Spaces", home=tmp_path,
    )
    assert target.read_text() == first


def test_grok_writer_owns_only_minni_file(tmp_path):
    hooks = tmp_path / ".grok/hooks"
    hooks.mkdir(parents=True)
    other = hooks / "other.json"
    other.write_text('{"hooks":{"Stop":[]}}')
    propagate.update_grok_hooks(
        tmp_path / "plugin", "grok-alt", tmp_path / "vault", tmp_path / "socket",
        Path("workspace-unknown"), home=tmp_path,
    )
    assert other.read_text() == '{"hooks":{"Stop":[]}}'
    command = json.loads((hooks / "minni.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "MINNI_GROK_AGENT_ID=grok-alt" in command
    assert "MINNI_GROK_WORKSPACE_ID=workspace-unknown" in command

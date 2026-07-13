from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import propagate  # noqa: E402


def test_kilo_native_plugin_is_stamped_and_uses_real_hook_api(tmp_path):
    install = tmp_path / "Kilo Plugins/minni"
    (install / "kilo").mkdir(parents=True)
    template = Path(__file__).resolve().parents[3] / "kilo/minni-plugin.js"
    shutil.copy2(template, install / "kilo/minni-plugin.js")
    rendered = propagate.render_kilo_plugin(
        install, "kilocode", tmp_path / "Kilo Vault", tmp_path / "run/minnid.sock",
        tmp_path / "Repo With Spaces",
    )
    assert "__MINNI_KILO_" not in rendered
    assert str(install / "dist/kilocode-hook.js") in rendered
    assert '"MINNI_KILOCODE_AGENT_ID": "kilocode"' in rendered
    assert "Kilo Vault" in rendered
    for hook in (
        '"chat.message"', '"experimental.chat.system.transform"',
        '"tool.execute.before"', '"experimental.session.compacting"', "event:",
    ):
        assert hook in rendered
    assert 'spawn("node"' in rendered
    assert "export default MinniPlugin;" in rendered
    assert 'export default { id: "minni"' not in rendered


def test_kilo_installer_writes_global_native_plugin(tmp_path):
    install = tmp_path / "install"
    (install / "kilo").mkdir(parents=True)
    template = Path(__file__).resolve().parents[3] / "kilo/minni-plugin.js"
    shutil.copy2(template, install / "kilo/minni-plugin.js")
    target = propagate.update_kilo_plugin(
        install, "kilocode", tmp_path / "vault", tmp_path / "socket",
        Path("workspace-unknown"), home=tmp_path,
    )
    assert target == tmp_path / ".config/kilo/plugin/minni.js"
    assert target.exists() and not target.is_symlink()


def test_kilo_config_removes_only_exact_legacy_server(tmp_path, monkeypatch):
    config = tmp_path / ".config/kilo/kilo.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcp": {
        "sovereign-memory": {"enabled": True},
        "sovereign-memory-tools": {"enabled": True},
        "other": {"enabled": True},
    }}))
    monkeypatch.setattr(propagate.Path, "expanduser", lambda self: config if str(self) == "~/.config/kilo/kilo.json" else self)
    propagate.update_kilo_config(
        tmp_path / "plugin/dist/server.js", "kilocode", tmp_path / "vault",
        tmp_path / "socket", tmp_path / "repo", remove_legacy=True,
    )
    data = json.loads(config.read_text())["mcp"]
    assert "sovereign-memory" not in data
    assert data["sovereign-memory-tools"] == {"enabled": True}
    assert data["other"] == {"enabled": True}
    assert data["minni"]["enabled"] is True
    assert data["minni"]["environment"]["MINNI_AGENT_ID"] == "kilocode"
    assert "env" not in data["minni"]


def test_kilo_native_callbacks_order_lifecycle_and_fail_open(tmp_path):
    install = tmp_path / "install"
    (install / "kilo").mkdir(parents=True)
    (install / "dist").mkdir()
    shutil.copy2(Path(__file__).resolve().parents[3] / "kilo/minni-plugin.js", install / "kilo/minni-plugin.js")
    events = tmp_path / "events.jsonl"
    fake_hook = install / "dist/kilocode-hook.js"
    fake_hook.write_text(
        """import fs from 'node:fs';
const chunks=[]; for await (const chunk of process.stdin) chunks.push(chunk);
const payload=JSON.parse(Buffer.concat(chunks).toString() || '{}');
fs.appendFileSync(process.env.EVENTS_FILE, JSON.stringify({event:process.argv[2],payload})+'\\n');
const event=process.argv[2];
if (event==='PreToolUse' && payload.tool_name==='deny') console.log(JSON.stringify({hookSpecificOutput:{permissionDecision:'deny',permissionDecisionReason:'blocked'}}));
else console.log(JSON.stringify({continue:true,hookSpecificOutput:{additionalContext:event+'-context'}}));
"""
    )
    rendered = propagate.render_kilo_plugin(
        install, "kilocode", tmp_path / "vault", tmp_path / "socket", "workspace-test",
    ).replace('"MINNI_SOCKET_PATH":', f'"EVENTS_FILE": {json.dumps(str(events))}, "MINNI_SOCKET_PATH":')
    plugin = tmp_path / "minni.js"
    plugin.write_text(rendered)
    scenario = f"""
import plugin from {json.dumps(plugin.as_uri())};
const hooks = await plugin({{directory:'/repo'}});
const message = {{sessionID:'s1'}};
const parts = {{parts:[{{type:'text',text:'hello'}}]}};
await hooks['chat.message'](message, parts);
const system={{system:[]}}; await hooks['experimental.chat.system.transform']({{sessionID:'s1',model:{{}}}}, system);
await hooks.event({{event:{{type:'session.idle',properties:{{sessionID:'s1'}}}}}});
await hooks['chat.message'](message, parts);
await hooks.event({{event:{{type:'session.deleted',properties:{{info:{{id:'s1'}}}}}}}});
await hooks['chat.message'](message, parts);
let denied=false; try {{ await hooks['tool.execute.before']({{sessionID:'s1',tool:'deny',callID:'c'}},{{args:{{}}}}); }} catch {{ denied=true; }}
if (!denied || system.system.join('|') !== 'SessionStart-context|UserPromptSubmit-context') process.exit(2);
"""
    subprocess.run(["node", "--input-type=module", "-e", scenario], check=True, timeout=30)
    recorded = [json.loads(line)["event"] for line in events.read_text().splitlines()]
    assert recorded == [
        "SessionStart", "UserPromptSubmit", "Stop", "UserPromptSubmit",
        "SessionStart", "UserPromptSubmit", "PreToolUse",
    ]

    fake_hook.unlink()
    fail_open = f"""
import plugin from {json.dumps(plugin.as_uri())};
const hooks = await plugin({{directory:'/repo'}});
await hooks['chat.message']({{sessionID:'missing'}},{{parts:[{{type:'text',text:'hello'}}]}});
await hooks['tool.execute.before']({{sessionID:'missing',tool:'read',callID:'c'}},{{args:{{}}}});
await hooks['experimental.session.compacting']({{sessionID:'missing'}},{{context:[]}});
await hooks.event({{event:{{type:'session.idle',properties:{{sessionID:'missing'}}}}}});
"""
    subprocess.run(["node", "--input-type=module", "-e", fail_open], check=True, timeout=30)

"""Exercise the real console from shipped payloads with no source/dependency fallback."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins/minni"


@pytest.fixture(scope="module")
def built_payload():
    if not shutil.which("node") or not (PLUGIN / "node_modules/esbuild").exists():
        pytest.skip("requires installed Node build dependencies")
    from minni.wire.from_repo import build_from_repo

    payload, manifest = build_from_repo(REPO)
    try:
        yield payload, manifest
    finally:
        shutil.rmtree(payload)


@pytest.mark.parametrize("kind", ["wire", "wheel-stage"])
def test_installed_console_serves_built_assets_and_keeps_api_auth(tmp_path, monkeypatch, built_payload, kind):
    payload, manifest = built_payload
    if kind == "wheel-stage":
        spec = importlib.util.spec_from_file_location("console_stage", REPO / "scripts/stage_payload.py")
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        monkeypatch.setattr(stage, "PAYLOAD_ROOT", tmp_path / "stage")
        stage.copy_payload_tree("0.5.0")
        payload = stage.PAYLOAD_ROOT
    else:
        assert "frontend/index.html" in manifest.files
        assert "frontend/app.js" in manifest.files
        assert "frontend/styles.css" in manifest.files
    assert not (payload / "node_modules").exists()
    assert json.loads((payload / "package.json").read_text())["type"] == "module"
    script = rf"""
        import assert from 'node:assert/strict';
        import {{pathToFileURL}} from 'node:url';
        const {{createUiServer}} = await import(pathToFileURL({json.dumps(str(payload / 'dist/ui-server.js'))}));
        const app = createUiServer({{port:0}});
        await app.start();
        const base = `http://127.0.0.1:${{app.server.address().port}}`;
        try {{
          const response = await fetch(base+'/');
          assert.equal(response.status,200);
          const html = await response.text();
          assert.match(html,/id="root"/);
          const assets=[...html.matchAll(/(?:src|href)="(\/(?:app.js|styles.css))"/g)].map(m=>m[1]);
          assert.equal(assets.length,2,'the built JS and CSS must be referenced');
          for (const asset of assets) {{
            const res=await fetch(base+asset);assert.equal(res.status,200,asset);
            assert.ok((await res.text()).length>100,asset);
          }}
          assert.equal((await fetch(base+'/api/status')).status,403);
          assert.equal((await fetch(base+'/api/health')).status,200);
          console.log('installed console assets and authentication passed');
        }} finally {{await app.close();}}
    """
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.run(
        [shutil.which("node"), "--input-type=module", "-e", script],
        cwd=tmp_path, env={"PATH": os.environ["PATH"], "HOME": str(home),
                           "MINNI_HOME": str(home / ".minni"),
                           "MINNI_AGENT_ID": "codex", "MINNI_CONSOLE_TOKEN": "test-console-token"},
        capture_output=True, text=True, timeout=25,
    )
    assert proc.returncode == 0, proc.stderr
    assert "installed console assets and authentication passed" in proc.stdout

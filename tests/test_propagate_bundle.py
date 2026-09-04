"""A propagated MCP entrypoint must launch without installed node_modules."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugins/minni"
spec = importlib.util.spec_from_file_location(
    "propagate_bundle_test", PLUGIN / "skills/minni-install/scripts/propagate.py",
)
assert spec and spec.loader
propagate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(propagate)


@pytest.mark.parametrize("no_build", [False, True])
@pytest.mark.parametrize("copy_backend", ["rsync", "fallback"])
def test_propagated_entrypoint_runs_without_dependencies(
    tmp_path, monkeypatch, no_build, copy_backend,
):
    if not shutil.which("node") or not shutil.which("npm"):
        pytest.skip("requires Node/npm")
    esbuild = Path(os.environ.get("MINNI_TEST_ESBUILD_PATH", PLUGIN / "node_modules/esbuild"))
    if not esbuild.is_dir():
        pytest.skip("run npm ci in plugins/minni for the real bundler smoke")
    if copy_backend == "rsync" and not shutil.which("rsync"):
        pytest.skip("rsync unavailable; fallback covered separately")

    repo = tmp_path / "repo"
    source = repo / "plugins/minni"
    (source / "dist").mkdir(parents=True)
    (source / "scripts").mkdir()
    dependency = source / "node_modules/fixture-dependency"
    dependency.mkdir(parents=True)
    (source / "node_modules/esbuild").symlink_to(esbuild.resolve(), target_is_directory=True)
    (dependency / "package.json").write_text(json.dumps({
        "name": "fixture-dependency", "type": "module", "exports": "./index.js",
    }))
    (dependency / "index.js").write_text('export const result = "dependency-loaded";\n')
    entrypoint = 'import { result } from "fixture-dependency"; console.log(result);\n'
    (source / "dist/server.js").write_text(entrypoint)
    (source / "build.cjs").write_text(
        f'require("node:fs").writeFileSync("dist/server.js", {json.dumps(entrypoint)});\n'
    )
    (source / "package.json").write_text(json.dumps({
        "type": "module", "scripts": {"build": "node build.cjs"},
    }))
    shutil.copy2(PLUGIN / "scripts/bundle_server.mjs", source / "scripts/bundle_server.mjs")
    install = tmp_path / "installed"
    monkeypatch.setattr(propagate, "bootstrap_vault", lambda _args: 0)
    monkeypatch.setattr(propagate, "vault_for", lambda _agent: tmp_path / "vault")
    monkeypatch.setattr(propagate, "native_afm_env", lambda _repo: {})
    if copy_backend == "fallback":
        monkeypatch.setattr(propagate.shutil, "which", lambda _name: None)
    args = argparse.Namespace(
        repo=str(repo), workspace=str(repo), no_build=no_build,
        install_root=str(install), agent="audit-test", socket=str(tmp_path / "minnid.sock"),
    )
    result = propagate.update_one_plugin("generic", args)
    assert not (install / "node_modules").exists()
    # Execute the path actually handed to the host, outside the source tree.
    env = dict(os.environ)
    env.pop("NODE_PATH", None)
    launched = subprocess.run(
        ["node", result["server"]], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout.strip() == "dependency-loaded"

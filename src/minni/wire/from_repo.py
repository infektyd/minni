"""--from-repo build path (§4.5)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from minni.wire.manifest import PayloadManifest, dev_version, sha256_file, utc_now_iso

PAYLOAD_ITEMS = (
    "dist", ".claude-plugin", ".codex-plugin", ".gemini-plugin", ".kilocode-plugin",
    ".mcp.json", "commands", "hooks", "skills", "README.md",
)
MANIFEST_STAMP_PATHS = (
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".kilocode-plugin/plugin.json",
    ".gemini-plugin/gemini-extension.json",
)
JUNK = {".DS_Store", "__pycache__", ".pytest_cache"}


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True)


def build_from_repo(repo_root: Path) -> tuple[Path, PayloadManifest]:
    plugin_dir = repo_root / "plugins" / "minni"
    if not plugin_dir.is_dir():
        raise ValueError(f"no plugins/minni under {repo_root}")

    _run(["npm", "run", "build:server"], cwd=plugin_dir)
    bundle_script = plugin_dir / "scripts" / "bundle_server.mjs"
    if bundle_script.exists():
        _run(["node", str(bundle_script)], cwd=plugin_dir)

    version = dev_version(repo_root)
    tmp = Path(tempfile.mkdtemp(prefix="minni-from-repo-"))
    try:
        for item in PAYLOAD_ITEMS:
            src = plugin_dir / item
            if not src.exists():
                raise ValueError(f"missing {src}")
            dest = tmp / item
            if src.is_dir():
                shutil.copytree(
                    src, dest,
                    ignore=shutil.ignore_patterns(*JUNK),
                )
            else:
                shutil.copy2(src, dest)

        import json

        for rel in MANIFEST_STAMP_PATHS:
            path = tmp / rel
            data = json.loads(path.read_text(encoding="utf-8"))
            data["version"] = version
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        files: dict[str, str] = {}
        for path in sorted(tmp.rglob("*")):
            if path.is_file() and not any(p in JUNK for p in path.parts):
                rel = path.relative_to(tmp).as_posix()
                files[rel] = sha256_file(path)

        manifest = PayloadManifest(
            schema=1,
            version=version,
            git_sha=_git_sha(repo_root),
            built_at=utc_now_iso(),
            node_engine=">=20",
            files=files,
            path=tmp / "payload-manifest.json",
        )
        manifest_path = tmp / "payload-manifest.json"
        manifest_path.write_text(
            json.dumps({
                "schema": manifest.schema,
                "version": manifest.version,
                "git_sha": manifest.git_sha,
                "built_at": manifest.built_at,
                "node_engine": manifest.node_engine,
                "files": manifest.files,
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        files["payload-manifest.json"] = sha256_file(manifest_path)
        manifest = PayloadManifest(
            schema=1,
            version=version,
            git_sha=manifest.git_sha,
            built_at=manifest.built_at,
            node_engine=">=20",
            files=files,
            path=manifest_path,
        )
        return tmp, manifest
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _git_sha(repo_root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root, text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def self_check_manifest(manifest: PayloadManifest) -> None:
    if manifest.schema != 1:
        raise ValueError("payload manifest schema mismatch")
    if not manifest.version or not manifest.files:
        raise ValueError("payload manifest missing version or files")
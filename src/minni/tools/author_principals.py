#!/usr/bin/env python3
"""Render and optionally write Minni per-agent principal files.

Dry-run is the default. Use --apply when the operator is ready to write
~/.minni/principals/<agent_id>.json with 0600 permissions.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

AGENT_VAULT_DIRS: dict[str, str] = {
    "claude-code": "claudecode-vault",
    "codex": "codex-vault",
    "cursor": "cursor-vault",
    "gemini": "gemini-vault",
    "grok-build": "grok-build-vault",
    "kilocode": "kilocode-vault",
}

TEMPLATE_TOKEN = "__MINNI_HOME__"
DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "principal_templates"


def _default_minni_home() -> Path:
    return Path(os.environ.get("MINNI_HOME", "~/.minni")).expanduser()


def _replace_tokens(value: Any, *, minni_home: Path) -> Any:
    if isinstance(value, str):
        return value.replace(TEMPLATE_TOKEN, str(minni_home))
    if isinstance(value, list):
        return [_replace_tokens(item, minni_home=minni_home) for item in value]
    if isinstance(value, dict):
        return {key: _replace_tokens(item, minni_home=minni_home) for key, item in value.items()}
    return value


def _load_template(agent_id: str, *, template_dir: Path = DEFAULT_TEMPLATE_DIR) -> dict[str, Any]:
    if agent_id not in AGENT_VAULT_DIRS:
        raise ValueError(f"unknown agent template {agent_id!r}")
    path = template_dir / f"{agent_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"principal template {path} must contain a JSON object")
    return raw


def render_principal(
    agent_id: str,
    *,
    minni_home: Path | str | None = None,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
    as_json: bool = False,
) -> dict[str, Any] | str:
    home = Path(minni_home).expanduser() if minni_home is not None else _default_minni_home()
    rendered = _replace_tokens(_load_template(agent_id, template_dir=template_dir), minni_home=home)
    if rendered.get("agent_id") != agent_id:
        raise ValueError(
            f"principal template {agent_id}.json declares agent_id={rendered.get('agent_id')!r}"
        )
    if as_json:
        return json.dumps(rendered, indent=2) + "\n"
    return rendered


def _write_0600(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    finally:
        if path.exists():
            os.chmod(path, 0o600)


def author_principals(
    *,
    minni_home: Path | str | None = None,
    principals_dir: Path | str | None = None,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
    apply: bool = False,
) -> list[dict[str, Any]]:
    home = Path(minni_home).expanduser() if minni_home is not None else _default_minni_home()
    target_dir = (
        Path(principals_dir).expanduser()
        if principals_dir is not None
        else home / "principals"
    )
    results: list[dict[str, Any]] = []
    for agent_id in AGENT_VAULT_DIRS:
        content = render_principal(
            agent_id,
            minni_home=home,
            template_dir=template_dir,
            as_json=True,
        )
        assert isinstance(content, str)
        target = target_dir / f"{agent_id}.json"
        if apply:
            _write_0600(target, content)
        results.append(
            {
                "agent_id": agent_id,
                "path": str(target),
                "dry_run": not apply,
                "would_write": True,
            }
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write files; default is dry-run")
    parser.add_argument("--minni-home", default=None, help="Minni home; default $MINNI_HOME or ~/.minni")
    parser.add_argument("--principals-dir", default=None, help="target principals dir; default <minni-home>/principals")
    parser.add_argument("--template-dir", default=str(DEFAULT_TEMPLATE_DIR), help="principal templates dir")
    args = parser.parse_args(argv)

    results = author_principals(
        minni_home=args.minni_home,
        principals_dir=args.principals_dir,
        template_dir=Path(args.template_dir),
        apply=args.apply,
    )
    print(json.dumps({"dry_run": not args.apply, "principals": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

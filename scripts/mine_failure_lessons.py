"""CLI entrypoint for Phase 1b failure-lesson signature mining and vault handle registration (Rev 5 §7, S4)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any
import uuid

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.recall_replay.miner import (  # noqa: E402
    DEFAULT_APPROVED_LESSON_RULES,
    mine_corpus_signatures,
    write_miner_report,
)
from scripts.recall_replay.private_root import resolve_private_root  # noqa: E402


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def register_failure_lesson_handles(
    vault_path: str | Path,
    *,
    write_vault_notes: bool = False,
) -> dict[str, Any]:
    """Write all approved failure-lesson handles (m:7a11e501..m:7a11e513) into <vault>/.handles.json and default.handles.json."""
    vpath = Path(vault_path)
    vpath.mkdir(parents=True, exist_ok=True)
    handles_path = vpath / ".handles.json"
    existing: dict[str, Any] = {}
    if handles_path.is_file():
        try:
            loaded = json.loads(handles_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = dict(loaded)
        except (OSError, json.JSONDecodeError):
            existing = {}

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    for rule in DEFAULT_APPROVED_LESSON_RULES:
        handle = str(rule["handle"])
        lesson_id = str(rule["lesson_id"])
        rel_path = f"wiki/scars/{lesson_id}.md"
        entry = {
            "handle": handle,
            "kind": "vault",
            "title": str(rule["title"]),
            "wikilink": f"[[wiki/scars/{lesson_id}]]",
            "source": rel_path,
            "lesson_id": lesson_id,
            "mkey": str(rule["mkey"]),
            "rule_id": str(rule["rule_id"]),
            "lesson_text": str(rule["lesson_text"]),
            "project_scope": str(rule.get("project_scope", "global")),
            "score": 1.0,
            "ts": now_iso,
        }
        existing[handle] = entry

        if write_vault_notes:
            note_file = vpath / rel_path
            if not note_file.exists():
                note_file.parent.mkdir(parents=True, exist_ok=True)
                note_file.write_text(
                    "---\n"
                    f"title: {json.dumps(str(rule['title']))}\n"
                    "type: decision\n"
                    "privacy: safe\n"
                    "status: accepted\n"
                    f"handle: {handle}\n"
                    f"rule_id: {rule['rule_id']}\n"
                    "---\n"
                    f"# {rule['title']}\n\n"
                    f"{rule['lesson_text']}\n",
                    encoding="utf-8",
                )

    _atomic_write_json(handles_path, existing)

    runtime_handles_path = vpath / ".runtime" / "recall-state.d" / "default.handles.json"
    runtime_existing: dict[str, Any] = {
        "v": 1,
        "session_id": "default",
        "updated_at": now_iso,
        "ts": now_iso,
        "handles": {},
    }
    if runtime_handles_path.is_file():
        try:
            loaded_rt = json.loads(runtime_handles_path.read_text(encoding="utf-8"))
            if isinstance(loaded_rt, dict) and isinstance(loaded_rt.get("handles"), dict):
                runtime_existing = dict(loaded_rt)
                runtime_existing["v"] = 1
                runtime_existing["handles"] = dict(loaded_rt["handles"])
        except (OSError, json.JSONDecodeError):
            pass
    for k, v in existing.items():
        if isinstance(v, dict) and k.startswith("m:"):
            runtime_existing["handles"][k] = v
    _atomic_write_json(runtime_handles_path, runtime_existing)
    return existing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.mine_failure_lessons",
        description="Mine Phase 1b failure-lesson signatures and register m:7a11e501..m:7a11e513 handles.",
    )
    parser.add_argument("--corpus", default=None, help="Path to corpus JSONL file or directory.")
    parser.add_argument("--private-root", default=None, help="Override MINNI_PRIVATE_ROOT.")
    parser.add_argument(
        "--vault",
        "--vault-path",
        dest="vault",
        default=None,
        help="Target vault path to seed .handles.json and wiki/scars notes.",
    )
    parser.add_argument(
        "--write-vault-notes",
        action="store_true",
        default=True,
        help="Also write wiki/scars/<lesson_id>.md files inside --vault (default: True).",
    )
    parser.add_argument("--min-occurrences", type=int, default=5)
    parser.add_argument("--min-eligible-classes", type=int, default=5)
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.private_root:
        env["MINNI_PRIVATE_ROOT"] = str(args.private_root)
    private_root = resolve_private_root(env=env)

    report = mine_corpus_signatures(
        args.corpus,
        private_root=private_root,
        env=env,
        min_occurrences=args.min_occurrences,
        min_eligible_classes=args.min_eligible_classes,
    )
    report_path = write_miner_report(report, private_root=private_root, env=env)

    registered_count = 0
    if args.vault:
        handles = register_failure_lesson_handles(
            args.vault,
            write_vault_notes=bool(args.write_vault_notes),
        )
        registered_count = len(handles)

    sys.stdout.write(
        json.dumps(
            {
                "status": "ok",
                "eligible_classes_count": report["eligible_classes_count"],
                "masked_exit_matches": report["masked_exit_matches"],
                "go_decision": report["go_decision"],
                "registered_handles": registered_count,
                "report_path": str(report_path.name),
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

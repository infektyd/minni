"""CLI entrypoint for `python -m scripts.recall_replay` (Rev 5 §8.1, S1c / S4 / S5)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from scripts.recall_replay.miner import mine_corpus_signatures, write_miner_report
from scripts.recall_replay.private_root import resolve_private_root
from scripts.recall_replay.replay import (
    load_live_or_private_corpus,
    run_handler_replay,
    run_pure_replay,
    write_replay_report,
)


def _derive_handler_events(loaded_corpus: dict[str, object], allow_synthetic: bool) -> list[dict[str, object]]:
    explicit = loaded_corpus.get("handler_events") or loaded_corpus.get("events")
    if isinstance(explicit, list) and explicit:
        return [dict(x) for x in explicit if isinstance(x, dict)]
    turns = loaded_corpus.get("turns")
    notes = loaded_corpus.get("notes")
    first_note = notes[0] if isinstance(notes, list) and notes and isinstance(notes[0], dict) else {}
    if isinstance(turns, list) and turns:
        derived: list[dict[str, object]] = []
        for idx, t in enumerate(turns[:50]):
            if not isinstance(t, dict):
                continue
            reply_val = (
                t.get("assistant_reply")
                if isinstance(t.get("assistant_reply"), str)
                else t.get("reply_text")
                if isinstance(t.get("reply_text"), str)
                else t.get("last_assistant_message")
                if isinstance(t.get("last_assistant_message"), str)
                else ""
            )
            derived.append(
                {
                    "session_id": str(t.get("session_id") or f"sess-corpus-{idx + 1}"),
                    "prompt_id": str(t.get("prompt_id") or f"pid-corpus-{idx + 1}"),
                    "prompt": str(t.get("prompt") or "Check memory"),
                    "note_rel_path": str(
                        t.get("note_rel_path") or first_note.get("path") or "wiki/decisions/d1.md"
                    ),
                    "note_title": str(
                        t.get("note_title") or first_note.get("title") or "Corpus Note"
                    ),
                    "note_body": str(
                        t.get("note_body") or first_note.get("content") or ""
                    ),
                    "assistant_reply": str(reply_val),
                }
            )
        if derived:
            return derived
    if allow_synthetic:
        return [
            {
                "session_id": "sess-s5-final",
                "prompt_id": "pid-s5-1",
                "prompt": "Check strict realpath containment on private root",
                "note_rel_path": "wiki/decisions/d1.md",
                "note_title": "Strict Realpath Guard",
                "note_body": "Decision: enforce strict realpath containment on private root.",
                "assistant_reply": "We will enforce strict realpath containment on private root per [[wiki/decisions/d1.md]].",
            }
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.recall_replay",
        description="Run Rev 5 deterministic replay (S1c / S5) and Phase 1b signature miner (S4).",
    )
    parser.add_argument(
        "--private-root",
        default=None,
        help="Override MINNI_PRIVATE_ROOT directory.",
    )
    parser.add_argument(
        "--corpus",
        default=None,
        help="Explicit corpus JSON/JSONL file or directory.",
    )
    parser.add_argument(
        "--minni-home",
        default=None,
        help="Override MINNI_HOME directory containing minni.db and *-vault.",
    )
    parser.add_argument(
        "--allow-synthetic-fixture",
        action="store_true",
        default=False,
        help="Allow fallback to synthetic campaign baseline when no corpus is present.",
    )
    parser.add_argument(
        "--report-name",
        default="replay-final-s5.json",
        help="Output filename under <private_root>/reports/.",
    )
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.private_root:
        env["MINNI_PRIVATE_ROOT"] = str(args.private_root)
    private_root = resolve_private_root(env=env)

    eff_minni_home: str | Path | None = args.minni_home or env.get("MINNI_HOME")
    if eff_minni_home is None and args.private_root is None and "MINNI_PRIVATE_ROOT" not in os.environ:
        default_home = Path.home() / ".minni"
        if default_home.is_dir():
            eff_minni_home = default_home

    loaded_corpus = load_live_or_private_corpus(
        corpus_path=args.corpus,
        private_root=private_root,
        minni_home=eff_minni_home,
    )

    try:
        pure = run_pure_replay(
            loaded_corpus,
            corpus_path=args.corpus,
            private_root=private_root,
            minni_home=eff_minni_home,
            allow_synthetic_fixture=bool(args.allow_synthetic_fixture),
        )
    except ValueError as exc:
        sys.stderr.write(f"recall_replay error: {exc}\n")
        return 2

    handler_events = _derive_handler_events(loaded_corpus, bool(args.allow_synthetic_fixture))
    handler = run_handler_replay(
        handler_events,
        private_root=private_root,
        env=env,
    )
    miner = mine_corpus_signatures(
        args.corpus if args.corpus and Path(args.corpus).is_file() else None,
        private_root=private_root,
        env=env,
    )
    miner_path = write_miner_report(miner, private_root=private_root, env=env)

    combined = {
        "slice": "S5",
        "recall_to_use_rate": pure["recall_to_use_rate"],
        "p_at_3": pure["p_at_3"],
        "p50_boot_tokens": pure["p50_boot_tokens"],
        "pure_replay": pure,
        "handler_replay": {
            "status": handler.get("status"),
            "turns_count": len(handler.get("turns", [])),
            "surfaced_count": len(handler.get("surfaced", [])),
            "judged_count": len(handler.get("judged", [])),
            "exposures_count": len(handler.get("exposures", [])),
        },
        "phase1b_miner": {
            "eligible_classes_count": miner["eligible_classes_count"],
            "masked_exit_matches": miner["masked_exit_matches"],
            "go_decision": miner["go_decision"],
            "report_path": str(miner_path.name),
        },
    }
    out_path = write_replay_report(
        combined,
        report_name=args.report_name,
        private_root=private_root,
        env=env,
    )
    sys.stdout.write(
        json.dumps(
            {
                "status": "ok",
                "all_targets_passed": pure["target"]["all_targets_passed"],
                "recall_to_use_rate": pure["recall_to_use_rate"],
                "p_at_3": pure["p_at_3"],
                "p50_boot_tokens": pure["p50_boot_tokens"],
                "phase1b_go_decision": miner["go_decision"],
                "phase1b_eligible_classes": miner["eligible_classes_count"],
                "report": str(out_path.name),
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


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
    run_handler_replay,
    run_pure_replay,
    write_replay_report,
)


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
        "--report-name",
        default="replay-final-s5.json",
        help="Output filename under <private_root>/reports/.",
    )
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.private_root:
        env["MINNI_PRIVATE_ROOT"] = str(args.private_root)
    private_root = resolve_private_root(env=env)

    pure = run_pure_replay()
    handler = run_handler_replay(
        [
            {
                "session_id": "sess-s5-final",
                "prompt_id": "pid-s5-1",
                "prompt": "Check strict realpath containment on private root",
                "note_rel_path": "wiki/decisions/d1.md",
                "note_title": "Strict Realpath Guard",
                "note_body": "Decision: enforce strict realpath containment on private root.",
                "assistant_reply": "We will enforce strict realpath containment on private root per [[wiki/decisions/d1.md]].",
            }
        ],
        private_root=private_root,
        env=env,
    )
    miner = mine_corpus_signatures(private_root=private_root, env=env)
    miner_path = write_miner_report(miner, private_root=private_root, env=env)

    combined = {
        "slice": "S5",
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

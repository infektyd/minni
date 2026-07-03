"""
Minni offline recall evaluation harness.

The CLI remains here, while implementation lives in focused modules:
dataset.py, retrievers.py, metrics.py, and judging.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .dataset import (
    harvest_queries,
    load_queries,
    queries_path,
    repo_root,
    validate_queries,
)
from .judging import JudgeUnavailable, RubricScore, score_answer_placeholder
from .metrics import (
    KNOWN_RETRIEVE_KWARGS,
    _calibration_error,
    _extract_doc_ids,
    _mrr,
    _ndcg_at_k,
    _recall_at_k,
    _safe_search,
    _token_budget_recall_at_k,
    evaluate_gate,
    run_eval,
)
from .retrievers import (
    _MockSearcher,
    MockSearcher,
    RawContextSearcher,
    RealSearcher,
    RipgrepSearcher,
    SearcherProtocol,
    VendorMemorySearcher,
    make_searcher,
)

logger = logging.getLogger("sovereign.eval")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


CONFIGS: Dict[str, Dict[str, Any]] = {
    "baseline": {"use_hyde": False},
    "with-expand": {"expand": True},
    "with-hyde": {"use_hyde": True},
    "fp32-baseline": {},
    "int8-quantized": {},
    "with-semantic-merge": {},
}

_KNOWN_RETRIEVE_KWARGS = KNOWN_RETRIEVE_KWARGS


def _repo_root() -> Path:
    return repo_root()


def _queries_path() -> Path:
    return queries_path()


def _reports_dir() -> Path:
    d = repo_root() / "eval" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_json_report(report: Dict[str, Any], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    logger.info("JSON report written to %s", path)


def _write_markdown_comparison(
    reports: Dict[str, Dict[str, Any]],
    path: Path,
    ks: Tuple[int, ...] = (1, 3, 5, 10),
) -> None:
    """Write a Markdown comparison table across all configs."""
    lines = []
    lines.append("# Minni - Recall Eval Report")
    lines.append(f"\n**Generated:** {datetime.now(timezone.utc).isoformat()}\n")
    lines.append(f"**Queries:** {next(iter(reports.values()))['summary']['n_queries']}\n")

    k_cols = " | ".join(f"R@{k}" for k in ks)
    lines.append(f"| Config | {k_cols} | nDCG@10 | TB-R@5 | MRR | Cal.Err | Latency(s) |")
    lines.append(f"|--------|{'|'.join(['--------'] * len(ks))}|---------|--------|-----|---------|------------|")

    for config_name, report in reports.items():
        s = report["summary"]
        r_cols = " | ".join(
            f"{s['recall_at_k'].get(k, 0.0):.4f}" for k in ks
        )
        ndcg10 = s.get("ndcg_at_k", {}).get(10, 0.0)
        tb_r5 = s.get("token_budget_recall_at_k", {}).get(5, 0.0)
        cal = s["mean_calibration_error"]
        cal_str = f"{cal:.4f}" if cal is not None else "n/a"
        lines.append(
            f"| {config_name} | {r_cols} | {ndcg10:.4f} | {tb_r5:.4f} | "
            f"{s['mrr']:.4f} | {cal_str} | {s['mean_latency_s']:.4f} |"
        )

    lines.append("\n## Gate Rule\n")
    lines.append(
        "A feature may flip its default only after the harness shows >=+5% recall@5 "
        "compared to the `baseline` config, with no regression on any individual query class."
    )
    lines.append("")

    with path.open("w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    logger.info("Markdown comparison written to %s", path)


def cmd_run(args: argparse.Namespace) -> None:
    """Run evaluation for one or more configs and write reports."""
    config_names = [c.strip() for c in args.config.split(",")]
    retriever_names = [
        c.strip() for c in getattr(args, "retrievers", "minnid").split(",") if c.strip()
    ]

    unknown = [c for c in config_names if c not in CONFIGS]
    if unknown:
        logger.error("Unknown config(s): %s. Available: %s", unknown, list(CONFIGS))
        sys.exit(1)

    query_path = Path(args.queries) if getattr(args, "queries", "") else None
    queries = load_queries(query_path)
    if not queries:
        logger.warning("No queries loaded - producing empty report.")

    if getattr(args, "gate", False):
        validation = validate_queries(queries)
        if not validation["ok"]:
            logger.error("Query validation failed for gate run:")
            for error in validation["errors"]:
                logger.error("  - %s", error)
            sys.exit(2)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reports: Dict[str, Dict[str, Any]] = {}
    ks = (1, 3, 5, 10)

    for retriever_name in retriever_names:
        try:
            searcher = _MockSearcher(queries) if args.mock else make_searcher(retriever_name, queries)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not initialise retriever %r: %s", retriever_name, exc)
            sys.exit(1)

        for config_name in config_names:
            config_kwargs = CONFIGS[config_name]
            report_name = (
                retriever_name
                if len(config_names) == 1
                else f"{retriever_name}-{config_name}"
            )
            logger.info("Evaluating retriever=%s config=%s", retriever_name, config_name)
            report = run_eval(searcher, queries, report_name, config_kwargs, ks=ks)
            reports[report_name] = report

            json_path = _reports_dir() / f"{timestamp}-{report_name}.json"
            _write_json_report(report, json_path)

    md_path = _reports_dir() / f"{timestamp}-comparison.md"
    _write_markdown_comparison(reports, md_path, ks=ks)

    gate_report = None
    if getattr(args, "gate", False):
        gate_report = evaluate_gate(reports)
        gate_path = _reports_dir() / f"{timestamp}-gate.json"
        _write_json_report(gate_report, gate_path)
        if not gate_report["ok"]:
            logger.error("Gate failed: %s loss_rate=%s", gate_report["metric"], gate_report["loss_rate"])
            sys.exit(3)

    print(f"\n{'='*60}")
    print(f"Eval complete - {len(queries)} queries, {len(reports)} report(s)")
    print(f"{'='*60}")
    for config_name, report in reports.items():
        s = report["summary"]
        r5 = s["recall_at_k"].get(5, 0.0)
        print(f"  {config_name:<20} R@5={r5:.4f}  MRR={s['mrr']:.4f}")
    if gate_report:
        print(f"  gate                 ok={gate_report['ok']} loss_rate={gate_report['loss_rate']:.4f}")
    print(f"\nReports: {_reports_dir()}")


def cmd_validate(args: argparse.Namespace) -> None:
    queries = load_queries(Path(args.path) if args.path else None)
    report = validate_queries(queries, min_reviewed=args.min_reviewed)
    print(json.dumps(report, indent=2))
    if not report["ok"]:
        sys.exit(2)


def cmd_harvest(args: argparse.Namespace) -> None:
    roots = [Path(p) for p in args.roots]
    candidates = harvest_queries(roots, limit=args.limit)
    out_path = Path(args.output) if args.output else repo_root() / "eval" / "harvest-candidates.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for item in candidates:
            fh.write(json.dumps(item) + "\n")
    print(f"Harvested {len(candidates)} candidate queries to {out_path}")


def cmd_record(args: argparse.Namespace) -> None:
    """Append a new query entry to eval/queries.jsonl."""
    p = _queries_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    expected_ids = []
    if args.expected_ids:
        expected_ids = [int(i.strip()) for i in args.expected_ids.split(",")]

    entry = {
        "query": args.query,
        "expected_doc_ids": expected_ids,
        "notes": args.notes or "",
    }

    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")

    print(f"Recorded: {entry}")
    print(f"File: {p}")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m engine.eval.harness",
        description="Minni offline recall evaluation harness.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run evaluation across configs")
    run_p.add_argument(
        "--config",
        default="baseline",
        help="Comma-separated config names (default: baseline). Available: "
             + ", ".join(CONFIGS),
    )
    run_p.add_argument(
        "--queries",
        default="",
        help="Optional JSONL query path (default: eval/queries.jsonl)",
    )
    run_p.add_argument(
        "--mock",
        action="store_true",
        help="Use the deterministic mock searcher instead of the live engine",
    )
    run_p.add_argument(
        "--retrievers",
        default="minnid",
        help="Comma-separated retrievers: minnid,ripgrep,raw-context,vendor,mock",
    )
    run_p.add_argument(
        "--gate",
        action="store_true",
        help="Validate queries and fail if minnid loses to ripgrep on >20%% of comparable queries",
    )

    rec_p = sub.add_parser("record", help="Append a query to eval/queries.jsonl")
    rec_p.add_argument("--query", required=True, help="Query string")
    rec_p.add_argument(
        "--expected-ids",
        default="",
        help="Comma-separated expected doc_ids (e.g. 8412,8413)",
    )
    rec_p.add_argument("--notes", default="", help="Optional notes / class label")

    validate_p = sub.add_parser("validate", help="Validate eval query JSONL for gate use")
    validate_p.add_argument("--path", default="", help="Optional JSONL path")
    validate_p.add_argument("--min-reviewed", type=int, default=300)

    harvest_p = sub.add_parser("harvest", help="Harvest review candidates from local files")
    harvest_p.add_argument(
        "roots",
        nargs="*",
        default=["session-extracts", "docs/contracts", "codex-vault/wiki"],
        help="Files or directories to harvest",
    )
    harvest_p.add_argument("--limit", type=int, default=300)
    harvest_p.add_argument("--output", default="")

    args = parser.parse_args(argv)

    if args.command == "run":
        cmd_run(args)
    elif args.command == "record":
        cmd_record(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "harvest":
        cmd_harvest(args)


if __name__ == "__main__":
    main()


__all__ = [
    "CONFIGS",
    "JudgeUnavailable",
    "MockSearcher",
    "RawContextSearcher",
    "RealSearcher",
    "RipgrepSearcher",
    "RubricScore",
    "SearcherProtocol",
    "VendorMemorySearcher",
    "_KNOWN_RETRIEVE_KWARGS",
    "_MockSearcher",
    "_calibration_error",
    "_extract_doc_ids",
    "_mrr",
    "_ndcg_at_k",
    "_queries_path",
    "_recall_at_k",
    "_repo_root",
    "_safe_search",
    "_token_budget_recall_at_k",
    "_write_json_report",
    "_write_markdown_comparison",
    "cmd_harvest",
    "cmd_record",
    "cmd_run",
    "cmd_validate",
    "evaluate_gate",
    "harvest_queries",
    "load_queries",
    "make_searcher",
    "run_eval",
    "score_answer_placeholder",
    "validate_queries",
]

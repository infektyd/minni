"""
Minni offline recall evaluation harness.

The CLI remains here, while implementation lives in focused modules:
dataset.py, retrievers.py, metrics.py, and judging.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import stat
import uuid
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
    validate_quality_queries,
)
from .provenance import (
    LIVE_BACKENDS,
    backend_envelope_options,
    backend_ignored_options,
    build_gate_provenance,
    build_report_provenance,
    code_provenance,
    corpus_provenance,
    environment_provenance,
    principal_provenance,
    query_file_provenance,
    retrieval_options_provenance,
)
from .judging import JudgeUnavailable, RubricScore, score_answer_placeholder
from .metrics import (
    KNOWN_RETRIEVE_KWARGS,
    QUALITY_GATE_DEFAULT_MIN_IMPROVEMENT,
    _calibration_error,
    _extract_doc_ids,
    _mrr,
    _ndcg_at_k,
    _recall_at_k,
    _safe_search,
    _token_budget_recall_at_k,
    evaluate_gate,
    evaluate_quality_gate,
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
    "no-expand": {"use_hyde": False, "expand": False},
    "with-expand": {"use_hyde": False, "expand": True},
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
    # Fresh directories are created private (0700 has no group/other bits, so
    # no umask can broaden them). A pre-existing group/other-writable
    # directory fails here — before any retrieval work — instead of running
    # the whole study and then writing zero reports.
    d = repo_root() / "eval" / "reports"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError(
                "Default report directory must be owned by this user and not "
                "writable by others; fix its mode or rerun with --output-dir "
                "pointing at a private directory"
            )
    finally:
        os.close(fd)
    return d


def _resolve_reports_dir(output_dir: Any = None) -> Path:
    """User-selected report directory, defaulting to the in-repo reports dir.

    A private study keeps its reports outside version control by passing
    ``--output-dir``; the default preserves the existing ``eval/reports``
    location for legacy runs.
    """
    if output_dir:
        d = Path(output_dir).expanduser()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Do not chmod an existing directory: it may be a shared /tmp or an
        # unrelated user folder. The caller must select a private destination.
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise ValueError("Explicit report directory must be owned by this user and private (0700)")
        finally:
            os.close(fd)
        return d
    return _reports_dir()


def _write_private_report(path: Path, text: str) -> None:
    """Publish private bytes through a pinned directory, never a report symlink."""
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".minni-report-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("Report directory must be owned by this user and not writable by others")
        try:
            previous = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1:
                raise ValueError("Report destination must be a regular, unlinked file")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        # Replacement never follows a destination symlink, including one
        # introduced after the check. Existing broad file modes are replaced.
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


def _write_json_report(report: Dict[str, Any], path: Path) -> None:
    _write_private_report(path, json.dumps(report, indent=2))
    logger.info("JSON report written to %s", path)


def _check_single_file_destination(directory_fd: int, name: str) -> None:
    """Reject a symlink, directory, hardlinked, or foreign-owned destination."""
    try:
        existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1:
        raise ValueError("Output destination must be a regular, unlinked file")
    if existing.st_uid != os.getuid():
        raise ValueError("Output destination is owned by another user")


def _open_single_file_parent(path: Path) -> int:
    """Open the output parent for a direct-file write such as fixture output.

    The parent symlink is followed once (macOS `/tmp` itself is a symlink),
    then the opened directory must be private or a sticky shared directory:
    a single 0600 file in a sticky directory is private by its own mode,
    while a non-sticky group/other-writable parent lets another user swap
    the destination. Callers must ``os.close`` the returned fd.
    """
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
    except OSError:
        os.close(fd)
        raise
    if mode & 0o022 and not mode & stat.S_ISVTX:
        os.close(fd)
        raise ValueError(
            "Output directory is writable by others without a sticky bit; "
            "use a private directory"
        )
    return fd


def _preflight_single_file(path: Path) -> None:
    """Fail fast (ValueError) when a direct-file destination is unusable."""
    if not path.parent.exists() or not path.parent.is_dir():
        raise ValueError(f"Output parent {path.parent} is not a directory")
    directory = _open_single_file_parent(path)
    try:
        _check_single_file_destination(directory, path.name)
    finally:
        os.close(directory)


def _write_private_single_file(path: Path, text: str) -> None:
    """Write one 0600 file, allowing a sticky shared parent such as /tmp."""
    directory = _open_single_file_parent(path)
    temporary = f".minni-report-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        _check_single_file_destination(directory, path.name)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        created = True
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        if created:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.close(directory)


def _write_markdown_comparison(
    reports: Dict[str, Dict[str, Any]],
    path: Path,
    ks: Tuple[int, ...] = (1, 3, 5, 10),
    run_provenance: Optional[Dict[str, Any]] = None,
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
    if run_provenance is not None:
        lines.append("\n## Run Provenance\n")
        lines.append(
            f"Query file: `{run_provenance.get('query_effective_path')}` "
            f"(scored-content digest "
            f"`{run_provenance.get('query_loaded_digest') or 'unknown'}`; "
            f"separately observed file bytes "
            f"`{run_provenance.get('query_file_sha256') or 'unknown'}` "
            "with unverified correspondence)."
        )
        lines.append(
            f"Code revision: `{run_provenance.get('code_revision')}` "
            f"(dirty: {run_provenance.get('code_dirty')})."
        )
        lines.append(
            f"Mock run: {run_provenance.get('mock')}; "
            f"live backends mutable: {run_provenance.get('live_backend_present')}."
        )
        lines.append(
            "Corpus snapshot: "
            f"`{run_provenance.get('corpus_snapshot')}` "
            "(live databases are never hashed; unknown means unverifiable, not frozen)."
        )
        lines.append(
            "Per-report JSON carries the full provenance block, including "
            "requested/effective options, principal availability, run order, "
            "and timing caveats. Provenance is not a passing certification."
        )
        lines.append("")

    _write_private_report(path, "\n".join(lines) + "\n")
    logger.info("Markdown comparison written to %s", path)


def _resolve_quality_keys(
    reports: Dict[str, Dict[str, Any]],
    baseline: str,
    candidate: str,
) -> Tuple[str, Optional[str]]:
    """
    Resolve user-supplied baseline/candidate names to report keys.

    ``cmd_run`` supplies explicit retriever/config metadata. Bare reports
    accept only exact known retriever/config names, never an arbitrary suffix.
    An explicit unresolved candidate remains explicit and cannot auto-select.
    """

    def resolve(name: str) -> Optional[str]:
        if name in reports:
            return name
        matched = []
        for key, report in reports.items():
            config = report.get("quality_config") if isinstance(report, dict) else None
            if config is not None:
                if config == name:
                    matched.append(key)
            elif name in CONFIGS and any(key == f"{retriever}-{name}" for retriever in
                    ("minnid", "sovrd", "baseline", "mock", "ripgrep", "rg",
                     "raw-context", "raw_context", "raw", "vendor",
                     "vendor-memory", "vendor_memory")):
                matched.append(key)
        if len(matched) == 1:
            return matched[0]
        return None

    baseline_key = resolve(baseline)
    candidate_key = resolve(candidate) if candidate else None
    if candidate_key is None and not candidate and len(reports) == 2 and baseline_key:
        others = [key for key in reports if key != baseline_key]
        candidate_key = others[0] if len(others) == 1 else None
    return baseline_key or baseline, candidate_key or candidate or None


def _preflight_quality_gate(
    args: argparse.Namespace,
    config_names: list,
    retriever_names: list,
    queries: Optional[list] = None,
) -> None:
    """
    Fail fast (exit 2) on an unusable quality-gate invocation before any
    retriever work: non-normative metric, unresolvable explicit names, or
    no auto-selectable candidate.
    """
    if len(config_names) != len(set(config_names)):
        logger.error("Quality gate config names must be unique")
        sys.exit(2)
    if getattr(args, "gate", False):
        logger.error("--gate and --quality-gate are mutually exclusive")
        sys.exit(2)
    if len(retriever_names) != 1 or retriever_names[0] not in {"minnid", "sovrd", "baseline", "mock"}:
        logger.error("Quality gate requires one document-ID retriever with multiple configs")
        sys.exit(2)
    if set(config_names) & {"fp32-baseline", "int8-quantized", "with-semantic-merge"}:
        logger.error("Quality gate does not support placeholder ablations without implemented options")
        sys.exit(2)
    if any(key not in KNOWN_RETRIEVE_KWARGS
           for config in config_names for key in CONFIGS[config]):
        logger.error("Quality gate config contains unsupported retrieval options")
        sys.exit(2)
    metric = getattr(args, "quality_metric", "recall_at_k") or "recall_at_k"
    if metric != "recall_at_k":
        logger.error(
            "Quality gate compares normative recall_at_k only, not %r", metric
        )
        sys.exit(2)
    k = getattr(args, "quality_k", 5)
    threshold = getattr(args, "min_improvement", QUALITY_GATE_DEFAULT_MIN_IMPROVEMENT)
    if (isinstance(k, bool) or not isinstance(k, int) or k not in (1, 3, 5, 10)):
        logger.error("Quality gate K must be one of 1, 3, 5, 10; got %r", k)
        sys.exit(2)
    if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
            or not math.isfinite(threshold) or threshold < 0):
        logger.error("Quality gate improvement must be finite and nonnegative")
        sys.exit(2)
    if queries is not None:
        validation = validate_quality_queries(queries)
        if not validation["ok"]:
            logger.error("Invalid quality corpus: %s", validation["errors"])
            sys.exit(2)
    expected = [
        retriever if len(config_names) == 1 else f"{retriever}-{config}"
        for retriever in retriever_names
        for config in config_names
    ]
    baseline_req = getattr(args, "quality_baseline", "baseline") or "baseline"
    candidate_req = getattr(args, "quality_candidate", "") or ""
    baseline_key, candidate_key = _resolve_quality_keys(
        {key: {"quality_config": config} for key, config in
         zip(expected, [config for _ in retriever_names for config in config_names])},
        baseline_req, candidate_req
    )
    if baseline_key not in expected:
        logger.error(
            "Quality gate baseline %r matches no expected report %s",
            baseline_req,
            expected,
        )
        sys.exit(2)
    if candidate_req:
        if candidate_key not in expected:
            logger.error(
                "Quality gate candidate %r matches no expected report %s",
                candidate_req,
                expected,
            )
            sys.exit(2)
    elif len(expected) != 2:
        logger.error(
            "Quality gate needs exactly two reports to auto-select a "
            "candidate (have %d); pass --quality-candidate",
            len(expected),
        )
        sys.exit(2)

    if baseline_key == candidate_key:
        logger.error("Quality gate baseline and candidate must be distinct configs")
        sys.exit(2)
    configs_by_key = {
        key: config for key, config in
        zip(expected, [config for _ in retriever_names for config in config_names])
    }
    baseline_options = {"expand": True, **CONFIGS[configs_by_key[baseline_key]]}
    candidate_options = {"expand": True, **CONFIGS[configs_by_key[candidate_key]]}
    if baseline_options == candidate_options:
        logger.error("Quality gate requires different effective retrieval options, not only labels")
        sys.exit(2)
    hyde_values = [
        CONFIGS[configs_by_key[key]].get("use_hyde")
        for key in (baseline_key, candidate_key)
    ]
    if any(value is not False for value in hyde_values):
        logger.error(
            "Quality gate requires HyDE constant and off (use_hyde=False) on "
            "both configs; got baseline=%r candidate=%r",
            hyde_values[0], hyde_values[1],
        )
        sys.exit(2)


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

    run_order = [
        retriever if len(config_names) == 1 else f"{retriever}-{config}"
        for retriever in retriever_names
        for config in config_names
    ]
    reserved = ({"gate"} if getattr(args, "gate", False) else set()) | (
        {"quality-gate"} if getattr(args, "quality_gate", False) else set()
    )
    folded = [name.casefold() for name in run_order]
    if (not run_order or len(set(run_order)) != len(run_order)
            or len(set(folded)) != len(folded)
            or reserved.intersection(run_order)
            or {name.casefold() for name in reserved}.intersection(folded)):
        logger.error(
            "Each evaluation must have a unique report name; remove repeated "
            "configs/retrievers (names compare case-insensitively: shared "
            "backends and report files collide on case-insensitive filesystems)"
        )
        sys.exit(2)
    if any(Path(name).name != name or name in (".", "..") for name in run_order):
        logger.error("Report names must be plain filenames")
        sys.exit(2)

    query_path = Path(args.queries) if getattr(args, "queries", "") else None
    try:
        queries = load_queries(query_path, strict=True) if getattr(args, "quality_gate", False) else load_queries(query_path)
    except ValueError as exc:
        logger.error("Invalid quality corpus: %s", exc)
        sys.exit(2)
    if not queries:
        logger.warning("No queries loaded - producing empty report.")

    if getattr(args, "gate", False) or getattr(args, "quality_gate", False):
        validation = (validate_quality_queries(queries) if getattr(args, "quality_gate", False)
                      else validate_queries(queries))
        if not validation["ok"]:
            logger.error("Query validation failed for gate run:")
            for error in validation["errors"]:
                logger.error("  - %s", error)
            sys.exit(2)

    if getattr(args, "quality_gate", False):
        _preflight_quality_gate(args, config_names, retriever_names, queries)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_started_iso = datetime.now(timezone.utc).isoformat()
    reports: Dict[str, Dict[str, Any]] = {}
    ks = (1, 3, 5, 10)
    try:
        reports_dir = _resolve_reports_dir(getattr(args, "output_dir", ""))
    except (ValueError, OSError) as exc:
        logger.error("Invalid report directory: %s", exc)
        sys.exit(2)

    effective_query_path = query_path or _queries_path()
    query_prov = query_file_provenance(query_path, effective_query_path, queries)
    code_prov = code_provenance(repo_root())
    env_prov = environment_provenance()
    # Actual constructed backend states, collected per report below. The run
    # summary is derived from these, never from CLI flags alone: e.g.
    # `--retrievers mock` without `--mock` still constructs a mock backend.
    backend_states: list = []

    for retriever_name in retriever_names:
        try:
            searcher = _MockSearcher(queries) if args.mock else make_searcher(retriever_name, queries)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not initialise retriever %r: %s", retriever_name, exc)
            sys.exit(1)
        is_mock = bool(getattr(args, "mock", False)) or retriever_name.strip().lower() == "mock"

        for config_name in config_names:
            config_kwargs = CONFIGS[config_name]
            report_name = (
                retriever_name
                if len(config_names) == 1
                else f"{retriever_name}-{config_name}"
            )
            logger.info("Evaluating retriever=%s config=%s", retriever_name, config_name)
            try:
                report = run_eval(searcher, queries, report_name, config_kwargs, ks=ks,
                                  strict_search=getattr(args, "quality_gate", False))
            except RuntimeError:
                logger.error("Quality evaluation aborted: retrieval failed; no comparison accepted")
                sys.exit(3)
            report["quality_config"] = config_name
            report["quality_retriever"] = retriever_name
            report["provenance"] = build_report_provenance(
                query=query_prov,
                code=code_prov,
                retrieval=retrieval_options_provenance(
                    config_name, config_kwargs, KNOWN_RETRIEVE_KWARGS,
                    backend_ignored=backend_ignored_options(retriever_name),
                    backend_envelope=backend_envelope_options(retriever_name),
                ),
                principal=principal_provenance(retriever_name, is_mock=is_mock),
                corpus=corpus_provenance(is_mock=is_mock, retriever_name=retriever_name),
                environment=env_prov,
                retriever_name=retriever_name,
                run_index=run_order.index(report_name),
                run_order=run_order,
                started_iso=run_started_iso,
                mock=is_mock,
            )
            reports[report_name] = report
            backend_states.append((retriever_name, is_mock))

            json_path = reports_dir / f"{timestamp}-{report_name}.json"
            _write_json_report(report, json_path)

    snapshots = sorted({
        report["provenance"]["corpus"]["snapshot"] for report in reports.values()
    })
    run_prov_summary = {
        "query_effective_path": query_prov["effective_path"],
        "query_loaded_digest": query_prov["loaded_queries_digest"],
        "query_file_sha256": query_prov["file_sha256"],
        "code_revision": code_prov["revision"],
        "code_dirty": code_prov["dirty"],
        "mock": bool(backend_states) and all(mock for _, mock in backend_states),
        "live_backend_present": any(
            not mock and name.strip().lower() in LIVE_BACKENDS
            for name, mock in backend_states
        ),
        "corpus_snapshot": (
            snapshots[0] if len(snapshots) == 1
            else f"mixed: {', '.join(snapshots)}" if snapshots
            else "unknown"
        ),
    }
    md_path = reports_dir / f"{timestamp}-comparison.md"
    _write_markdown_comparison(reports, md_path, ks=ks, run_provenance=run_prov_summary)

    gate_report = None
    if getattr(args, "gate", False):
        gate_report = evaluate_gate(reports)
        gate_report["provenance"] = build_gate_provenance(
            kind="legacy-loss-rate",
            query=query_prov,
            code=code_prov,
            baseline="ripgrep",
            candidate="minnid",
            decision=gate_report,
            corpus_snapshot=run_prov_summary["corpus_snapshot"],
            mock=run_prov_summary["mock"],
            live_backend_present=run_prov_summary["live_backend_present"],
            started_iso=run_started_iso,
        )
        gate_path = reports_dir / f"{timestamp}-gate.json"
        _write_json_report(gate_report, gate_path)
        if not gate_report["ok"]:
            logger.error("Gate failed: %s loss_rate=%s", gate_report["metric"], gate_report["loss_rate"])
            sys.exit(3)

    quality_report = None
    if getattr(args, "quality_gate", False):
        baseline_key, candidate_key = _resolve_quality_keys(
            reports,
            getattr(args, "quality_baseline", "baseline") or "baseline",
            getattr(args, "quality_candidate", "") or "",
        )
        quality_report = evaluate_quality_gate(
            reports,
            baseline=baseline_key,
            candidate=candidate_key,
            metric=getattr(args, "quality_metric", "recall_at_k") or "recall_at_k",
            k=getattr(args, "quality_k", 5),
            min_relative_improvement=getattr(
                args, "min_improvement", QUALITY_GATE_DEFAULT_MIN_IMPROVEMENT
            ),
        )
        quality_report["provenance"] = build_gate_provenance(
            kind="quality",
            query=query_prov,
            code=code_prov,
            baseline=baseline_key,
            candidate=candidate_key,
            decision=quality_report,
            corpus_snapshot=run_prov_summary["corpus_snapshot"],
            mock=run_prov_summary["mock"],
            live_backend_present=run_prov_summary["live_backend_present"],
            started_iso=run_started_iso,
        )
        quality_path = reports_dir / f"{timestamp}-quality-gate.json"
        _write_json_report(quality_report, quality_path)
        if not quality_report["ok"]:
            logger.error("Quality gate failed: %s", quality_report["reason"])
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
    if quality_report:
        print(f"  quality gate         ok={quality_report['ok']} reason={quality_report['reason']}")
    print(f"\nReports: {reports_dir}")


def cmd_validate(args: argparse.Namespace) -> None:
    quality = getattr(args, "quality_gate", False)
    try:
        queries = load_queries(Path(args.path) if args.path else None, strict=quality)
        validator = validate_quality_queries if quality else validate_queries
        report = validator(queries, min_reviewed=args.min_reviewed)
    except ValueError as exc:
        report = {"ok": False, "errors": [str(exc)]}
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
        prog="python -m minni.eval.harness",
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
        "--output-dir",
        default="",
        help="Report output directory (default: eval/reports). Point it "
             "outside the repo to keep private study reports out of version control.",
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
    run_p.add_argument(
        "--quality-gate",
        action="store_true",
        help="Enforce the baseline-vs-candidate quality gate: +5%% recall@5 "
             "with no query-class regression (WORKFLOWS Eval Gate)",
    )
    run_p.add_argument(
        "--quality-baseline",
        default="baseline",
        help="Baseline config report name (default: baseline)",
    )
    run_p.add_argument(
        "--quality-candidate",
        default="",
        help="Candidate config report name (default: auto-resolve when two reports exist)",
    )
    run_p.add_argument(
        "--min-improvement",
        type=float,
        default=QUALITY_GATE_DEFAULT_MIN_IMPROVEMENT,
        help="Required relative gain on the quality metric (default: 0.05)",
    )
    run_p.add_argument(
        "--quality-metric",
        default="recall_at_k",
        help="Per-query metric compared by the quality gate "
             "(only the normative recall_at_k is accepted)",
    )
    run_p.add_argument(
        "--quality-k",
        type=int,
        default=5,
        help="K for the quality-gate metric (default: 5)",
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
    validate_p.add_argument("--quality-gate", action="store_true",
                            help="Use the strict corpus contract shared with quality runs")
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

    fixture_p = sub.add_parser("fixture", help="Run machine-curated retrieval against a disposable real corpus")
    fixture_p.add_argument("--profile", choices=["lexical-deadline", "hybrid"], default="lexical-deadline")
    fixture_p.add_argument("--repeats", type=int, default=3)
    fixture_p.add_argument("--corpus", type=Path, help="Synthetic or source-grounded fixture JSON")
    fixture_p.add_argument("--output", required=True, help="JSON report path outside the fixture corpus")

    args = parser.parse_args(argv)

    if args.command == "run":
        cmd_run(args)
    elif args.command == "record":
        cmd_record(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "harvest":
        cmd_harvest(args)
    elif args.command == "fixture":
        from .fixture import run_fixture
        out_path = Path(args.output)
        try:
            _preflight_single_file(out_path)
        except (ValueError, OSError) as exc:
            logger.error("Invalid fixture output: %s", exc)
            sys.exit(2)
        report = run_fixture(path=args.corpus, profile=args.profile, repeats=args.repeats)
        try:
            _write_private_single_file(out_path, json.dumps(report, indent=2))
        except (ValueError, OSError) as exc:
            logger.error("Could not write fixture report: %s", exc)
            sys.exit(1)
        logger.info("JSON report written to %s", out_path)
        print(json.dumps(report["summary"], indent=2))
        if not report["summary"]["ok"]:
            sys.exit(3)


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
    "_resolve_reports_dir",
    "_safe_search",
    "_token_budget_recall_at_k",
    "_write_json_report",
    "_write_markdown_comparison",
    "_preflight_single_file",
    "_write_private_single_file",
    "cmd_harvest",
    "cmd_record",
    "cmd_run",
    "cmd_validate",
    "_preflight_quality_gate",
    "_resolve_quality_keys",
    "evaluate_gate",
    "evaluate_quality_gate",
    "harvest_queries",
    "load_queries",
    "make_searcher",
    "run_eval",
    "score_answer_placeholder",
    "validate_queries",
]

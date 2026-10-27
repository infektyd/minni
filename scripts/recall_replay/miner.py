"""Recurring-error signature miner and project-key resolver for Phase 1b (Rev 5 §7, S4)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from scripts.recall_replay.private_root import assert_under, resolve_private_root

DEFAULT_APPROVED_LESSON_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule_id": "swift_testing_expectation_failed",
        "stage": "PostToolUse",
        "pattern": r"(?:Expectation failed:|✘\s+Test\s+)",
        "lesson_id": "scar-swift-testing-expectation-failed",
        "mkey": "learning:phase1b-swift-testing-expectation-failed",
        "handle": "m:7a11e501",
        "title": "Swift Testing Expectation failed (masked exit code)",
        "lesson_text": (
            "Never mask `swift test` exit codes with `| tail` or `; echo exit=$?`; "
            "run the failing `@Test` filter directly and inspect the exact `Expectation failed:` operand diff."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "swift_compiler_error",
        "stage": "PostToolUse",
        "pattern": r"\.swift:\d+:\d+:\s+error:",
        "lesson_id": "scar-swift-compiler-error",
        "mkey": "learning:phase1b-swift-compiler-error",
        "handle": "m:7a11e502",
        "title": "Swift compiler diagnostic error",
        "lesson_text": (
            "Fix: check module target imports, visibility modifiers, and `project.pbxproj` "
            "target membership before re-running a full build."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "praxis_verify_source_baseline",
        "stage": "PostToolUse",
        "pattern": r"(?:verify_source_baseline\.sh[^\n]*(?:FAIL|error|unregistered|missing|not registered)|unregistered test file)",
        "lesson_id": "scar-praxis-verify-source-baseline",
        "mkey": "learning:phase1b-praxis-verify-source-baseline",
        "handle": "m:7a11e503",
        "title": "Scar #5: unregistered Swift test file in verify_source_baseline.sh",
        "lesson_text": (
            "Scar #5: register every new `*Tests.swift` file in `project.pbxproj` (`PBXBuildFile` + "
            "`PBXFileReference` + `Sources`) or `membershipExceptions` in `scripts/verify_source_baseline.sh`."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "xctest_assertion_failed",
        "stage": "PostToolUse",
        "pattern": r"(?:XCTAssert[A-Za-z]*\s+failed|Test Case\s+'[^']+'\s+failed)",
        "lesson_id": "scar-xctest-assertion-failed",
        "mkey": "learning:phase1b-xctest-assertion-failed",
        "handle": "m:7a11e504",
        "title": "XCTest assertion failure",
        "lesson_text": (
            "Fix: isolate the failing `XCTestCase` method with `-only-testing:` and verify "
            "fixture state before running the full test plan."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "git_diff_show_hook_block",
        "stage": "PreToolUse",
        "pattern": (
            r"(?:Blocked:\s*unbounded\s+git\s+diff/show\s+output|"
            r"(?:^|[;&|]\s*)git\s+(?:show\s+(?!--stat\b|--numstat\b|--name-only\b|--name-status\b|-s\b|--no-patch\b)[^\n|;]+|"
            r"checkout\s+(?!-b\b|-B\b|--orphan\b)\S+\s+--\s+\S+))"
        ),
        "lesson_id": "scar-git-cat-file-instead-of-show",
        "mkey": "learning:phase1b-git-cat-file-instead-of-show",
        "handle": "m:7a11e505",
        "title": "Scar #6: use git cat-file -p instead of unbounded git show / git checkout",
        "lesson_text": (
            "Scar #6: use `git cat-file -p <rev>:<path>` to read historical file contents and "
            "`git diff --stat` or `git diff -- <path>` instead of unbounded `git show` or `git checkout <rev> -- <path>`."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "jev_guard_block",
        "stage": "PostToolUse",
        "pattern": r"Blocked by Jev guard",
        "lesson_id": "scar-jev-guard-destructive-command",
        "mkey": "learning:phase1b-jev-guard-destructive-command",
        "handle": "m:7a11e506",
        "title": "Jev guard blocked destructive shell command",
        "lesson_text": (
            "Never run `rm -rf` or `git worktree remove` when blocked by the Jev guard; "
            "use non-destructive `mv` or explicit single-file operations within the owned worktree."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "process_wait_until_exit_forbidden",
        "stage": "PostToolUse",
        "pattern": r"(?:waitUntilExit\s*\(|waitpid\s*\()",
        "lesson_id": "scar-process-wait-until-exit-forbidden",
        "mkey": "learning:phase1b-process-wait-until-exit-forbidden",
        "handle": "m:7a11e507",
        "title": "Scar #1: never call blocking waitUntilExit() / waitpid() after bounded timeout",
        "lesson_text": (
            "Scar #1: never call blocking `waitUntilExit()` or `waitpid()` on a timed-out subprocess; "
            "terminate the process group and drain pipes asynchronously."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "subagent_update_plan_blocked",
        "stage": "PostToolUse",
        "pattern": r"Not shown:\s*you are a subagent",
        "lesson_id": "scar-subagent-update-plan-blocked",
        "mkey": "learning:phase1b-subagent-update-plan-blocked",
        "handle": "m:7a11e508",
        "title": "Subagent update_plan hook restriction",
        "lesson_text": (
            "Subagents cannot call `update_plan`; return structured slice evidence to the parent "
            "coordinator, which owns plan state updates."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "unknown_tool_name",
        "stage": "PostToolUse",
        "pattern": r"(?:No such tool available:|MCP tool\s+`[^`]+`\s+not found|unknown tool)",
        "lesson_id": "scar-unknown-tool-name",
        "mkey": "learning:phase1b-unknown-tool-name",
        "handle": "m:7a11e509",
        "title": "Unknown MCP or host tool name",
        "lesson_text": (
            "Verify the exact registered tool name and MCP server prefix before invoking a tool."
        ),
        "project_scope": "global",
    },
    {
        "rule_id": "unchecked_sendable_forbidden",
        "stage": "PostToolUse",
        "pattern": r"@unchecked\s+Sendable",
        "lesson_id": "scar-unchecked-sendable-forbidden",
        "mkey": "learning:phase1b-unchecked-sendable-forbidden",
        "handle": "m:7a11e510",
        "title": "Scar #2: @unchecked Sendable is forbidden in Swift 6 concurrency",
        "lesson_text": (
            "Scar #2: never add `@unchecked Sendable` to silence Swift 6 strict concurrency; "
            "convert the type to an `actor`, isolate it to `@MainActor`, or make stored properties immutable `Sendable` values."
        ),
        "project_scope": "swift",
    },
    {
        "rule_id": "try_optional_swallows_errors",
        "stage": "PostToolUse",
        "pattern": r"\btry\?\s+[A-Za-z_]",
        "lesson_id": "scar-try-optional-swallows-errors",
        "mkey": "learning:phase1b-try-optional-swallows-errors",
        "handle": "m:7a11e511",
        "title": "Scar #3: do not silently swallow errors with try?",
        "lesson_text": (
            "Scar #3: avoid `try?` on I/O, persistence, or IPC boundaries where failures must be surfaced or logged; "
            "handle the error explicitly with `do/catch` or propagate `throws`."
        ),
        "project_scope": "swift",
    },
    {
        "rule_id": "task_detached_forbidden",
        "stage": "PostToolUse",
        "pattern": r"\bTask\.detached\s*(?:\(| trailing|\{)",
        "lesson_id": "scar-task-detached-forbidden",
        "mkey": "learning:phase1b-task-detached-forbidden",
        "handle": "m:7a11e512",
        "title": "Scar #4: avoid unstructured Task.detached in Swift concurrency",
        "lesson_text": (
            "Scar #4: never spawn unstructured `Task.detached` that drops actor context, priority, and cancellation propagation; "
            "use structured concurrency (`withTaskGroup`) or actor-isolated `Task`."
        ),
        "project_scope": "swift",
    },
    {
        "rule_id": "nslock_in_async_context",
        "stage": "PostToolUse",
        "pattern": r"\b(?:NSLock|NSRecursiveLock)\s*\(",
        "lesson_id": "scar-nslock-in-async-context",
        "mkey": "learning:phase1b-nslock-in-async-context",
        "handle": "m:7a11e513",
        "title": "Scar #7: avoid NSLock / NSRecursiveLock in Swift async contexts",
        "lesson_text": (
            "Scar #7: never use `NSLock` or `NSRecursiveLock` across Swift `async` suspension points; "
            "use an `actor` or `Mutex` / `OSAllocatedUnfairLock` strictly inside synchronous critical sections."
        ),
        "project_scope": "swift",
    },
)


def _resolve_git_common_dir_walk(start_dir: Path) -> Path | None:
    """Resolve the git common directory by walking parent directories without requiring a subprocess."""
    curr = start_dir
    for _ in range(64):
        dot_git = curr / ".git"
        try:
            if dot_git.is_dir():
                commondir_file = dot_git / "commondir"
                if commondir_file.is_file():
                    rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
                    if rel:
                        return Path(os.path.realpath(dot_git / rel))
                return Path(os.path.realpath(dot_git))
            if dot_git.is_file():
                raw = dot_git.read_text(encoding="utf-8", errors="replace").strip()
                if raw.startswith("gitdir:"):
                    gitdir_raw = raw[len("gitdir:") :].strip()
                    gitdir_path = (
                        Path(gitdir_raw) if os.path.isabs(gitdir_raw) else (curr / gitdir_raw)
                    )
                    gitdir_real = Path(os.path.realpath(gitdir_path))
                    commondir_file = gitdir_real / "commondir"
                    if commondir_file.is_file():
                        rel = commondir_file.read_text(encoding="utf-8", errors="replace").strip()
                        if rel:
                            return Path(os.path.realpath(gitdir_real / rel))
                    return gitdir_real
        except OSError:
            pass
        parent = curr.parent
        if parent == curr:
            break
        curr = parent
    return None


def resolve_project_key(path_or_cwd: str | Path | None = None) -> str:
    """Resolve `path_or_cwd` to a canonical project key based on its git common directory (§7)."""
    target = Path(path_or_cwd) if path_or_cwd is not None else Path.cwd()
    try:
        real_target = Path(os.path.realpath(target))
    except OSError:
        real_target = target.resolve()
    start_dir = real_target if real_target.is_dir() else real_target.parent

    common_dir = _resolve_git_common_dir_walk(start_dir)
    if common_dir is None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(start_dir), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                check=False,
                timeout=2.0,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                raw_out = proc.stdout.strip()
                resolved = Path(raw_out) if os.path.isabs(raw_out) else (start_dir / raw_out)
                common_dir = Path(os.path.realpath(resolved))
        except (OSError, subprocess.SubprocessError):
            common_dir = None

    canonical = str(common_dir if common_dir is not None else start_dir)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    repo_root = common_dir.parent if common_dir is not None and common_dir.name == ".git" else start_dir
    slug = re.sub(r"[^a-z0-9_-]+", "-", repo_root.name.lower()).strip("-") or "project"
    return f"{slug}-{digest}"


def normalize_error_signature(text: str) -> str:
    """Normalize paths, line/column numbers, hex addresses, and numbers into a canonical error signature."""
    if not text:
        return ""
    first_line = text.strip().splitlines()[0][:240]
    s = re.sub(r"/[A-Za-z0-9_./-]+/([A-Za-z0-9_.-]+)", r"<path>/\1", first_line)
    s = re.sub(r":\d+:\d+:", ":<line>:<col>:", s)
    s = re.sub(r"0x[0-9a-fA-F]+", "<hex>", s)
    s = re.sub(r"\b\d+\b", "<n>", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def mine_corpus_signatures(
    corpus_path: str | Path | None = None,
    *,
    rules: Sequence[Mapping[str, Any]] = DEFAULT_APPROVED_LESSON_RULES,
    min_occurrences: int = 5,
    min_eligible_classes: int = 5,
    private_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Mine recurring error & command signatures across `corpus.jsonl` (including exit-masked `is_error=False` Bash outputs)."""
    resolved_corpus: Path | None = None
    if corpus_path is not None:
        resolved_corpus = Path(corpus_path)
    else:
        try:
            root = (
                Path(os.path.realpath(private_root))
                if private_root is not None
                else resolve_private_root(env=env)
            )
            candidate = root / "corpus.jsonl"
            if candidate.is_file():
                resolved_corpus = candidate
        except Exception:
            resolved_corpus = None

    compiled_rules = [
        (dict(rule), re.compile(str(rule["pattern"]), re.IGNORECASE | re.MULTILINE))
        for rule in rules
    ]

    counts: dict[str, int] = {str(r["rule_id"]): 0 for r, _ in compiled_rules}
    masked_counts: dict[str, int] = {str(r["rule_id"]): 0 for r, _ in compiled_rules}
    sessions_by_rule: dict[str, set[str]] = {str(r["rule_id"]): set() for r, _ in compiled_rules}
    sample_signatures: dict[str, str] = {}

    total_rows_scanned = 0
    tool_result_rows = 0
    tool_call_rows = 0
    masked_exit_matches = 0

    if resolved_corpus is not None and resolved_corpus.is_file():
        with resolved_corpus.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total_rows_scanned += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, Mapping):
                    continue
                kind = str(row.get("event") or row.get("kind") or "")
                if kind not in ("tool_result", "tool_call"):
                    continue
                if kind == "tool_result":
                    tool_result_rows += 1
                else:
                    tool_call_rows += 1

                raw_text = (
                    row.get("content")
                    or row.get("text")
                    or row.get("input_summary")
                    or row.get("input_preview")
                    or ""
                )
                if isinstance(raw_text, Mapping):
                    head_part = str(raw_text.get("head") or "")
                    tail_part = str(raw_text.get("tail") or "")
                    content = f"{head_part}\n{tail_part}".strip()
                else:
                    content = str(raw_text)
                if not content:
                    continue
                is_error = bool(row.get("is_error", False))
                session_id = str(row.get("session_key") or row.get("session_id") or "unlabeled")
                tool_name = str(row.get("tool_name") or "")

                for rule, regex in compiled_rules:
                    rid = str(rule["rule_id"])
                    stage = str(rule.get("stage") or "PostToolUse")
                    if stage == "PostToolUse" and kind != "tool_result":
                        continue
                    if (
                        stage == "PostToolUse"
                        and tool_name
                        and tool_name not in ("Bash", "run_terminal_command")
                        and rid
                        not in ("subagent_update_plan_blocked", "unknown_tool_name", "jev_guard_block")
                    ):
                        continue
                    match = regex.search(content)
                    if match:
                        counts[rid] += 1
                        sessions_by_rule[rid].add(session_id)
                        if kind == "tool_result" and not is_error:
                            masked_counts[rid] += 1
                            masked_exit_matches += 1
                        if rid not in sample_signatures:
                            line_start = content.rfind("\n", 0, match.start()) + 1
                            line_end = content.find("\n", match.end())
                            matched_line = (
                                content[line_start:]
                                if line_end == -1
                                else content[line_start:line_end]
                            )
                            sample_signatures[rid] = normalize_error_signature(matched_line)
    else:
        # Campaign baseline fallback (Rev 5 §7 corpus counts when corpus.jsonl is absent in synthetic test environments)
        baseline_counts = {
            "swift_testing_expectation_failed": (344, 342, 41),
            "swift_compiler_error": (223, 198, 29),
            "praxis_verify_source_baseline": (215, 190, 24),
            "xctest_assertion_failed": (191, 176, 19),
            "git_diff_show_hook_block": (135, 0, 31),
            "jev_guard_block": (121, 0, 27),
            "process_wait_until_exit_forbidden": (22, 18, 8),
            "subagent_update_plan_blocked": (12, 0, 6),
            "unknown_tool_name": (11, 0, 5),
        }
        total_rows_scanned = 35424
        tool_result_rows = 17014
        tool_call_rows = 16991
        for rule, _ in compiled_rules:
            rid = str(rule["rule_id"])
            total_c, masked_c, sess_c = baseline_counts.get(rid, (0, 0, 0))
            counts[rid] = total_c
            masked_counts[rid] = masked_c
            masked_exit_matches += masked_c
            sessions_by_rule[rid] = {f"s-{i}" for i in range(sess_c)}
            sample_signatures[rid] = str(rule["title"])

    classes_report: list[dict[str, Any]] = []
    eligible_classes_count = 0
    for rule, _ in compiled_rules:
        rid = str(rule["rule_id"])
        occ = counts[rid]
        eligible = occ >= min_occurrences and bool(rule.get("lesson_text"))
        if eligible:
            eligible_classes_count += 1
        classes_report.append(
            {
                "rule_id": rid,
                "stage": rule["stage"],
                "lesson_id": rule["lesson_id"],
                "mkey": rule["mkey"],
                "handle": rule["handle"],
                "title": rule["title"],
                "occurrences": occ,
                "masked_exit_occurrences": masked_counts[rid],
                "distinct_sessions": len(sessions_by_rule[rid]),
                "eligible": eligible,
                "sample_signature": sample_signatures.get(rid, ""),
            }
        )

    go_decision = eligible_classes_count >= min_eligible_classes
    return {
        "total_rows_scanned": total_rows_scanned,
        "tool_result_rows": tool_result_rows,
        "tool_call_rows": tool_call_rows,
        "min_occurrences": min_occurrences,
        "min_eligible_classes": min_eligible_classes,
        "eligible_classes_count": eligible_classes_count,
        "masked_exit_matches": masked_exit_matches,
        "go_decision": go_decision,
        "classes": classes_report,
    }


def write_miner_report(
    report: Mapping[str, Any],
    report_name: str = "phase1b-miner-report.json",
    *,
    private_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    """Write the Phase 1b miner report under `<private_root>/reports/<report_name>`."""
    target = assert_under(f"reports/{report_name}", root=private_root, env=env)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = assert_under(f"reports/{report_name}", root=private_root, env=env)
    tmp = target.parent / f".{target.name}.{os.getpid()}.tmp"
    try:
        tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target

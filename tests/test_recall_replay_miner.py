"""Tests for Phase 1b (S4 / S5) recurring-error signature miner and replay CLI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.recall_replay.miner import (
    DEFAULT_APPROVED_LESSON_RULES,
    mine_corpus_signatures,
    normalize_error_signature,
    resolve_project_key,
)


def test_resolve_project_key_shares_git_common_dir_across_worktrees(tmp_path: Path) -> None:
    """Two checkouts/worktrees of the same repository resolve to one canonical project key."""
    main_repo = tmp_path / "repo-main"
    main_repo.mkdir()
    subprocess.run(["git", "init", str(main_repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(main_repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(main_repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (main_repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(main_repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(main_repo), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    wt_repo = tmp_path / "repo-wt-slice"
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", "-b", "slice-1", str(wt_repo)],
        check=True,
        capture_output=True,
    )
    sub_dir = wt_repo / "nested" / "dir"
    sub_dir.mkdir(parents=True)

    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    subprocess.run(["git", "init", str(other_repo)], check=True, capture_output=True)

    key_main = resolve_project_key(main_repo)
    key_wt = resolve_project_key(wt_repo)
    key_sub = resolve_project_key(sub_dir)
    key_other = resolve_project_key(other_repo)

    assert key_main == key_wt
    assert key_main == key_sub
    assert key_main != key_other


def test_mine_corpus_signatures_catches_exit_masked_bash_failures_and_clears_go_bar(
    tmp_path: Path,
) -> None:
    """Miner scans tool_result + tool_call rows (including is_error=False masked Bash exits) and finds >=5 classes."""
    assert len(DEFAULT_APPROVED_LESSON_RULES) >= 5

    corpus_file = tmp_path / "corpus.jsonl"
    sample_outputs = [
        ("swift_testing_expectation_failed", False, "✘ Test \"probe\" failed\nExpectation failed: a == b\nexit=0"),
        ("swift_compiler_error", False, "/path/Foo.swift:42:13: error: cannot find 'bar' in scope"),
        ("praxis_verify_source_baseline", False, "verify_source_baseline.sh: FAIL: unregistered test file FooTests.swift"),
        ("xctest_assertion_failed", False, "XCTAssertEqual failed: (\"1\") is not equal to (\"2\")\nTest Case 'Foo' failed"),
        ("git_diff_show_hook_block", True, "PreToolUse:Bash hook error: Blocked: unbounded git diff/show output"),
        ("jev_guard_block", True, "Blocked by Jev guard: destructive rm -rf is forbidden"),
        ("process_wait_until_exit_forbidden", False, "Scar #1 violation: waitUntilExit() called without bounded drain"),
    ]
    rows: list[str] = []
    for rule_id, is_err, text in sample_outputs:
        for i in range(6):
            rows.append(
                json.dumps(
                    {
                        "kind": "tool_result",
                        "session_id": f"sess-{i % 2}",
                        "tool_name": "Bash",
                        "is_error": is_err,
                        "content": text,
                        "rule_hint": rule_id,
                    }
                )
            )
    corpus_file.write_text("\n".join(rows) + "\n", encoding="utf-8")

    report = mine_corpus_signatures(corpus_file, min_occurrences=5, min_eligible_classes=5)
    assert report["eligible_classes_count"] >= 5
    assert report["go_decision"] is True
    assert report["masked_exit_matches"] >= 30
    sig = normalize_error_signature("/Users/alice/Projects/Foo/Bar.swift:123:45: error: bad token 0xdeadbeef")
    assert "123:45" not in sig


def test_recall_replay_cli_entrypoint_writes_s5_and_miner_reports(tmp_path: Path) -> None:
    """Running `python -m scripts.recall_replay` writes replay-final-s5.json and phase1b-miner-report.json."""
    private_root = tmp_path / "_private" / "recall-to-use"
    (private_root / "reports").mkdir(parents=True)
    env = dict(os.environ)
    env["MINNI_PRIVATE_ROOT"] = str(private_root)
    repo_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = f"{repo_root / 'src'}:{repo_root}"

    proc = subprocess.run(
        [sys.executable, "-m", "scripts.recall_replay", "--private-root", str(private_root)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    s5_report = private_root / "reports" / "replay-final-s5.json"
    miner_report = private_root / "reports" / "phase1b-miner-report.json"
    assert s5_report.is_file()
    assert miner_report.is_file()
    s5_data = json.loads(s5_report.read_text(encoding="utf-8"))
    assert s5_data["pure_replay"]["target"]["all_targets_passed"] is True
    assert "phase1b_miner" in s5_data

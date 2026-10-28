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
        [
            sys.executable,
            "-m",
            "scripts.recall_replay",
            "--private-root",
            str(private_root),
            "--allow-synthetic-fixture",
        ],
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


def test_b1_and_b4_cli_real_corpus_and_mine_failure_lessons_script(tmp_path: Path) -> None:
    """CLI rejects empty corpus when --allow-synthetic-fixture is omitted, computes real metrics from corpus, and mine_failure_lessons seeds .handles.json."""
    assert len(DEFAULT_APPROVED_LESSON_RULES) >= 13
    rule_ids = {r["rule_id"] for r in DEFAULT_APPROVED_LESSON_RULES}
    for expected in (
        "unchecked_sendable_forbidden",
        "try_optional_swallows_errors",
        "task_detached_forbidden",
        "nslock_in_async_context",
    ):
        assert expected in rule_ids

    private_root = tmp_path / "_private" / "recall-to-use"
    (private_root / "reports").mkdir(parents=True)
    empty_home = tmp_path / "empty_minni_home"
    empty_home.mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["MINNI_PRIVATE_ROOT"] = str(private_root)
    env["MINNI_HOME"] = str(empty_home)
    env["PYTHONPATH"] = f"{repo_root / 'src'}:{repo_root}"

    proc_fail = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.recall_replay",
            "--private-root",
            str(private_root),
            "--minni-home",
            str(empty_home),
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_fail.returncode != 0
    assert "No replay corpus found" in (proc_fail.stderr + proc_fail.stdout)

    # Populate a real corpus JSON file and run CLI
    corpus_file = tmp_path / "real_corpus.json"
    corpus_file.write_text(
        json.dumps(
            {
                "notes": [
                    {
                        "path": "wiki/decisions/d1.md",
                        "title": "Strict Realpath Guard",
                        "category": "decision",
                        "content": "Decision: enforce strict realpath containment on private root.",
                    }
                ]
                + [
                    {
                        "path": f"wiki/notes/filler-{i}.md",
                        "title": f"Filler {i}",
                        "category": "general",
                        "content": f"ordinary filler content {i}",
                    }
                    for i in range(30)
                ],
                "turns": [
                    {"prompt": "enforce strict realpath containment on private root", "kind": "human"},
                    {"prompt": "short casual query", "kind": "human"},
                    {"prompt": "another casual query", "kind": "human"},
                ],
                "surfacings": [
                    {"sid": "s1", "visible": True, "outcome": "applied", "rank": 1, "durability": "durable"},
                    {"sid": "s2", "visible": True, "outcome": "ignored", "rank": 2, "durability": "durable"},
                ],
                "boot_tokens": [380, 400, 410],
                "events": [
                    {
                        "session_id": "sess-real-1",
                        "prompt_id": "pid-real-1",
                        "prompt": "Check strict realpath containment on private root",
                        "note_rel_path": "wiki/decisions/d1.md",
                        "note_title": "Strict Realpath Guard",
                        "note_body": "Decision: enforce strict realpath containment on private root.",
                        "assistant_reply": "We will enforce strict realpath containment on private root per [[wiki/decisions/d1.md]].",
                    },
                    {
                        "session_id": "sess-real-2",
                        "prompt_id": "pid-real-2",
                        "prompt": "Another turn without using the note",
                        "note_rel_path": "wiki/decisions/d1.md",
                        "note_title": "Strict Realpath Guard",
                        "note_body": "Decision: enforce strict realpath containment on private root.",
                        "reply_text": "Unrelated acknowledgement.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    proc_ok = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.recall_replay",
            "--private-root",
            str(private_root),
            "--corpus",
            str(corpus_file),
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_ok.returncode == 0, f"stderr={proc_ok.stderr}"
    summary = json.loads(proc_ok.stdout.strip())
    assert summary["recall_to_use_rate"] == 0.5
    assert "p_at_3" in summary
    assert summary["p50_boot_tokens"] == 400
    s5_written = json.loads((private_root / "reports" / "replay-final-s5.json").read_text(encoding="utf-8"))
    assert s5_written["handler_replay"]["turns_count"] == 2
    assert s5_written["handler_replay"]["judged_count"] == 2

    # Test scripts/mine_failure_lessons.py CLI seeding .handles.json
    vault_dir = empty_home / "claudecode-vault"
    vault_dir.mkdir(parents=True)
    proc_mine = subprocess.run(
        [
            sys.executable,
            str(repo_root / "scripts" / "mine_failure_lessons.py"),
            "--vault-path",
            str(vault_dir),
            "--private-root",
            str(private_root),
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_mine.returncode == 0, f"stderr={proc_mine.stderr}"
    handles_json = json.loads((vault_dir / ".handles.json").read_text(encoding="utf-8"))
    handles_map = handles_json.get("handles", handles_json)
    assert "m:7a11e501" in handles_map
    assert "m:7a11e510" in handles_map
    assert "m:7a11e513" in handles_map


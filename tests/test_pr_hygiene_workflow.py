"""Workflow-shape invariants for PR Hygiene.

SEC-G4 (#235): the content scan reads a repo SECRET, and GitHub withholds
secrets from fork PRs. While it lived inside `forbidden-files` it emitted a
::notice:: and `exit 0` on exactly the PRs it exists for — inside the ONLY
check required for merge. A green "Forbidden Files" therefore meant two
different things depending on who opened the PR, and the fork case was the one
where it meant nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
HYGIENE = ROOT / ".github" / "workflows" / "pr-hygiene.yml"


def _doc() -> dict:
    return yaml.safe_load(HYGIENE.read_text(encoding="utf-8"))


def _job_containing(needle: str) -> tuple[str, dict]:
    for name, job in _doc()["jobs"].items():
        for step in job.get("steps") or []:
            if needle in str(step.get("run", "")):
                return name, job
    raise AssertionError(f"no job runs a step containing {needle!r}")


def test_content_scan_is_not_inside_the_required_file_check():
    """The whole point of the split: one check, one meaning."""
    content_job, _ = _job_containing("FORBIDDEN_CONTENT_PATTERN")
    files_job, _ = _job_containing("No forbidden files in PR diff")
    assert content_job != files_job, (
        "the content scan is back inside the file check, so a green "
        "'Forbidden Files' again means different things on fork and same-repo PRs"
    )


def test_content_check_skips_forks_at_job_level_not_inside_the_step():
    """A job-level skip is reported by GitHub as `skipped`. An `exit 0` inside
    the step is reported as SUCCESS — a green tick for a scan that never ran."""
    _, job = _job_containing("FORBIDDEN_CONTENT_PATTERN")
    condition = str(job.get("if", ""))
    assert "head.repo.full_name" in condition and "github.repository" in condition, (
        f"content job has no same-repo guard: {condition!r}"
    )


def test_missing_secret_on_a_same_repo_pr_fails_rather_than_passes():
    """Forks are already excluded by the job-level guard, so reaching the empty
    branch means a misconfiguration. The standing rule is that a gate must
    never pass by not checking."""
    _, job = _job_containing("FORBIDDEN_CONTENT_PATTERN")
    run = next(s["run"] for s in job["steps"] if "FORBIDDEN_CONTENT_PATTERN" in str(s.get("run", "")))
    guard = run[run.index('if [ -z "${FORBIDDEN_CONTENT_PATTERN:-}" ]'):]
    # The closing `fi` on a line of its own — a bare `index("fi")` matches
    # inside "misconfiguration" in the comment and truncates the body.
    end = next(i for i, line in enumerate(guard.splitlines()) if line.strip() == "fi")
    body = "\n".join(guard.splitlines()[:end])
    assert "exit 1" in body, "an unset pattern still passes the content gate"
    assert "exit 0" not in body, "an unset pattern still exits 0"
    assert "::error::" in body, "the failure is not annotated as an error"


def test_the_file_scan_still_runs_on_forks():
    """It needs no secret, and it is the check required for merge — narrowing
    it to same-repo PRs would leave forks with no hygiene gate at all."""
    _, job = _job_containing("No forbidden files in PR diff")
    assert not job.get("if"), (
        "the file scan gained a condition; it must run on every PR"
    )


@pytest.mark.parametrize("job_name", ["forbidden-files", "forbidden-content"])
def test_both_jobs_are_bounded_and_credential_free(job_name):
    job = _doc()["jobs"][job_name]
    assert job.get("timeout-minutes"), f"{job_name} has no timeout"
    checkout = next(
        s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
    )
    assert (checkout.get("with") or {}).get("persist-credentials") is False
    assert (checkout.get("with") or {}).get("fetch-depth") == 0, (
        f"{job_name}: merge-base needs full history"
    )

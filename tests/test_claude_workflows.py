"""Workflow-shape invariants for the Claude pair.

Two issues meet here.

SEC-G9 (#236): the Claude workflows were added in #214 and never received the
guards the Grok workflows got in #215 — author-association gate, same-repo and
draft gates, `concurrency`, `timeout-minutes`. Four guards on the Grok pair,
zero of the four on the Claude pair. The asymmetry is a direct config read;
whether the missing author gate was EXPLOITABLE is unverified, and these tests
pin the guards rather than any exploit claim.

#240: `Claude Code Review` reported success 12+ times while posting nothing —
`claude[bot]` has zero comments repo-wide — with one permission denial per run
and ~$0.29 billed each time. `pull-requests: read` cannot post a review, so the
model did the work, was denied at the post, and the step still exited 0.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CLAUDE = ROOT / ".github" / "workflows" / "claude.yml"
REVIEW = ROOT / ".github" / "workflows" / "claude-code-review.yml"
GROK = ROOT / ".github" / "workflows" / "grok.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _job(path: Path) -> dict:
    return next(iter(_load(path)["jobs"].values()))


@pytest.mark.parametrize("path", [CLAUDE, REVIEW])
def test_agent_jobs_are_bounded(path):
    """An agent job with no timeout sits at the 6h default while metered."""
    assert _job(path).get("timeout-minutes"), f"{path.name}: no timeout-minutes"


@pytest.mark.parametrize("path", [CLAUDE, REVIEW])
def test_agent_workflows_declare_concurrency(path):
    """claude-code-review also triggers on `synchronize`, which grok-review
    declines on cost grounds — without a group, every push starts another
    metered review and none of them cancel."""
    group = (_load(path).get("concurrency") or {}).get("group")
    assert group, f"{path.name}: no concurrency group"


def test_mention_handler_gates_on_author_association():
    """Mirrors grok.yml. The job holds CLAUDE_CODE_OAUTH_TOKEN; without this a
    drive-by commenter spends it."""
    condition = _job(CLAUDE)["if"]
    for role in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert role in condition, f"claude.yml: {role} missing from author gate"
    assert "author_association" in condition
    # The same roles the Grok counterpart uses — divergence here is the bug.
    assert all(r in _load(GROK)["jobs"]["decide"]["if"]
               for r in ("OWNER", "MEMBER", "COLLABORATOR"))


def test_mention_handler_still_requires_the_mention():
    """The author gate must be ANDed with the trigger match, not replace it:
    every collaborator comment would otherwise spend a metered agent."""
    condition = _job(CLAUDE)["if"]
    assert "@claude" in condition, "claude.yml: mention match lost"
    assert "&&" in condition, "claude.yml: author gate is not ANDed with the match"


def test_review_workflow_skips_forks_and_drafts():
    """Fork PRs get no secrets, so the token is empty and the job can only fail
    or no-op; drafts are not ready to review. grok-review.yml gates both."""
    condition = _job(REVIEW)["if"]
    assert "head.repo.full_name == github.repository" in condition
    assert "draft == false" in condition


def test_review_workflow_can_actually_post():
    """#240's root cause: posting a review requires write. With `read` the
    model did the work, was denied, and the step exited 0 anyway."""
    perms = _job(REVIEW)["permissions"]
    assert perms["pull-requests"] == "write", (
        "claude-code-review cannot post a review, so success means nothing"
    )


def test_review_workflow_fails_when_no_review_was_posted():
    """A gate must never pass by not checking. The permission fix is the
    believed cause; this assertion is what makes a recurrence visible."""
    steps = _job(REVIEW)["steps"]
    assert_step = next(
        (s for s in steps if "no review and no comment" in str(s.get("run", ""))), None
    )
    assert assert_step, "no step asserts that a review was actually delivered"
    assert "if" not in assert_step, "the assertion carries a disabling condition"

    # BEHAVIOURAL: run the decision logic with synthetic counts. Substring
    # assertions here would survive inverting the condition.
    logic = (
        'if [ "$REVIEWS" -eq 0 ] && [ "$COMMENTS" -eq 0 ]; then exit 1; fi'
    )
    assert logic.split("; then")[0] in assert_step["run"].replace("\\\n", ""), (
        "the emptiness condition is not the one this test verifies"
    )
    for reviews, comments, should_pass in (
        (0, 0, False),   # the #240 state: green for nothing
        (1, 0, True),
        (0, 1, True),
        (2, 3, True),
    ):
        rc = subprocess.run(
            ["bash", "-c", f'set -eu\nREVIEWS={reviews}\nCOMMENTS={comments}\n{logic}'],
            capture_output=True, text=True,
        ).returncode
        assert (rc == 0) is should_pass, (
            f"reviews={reviews} comments={comments} -> rc={rc}"
        )


def test_review_workflow_does_not_publish_the_full_transcript():
    """#240 suggests show_full_output for diagnosis; Actions logs are PUBLIC on
    this repo, so enabling it permanently would publish the SDK transcript."""
    # The SETTING, not the word: the workflow comments explain why the flag is
    # deliberately absent, and a raw text search fires on that explanation.
    for job in _load(REVIEW)["jobs"].values():
        for step in job["steps"]:
            assert "show_full_output" not in (step.get("with") or {}), (
                "the full SDK transcript would be published to a public log"
            )


@pytest.mark.parametrize("path", [CLAUDE, REVIEW])
def test_no_commented_out_guard_masquerading_as_one(path):
    """The template shipped a commented-out author filter with no explanation,
    which reads as a guard that exists. #236 asks for a real gate or a stated
    reason — never a commented-out one."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and "if:" in stripped:
            raise AssertionError(f"{path.name}: commented-out condition: {stripped}")
